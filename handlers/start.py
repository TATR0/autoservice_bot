"""
handlers/start.py

/start               — определяем роль и показываем нужное меню
/start SVC_<uuid>    — клиент пришёл по ссылке сервиса
/start ADM_<token>   — приглашение стать администратором
"""

import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import keyboards as kb
import subscription
from database import db
from handlers.common import set_active_service, show_main_menu
from notifications import safe_send
from validators import format_phone, h

logger = logging.getLogger(__name__)
router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    # Сбрасываем незавершённый FSM-поток, активный сервис сохраняем
    data = await state.get_data()
    await state.clear()
    if data.get("active_service"):
        await set_active_service(state, data["active_service"])

    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) == 2 else ""

    if payload.startswith("SVC_"):
        await _handle_service_link(message, payload[4:])
        return

    if payload.startswith("ADM_"):
        await _handle_invite_link(message, payload[4:])
        return

    await show_main_menu(message, state)


async def _handle_service_link(message: Message, idservice: str) -> None:
    service = await db.get_service(idservice)
    if not service:
        await message.answer(
            "❌ Сервис не найден или больше не активен.",
            reply_markup=kb.kb_client_main(),
        )
        return

    # Ссылку могли сохранить или переслать — фильтр поиска её не прикрывает.
    # Телефон даём, как в ветке ниже: сервис не должен терять заказ из-за
    # того, что не заплатил нам
    if not subscription.is_active(service["paid_until"], datetime.now(timezone.utc)):
        await message.answer(
            "⚠️ " + config.CLOSED_FOR_BOOKING.format(
                phone=service["service_number"]
            ),
            reply_markup=kb.kb_client_main(),
        )
        return

    if not kb.webapp_url(idservice):
        await message.answer(
            "⚠️ Онлайн-форма временно недоступна.\n"
            f"Позвоните в сервис: <code>{h(service['service_number'])}</code>"
        )
        return

    await message.answer(
        f"🔧 <b>Добро пожаловать!</b>\n\n"
        f"Вы открыли форму записи в <b>{h(service['service_name'])}</b>.\n"
        f"📍 {h(service['city'])}, {h(service['location_service'])}",
        reply_markup=kb.kb_client_service(),
    )
    # Отдельным сообщением, потому что inline-кнопку и reply-клавиатуру
    # нельзя приложить к одному сообщению.
    await message.answer(
        "Нажмите кнопку ниже, чтобы заполнить заявку 👇",
        reply_markup=kb.kb_open_webapp(idservice),
    )


async def _handle_invite_link(message: Message, token: str) -> None:
    invite = await db.get_valid_invite(token)
    if not invite:
        await message.answer(
            "❌ Приглашение недействительно: срок истёк или ссылку уже использовали.\n\n"
            "Попросите управляющего прислать новую.",
            reply_markup=kb.kb_client_main(),
        )
        return

    if await db.is_admin(str(invite["idservice"]), message.from_user.id):
        await message.answer(
            f"ℹ️ Вы уже администратор сервиса <b>{h(invite['service_name'])}</b>."
        )
        return

    await message.answer(
        f"👥 Вас приглашают стать администратором сервиса "
        f"<b>{h(invite['service_name'])}</b>.\n\n"
        "Администратор видит заявки клиентов и меняет их статусы.",
        reply_markup=kb.kb_accept_invite(token),
    )


@router.callback_query(F.data.startswith("invite_accept:"))
async def invite_accept(callback: CallbackQuery, state: FSMContext) -> None:
    token = callback.data.split(":", 1)[1]
    invite = await db.use_invite(token, callback.from_user.id)

    if invite is None:
        await callback.answer("❌ Приглашение уже использовано или истекло.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    svc = await db.get_service(str(invite["idservice"]))
    svc_name = svc["service_name"] if svc else "сервис"

    await set_active_service(state, str(invite["idservice"]))
    await callback.message.edit_text(
        f"✅ Вы стали администратором сервиса <b>{h(svc_name)}</b>."
    )
    await callback.answer("Готово!")
    await show_main_menu(
        callback.message,
        state,
        greeting=f"🔧 Активный сервис: <b>{h(svc_name)}</b>",
        user_id=callback.from_user.id,
    )

    # Управляющий обязан узнать, кто именно принял приглашение: ссылку могли
    # переслать не тому, а новый администратор видит персональные данные всех
    # клиентов сервиса.
    if svc:
        new_admin = await db.get_user(callback.from_user.id)
        await safe_send(
            callback.bot,
            svc["owner_id"],
            f"👥 <b>Новый администратор</b> в сервисе <b>{h(svc_name)}</b>:\n"
            f"{h(db.user_title(new_admin, callback.from_user.id))}\n\n"
            f"<i>Если вы не приглашали этого человека, снимите его кнопкой "
            f"«{kb.BTN_REMOVE_ADMIN}».</i>",
        )

    user = await db.get_user(callback.from_user.id)
    await safe_send(
        callback.bot,
        invite["created_by"],
        f"👥 <b>{h(db.user_title(user, callback.from_user.id))}</b> принял приглашение "
        f"и стал администратором сервиса <b>{h(svc_name)}</b>.",
    )


# ── Выбор активного сервиса ──────────────────────────────────────────────────

@router.message(Command("menu"))
@router.message(F.text == kb.BTN_SWITCH)
async def switch_service(message: Message, state: FSMContext) -> None:
    services = await db.get_user_services(message.from_user.id)
    if not services:
        await show_main_menu(message, state)
        return
    if len(services) == 1:
        await set_active_service(state, str(services[0]["idservice"]))
        await show_main_menu(message, state)
        return

    await message.answer(
        "Выберите активный сервис:",
        reply_markup=kb.kb_select_service(services, "pick_service"),
    )


@router.callback_query(F.data.startswith("pick_service:"))
async def pick_service(callback: CallbackQuery, state: FSMContext) -> None:
    idservice = callback.data.split(":", 1)[1]

    if not await db.has_access(idservice, callback.from_user.id):
        await callback.answer("❌ У вас нет доступа к этому сервису.", show_alert=True)
        return

    await set_active_service(state, idservice)
    svc = await db.get_service(idservice)
    svc_name = svc["service_name"] if svc else idservice

    await callback.message.edit_text(f"✅ Активный сервис: <b>{h(svc_name)}</b>")
    await callback.answer()
    await show_main_menu(
        callback.message,
        state,
        greeting=f"🔧 Работаем с сервисом <b>{h(svc_name)}</b>",
        user_id=callback.from_user.id,
    )


@router.callback_query(F.data == "cancel_action")
async def cancel_action(callback: CallbackQuery) -> None:
    try:
        await callback.message.delete()
    except Exception:
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Отменено.")
