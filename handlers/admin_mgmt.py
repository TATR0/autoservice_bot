"""
handlers/admin_mgmt.py — администраторы сервиса.

• ➕ Пригласить админа — инвайт-ссылка вместо ввода tg id.
  Ввод id не работал: обычный человек его не знает, а боту нельзя написать
  первым тому, кто не запускал бота (403 Forbidden).
• ➖ Удалить админа — только управляющий, себя снять нельзя.
• 🚪 Уйти из администраторов — самоудаление.
• 🗑 Удалить сервис — каскадно закрывает админов и живые заявки.
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import default_state
from aiogram.types import CallbackQuery, Message

import config
import keyboards as kb
from database import db
from handlers.common import require_active_service, show_main_menu
from notifications import broadcast, safe_send
from validators import h

logger = logging.getLogger(__name__)
router = Router()


async def _admin_titles(admins: list) -> dict[int, str]:
    return {
        adm["idusertg"]: db.user_title(adm, adm["idusertg"]) for adm in admins
    }


# ── Список администраторов ───────────────────────────────────────────────────

@router.message(F.text == kb.BTN_ADMINS, StateFilter(default_state))
async def list_admins(message: Message, state: FSMContext) -> None:
    svc = await require_active_service(message, state)
    if svc is None:
        return

    admins = await db.get_active_admins(str(svc["idservice"]))
    text = f"<b>👥 Администраторы — {h(svc['service_name'])}</b>\n\n"
    if admins:
        for adm in admins:
            mark = " 👑" if adm["idusertg"] == svc["owner_id"] else ""
            blocked = " 🚫<i>бот заблокирован</i>" if adm["is_blocked"] else ""
            text += f"• {h(db.user_title(adm, adm['idusertg']))}{mark}{blocked}\n"
    else:
        text += "<i>Администраторов нет</i>"
    await message.answer(text)


# ── Приглашение администратора ───────────────────────────────────────────────

@router.message(F.text == kb.BTN_INVITE, StateFilter(default_state))
async def invite_admin(message: Message, state: FSMContext) -> None:
    svc = await require_active_service(message, state)
    if svc is None:
        return

    if svc["owner_id"] != message.from_user.id:
        await message.answer("❌ Приглашать администраторов может только управляющий.")
        return

    token = await db.create_invite(str(svc["idservice"]), message.from_user.id)
    link = db.invite_link(token)

    await message.answer(
        f"👥 <b>Приглашение администратора</b>\n"
        f"Сервис: <b>{h(svc['service_name'])}</b>\n\n"
        "Перешлите эту ссылку сотруднику — он откроет её и подтвердит согласие:\n\n"
        f"{h(link)}\n\n"
        f"⏳ Ссылка действует {config.INVITE_TTL_DAYS} дней и сработает один раз.",
        reply_markup=kb.kb_share_invite(link, svc["service_name"]),
    )


# ── Удаление администратора ──────────────────────────────────────────────────

@router.message(F.text == kb.BTN_REMOVE_ADMIN, StateFilter(default_state))
async def remove_admin_start(message: Message, state: FSMContext) -> None:
    svc = await require_active_service(message, state)
    if svc is None:
        return

    if svc["owner_id"] != message.from_user.id:
        await message.answer("❌ Удалять администраторов может только управляющий.")
        return

    admins = await db.get_active_admins(str(svc["idservice"]))
    removable = [a for a in admins if a["idusertg"] != svc["owner_id"]]
    if not removable:
        await message.answer(
            "ℹ️ Кроме вас, администраторов в этом сервисе нет.\n"
            "<i>Управляющего снять нельзя — сервис остался бы без хозяина.</i>"
        )
        return

    await message.answer(
        "Выберите администратора для удаления:",
        reply_markup=kb.kb_select_admin(
            removable, svc["owner_id"], await _admin_titles(removable)
        ),
    )


@router.callback_query(F.data.startswith("rmadmin:"))
async def remove_admin_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    admin_tg_id = int(callback.data.split(":", 1)[1])

    svc = await require_active_service(callback.message, state, callback.from_user.id)
    if svc is None:
        await callback.answer()
        return

    if svc["owner_id"] != callback.from_user.id:
        await callback.answer("❌ Только управляющий может это сделать.", show_alert=True)
        return
    if admin_tg_id == svc["owner_id"]:
        await callback.answer("❌ Управляющего снять нельзя.", show_alert=True)
        return

    await db.remove_admin(str(svc["idservice"]), admin_tg_id)
    user = await db.get_user(admin_tg_id)

    await callback.message.edit_text(
        f"✅ {h(db.user_title(user, admin_tg_id))} удалён(а) из администраторов "
        f"<b>{h(svc['service_name'])}</b>."
    )
    await callback.answer("Готово.")

    await safe_send(
        callback.bot,
        admin_tg_id,
        f"ℹ️ Вы больше не администратор сервиса <b>{h(svc['service_name'])}</b>.",
    )


# ── Самоудаление администратора ──────────────────────────────────────────────

@router.message(Command("leave_admin"), StateFilter(default_state))
@router.message(F.text == kb.BTN_LEAVE, StateFilter(default_state))
async def leave_admin_start(message: Message, state: FSMContext) -> None:
    services = await db.get_user_services(message.from_user.id)
    leavable = [s for s in services if s["role"] == "admin"]

    if not leavable:
        await message.answer(
            "ℹ️ Вы не администратор ни в одном чужом сервисе.\n\n"
            "<i>Управляющий не может покинуть собственный сервис — "
            f"для этого есть «{kb.BTN_DELETE_SERVICE}».</i>"
        )
        return

    if len(leavable) == 1:
        svc = leavable[0]
        await message.answer(
            f"Выйти из администраторов сервиса <b>{h(svc['service_name'])}</b>?",
            reply_markup=kb.kb_confirm("leave_admin", str(svc["idservice"])),
        )
        return

    await message.answer(
        "Из какого сервиса вы хотите выйти?",
        reply_markup=kb.kb_select_service(leavable, "leave_pick"),
    )


@router.callback_query(F.data.startswith("leave_pick:"))
async def leave_admin_pick(callback: CallbackQuery) -> None:
    idservice = callback.data.split(":", 1)[1]
    svc = await db.get_service(idservice)
    if not svc:
        await callback.answer("❌ Сервис не найден.", show_alert=True)
        return

    await callback.message.edit_text(
        f"Выйти из администраторов сервиса <b>{h(svc['service_name'])}</b>?",
        reply_markup=kb.kb_confirm("leave_admin", idservice),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("leave_admin:"))
async def leave_admin_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    idservice = callback.data.split(":", 1)[1]

    if await db.is_owner(idservice, callback.from_user.id):
        await callback.answer(
            "❌ Управляющий не может покинуть собственный сервис.", show_alert=True
        )
        return
    if not await db.is_admin(idservice, callback.from_user.id):
        await callback.answer("Вы уже не администратор этого сервиса.", show_alert=True)
        return

    svc = await db.get_service(idservice)
    svc_name = svc["service_name"] if svc else idservice
    await db.remove_admin(idservice, callback.from_user.id)

    data = await state.get_data()
    if str(data.get("active_service")) == str(idservice):
        await state.update_data(active_service=None)

    await callback.message.edit_text(
        f"✅ Вы вышли из администраторов сервиса <b>{h(svc_name)}</b>."
    )
    await callback.answer()
    await show_main_menu(callback.message, state, user_id=callback.from_user.id)

    if svc:
        user = await db.get_user(callback.from_user.id)
        await safe_send(
            callback.bot,
            svc["owner_id"],
            f"ℹ️ {h(db.user_title(user, callback.from_user.id))} вышел(а) из "
            f"администраторов сервиса <b>{h(svc_name)}</b>.",
        )


# ── Удаление сервиса ─────────────────────────────────────────────────────────

@router.message(F.text == kb.BTN_DELETE_SERVICE, StateFilter(default_state))
async def delete_service_start(message: Message, state: FSMContext) -> None:
    svc = await require_active_service(message, state)
    if svc is None:
        return

    if svc["owner_id"] != message.from_user.id:
        await message.answer("❌ Удалить сервис может только управляющий.")
        return

    stats = await db.get_service_stats(str(svc["idservice"]))
    active = (stats["st_new"] or 0) + (stats["st_work"] or 0) if stats else 0

    await message.answer(
        f"🗑 <b>Удалить сервис «{h(svc['service_name'])}»?</b>\n\n"
        "Что произойдёт:\n"
        "• сервис исчезнет из поиска по городу;\n"
        "• все администраторы потеряют доступ;\n"
        f"• {active} активных заявок будут закрыты, клиентов уведомим.\n\n"
        "<i>Действие необратимо.</i>",
        reply_markup=kb.kb_confirm("delsvc", str(svc["idservice"])),
    )


@router.callback_query(F.data.startswith("delsvc:"))
async def delete_service_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    idservice = callback.data.split(":", 1)[1]

    if not await db.is_owner(idservice, callback.from_user.id):
        await callback.answer("❌ Только управляющий может удалить сервис.", show_alert=True)
        return

    svc = await db.get_service(idservice)
    svc_name = svc["service_name"] if svc else idservice
    staff = await db.get_staff_ids(idservice)

    closed_requests = await db.close_service(idservice)

    await state.update_data(active_service=None)
    await callback.message.edit_text(f"🗑 Сервис <b>{h(svc_name)}</b> удалён.")
    await callback.answer("Сервис удалён.")
    await show_main_menu(callback.message, state, user_id=callback.from_user.id)

    # Бывшим сотрудникам
    await broadcast(
        callback.bot,
        [uid for uid in staff if uid != callback.from_user.id],
        f"ℹ️ Сервис <b>{h(svc_name)}</b> был удалён управляющим. "
        "Доступ к его заявкам закрыт.",
    )

    # Клиентам закрытых заявок
    client_ids = {r["idclienttg"] for r in closed_requests if r["idclienttg"]}
    if client_ids:
        await broadcast(
            callback.bot,
            list(client_ids),
            f"{config.CLIENT_NOTIFICATIONS['service_closed']}\n\n"
            f"<b>Сервис:</b> {h(svc_name)}",
        )
