"""
middlewares.py — сквозная обработка апдейтов.

UserMiddleware держит таблицу users в актуальном состоянии: имя админа
в списках, флаг is_blocked, телефон для автоподстановки. Апсерт делается
в одном месте, а не в каждом хендлере.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from database import db

logger = logging.getLogger(__name__)


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user and not user.is_bot:
            try:
                await db.upsert_user(
                    user.id,
                    username=user.username,
                    first_name=user.first_name,
                    last_name=user.last_name,
                )
            except Exception:
                # Профиль — не повод ронять обработку апдейта
                logger.exception("Не удалось сохранить профиль пользователя %s", user.id)
        return await handler(event, data)


class ErrorLoggingMiddleware(BaseMiddleware):
    """Ловит падения хендлеров, чтобы пользователь не остался без ответа."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception:
            logger.exception("Ошибка обработки апдейта")
            from aiogram.types import CallbackQuery, Message

            try:
                if isinstance(event, Message):
                    await event.answer("⚠️ Что-то пошло не так. Попробуйте ещё раз или нажмите /start.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("⚠️ Что-то пошло не так.", show_alert=True)
            except Exception:
                pass
            return None
