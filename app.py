"""
app.py — единственная точка входа: Telegram-бот (webhook) + REST API + статика WebApp.

Один процесс вместо пары «worker + web»: Background Worker на Render платный,
а два параллельных getUpdates при пересборке конфликтуют. Webhook не держит
постоянное соединение и будит сервис входящим апдейтом.

Запуск: uvicorn app:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeDefault, Update
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
from database import db
from fsm_storage import build_storage
from handlers import admin_actions, admin_mgmt, catalog, register, requests, start
from handlers.requests import RequestRejected, create_request_flow
from middlewares import ErrorLoggingMiddleware, UserMiddleware
from validators import ValidationError, validate_uuid
from webapp_auth import InitDataError, verify_init_data

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).parent / "webapp"

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
    admin_mgmt.router,
    admin_actions.router,
)

for observer in (dp.message, dp.callback_query):
    observer.middleware(ErrorLoggingMiddleware())
    observer.middleware(UserMiddleware())


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.validate()
    await db.connect()

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="menu", description="Выбрать активный сервис"),
        ],
        scope=BotCommandScopeDefault(),
    )

    if config.BASE_URL:
        webhook_url = f"{config.BASE_URL}/webhook/{config.WEBHOOK_SECRET}"
        await bot.set_webhook(
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
    await dp.feed_update(bot, update)
    return {"ok": True}


# ── REST API для WebApp ──────────────────────────────────────────────────────

@app.get("/api/services")
async def api_services(city: str = Query(..., min_length=2, max_length=60)):
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
async def api_service(service_id: str):
    # Без проверки формата asyncpg бросит DataError на мусорном id,
    # и клиент получит 500 вместо понятного «сервис не найден»
    try:
        service_id = validate_uuid(service_id, field="Сервис")
    except ValidationError:
        raise HTTPException(status_code=404, detail="Сервис не найден")

    svc = await db.get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail="Сервис не найден")

    catalog = await db.get_catalog(service_id)
    return {
        "idservice": str(svc["idservice"]),
        "service_name": svc["service_name"],
        "service_number": svc["service_number"],
        "city": svc["city"],
        "location_service": svc["location_service"],
        "catalog": [
            {"idcatalog": str(c["idcatalog"]), "title": c["title"]} for c in catalog
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
    idcatalog: str = ""
    urgency: str = ""
    comment: str = ""
    consent: bool = False


@app.post("/api/requests")
async def api_create_request(payload: RequestPayload):
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
async def api_me(init_data: str = Query(..., alias="init_data")):
    """Профиль клиента для автоподстановки имени и телефона в форму."""
    try:
        tg_user = verify_init_data(init_data)
    except InitDataError:
        raise HTTPException(status_code=401, detail="Не удалось подтвердить Telegram-аккаунт")

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
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


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
