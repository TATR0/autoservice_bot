"""
handlers/subscription.py — продление подписки владельцем бота.

Пока приёма денег нет, продлевает человек: /extend <idservice> <дней>. Когда
появится провайдер, он позовёт тот же db.extend_subscription, только с другим
source и с external_id для идемпотентности.
"""

from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

import config
import render
from database import db
from notifications import safe_send
from validators import ValidationError, h, validate_uuid

logger = logging.getLogger(__name__)

router = Router()

MAX_DAYS = 36500  # сто лет: так выражается «бессрочная подписка»
USAGE = (
    "Продление подписки:\n"
    "<code>/extend &lt;idservice&gt; &lt;дней&gt;</code>\n"
    f"Дней — от 1 до {MAX_DAYS}. Со знаком минус — отобрать дни.\n\n"
    "Возврат звёзд:\n"
    "<code>/refund &lt;id списания&gt;</code>"
)


@router.message(Command("extend"))
async def extend_command(message: Message) -> None:
    # Постороннему не отвечаем вовсе: знать о существовании команды ему незачем
    if message.from_user.id not in config.BOT_OWNER_IDS:
        return

    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer(USAGE)
        return

    try:
        idservice = validate_uuid(parts[1], field="Сервис")
    except ValidationError as exc:
        await message.answer(f"❌ {exc}")
        return

    raw = parts[2]
    negative = raw.startswith("-")
    digits = raw[1:] if negative else raw
    if not digits.isdecimal() or not 1 <= int(digits) <= MAX_DAYS:
        await message.answer(USAGE)
        return
    days = -int(digits) if negative else int(digits)

    paid_until = await db.extend_subscription(
        idservice, days=days, granted_by=message.from_user.id
    )
    if paid_until is None:
        await message.answer("❌ Сервис не найден или удалён.")
        return

    svc = await db.get_service(idservice)
    await message.answer(
        f"✅ «{h(svc['service_name'])}» продлён на {days} дн.\n"
        f"Подписка действует до {render.local_dt(paid_until, svc['timezone'])}."
    )


@router.message(Command("refund"))
async def refund_command(message: Message, bot: Bot) -> None:
    """
    Вернуть звёзды и отобрать выданные дни.

    Порядок «сначала деньги, потом срок» выбран сознательно: обратный при
    отказе Telegram оставил бы сервис укороченным без возврата денег — то есть
    отобрал бы и товар, и оплату.
    """
    # Постороннему не отвечаем вовсе: знать о существовании команды ему незачем
    if message.from_user.id not in config.BOT_OWNER_IDS:
        return

    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer(USAGE)
        return

    payment = await db.get_stars_payment(parts[1])
    if payment is None:
        await message.answer("❌ Платёж не найден.")
        return
    if payment["refunded_at"] is not None:
        await message.answer(
            f"Этот платёж уже возвращён {render.local_dt(payment['refunded_at'])}."
        )
        return

    try:
        await bot.refund_star_payment(
            user_id=payment["granted_by"],
            telegram_payment_charge_id=parts[1],
        )
    except Exception as exc:
        logger.exception("Telegram отказал в возврате %s", parts[1])
        await message.answer(f"❌ Telegram отказал: {h(str(exc))}")
        return

    # Деньги уже ушли плательщику — шаг необратимый. Дальше два похода в базу,
    # и падать здесь молча нельзя: непойманное исключение уйдёт в
    # ErrorLoggingMiddleware без charge_id и idpayment, а повторный /refund —
    # тупик: Telegram откажет, потому что этот платёж он уже вернул
    try:
        paid_until = await db.revoke_payment(str(payment["idpayment"]))
    except Exception:
        logger.exception(
            "Возврат %s: звёзды вернулись, а revoke_payment упал,"
            " idpayment=%s, idservice=%s, days=%s",
            parts[1], payment["idpayment"], payment["idservice"], payment["days"],
        )
        await message.answer(
            "⚠️ Звёзды вернулись плательщику, но дни подписки не отобраны — "
            "сбой базы. Повторный /refund не поможет: Telegram этот платёж "
            "уже вернул. Отберите дни вручную:\n"
            f"<code>/extend {payment['idservice']} -{payment['days']}</code>"
        )
        return

    if paid_until is None:
        await message.answer("Звёзды возвращены, но платёж уже был отменён раньше.")
        return

    # Дальше и деньги, и дни уже списаны обратно — падать нельзя. get_service
    # нужен только для текста ответа
    try:
        svc = await db.get_service(str(payment["idservice"]))
    except Exception:
        logger.exception("Возврат %s: дни отобраны, но сервис не прочитать", parts[1])
        await message.answer("↩️ Возвращено.")
        return

    if svc is None:
        # Сервис удалён (idrecstatus != 0): db.get_service его не находит,
        # хотя db.revoke_payment уже отобрал дни исправно. Деньги и дни к
        # этому моменту уже списаны обратно — упасть здесь нельзя. Плательщику
        # слать нечего: сервиса, срок которого продлевали, больше нет
        await message.answer("↩️ Возвращено. Сервис удалён, срок отобран.")
        return

    await message.answer(
        f"↩️ Возвращено. Срок сервиса «{h(svc['service_name'])}» — "
        f"{render.local_dt(paid_until, svc['timezone'])}."
    )
    await safe_send(bot, payment["granted_by"], render.refund_done(svc, paid_until))
