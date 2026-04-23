"""
bot.py — точка входа, polling-режим (для Render).
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault

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

    dp.include_routers(
        requests.router,
        start.router,
        register.router,
        admin_mgmt.router,
        admin_actions.router,
    )

    await db.connect()

    # Команды в меню бота (кнопка Menu / /)
    await bot.set_my_commands(
        commands=[
            BotCommand(command="start",            description="🏠 Главное меню"),
            BotCommand(command="recording",        description="🚗 Записаться в автосервис"),
            BotCommand(command="register_service", description="📝 Зарегистрировать сервис"),
            BotCommand(command="leave_admin",      description="🚪 Уйти из администраторов"),
        ],
        scope=BotCommandScopeDefault(),
    )

    logger.info("🚀 Бот запущен (polling)")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await db.close()
        await bot.session.close()
        logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())