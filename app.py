"""
app.py — единственная точка входа: Telegram-бот (webhook) + REST API + статика WebApp.

Один процесс вместо пары «worker + web»: Background Worker на Render платный,
а два параллельных getUpdates при пересборке конфликтуют. Webhook не держит
постоянное соединение и будит сервис входящим апдейтом.

Запуск: uvicorn app:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import BotCommand, BotCommandScopeDefault, Update
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
import subscription
from database import db
from fsm_storage import build_storage
from handlers import admin_actions, admin_mgmt, catalog, register, requests, schedule, start
from handlers.requests import RequestRejected, create_request_flow
from middlewares import ErrorLoggingMiddleware, UserMiddleware
from ratelimit import DatabaseGate, RateLimiter, enforce
from validators import ValidationError, validate_uuid
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

    try:
        yield
    finally:
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
    if secret != config.WEBHOOK_SECRET or x_telegram_bot_api_secret_token != config.WEBHOOK_SECRET:
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
                detail=config.CLOSED_FOR_BOOKING.format(phone=svc["service_number"]),
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
