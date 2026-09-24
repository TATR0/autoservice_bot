"""
app.py — единственная точка входа: Telegram-бот (webhook) + REST API + статика WebApp.

Один процесс вместо пары «worker + web»: Background Worker на Render платный,
а два параллельных getUpdates при пересборке конфликтуют. Webhook не держит
постоянное соединение и будит сервис входящим апдейтом.

Запуск: uvicorn app:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import BotCommand, BotCommandScopeDefault, Update
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
import policy
import subscription
import yoomoney
from database import db
from fsm_storage import build_storage
from handlers import (
    admin_actions, admin_mgmt, catalog, payment, privacy, register, requests,
    schedule, start,
)
from handlers import subscription as subscription_handlers
from handlers.requests import RequestRejected, create_request_flow
from middlewares import ErrorLoggingMiddleware, UserMiddleware
from notifications import send_reminders_forever, send_subscription_reminders
from retention import purge_forever
from ratelimit import DatabaseGate, RateLimiter, enforce
from validators import ValidationError, format_phone, validate_uuid
from webapp_auth import InitDataError, verify_init_data

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).parent / "webapp"

# ── Лимиты публичного API ────────────────────────────────────────────────────
# Поиск по городу и карточка сервиса открыты без аутентификации, поэтому
# ограничены жёстче всего по частоте. Отправка заявки дополнительно защищена
# кулдауном по Telegram-аккаунту в create_request_flow, здесь — грубый отсев.
_lookup_limiter = RateLimiter(limit=60, window_seconds=60)
_profile_limiter = RateLimiter(limit=30, window_seconds=60)
_submit_limiter = RateLimiter(limit=10, window_seconds=60)

# Публичному API оставляем меньше соединений, чем есть в пуле: остаток
# гарантированно достаётся боту, иначе наплыв на /api/services заодно
# остановит обработку апдейтов Telegram.
_db_gate = DatabaseGate(max_concurrent=max(1, config.DB_POOL_MAX - 2))

# Ссылки на фоновые задачи обработки апдейтов. Без них сборщик мусора вправе
# уничтожить задачу на середине: asyncio держит только слабую ссылку.
_background_tasks: set[asyncio.Task] = set()

bot = Bot(
    token=config.BOT_TOKEN or "0:placeholder",
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=build_storage())

# Порядок важен: более специфичные роутеры — раньше, fallback внутри
# admin_actions должен остаться последним.
dp.include_routers(
    requests.router,
    start.router,
    register.router,
    catalog.router,
    schedule.router,
    payment.router,
    privacy.router,
    subscription_handlers.router,
    admin_mgmt.router,
    admin_actions.router,
)

for observer in (dp.message, dp.callback_query):
    observer.middleware(ErrorLoggingMiddleware())
    observer.middleware(UserMiddleware())


async def _telegram_retry(what: str, call, *args, attempts: int = 4, **kwargs):
    """
    Вызов к Telegram с повтором на сетевых обрывах.

    Соединение до api.telegram.org рвётся и без всякой вины сервиса
    (ServerDisconnectedError на живом keep-alive). Один такой обрыв на старте
    ронял весь процесс: на Render это провалившийся деплой, локально — старт,
    который надо повторять руками. Ошибки самого Telegram (неверный токен,
    нерезолвимый вебхук) повторять бессмысленно, их пропускаем наверх.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await call(*args, **kwargs)
        except TelegramNetworkError as exc:
            if attempt == attempts:
                raise
            delay = 2 ** (attempt - 1)
            logger.warning(
                "%s: сеть недоступна (%s), повтор через %d с (%d/%d)",
                what, exc, delay, attempt, attempts - 1,
            )
            await asyncio.sleep(delay)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.validate()
    await db.connect()

    await _telegram_retry(
        "Список команд",
        bot.set_my_commands,
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="menu", description="Выбрать активный сервис"),
            # Право забрать свои данные бесполезно, если о нём никто не знает
            BotCommand(command="forget_me", description="Удалить мои данные"),
            # Telegram требует её от ботов, продающих за звёзды
            BotCommand(command="paysupport", description="Вопрос по оплате"),
        ],
        scope=BotCommandScopeDefault(),
    )

    if config.BASE_URL:
        webhook_url = f"{config.BASE_URL}/webhook/{config.WEBHOOK_SECRET}"
        await _telegram_retry(
            "Установка вебхука",
            bot.set_webhook,
            webhook_url,
            secret_token=config.WEBHOOK_SECRET,
            drop_pending_updates=True,
            allowed_updates=dp.resolve_used_update_types(),
        )
        logger.info("🚀 Webhook установлен: %s/webhook/***", config.BASE_URL)
    else:
        logger.warning("BASE_URL не задан — webhook не установлен, бот не получит апдейты.")

    # Напоминания о подписке будит сам бот. Внешний крон остаётся возможным
    # (эндпоинт тика никуда не делся), но обязательным больше не является:
    # разворачивание на новом сервере не должно требовать помнить про него
    reminders = asyncio.create_task(
        send_reminders_forever(bot, config.REMINDER_TICK_SECONDS)
    )
    # Срок хранения персональных данных сторожит свой круг: чистка суточная,
    # напоминания часовые, и общий таймер обоим не подходит
    purge = asyncio.create_task(purge_forever())

    try:
        yield
    finally:
        for background in (reminders, purge):
            background.cancel()
            with suppress(asyncio.CancelledError):
                await background

        if config.BASE_URL:
            try:
                await bot.delete_webhook()
            except Exception:
                logger.warning("Не удалось снять webhook", exc_info=True)

        # Даём фоновым задачам доработать до закрытия пула: иначе на середине
        # обработки апдейта у них из-под ног уедет соединение с базой.
        if _background_tasks:
            logger.info("Ожидаю %d фоновых задач", len(_background_tasks))
            done, pending = await asyncio.wait(set(_background_tasks), timeout=15)
            for task in pending:
                task.cancel()

        await db.close()
        await bot.session.close()
        logger.info("Остановлено")


