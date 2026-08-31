"""
notifications.py — безопасная отправка сообщений.

Учитывает лимиты Telegram (~30 сообщений в секунду), ловит RetryAfter
и помечает пользователей, заблокировавших бота.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardMarkup

import config
import keyboards as kb
import render
import subscription
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


async def alert_owners(bot: Bot, text: str) -> int:
    """
    Сообщить владельцу бота об аварии, которую придётся разбирать руками.

    Лог для этого не годится: в него не смотрят без повода, а повод —
    как раз то, о чём лог и молчит. Возвращается число доставленных писем.

    Без BOT_OWNER_IDS письмо уходит в мастер-чат: он заведён для
    недоставленного и остаётся последним адресом, где есть кому прочитать.
    Когда владелец известен, дублировать туда незачем — мастер-чат вполне
    может оказаться группой с посторонними.
    """
    targets = config.BOT_OWNER_IDS or (
        (config.MASTER_CHAT_ID,) if config.MASTER_CHAT_ID else ()
    )
    delivered = 0
    for chat_id in targets:
        if await safe_send(bot, chat_id, text):
            delivered += 1
    return delivered


async def notify_staff(
    bot: Bot,
    idservice: str,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    exclude: int | None = None,
    master_alert: str | None = None,
) -> int:
    """
    Уведомить всех активных администраторов сервиса и владельца
    (без дублей). Если не доставлено никому — сообщить в мастер-чат.

    В мастер-чат уходит master_alert, а не исходный текст: владелец бота —
    не сотрудник сервиса, и персональные данные клиента ему не предназначены.
    Клиент соглашался на обработку данных ради записи в автосервис, а не ради
    пересылки оператору платформы. Без master_alert отправляется сухое
    уведомление вообще без подробностей.
    """
    staff = [uid for uid in await db.get_staff_ids(idservice) if uid != exclude]
    delivered = await broadcast(bot, staff, text, reply_markup=reply_markup)

    if delivered == 0 and config.MASTER_CHAT_ID:
        await safe_send(
            bot,
            config.MASTER_CHAT_ID,
            master_alert
            or (
                "⚠️ <b>Некому доставить уведомление сервиса</b>\n"
                f"<code>{idservice}</code>\n\n"
                "<i>Все сотрудники недоступны или заблокировали бота.</i>"
            ),
        )
    return delivered


async def send_subscription_reminders(bot: Bot) -> int:
    """
    Разослать причитающиеся напоминания о подписке. Возвращает число писем.

    Право на письмо занимается до отправки: при сбое теряется одно
    напоминание, а не управляющий получает его заново на каждом тике.

    Состояние подписки здесь не меняется — оно выводится из paid_until.
    Поэтому молчавший неделю крон стоит писем, а не поехавших гейтов.
    """
    now = datetime.now(timezone.utc)
    sent = 0
    for svc in await db.services_for_reminders():
        stage = subscription.due_stage(svc["paid_until"], now)
        if stage is None:
            continue
        if not await db.claim_reminder(
            str(svc["idservice"]), svc["paid_until"], stage
        ):
            continue
        if await safe_send(
            bot,
            svc["owner_id"],
            render.subscription_reminder(stage, svc),
            reply_markup=kb.kb_pay(),
        ):
            sent += 1
        await asyncio.sleep(BROADCAST_DELAY)
    return sent


async def send_reminders_forever(bot: Bot, interval: float) -> None:
    """
    Свой будильник вместо внешнего крона. Запускается вместе с приложением.

    Внешний крон — это ещё одна вещь, которую надо не забыть настроить на новом
    сервере и которая, если её забыли, молча не работает: гейты-то стоят, просто
    писем нет. Здесь забывать нечего.

    Первый круг — сразу на старте: перезапуск не должен стоить письма.
    Пропущенные ничего не ломают, а лишних не будет — право на письмо занимается
    уникальным индексом, поэтому и два процесса с этим циклом безопасны.

    Цикл не умирает: обрыв сети или базы через час стоит попробовать снова.
    """
    while True:
        try:
            sent = await send_subscription_reminders(bot)
            if sent:
                logger.info("Напоминания о подписке: отправлено %d", sent)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Круг напоминаний о подписке не отработал")
        await asyncio.sleep(interval)
