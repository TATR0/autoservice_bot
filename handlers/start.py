"""
handlers/start.py

/start               — определяем роль и показываем нужное меню
/start SVC_<uuid>    — клиент пришёл по ссылке сервиса
"""

import logging

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from database import db
from keyboards import (
    kb_admin_main,
    kb_client_main,
    kb_client_webservice,
    kb_owner_main,
    kb_register_prompt,
)

logger = logging.getLogger(__name__)
router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id
    args = message.text.strip().split(maxsplit=1)
    deep_link = args[1] if len(args) == 2 else None

    # ── Пришёл по ссылке сервиса ─────────────────────────────────────────────
    if deep_link and deep_link.startswith("SVC_"):
        idservice = deep_link[4:]
        service = await db.get_service(idservice)
        if not service:
            await message.answer("❌ Сервис не найден или больше не активен.")
            return

        await message.answer(
            f"🔧 <b>Добро пожаловать!</b>\n\n"
            f"Вы открыли форму записи в <b>{service['service_name']}</b>.\n"
            f"Нажмите кнопку ниже, чтобы заполнить заявку 👇",
            parse_mode="HTML",
            reply_markup=kb_client_webservice(idservice),
        )
        return

    # ── Определяем роль ───────────────────────────────────────────────────────
    owned = await db.get_owned_services(user_id)
    if owned:
        # Управляющий (owner хотя бы одного сервиса)
        names = ", ".join(s["service_name"] for s in owned[:3])
        await message.answer(
            f"👋 <b>Добро пожаловать, управляющий!</b>\n\n"
            f"Ваши сервисы: <i>{names}</i>",
            parse_mode="HTML",
            reply_markup=kb_owner_main(),
        )
        return

    admin_services = await db.get_admin_services(user_id)
    if admin_services:
        # Администратор
        names = ", ".join(s["service_name"] for s in admin_services[:3])
        await message.answer(
            f"👋 <b>Добро пожаловать, администратор!</b>\n\n"
            f"Вы обслуживаете: <i>{names}</i>",
            parse_mode="HTML",
            reply_markup=kb_admin_main(),
        )
        return

    # Обычный пользователь
    await message.answer(
        "🚗 <b>Добро пожаловать!</b>\n\n"
        "Здесь вы можете записаться в автосервис онлайн.\n\n"
        "Нажмите кнопку <b>«Записаться в автосервис»</b> — "
        "откроется форма, выберите город и сервис.",
        parse_mode="HTML",
        reply_markup=kb_client_main(),
    )
