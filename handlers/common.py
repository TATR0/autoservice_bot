"""
handlers/common.py — активный сервис и показ главного меню.

Пользователь может быть владельцем одного сервиса и админом другого.
Выбранный сервис лежит в FSM-данных под ключом active_service, все кнопки
меню работают в его контексте.
"""

from __future__ import annotations

import logging

import asyncpg
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import keyboards as kb
from database import db
from validators import h

logger = logging.getLogger(__name__)

ACTIVE_KEY = "active_service"


async def set_active_service(state: FSMContext, idservice: str) -> None:
    await state.update_data(**{ACTIVE_KEY: idservice})


async def get_active_service(state: FSMContext, tg_id: int) -> asyncpg.Record | None:
    """
    Вернуть выбранный сервис. Если выбора не было или он больше не доступен —
    взять единственный доступный. Если сервисов несколько — вернуть None,
    хендлер попросит выбрать.
    """
    services = await db.get_user_services(tg_id)
    if not services:
        return None

    data = await state.get_data()
    active_id = data.get(ACTIVE_KEY)
    if active_id:
        for svc in services:
            if str(svc["idservice"]) == str(active_id):
                return svc

    if len(services) == 1:
        await set_active_service(state, str(services[0]["idservice"]))
        return services[0]
    return None


async def require_active_service(
    message: Message, state: FSMContext, user_id: int | None = None
) -> asyncpg.Record | None:
    """
    Получить активный сервис или объяснить пользователю, что делать дальше.
    Возвращает None, если продолжать нельзя.
    """
    user_id = user_id or message.from_user.id
    services = await db.get_user_services(user_id)
    if not services:
        await message.answer(
            "❌ У вас нет доступных сервисов.",
            reply_markup=kb.kb_client_main(),
        )
        return None

    svc = await get_active_service(state, user_id)
    if svc is None:
        await message.answer(
            "У вас несколько сервисов. Выберите, с каким работать:",
            reply_markup=kb.kb_select_service(services, "pick_service"),
        )
        return None
    return svc


async def require_owner_service(
    message: Message, state: FSMContext, user_id: int | None = None
) -> asyncpg.Record | None:
    """Активный сервис, если пользователь — его управляющий. Иначе None."""
    user_id = user_id or message.from_user.id
    svc = await require_active_service(message, state, user_id)
    if svc is None:
        return None
    if svc["owner_id"] != user_id:
        await message.answer("❌ Это может только управляющий сервисом.")
        return None
    return svc


def main_menu_markup(svc, role: str, many: bool):
    if role == "owner":
        return kb.kb_owner_main(str(svc["idservice"]), many_services=many)
    return kb.kb_admin_main(str(svc["idservice"]), many_services=many)


async def show_main_menu(
    message: Message,
    state: FSMContext,
    *,
    greeting: str | None = None,
    user_id: int | None = None,
) -> None:
    """
    Показать меню, соответствующее роли пользователя в активном сервисе.

    user_id обязателен, когда меню показывается из callback: там
    message.from_user — это бот, а не пользователь.
    """
    user_id = user_id or message.from_user.id
    services = await db.get_user_services(user_id)

    if not services:
        await message.answer(
            greeting
            or (
                "🚗 <b>Добро пожаловать!</b>\n\n"
                "Здесь можно записаться в автосервис онлайн.\n\n"
                f"Нажмите <b>«{kb.BTN_BOOK}»</b> — откроется форма: "
                "выберите город, сервис и заполните заявку.\n\n"
                f"Свой автосервис регистрируется кнопкой <b>«{kb.BTN_REGISTER}»</b>."
            ),
            reply_markup=kb.kb_client_main(),
        )
        return

    svc = await get_active_service(state, user_id)
    if svc is None:
        await message.answer(
            greeting or "👋 У вас несколько сервисов. Выберите активный:",
            reply_markup=kb.kb_select_service(services, "pick_service"),
        )
        return

    role = svc["role"]
    role_title = "управляющий" if role == "owner" else "администратор"
    text = greeting or (
        f"👋 <b>Добро пожаловать, {role_title}!</b>\n\n"
        f"Активный сервис: <b>{h(svc['service_name'])}</b>"
    )
    await message.answer(
        text,
        reply_markup=main_menu_markup(svc, role, len(services) > 1),
    )
