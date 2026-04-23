"""
bot.py — точка входа, polling-режим (для Render).
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommandScopeDefault, MenuButtonWebApp, WebAppInfo

from config import BOT_TOKEN, WEBAPP_URL
from database import db
from handlers import admin_actions, admin_mgmt, register, requests, start

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher(storage=MemoryStorage())

    dp.include_routers(
        requests.router,
        start.router,
        register.router,
        admin_mgmt.router,
        admin_actions.router,
    )

    await db.connect()

    # Убираем команды из меню
    await bot.delete_my_commands(scope=BotCommandScopeDefault())

    # Кнопка Menu Button — открывает WebApp напрямую
    if WEBAPP_URL:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="🚗 Записаться",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )
        )
        logger.info("✅ Menu Button установлена: %s", WEBAPP_URL)
    else:
        await bot.set_chat_menu_button()
        logger.warning("⚠️ WEBAPP_URL не задан — Menu Button не установлена")

    logger.info("🚀 Бот запущен (polling)")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await db.close()
        await bot.session.close()
        logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())