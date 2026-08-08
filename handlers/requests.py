"""
handlers/requests.py

• «📋 Мои заявки» — список заявок клиента
• Отмена заявки клиентом
• create_request_flow() — общая логика создания заявки, вызывается из app.py
  (POST /api/requests) и из legacy-обработчика web_app_data

Заявки принимаются по HTTP, а не через tg.sendData(): sendData работает
только для WebApp, открытых кнопкой reply-клавиатуры, и молча теряет данные
во всех остальных случаях.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import default_state
from aiogram.types import CallbackQuery, Message

import config
import keyboards as kb
import render
from database import db
from notifications import notify_staff, safe_send
from validators import ValidationError, h, validate_request_fields

logger = logging.getLogger(__name__)
router = Router()


class RequestRejected(Exception):
    """Заявку принять нельзя — текст предназначен клиенту."""


async def create_request_flow(
    bot: Bot,
    *,
    client_tg_id: int,
    payload: dict,
) -> tuple[dict, bool]:
    """
    Провалидировать, сохранить заявку и разослать уведомления.

    Возвращает (краткое описание заявки, is_duplicate).
    Бросает RequestRejected с понятным клиенту текстом.
    """
    service_id = str(payload.get("service_id") or "").strip()
    if not service_id:
        raise RequestRejected("Не выбран автосервис.")

    service = await db.get_service(service_id)
    if not service:
        raise RequestRejected("Сервис не найден или больше не принимает заявки.")

    try:
        fields = validate_request_fields(payload)
    except ValidationError as exc:
        raise RequestRejected(str(exc)) from exc

    client_uid = str(payload.get("client_uid") or "").strip()[:64] or None

    # ── Антиспам ─────────────────────────────────────────────────────────────
    # client_uid ловит двойной тап, поэтому кулдаун проверяем только для
    # действительно новых отправок.
    if client_uid is None:
        elapsed = await db.seconds_since_last_request(client_tg_id)
        if elapsed is not None and elapsed < config.REQUEST_COOLDOWN_SECONDS:
            wait = int(config.REQUEST_COOLDOWN_SECONDS - elapsed) + 1
            raise RequestRejected(f"Слишком часто. Повторите через {wait} сек.")

    active = await db.count_active_client_requests(client_tg_id, service_id)
    if active >= config.MAX_ACTIVE_REQUESTS_PER_CLIENT:
        raise RequestRejected(
            f"У вас уже {active} активных заявки(ок) в этом сервисе. "
            "Дождитесь их обработки."
        )

    request, is_duplicate = await db.create_request(
        idservice=service_id,
        client_tg_id=client_tg_id,
        client_uid=client_uid,
        **fields,
    )

    summary = {
        "request_id": str(request["idrequests"]),
        "number": render.request_number(request["seq"]),
        "service_name": service["service_name"],
        "status": request["status"],
    }

    if is_duplicate:
        logger.info("Повторная отправка заявки %s, дубль не создан", summary["number"])
        return summary, True

    await db.set_user_phone(client_tg_id, fields["phone"])

    # Карточка администраторам и владельцу
    await notify_staff(
        bot,
        service_id,
        render.request_card_for_staff(request, tz=service["timezone"]),
        reply_markup=kb.kb_request_actions(summary["request_id"], request["status"]),
    )

    # Подтверждение клиенту
    await safe_send(
        bot,
        client_tg_id,
        f"✅ <b>Заявка {summary['number']} отправлена!</b>\n\n"
        f"<b>Сервис:</b> {h(service['service_name'])}\n"
        f"<b>Услуга:</b> {h(render.service_type_label(fields['service_type']))}\n"
        f"<b>Автомобиль:</b> {h(fields['brand'])} {h(fields['model'])}\n\n"
        "Администратор свяжется с вами в ближайшее время.",
        reply_markup=kb.kb_client_request_actions(summary["request_id"]),
    )
    return summary, False


# ── Legacy: WebApp, открытый кнопкой reply-клавиатуры ────────────────────────
# Основной путь — POST /api/requests. Этот обработчик остаётся страховкой,
# если у клиента закешировалась старая версия формы.

@router.message(F.web_app_data)
async def handle_webapp_data(message: Message) -> None:
    import json

    try:
        payload = json.loads(message.web_app_data.data)
    except json.JSONDecodeError:
        await message.answer("❌ Получены некорректные данные формы.")
        return

    # Старая форма присылала ключ "service" вместо "service_type"
    payload.setdefault("service_type", payload.get("service"))

    try:
        await create_request_flow(
            message.bot, client_tg_id=message.from_user.id, payload=payload
        )
    except RequestRejected as exc:
        await message.answer(f"❌ {h(exc)}")
    except Exception:
        logger.exception("Ошибка при создании заявки из web_app_data")
        await message.answer("❌ Не удалось сохранить заявку. Попробуйте позже.")


# ── «Мои заявки» ─────────────────────────────────────────────────────────────

@router.message(F.text == kb.BTN_MY_REQUESTS, StateFilter(default_state))
async def my_requests(message: Message) -> None:
    reqs = await db.get_client_requests(message.from_user.id, limit=10)
    if not reqs:
        await message.answer(
            "У вас ещё нет заявок.\n\nЗапишитесь в автосервис через кнопку ниже 👇",
            reply_markup=kb.kb_client_main(),
        )
        return

    text = "<b>📋 Ваши заявки:</b>\n\n" + "".join(
        render.request_line_for_client(r) for r in reqs
    )
    for chunk in render.split_text(text):
        await message.answer(chunk)


# ── Отмена заявки клиентом ───────────────────────────────────────────────────

@router.callback_query(F.data.startswith("cancelreq:"))
async def cancel_request(callback: CallbackQuery) -> None:
    request_id = callback.data.split(":", 1)[1]

    req = await db.get_request(request_id)
    if not req:
        await callback.answer("❌ Заявка не найдена.", show_alert=True)
        return
    if req["idclienttg"] != callback.from_user.id:
        await callback.answer("❌ Это не ваша заявка.", show_alert=True)
        return

    updated = await db.update_request_status(
        request_id,
        "cancelled",
        changed_by=callback.from_user.id,
        allowed_from=config.STATUS_TRANSITIONS["cancelled"],
        note="отменена клиентом",
    )
    if updated is None:
        await callback.answer(
            "Заявку уже нельзя отменить — она обработана сервисом.", show_alert=True
        )
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"🚫 Заявка {render.request_number(updated['seq'])} отменена."
    )
    await callback.answer("Заявка отменена.")

    if updated["idservice"]:
        user = await db.get_user(callback.from_user.id)
        await notify_staff(
            callback.bot,
            str(updated["idservice"]),
            f"🚫 <b>Клиент отменил заявку {render.request_number(updated['seq'])}</b>\n"
            f"👤 {h(db.user_title(user, callback.from_user.id))}\n"
            f"🚗 {h(updated['brand'])} {h(updated['model'])} ({h(updated['plate'])})",
        )
