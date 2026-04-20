"""
handlers/requests.py

• Обработка данных из Telegram WebApp (webapp_data) → создание заявки
• «📋 Мои заявки» для клиента
"""

import json
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.types import Message

from config import (
    CLIENT_NOTIFICATIONS,
    MASTER_CHAT_ID,
    REQUEST_STATUS_LABELS,
    SERVICE_TYPES,
    URGENCY_LABELS,
)
from database import db
from keyboards import kb_client_main, kb_request_actions

logger = logging.getLogger(__name__)
router = Router()


# ─────────────────────────────────────────────────────────────────────────────
# WebApp → новая заявка
# ─────────────────────────────────────────────────────────────────────────────

@router.message(F.web_app_data)
async def handle_webapp_data(message: Message) -> None:
    try:
        data: dict = json.loads(message.web_app_data.data)
    except json.JSONDecodeError:
        await message.answer("❌ Ошибка: получены некорректные данные.")
        return

    service_id   = data.get("service_id", "")
    client_name  = data.get("client_name") or "Не указано"
    phone        = data.get("phone") or "—"
    brand        = data.get("brand") or "—"
    model        = data.get("model") or "—"
    plate        = data.get("plate") or "—"
    service_key  = data.get("service") or "other"
    urgency_key  = data.get("urgency") or "low"
    comment      = data.get("comment") or ""

    service_label = SERVICE_TYPES.get(service_key, service_key)
    urgency_label = URGENCY_LABELS.get(urgency_key, urgency_key)

    # Сохраняем в БД
    try:
        request_id = await db.create_request(
            idservice=service_id,
            client_tg_id=message.from_user.id,
            client_name=client_name,
            phone=phone,
            brand=brand,
            model=model,
            plate=plate,
            service_type=service_key,
            urgency=urgency_key,
            comment=comment,
        )
    except Exception as exc:
        logger.exception("Ошибка при сохранении заявки")
        await message.answer(f"❌ Не удалось сохранить заявку:\n<code>{exc}</code>", parse_mode="HTML")
        return

    # Формируем сообщение для администратора
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    admin_msg = (
        "🚗 <b>НОВАЯ ЗАЯВКА</b>\n"
        "─────────────────────\n"
        f"👤 <b>Клиент:</b> {client_name}\n"
        f"📞 <b>Телефон:</b> <code>{phone}</code>\n"
        f"💬 <b>Telegram ID:</b> <code>{message.from_user.id}</code>\n\n"
        f"🚙 <b>Автомобиль:</b> {brand} {model}\n"
        f"🔢 <b>Гос. номер:</b> <code>{plate}</code>\n\n"
        f"🔧 <b>Услуга:</b> {service_label}\n"
        f"⚡ <b>Срочность:</b> {urgency_label}\n"
    )
    if comment:
        admin_msg += f"\n💬 <b>Комментарий:</b>\n<i>{comment}</i>\n"
    admin_msg += f"\n⏰ {ts}\n🆔 <code>{request_id}</code>"

    # Отправляем всем администраторам сервиса
    delivered = 0
    if service_id:
        admins = await db.get_active_admins(service_id)
        for adm in admins:
            try:
                await message.bot.send_message(
                    adm["idusertg"],
                    admin_msg,
                    parse_mode="HTML",
                    reply_markup=kb_request_actions(request_id),
                )
                delivered += 1
            except Exception:
                logger.warning("Не удалось отправить заявку админу %s", adm["idusertg"])

    # Если никому не доставили — в мастер-чат
    if delivered == 0 and MASTER_CHAT_ID:
        try:
            await message.bot.send_message(
                MASTER_CHAT_ID,
                f"⚠️ <b>Заявка без администраторов</b>\n\n{admin_msg}",
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Не удалось отправить в мастер-чат")

    # Подтверждение клиенту
    await message.answer(
        "✅ <b>Заявка отправлена!</b>\n\n"
        "Администратор свяжется с вами в ближайшее время.\n\n"
        f"🆔 Номер заявки: <code>{request_id}</code>",
        parse_mode="HTML",
        reply_markup=kb_client_main(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# «Мои заявки» — для клиента
# ─────────────────────────────────────────────────────────────────────────────

@router.message(F.text == "📋 Мои заявки")
async def my_requests(message: Message) -> None:
    # Сначала проверим: может быть, это администратор
    admin_services = await db.get_admin_services(message.from_user.id)
    if admin_services:
        # Показываем заявки по первому сервису (или по всем)
        text = "<b>📋 Заявки по вашим сервисам:</b>\n\n"
        for svc in admin_services:
            reqs = await db.get_service_requests(svc["idservice"], limit=10)
            text += f"<b>— {svc['service_name']} —</b>\n"
            if reqs:
                for r in reqs:
                    label = REQUEST_STATUS_LABELS.get(r["status"], r["status"])
                    text += f"  • {r['client_name']} | {label}\n"
            else:
                text += "  <i>Заявок нет</i>\n"
            text += "\n"
        await message.answer(text, parse_mode="HTML")
        return

    # Клиентские заявки
    reqs = await db.get_client_requests(message.from_user.id)
    if not reqs:
        await message.answer(
            "У вас ещё нет заявок.\n\nЗапишитесь в автосервис через кнопку ниже 👇",
            reply_markup=kb_client_main(),
        )
        return

    text = "<b>📋 Ваши заявки:</b>\n\n"
    for r in reqs:
        label  = REQUEST_STATUS_LABELS.get(r["status"], r["status"])
        sname  = r.get("service_name") or "—"
        date   = r["createdate"].strftime("%d.%m.%Y") if r["createdate"] else "—"
        text += (
            f"🚗 <b>{r['brand']} {r['model']}</b>\n"
            f"   Сервис: {sname}\n"
            f"   Статус: {label}\n"
            f"   Дата: {date}\n"
            f"   🆔 <code>{r['idrequests']}</code>\n\n"
        )
    await message.answer(text, parse_mode="HTML")
