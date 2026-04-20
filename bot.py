"""
bot.py — точка входа, polling-режим (для Render).
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommandScopeDefault

from config import BOT_TOKEN
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

    # Порядок важен: более специфичные роутеры — раньше
    dp.include_routers(
        requests.router,      # WebApp data (самый специфичный)
        start.router,         # /start
        register.router,      # регистрация сервиса
        admin_mgmt.router,    # добавить / удалить администратора
        admin_actions.router, # статусы заявок, просмотр, fallback
    )

    await db.connect()

    # Убираем меню команд (бот работает через кнопки)
    await bot.delete_my_commands(scope=BotCommandScopeDefault())
    await bot.set_chat_menu_button()

    logger.info("🚀 Бот запущен (polling)")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await db.close()
        await bot.session.close()
        logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())