app = FastAPI(
    title="AutoService Bot",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# ── Заголовки безопасности ───────────────────────────────────────────────────
# Защита в глубину: пользовательские данные в форме и так экранируются, но
# при ошибке экранирования CSP не даст загрузить чужой скрипт.
#
# 'unsafe-inline' обязателен: разметка формы лежит в одном файле вместе со
# своими <script> и <style>. Разнести их — отдельная работа, а запрет внешних
# источников работает и так. frame-ancestors намеренно не задаём: Telegram
# открывает мини-приложение во встроенном браузере, и лишний запрет рискует
# сломать форму на части платформ.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://telegram.org 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Content-Security-Policy", _CSP)
    return response


# ── Telegram webhook ─────────────────────────────────────────────────────────

@app.post("/webhook/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    # Проверяем и секрет в пути, и заголовок: без второго любой, кто угадает
    # URL, сможет слать боту поддельные апдейты от чужого имени.
    #
    # Сравнение постоянного времени, как и для TICK_SECRET ниже. Практической
    # атаки по времени на случайный токен через сеть нет, но два соседних
    # сравнения секретов не должны выглядеть по-разному: следующий читатель
    # решит, что где-то из них так можно.
    # Заголовок ASGI отдаёт декодированным latin-1, поэтому в байты его
    # возвращаем тем же способом; путь приходит обычной строкой.
    expected = config.WEBHOOK_SECRET.encode()
    if not hmac.compare_digest(secret.encode(), expected) or not hmac.compare_digest(
        (x_telegram_bot_api_secret_token or "").encode("latin-1", "replace"), expected
    ):
        raise HTTPException(status_code=403, detail="forbidden")

    update = Update.model_validate(await request.json(), context={"bot": bot})

    # Обработка уходит в фон, Telegram получает 200 сразу. Синхронно было
    # нельзя: один /start — это около десятка обращений к Supabase плюс
    # отправка сообщения, суммарно 6–8 секунд. Telegram столько не ждёт,
    # обрывает соединение с «Read timeout expired» и присылает апдейт заново —
    # снаружи это выглядит как молчащий бот и задвоенные ответы.
    task = asyncio.create_task(_process_update(update))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"ok": True}


async def _process_update(update: Update) -> None:
    """Обработать апдейт вне запроса вебхука."""
    try:
        await dp.feed_update(bot, update)
    except Exception:
        # Внутри диспетчера ошибки ловит ErrorLoggingMiddleware; сюда долетит
        # лишь то, что случилось вокруг него. Молча терять это нельзя —
        # отвечать на апдейт уже некому, останется только лог.
        logger.exception("Не удалось обработать апдейт %s", update.update_id)


# ── REST API для WebApp ──────────────────────────────────────────────────────

@app.get("/api/services")
async def api_services(
    request: Request, city: str = Query(..., min_length=2, max_length=60)
):
    enforce(_lookup_limiter, request)
    async with _db_gate:
        rows = await db.get_services_by_city(city)
    return [
        {
            "idservice": str(r["idservice"]),
            "service_name": r["service_name"],
            "service_number": r["service_number"],
            "city": r["city"],
            "location_service": r["location_service"],
        }
        for r in rows
    ]


@app.get("/api/service/{service_id}")
async def api_service(request: Request, service_id: str):
    enforce(_lookup_limiter, request)

    # Без проверки формата asyncpg бросит DataError на мусорном id,
    # и клиент получит 500 вместо понятного «сервис не найден»
    try:
        service_id = validate_uuid(service_id, field="Сервис")
    except ValidationError:
        raise HTTPException(status_code=404, detail="Сервис не найден")

    async with _db_gate:
        svc = await db.get_service(service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Сервис не найден")

        # Просрочка отключает продажу нового времени, а не сам сервис.
        # 403, а не 404: «не найден» — неправда, а неправда стоит вечера отладки
        if not subscription.is_active(svc["paid_until"], datetime.now(timezone.utc)):
            raise HTTPException(
                status_code=403,
                detail=config.CLOSED_FOR_BOOKING.format(
                    phone=format_phone(svc["service_number"])
                ),
            )

        items = await db.get_catalog(service_id)

        free = await db.free_slots(svc)

    return {
        "idservice": str(svc["idservice"]),
        "service_name": svc["service_name"],
        "service_number": svc["service_number"],
        "city": svc["city"],
        "location_service": svc["location_service"],
        "timezone": svc["timezone"],
        "slots": {
            day.isoformat(): [moment.strftime("%H:%M") for moment in times]
            for day, times in free.items()
        },
        "catalog": [
            {
                "idcatalog": str(c["idcatalog"]),
                "title": c["title"],
                "price_rub": c["price_rub"],
            }
            for c in items
        ],
    }


class RequestPayload(BaseModel):
    init_data: str = Field(min_length=1)
    service_id: str
    client_uid: str | None = None
    client_name: str = ""
    phone: str = ""
    brand: str = ""
    model: str = ""
    plate: str = ""
    idcatalogs: list[str] = Field(default_factory=list)
    scheduled_at: str = ""
    comment: str = ""
    consent: bool = False
    # Вторая галочка — правила пользования записью. По умолчанию False, как и
    # согласие: форма присылает обе, а «не прислали» и «не поставил» для
    # сервера одно и то же
    accepted_terms: bool = False


@app.post("/api/requests")
async def api_create_request(request: Request, payload: RequestPayload):
    enforce(_submit_limiter, request)

    # idclienttg берём только из проверенного initData, никогда из тела запроса
    try:
        tg_user = verify_init_data(payload.init_data)
    except InitDataError as exc:
        logger.info("Отклонён initData: %s", exc)
        raise HTTPException(status_code=401, detail="Не удалось подтвердить Telegram-аккаунт")

    if not payload.consent:
        raise HTTPException(
            status_code=400,
            detail="Нужно согласие на обработку персональных данных",
        )

    if not payload.accepted_terms:
        raise HTTPException(
            status_code=400,
            detail="Нужно принять правила пользования записью",
        )

    await db.upsert_user(
        int(tg_user["id"]),
        username=tg_user.get("username"),
        first_name=tg_user.get("first_name"),
        last_name=tg_user.get("last_name"),
    )

    try:
        summary, is_duplicate = await create_request_flow(
            bot,
            client_tg_id=int(tg_user["id"]),
            payload=payload.model_dump(),
        )
    except RequestRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "ok": True,
        "duplicate": is_duplicate,
        "number": summary["number"],
        "service_name": summary["service_name"],
    }


@app.get("/api/me")
async def api_me(request: Request, init_data: str = Query(..., alias="init_data")):
    """Профиль клиента для автоподстановки имени и телефона в форму."""
    enforce(_profile_limiter, request)
    try:
        tg_user = verify_init_data(init_data)
    except InitDataError:
        raise HTTPException(status_code=401, detail="Не удалось подтвердить Telegram-аккаунт")

    async with _db_gate:
        user = await db.get_user(int(tg_user["id"]))
    name = " ".join(
        p for p in (tg_user.get("first_name"), tg_user.get("last_name")) if p
    )
    return {"name": name, "phone": user["phone"] if user else None}


@app.post("/internal/subscriptions/tick")
async def subscriptions_tick(x_tick_secret: str | None = Header(default=None)):
    """
    Рассылка напоминаний о подписке. Дёргается внешним кроном.

    Состояние подписки тик не меняет: он только шлёт письма и помечает
    отправленное. Два тика внахлёст безопасны — право на письмо занимается
    уникальным индексом в базе.
    """
    # Пустой секрет закрывает эндпоинт совсем: иначе стенд с незаполненной
    # переменной оказался бы открыт всем.
    # Сравниваем байты, а не строки: compare_digest на строках требует ASCII и
    # на секрете с кириллицей бросил бы TypeError — эндпоинт отвечал бы 500 на
    # любой запрос, включая правильный, и напоминания не ушли бы никогда.
    # Заголовок ASGI отдаёт декодированным latin-1, поэтому и в байты его
    # возвращаем тем же способом: иначе секрет с кириллицей не совпал бы с собой
    if not config.TICK_SECRET or not hmac.compare_digest(
        (x_tick_secret or "").encode("latin-1", "replace"),
        config.TICK_SECRET.encode(),
    ):
        raise HTTPException(status_code=401, detail="unauthorized")

    # Без _db_gate: гейт бережёт соединения от наплыва публичного API, а тик
    # между походами в базу переписывается с Telegram — держать его слот всё
    # это время значит отнимать соединение у клиентов ради ожидания сети
    sent = await send_subscription_reminders(bot)
    return {"sent": sent}


@app.post("/yoomoney/notify")
async def yoomoney_notify(request: Request):
    """
    Уведомление ЮMoney о переводе на кошелёк: дни начисляются сами.

    Адрес публичный и ничем не спрятан — защищает только подпись секретом.
    Пустой секрет закрывает приём совсем: иначе стенд с незаполненной
    переменной продлевал бы подписку любому, кто пришлёт форму.

    ЮMoney ждёт 200 и повторяет доставку часами, если его не получит. Поэтому
    200 отвечаем и на разобранное, и на непонятое: повтор того, чего мы не
    поняли, понятнее не станет, а владельцу бота письмо уже ушло. Неверная
    подпись — 403: такое уведомление мы не признаём своим никогда.
    """
    if not config.YOOMONEY_NOTIFY_SECRET:
        logger.warning("Уведомление ЮMoney при пустом YOOMONEY_NOTIFY_SECRET")
        raise HTTPException(status_code=404, detail="not found")

    # parse_qsl, а не request.form(): разбор формы в starlette требует
    # python-multipart, и ради одного плоского urlencoded-тела тащить в образ
    # ещё одну библиотеку незачем. errors=replace — испорченная кодировка
    # должна кончиться непрошедшей подписью, а не пятисоткой
    raw = (await request.body()).decode("utf-8", "replace")
    form = dict(parse_qsl(raw, keep_blank_values=True))
    try:
        notice = yoomoney.parse_notification(
            form, config.YOOMONEY_NOTIFY_SECRET, raw,
        )
    except yoomoney.NotificationError as exc:
        # Номер операции и метку пишем и у отклонённого: за ним могут стоять
        # настоящие деньги — например, когда секрет в .env разошёлся с тем,
        # что на странице ЮMoney. Без этих двух полей в журнале не видно, кому
        # начислять дни руками. Значения не проверены подписью, поэтому
        # обрезаны: в журнал не должно влезать чужое полотно
        logger.warning(
            "Уведомление ЮMoney отклонено: %s; операция %r, метка %r",
            exc,
            str(form.get("operation_id") or "")[:64],
            str(form.get("label") or "")[:64],
        )
        # Отчего именно не сошлась подпись, снаружи не видно, а секрет
        # показывать нельзя — значит объяснение считается здесь же. Длина
        # секрета ловит самый частый случай: значение доехало до контейнера
        # обрезанным или не доехало вовсе
        guess = yoomoney.diagnose(form, raw, config.YOOMONEY_NOTIFY_SECRET)
        logger.warning(
            "Подпись: длина секрета %d, догадка — %s; поля %s; тип %r",
            len(config.YOOMONEY_NOTIFY_SECRET),
            guess or "ни одна не подошла",
            # Имена полей, без значений: по ним видно, тем ли способом ЮMoney
            # подписывает уведомление и той ли формой оно вообще пришло
            sorted(form)[:20],
            str(request.headers.get("content-type") or "")[:64],
        )
        raise HTTPException(status_code=403, detail="forbidden") from None

    # Без _db_gate — по той же причине, что и тик напоминаний: зачисление
    # между походами в базу переписывается с Telegram, и держать слот всё это
    # время значит отнимать соединение у клиентов ради ожидания сети. Наплыва
    # тут быть не может: уведомления шлёт ЮMoney, и каждое подписано
    result = await payment.credit_transfer(bot, notice)
    logger.info("ЮMoney: перевод %s — %s", notice.operation_id, result)
    # Тело ЮMoney не читает, важен только код ответа
    return Response(status_code=200)


# ── Документы ────────────────────────────────────────────────────────────────

async def _service_for_document(service: str):
    """
    Сервис для страницы документа — или None, если прочитать его не вышло.

    Страница обязана открыться всегда: молчащая база, мусорный id, удалённый
    или просроченный сервис — не повод не показать человеку, на что он
    соглашается. В этих случаях выходит общий текст, без имени сервиса.
    """
    if not service:
        return None
    try:
        service_id = validate_uuid(service, field="Сервис")
    except ValidationError:
        return None
    try:
        async with _db_gate:
            return await db.get_service(service_id)
    except Exception:
        logger.exception("Документ: не удалось прочитать сервис %s", service_id)
        return None


@app.get("/privacy")
async def privacy_page(request: Request, service: str = Query("", max_length=64)):
    """Документ, к которому отсылает галочка согласия на обработку данных."""
    enforce(_lookup_limiter, request)
    svc = await _service_for_document(service)
    return HTMLResponse(policy.render(svc, config.PII_RETENTION_DAYS))


@app.get("/terms")
async def terms_page(request: Request, service: str = Query("", max_length=64)):
    """Правила пользования записью — вторая галочка под формой."""
    enforce(_lookup_limiter, request)
    svc = await _service_for_document(service)
    return HTMLResponse(policy.render_terms(svc))


@app.get("/offer")
async def offer_page(request: Request):
    """
    Оферта на подписку. Открыта, только когда заполнены реквизиты исполнителя:
    договор с безымянной стороной не значит ничего, и показывать его хуже,
    чем не показывать вовсе.
    """
    enforce(_lookup_limiter, request)
    if not config.offer_published():
        raise HTTPException(status_code=404, detail="Оферта не опубликована")
    return HTMLResponse(policy.render_offer())


# ── Служебное ────────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/")
async def root():
    return {"service": "autoservice-bot", "app": config.WEBAPP_PATH}


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # headers обязательны: в них уезжает Retry-After при срабатывании лимита
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
        headers=exc.headers,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Необработанная ошибка на %s", request.url.path)
    return JSONResponse(status_code=500, content={"error": "Внутренняя ошибка сервиса"})


# ── Статика WebApp ───────────────────────────────────────────────────────────
# Отдаётся с того же домена, что и API, поэтому CORS-мидлварь не нужна.

if WEBAPP_DIR.is_dir():
    @app.get("/app")
    async def app_redirect():
        return FileResponse(WEBAPP_DIR / "index.html")

    app.mount("/app", StaticFiles(directory=WEBAPP_DIR, html=True), name="webapp")
else:
    logger.warning("Каталог %s не найден — форма записи отдаваться не будет", WEBAPP_DIR)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        reload=False,
    )
