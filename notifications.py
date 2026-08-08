"""
notifications.py — безопасная отправка сообщений.

Учитывает лимиты Telegram (~30 сообщений в секунду), ловит RetryAfter
и помечает пользователей, заблокировавших бота.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardMarkup

import config
from database import db

logger = logging.getLogger(__name__)

# Пауза между сообщениями при рассылке: держимся ниже лимита в 30 msg/s
BROADCAST_DELAY = 0.05


async def safe_send(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    retries: int = 2,
) -> bool:
    """Отправить сообщение, вернуть True при успехе. Исключения не пробрасываются."""
    for attempt in range(retries + 1):
        try:
            await bot.send_message(chat_id, text, reply_markup=reply_markup)
            return True
        except TelegramRetryAfter as exc:
            if attempt == retries:
                logger.warning("Флуд-лимит для %s, отправка отменена", chat_id)
                return False
            await asyncio.sleep(exc.retry_after)
        except TelegramForbiddenError:
            # Пользователь заблокировал бота или не начинал диалог
            logger.info("Пользователь %s недоступен, помечаю is_blocked", chat_id)
            await db.set_user_blocked(chat_id, True)
            return False
        except TelegramBadRequest as exc:
            logger.warning("Не удалось отправить сообщение %s: %s", chat_id, exc)
            return False
        except Exception:
            logger.exception("Ошибка отправки сообщения %s", chat_id)
            return False
    return False


async def broadcast(
    bot: Bot,
    chat_ids: list[int],
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> int:
    """Разослать сообщение списку чатов. Вернуть число доставленных."""
    delivered = 0
    for chat_id in chat_ids:
        if await safe_send(bot, chat_id, text, reply_markup=reply_markup):
            delivered += 1
        await asyncio.sleep(BROADCAST_DELAY)
    return delivered


async def notify_staff(
    bot: Bot,
    idservice: str,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    exclude: int | None = None,
) -> int:
    """
    Уведомить всех активных администраторов сервиса и владельца
    (без дублей). Если не доставлено никому — отправить в мастер-чат.
    """
    staff = [uid for uid in await db.get_staff_ids(idservice) if uid != exclude]
    delivered = await broadcast(bot, staff, text, reply_markup=reply_markup)

    if delivered == 0 and config.MASTER_CHAT_ID:
        await safe_send(
            bot,
            config.MASTER_CHAT_ID,
            f"⚠️ <b>Некому доставить уведомление сервиса</b>\n\n{text}",
        )
    return delivered
