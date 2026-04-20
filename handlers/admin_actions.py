"""
handlers/admin_actions.py

• Callback-кнопки статусов заявок (принять / позвонили / отказать)
• «📋 Заявки сервиса» — расширенный список для админа
• «ℹ️ О сервисе»
• «👥 Администраторы»
"""

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from config import CLIENT_NOTIFICATIONS, REQUEST_STATUS_LABELS
from database import db
from keyboards import kb_admin_main, kb_owner_main, kb_request_actions

logger = logging.getLogger(__name__)
router = Router()


# ─────────────────────────────────────────────────────────────────────────────
# Inline: смена статуса заявки
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("req:"))
async def request_status_cb(callback: CallbackQuery) -> None:
    _, status, request_id = callback.data.split(":", 2)

    req = await db.get_request(request_id)
    if not req:
        await callback.answer("❌ Заявка не найдена.", show_alert=True)
        return

    # Проверяем, что нажимающий — администратор этого сервиса
    if not await db.is_admin(req["idservice"], callback.from_user.id):
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

    # Уведомляем клиента
    notify = CLIENT_NOTIFICATIONS.get(status)
    if notify and req["idclienttg"]:
        svc = await db.get_service(req["idservice"])
        svc_name = svc["service_name"] if svc else "Автосервис"
        try:
            await callback.bot.send_message(
                req["idclienttg"],
                f"{notify}\n\n"
                f"<b>Сервис:</b> {svc_name}\n"
                f"🆔 <code>{request_id}</code>",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("Не удалось уведомить клиента %s: %s", req["idclienttg"], exc)


# ─────────────────────────────────────────────────────────────────────────────
# «📋 Заявки сервиса» — для администратора/управляющего
# ─────────────────────────────────────────────────────────────────────────────

@router.message(F.text == "📋 Заявки сервиса")
async def service_requests(message: Message) -> None:
    services = await db.get_admin_services(message.from_user.id)
    if not services:
        await message.answer("❌ Вы не являетесь администратором ни одного сервиса.")
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
            from config import SERVICE_TYPES, URGENCY_LABELS
            st = SERVICE_TYPES.get(r["service_type"], r["service_type"])
            ur = URGENCY_LABELS.get(r["urgency"], r["urgency"])
            date = r["createdate"].strftime("%d.%m %H:%M") if r["createdate"] else "—"
            text += (
                f"\n• <b>{r['client_name']}</b> | {label}\n"
                f"  📞 <code>{r['phone']}</code> | 🚗 {r['brand']} {r['model']}\n"
                f"  🔧 {st} | ⚡ {ur}\n"
                f"  🕒 {date} | 🆔 <code>{r['idrequests']}</code>\n"
            )

        # Telegram limit: 4096 chars
        chunks = _split_text(text, 4000)
        for chunk in chunks:
            await message.answer(chunk, parse_mode="HTML")


# ─────────────────────────────────────────────────────────────────────────────
# «ℹ️ О сервисе»
# ─────────────────────────────────────────────────────────────────────────────

@router.message(F.text == "ℹ️ О сервисе")
async def about_service(message: Message) -> None:
    services = await db.get_admin_services(message.from_user.id)
    owned = await db.get_owned_services(message.from_user.id)
    all_svc = {s["idservice"]: s for s in [*services, *owned]}.values()

    if not all_svc:
        await message.answer("❌ Нет доступных сервисов.")
        return

    for svc in all_svc:
        link = db.service_link(svc["idservice"])
        admins = await db.get_active_admins(svc["idservice"])
        admins_str = ", ".join(f"<code>{a['idusertg']}</code>" for a in admins) or "<i>нет</i>"
        is_owner = svc["owner_id"] == message.from_user.id
        role = "Управляющий" if is_owner else "Администратор"

        await message.answer(
            f"<b>ℹ️ {svc['service_name']}</b>\n\n"
            f"📞 Телефон: {svc['service_number']}\n"
            f"🏙 Город: {svc['city']}\n"
            f"📍 Адрес: {svc['location_service']}\n"
            f"👥 Администраторы: {admins_str}\n"
            f"🔑 Ваша роль: {role}\n\n"
            f"🔗 Ссылка для клиентов:\n<code>{link}</code>",
            parse_mode="HTML",
        )


# ─────────────────────────────────────────────────────────────────────────────
# «👥 Администраторы»
# ─────────────────────────────────────────────────────────────────────────────

@router.message(F.text == "👥 Администраторы")
async def list_admins(message: Message) -> None:
    services = await db.get_admin_services(message.from_user.id)
    owned = await db.get_owned_services(message.from_user.id)
    all_svc = list({s["idservice"]: s for s in [*services, *owned]}.values())

    if not all_svc:
        await message.answer("❌ Нет доступных сервисов.")
        return

    for svc in all_svc:
        admins = await db.get_active_admins(svc["idservice"])
        text = f"<b>👥 Администраторы — {svc['service_name']}</b>\n\n"
        if admins:
            for adm in admins:
                owner_mark = " 👑" if svc["owner_id"] == adm["idusertg"] else ""
                text += f"• <code>{adm['idusertg']}</code>{owner_mark}\n"
        else:
            text += "<i>Администраторов нет</i>"
        await message.answer(text, parse_mode="HTML")


# ─────────────────────────────────────────────────────────────────────────────
# Fallback
# ─────────────────────────────────────────────────────────────────────────────

@router.message()
async def fallback(message: Message) -> None:
    from keyboards import kb_client_main
    await message.answer(
        "❓ Неизвестная команда.\n\nНажмите /start для начала.",
        reply_markup=kb_client_main(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

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
