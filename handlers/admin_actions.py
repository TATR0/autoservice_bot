"""
handlers/admin_actions.py
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import default_state
from aiogram.types import CallbackQuery, Message

from config import CLIENT_NOTIFICATIONS, REQUEST_STATUS_LABELS, SERVICE_TYPES, URGENCY_LABELS
from database import db
from keyboards import kb_request_actions

logger = logging.getLogger(__name__)
router = Router()


async def _get_all_user_services(tg_id: int) -> list:
    admin_svcs = await db.get_admin_services(tg_id)
    owned_svcs = await db.get_owned_services(tg_id)
    merged = {s["idservice"]: s for s in [*admin_svcs, *owned_svcs]}
    return list(merged.values())


# ── Смена статуса заявки ──────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("req:"))
async def request_status_cb(callback: CallbackQuery) -> None:
    _, status, request_id = callback.data.split(":", 2)

    req = await db.get_request(request_id)
    if not req:
        await callback.answer("❌ Заявка не найдена.", show_alert=True)
        return

    has_access = (
        await db.is_admin(req["idservice"], callback.from_user.id)
        or await db.is_owner(req["idservice"], callback.from_user.id)
    )
    if not has_access:
        await callback.answer("❌ У вас нет прав изменять эту заявку.", show_alert=True)
        return

    await db.set_request_status(request_id, status)

    label = REQUEST_STATUS_LABELS.get(status, status)
    new_text = callback.message.html_text + f"\n\n📌 <b>Статус обновлён:</b> {label}"
    try:
        await callback.message.edit_text(new_text, parse_mode="HTML")
    except Exception:
        pass
    await callback.answer(f"✅ Статус: {label}")

    notify = CLIENT_NOTIFICATIONS.get(status)
    if notify and req["idclienttg"]:
        svc = await db.get_service(req["idservice"])
        svc_name = svc["service_name"] if svc else "Автосервис"
        try:
            await callback.bot.send_message(
                req["idclienttg"],
                f"{notify}\n\n<b>Сервис:</b> {svc_name}\n🆔 <code>{request_id}</code>",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("Не удалось уведомить клиента %s: %s", req["idclienttg"], exc)


# ── 📋 Заявки сервиса ─────────────────────────────────────────────────────────

@router.message(F.text == "📋 Заявки сервиса", StateFilter(default_state))
async def service_requests(message: Message) -> None:
    services = await _get_all_user_services(message.from_user.id)
    if not services:
        await message.answer("❌ У вас нет доступных сервисов.")
        return

    for svc in services:
        reqs = await db.get_service_requests(svc["idservice"], limit=15)
        text = f"<b>📋 {svc['service_name']}</b>\n"

        if not reqs:
            text += "<i>Заявок нет</i>"
            await message.answer(text, parse_mode="HTML")
            continue

        for r in reqs:
            label = REQUEST_STATUS_LABELS.get(r["status"], r["status"])
            st    = SERVICE_TYPES.get(r["service_type"], r["service_type"])
            ur    = URGENCY_LABELS.get(r["urgency"], r["urgency"])
            date  = r["createdate"].strftime("%d.%m %H:%M") if r["createdate"] else "—"
            text += (
                f"\n• <b>{r['client_name']}</b> | {label}\n"
                f"  📞 <code>{r['phone']}</code> | 🚗 {r['brand']} {r['model']}\n"
                f"  🔧 {st} | ⚡ {ur}\n"
                f"  🕒 {date} | 🆔 <code>{r['idrequests']}</code>\n"
            )

        for chunk in _split_text(text, 4000):
            await message.answer(chunk, parse_mode="HTML")


# ── ℹ️ О сервисе ──────────────────────────────────────────────────────────────

@router.message(F.text == "ℹ️ О сервисе", StateFilter(default_state))
async def about_service(message: Message) -> None:
    services = await _get_all_user_services(message.from_user.id)
    if not services:
        await message.answer("❌ У вас нет доступных сервисов.")
        return

    for svc in services:
        link       = db.service_link(svc["idservice"])
        admins     = await db.get_active_admins(svc["idservice"])
        admins_str = ", ".join(f"<code>{a['idusertg']}</code>" for a in admins) or "<i>нет</i>"
        is_owner   = svc["owner_id"] == message.from_user.id
        role       = "👑 Управляющий" if is_owner else "🔧 Администратор"

        await message.answer(
            f"<b>ℹ️ {svc['service_name']}</b>\n\n"
            f"📞 Телефон: {svc['service_number']}\n"
            f"🏙 Город: {svc['city']}\n"
            f"📍 Адрес: {svc['location_service']}\n"
            f"👥 Администраторы: {admins_str}\n"
            f"🔑 Ваша роль: {role}\n\n"
            f"🔗 <b>Ссылка для клиентов:</b>\n<code>{link}</code>",
            parse_mode="HTML",
        )


# ── 👥 Администраторы ─────────────────────────────────────────────────────────

@router.message(F.text == "👥 Администраторы", StateFilter(default_state))
async def list_admins(message: Message) -> None:
    services = await _get_all_user_services(message.from_user.id)
    if not services:
        await message.answer("❌ У вас нет доступных сервисов.")
        return

    for svc in services:
        admins = await db.get_active_admins(svc["idservice"])
        text   = f"<b>👥 Администраторы — {svc['service_name']}</b>\n\n"
        if admins:
            for adm in admins:
                owner_mark = " 👑" if svc["owner_id"] == adm["idusertg"] else ""
                text += f"• <code>{adm['idusertg']}</code>{owner_mark}\n"
        else:
            text += "<i>Администраторов нет</i>"
        await message.answer(text, parse_mode="HTML")


# ── 🚪 Уйти из администраторов (/leave_admin + кнопка) ───────────────────────

@router.message(Command("leave_admin"), StateFilter(default_state))
@router.message(F.text == "🚪 Уйти из администраторов", StateFilter(default_state))
async def leave_admin_start(message: Message) -> None:
    from keyboards import kb_confirm_leave, kb_select_service

    admin_svcs = await db.get_admin_services(message.from_user.id)
    owned_ids  = {s["idservice"] for s in await db.get_owned_services(message.from_user.id)}
    leavable   = [s for s in admin_svcs if s["idservice"] not in owned_ids]

    if not leavable:
        await message.answer(
            "ℹ️ Вы не являетесь администратором ни в одном чужом сервисе.\n\n"
            "<i>Управляющий не может покинуть собственный сервис.</i>",
            parse_mode="HTML",
        )
        return

    if len(leavable) == 1:
        svc = leavable[0]
        await message.answer(
            f"Вы уверены, что хотите выйти из администраторов сервиса "
            f"<b>{svc['service_name']}</b>?",
            parse_mode="HTML",
            reply_markup=kb_confirm_leave(svc["idservice"]),
        )
    else:
        await message.answer(
            "Из какого сервиса вы хотите выйти?",
            reply_markup=kb_select_service(leavable, "leave_pick"),
        )


@router.callback_query(F.data.startswith("leave_pick:"))
async def leave_admin_pick_service(callback: CallbackQuery) -> None:
    from keyboards import kb_confirm_leave
    idservice = callback.data.split(":", 1)[1]
    svc = await db.get_service(idservice)
    svc_name = svc["service_name"] if svc else idservice
    await callback.message.edit_text(
        f"Вы уверены, что хотите выйти из администраторов сервиса <b>{svc_name}</b>?",
        parse_mode="HTML",
        reply_markup=kb_confirm_leave(idservice),
    )


@router.callback_query(F.data.startswith("leave_admin:"))
async def leave_admin_confirm(callback: CallbackQuery) -> None:
    from keyboards import kb_client_main
    idservice = callback.data.split(":", 1)[1]

    if await db.is_owner(idservice, callback.from_user.id):
        await callback.answer("❌ Управляющий не может покинуть собственный сервис.", show_alert=True)
        return

    svc = await db.get_service(idservice)
    svc_name = svc["service_name"] if svc else idservice
    await db.remove_admin(idservice, callback.from_user.id)

    await callback.message.edit_text(
        f"✅ Вы вышли из администраторов сервиса <b>{svc_name}</b>.",
        parse_mode="HTML",
    )
    await callback.message.answer("Возвращаю вас в главное меню.", reply_markup=kb_client_main())


@router.callback_query(F.data == "leave_cancel")
async def leave_admin_cancel(callback: CallbackQuery) -> None:
    await callback.message.delete()
    await callback.answer("Отменено.")


# ── Fallback ──────────────────────────────────────────────────────────────────

@router.message(StateFilter(default_state))
async def fallback(message: Message) -> None:
    from keyboards import kb_client_main
    await message.answer(
        "❓ Неизвестная команда.\n\nНажмите /start для начала.",
        reply_markup=kb_client_main(),
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _split_text(text: str, limit: int) -> list[str]:
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current:
        chunks.append(current)
    return chunks
