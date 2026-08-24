"""
handlers/subscription.py — продление подписки владельцем бота.

Пока приёма денег нет, продлевает человек: /extend <idservice> <дней>. Когда
появится провайдер, он позовёт тот же db.extend_subscription, только с другим
source и с external_id для идемпотентности.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

import config
import render
from database import db
from validators import ValidationError, validate_uuid

router = Router()

MAX_DAYS = 366

USAGE = (
    "Продление подписки:\n"
    "<code>/extend &lt;idservice&gt; &lt;дней&gt;</code>\n"
    f"Дней — от 1 до {MAX_DAYS}."
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

    if not parts[2].isdigit() or not 1 <= int(parts[2]) <= MAX_DAYS:
        await message.answer(USAGE)
        return

    days = int(parts[2])
    paid_until = await db.extend_subscription(
        idservice, days=days, granted_by=message.from_user.id
    )
    if paid_until is None:
        await message.answer("❌ Сервис не найден или удалён.")
        return

    svc = await db.get_service(idservice)
    await message.answer(
        f"✅ «{svc['service_name']}» продлён на {days} дн.\n"
        f"Подписка действует до {render.local_dt(paid_until, svc['timezone'])}."
    )
