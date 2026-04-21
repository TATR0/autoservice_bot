"""
handlers/admin_mgmt.py

Управление администраторами — только для управляющих (owner).
Кнопки: ➕ Добавить админа, ➖ Удалить админа
"""

import logging
import re

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.types import CallbackQuery, Message

from database import db
from keyboards import kb_cancel, kb_owner_main, kb_select_admin, kb_select_service

logger = logging.getLogger(__name__)
router = Router()


# ─────────────────────────────────────────────────────────────────────────────
# FSM — добавление админа
# ─────────────────────────────────────────────────────────────────────────────

class AddAdmin(StatesGroup):
    select_service = State()
    enter_user     = State()


@router.message(F.text == "➕ Добавить админа", StateFilter(default_state))
async def add_admin_start(message: Message, state: FSMContext) -> None:
    owned = await db.get_owned_services(message.from_user.id)
    if not owned:
        await message.answer("❌ У вас нет сервисов, которыми вы управляете.")
        return

    await state.clear()
    if len(owned) == 1:
        await state.update_data(idservice=owned[0]["idservice"])
        await _ask_new_admin(message, state)
        return

    await message.answer(
        "Выберите сервис, в который нужно добавить администратора:",
        reply_markup=kb_select_service(owned, "addadmin_svc"),
    )
    await state.set_state(AddAdmin.select_service)


@router.callback_query(F.data.startswith("addadmin_svc:"), StateFilter(AddAdmin.select_service))
async def add_admin_service_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    idservice = callback.data.split(":", 1)[1]
    await state.update_data(idservice=idservice)
    await callback.message.delete()
    await _ask_new_admin(callback.message, state)


async def _ask_new_admin(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Введите Telegram-аккаунт нового администратора:\n\n"
        "• <code>@username</code>\n"
        "• <code>123456789</code> — числовой ID",
        parse_mode="HTML",
        reply_markup=kb_cancel(),
    )
    await state.set_state(AddAdmin.enter_user)


@router.message(StateFilter(AddAdmin.enter_user))
async def add_admin_user_entered(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("↩️ Отменено.", reply_markup=kb_owner_main())
        return

    tg_id, display = await _resolve_user(message, message.text.strip())
    if tg_id is None:
        return

    data = await state.get_data()
    idservice = data["idservice"]
    await state.clear()

    if await db.is_admin(idservice, tg_id):
        await message.answer(
            f"⚠️ Пользователь <code>{tg_id}</code> уже является администратором этого сервиса.",
            parse_mode="HTML",
            reply_markup=kb_owner_main(),
        )
        return

    await db.add_admin(idservice, tg_id)

    svc = await db.get_service(idservice)
    svc_name = svc["service_name"] if svc else idservice
    await message.answer(
        f"✅ <b>{display}</b> добавлен(а) как администратор <b>{svc_name}</b>.",
        parse_mode="HTML",
        reply_markup=kb_owner_main(),
    )
    try:
        await message.bot.send_message(
            tg_id,
            f"👋 Вас назначили администратором сервиса <b>{svc_name}</b>!\n"
            "Нажмите /start для работы с заявками.",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# FSM — удаление админа
# ─────────────────────────────────────────────────────────────────────────────

class RemoveAdmin(StatesGroup):
    select_service = State()
    select_admin   = State()


@router.message(F.text == "➖ Удалить админа", StateFilter(default_state))
async def remove_admin_start(message: Message, state: FSMContext) -> None:
    owned = await db.get_owned_services(message.from_user.id)
    if not owned:
        await message.answer("❌ У вас нет сервисов.")
        return

    await state.clear()
    if len(owned) == 1:
        await state.update_data(idservice=owned[0]["idservice"])
        await _show_admins_to_remove(message, state)
        return

    await message.answer(
        "Выберите сервис:",
        reply_markup=kb_select_service(owned, "rmadmin_svc"),
    )
    await state.set_state(RemoveAdmin.select_service)


@router.callback_query(F.data.startswith("rmadmin_svc:"), StateFilter(RemoveAdmin.select_service))
async def remove_admin_service_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    idservice = callback.data.split(":", 1)[1]
    await state.update_data(idservice=idservice)
    await callback.message.delete()
    await _show_admins_to_remove(callback.message, state)


async def _show_admins_to_remove(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    idservice = data["idservice"]
    admins = await db.get_active_admins(idservice)
    if not admins:
        await state.clear()
        await message.answer("ℹ️ У этого сервиса нет администраторов.", reply_markup=kb_owner_main())
        return

    await message.answer(
        "Выберите администратора для удаления:",
        reply_markup=kb_select_admin(admins),
    )
    await state.set_state(RemoveAdmin.select_admin)


@router.callback_query(F.data.startswith("rmadmin:"), StateFilter(RemoveAdmin.select_admin))
async def remove_admin_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    admin_tg_id = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    idservice = data["idservice"]
    await state.clear()

    await db.remove_admin(idservice, admin_tg_id)

    svc = await db.get_service(idservice)
    svc_name = svc["service_name"] if svc else idservice

    await callback.message.edit_text(
        f"✅ Администратор <code>{admin_tg_id}</code> удалён из сервиса <b>{svc_name}</b>.",
        parse_mode="HTML",
    )
    await callback.message.answer("Готово.", reply_markup=kb_owner_main())

    try:
        await callback.bot.send_message(
            admin_tg_id,
            f"ℹ️ Вы были удалены из администраторов сервиса <b>{svc_name}</b>.",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _resolve_user(message: Message, text: str) -> tuple[int | None, str]:
    m = re.match(r"^@(\w{5,32})$", text)
    if m:
        try:
            chat = await message.bot.get_chat(f"@{m.group(1)}")
            return chat.id, f"@{m.group(1)} (ID: {chat.id})"
        except Exception:
            await message.answer(
                f"❌ Пользователь <code>@{m.group(1)}</code> не найден.",
                parse_mode="HTML",
            )
            return None, ""
    if text.lstrip("-").isdigit():
        tg_id = int(text)
        try:
            await message.bot.get_chat(tg_id)
            return tg_id, f"ID {tg_id}"
        except Exception:
            await message.answer(f"❌ Пользователь <code>{tg_id}</code> не найден.", parse_mode="HTML")
            return None, ""
    await message.answer("❌ Неверный формат. Введите <code>@username</code> или числовой ID.", parse_mode="HTML")
    return None, ""
