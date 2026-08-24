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
from database import ForeignClientUid, SlotTaken, db
from notifications import notify_staff, safe_send
from handlers.common import require_active_service
from validators import (
    ValidationError, h, validate_catalog_ids, validate_request_fields,
    validate_scheduled_at, validate_uuid,
)

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
    try:
        service_id = validate_uuid(payload.get("service_id"), field="Сервис")
    except ValidationError as exc:
        raise RequestRejected(str(exc)) from exc

    service = await db.get_service(service_id)
    if not service:
        raise RequestRejected("Сервис не найден или больше не принимает заявки.")

    try:
        fields = validate_request_fields(payload)
        idcatalogs = validate_catalog_ids(payload.get("idcatalogs"))
    except ValidationError as exc:
        raise RequestRejected(str(exc)) from exc

    # Услуги обязаны принадлежать этому сервису и быть активными: клиент мог
    # держать форму открытой, пока управляющий убирал что-то из списка
    items = await db.get_catalog_items(service_id, idcatalogs)
    if len(items) != len(idcatalogs):
        found = {str(item["idcatalog"]) for item in items}
        missing = [cid for cid in idcatalogs if cid not in found]
        # Называем услугу по имени: «одна из услуг недоступна» не подсказывает,
        # что именно переснимать в форме
        gone = await db.get_catalog_items(service_id, missing, only_active=False)
        names = ", ".join(f"«{row['title']}»" for row in gone)
        raise RequestRejected(
            f"Услуга {names} больше не оказывается — выберите заново."
            if names
            else "Одна из выбранных услуг недоступна. Откройте форму заново."
        )

    by_id = {str(item["idcatalog"]): item for item in items}
    services = [
        {
            "idcatalog": cid,
            "title": by_id[cid]["title"],
            "price_rub": by_id[cid]["price_rub"],
        }
        for cid in idcatalogs
    ]

    # Время записи обязательно. Проверяется не формат, а принадлежность реально
    # свободному окну: payload можно отправить в обход формы, поэтому список
    # окон — единственный источник истины, а не то, что заявлено в теле.
    # Считается последним из проверок: это два запроса к базе, и платить за них
    # стоит только когда всё остальное в заявке уже сошлось.
    free = await db.free_slots(service)
    if not free:
        raise RequestRejected("Сейчас нет свободного времени для записи.")
    try:
        scheduled_at = validate_scheduled_at(
            payload.get("scheduled_at"), free=free, tz=service["timezone"]
        )
    except ValidationError as exc:
        raise RequestRejected(str(exc)) from None

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

    try:
        request, is_duplicate = await db.create_request(
            idservice=service_id,
            client_tg_id=client_tg_id,
            client_uid=client_uid,
            services=services,
            scheduled_at=scheduled_at,
            **fields,
        )
    except ForeignClientUid:
        logger.warning(
            "Клиент %s прислал client_uid чужой заявки", client_tg_id
        )
        raise RequestRejected(
            "Не удалось распознать отправку. Закройте форму, откройте заново и повторите."
        ) from None
    except SlotTaken:
        raise RequestRejected(
            "Это время только что заняли. Выберите другое — список обновится."
        ) from None

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
    positions = await db.get_request_services(summary["request_id"])
    await notify_staff(
        bot,
        service_id,
        render.request_card_for_staff(request, positions, tz=service["timezone"]),
        reply_markup=kb.kb_request_actions(summary["request_id"], request["status"]),
        master_alert=(
            f"⚠️ <b>Заявка {summary['number']} никому не доставлена</b>\n"
            f"Сервис: {h(service['service_name'])}\n\n"
            "<i>Сотрудники недоступны. Данные клиента не пересылаем — "
            "заявка ждёт в базе.</i>"
        ),
    )

    # Подтверждение клиенту
    services_line = "\n".join(
        f"• {render.titled_price(h(item['title']), item['price_rub'])}"
        for item in services
    )
    await safe_send(
        bot,
        client_tg_id,
        f"✅ <b>Заявка {summary['number']} отправлена!</b>\n\n"
        f"<b>Сервис:</b> {h(service['service_name'])}\n"
        f"<b>Услуги:</b>\n{services_line}\n"
        f"<b>Автомобиль:</b> {h(fields['brand'])} {h(fields['model'])}\n"
        # Время клиент выбирал сам, но нигде его после отправки не видел:
        # подтверждение записи без времени записи подтверждает половину
        f"<b>Запись:</b> {render.local_dt(scheduled_at, service['timezone'])}\n\n"
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

    # Старая форма присылала ключ service_type из общего справочника. Услуги
    # теперь свои у каждого сервиса, сопоставить их со старыми ключами нельзя.
    if not payload.get("idcatalogs"):
        await message.answer(
            "❌ Форма записи устарела — закройте её и откройте заново."
        )
        return

    try:
        await create_request_flow(
            message.bot, client_tg_id=message.from_user.id, payload=payload
        )
    except RequestRejected as exc:
        await message.answer(f"❌ {h(exc)}")
    except Exception:
        logger.exception("Ошибка при создании заявки из web_app_data")
        await message.answer("❌ Не удалось сохранить заявку. Попробуйте позже.")


# ── Открытие формы записи ────────────────────────────────────────────────────
# Форма открывается inline-кнопкой, а не кнопкой reply-клавиатуры: мини-
# приложения, открытые с reply-клавиатуры, не получают от Telegram подписанный
# initData, а без него сервер заявку не примет (см. keyboards.kb_open_webapp).

@router.message(F.text.in_({kb.BTN_BOOK, kb.BTN_BOOK_OWN}), StateFilter(default_state))
async def open_booking_form(message: Message, state: FSMContext) -> None:
    service_id = None
    if message.text == kb.BTN_BOOK_OWN:
        svc = await require_active_service(message, state)
        if svc is None:
            return
        service_id = str(svc["idservice"])

    markup = kb.kb_open_webapp(service_id)
    if markup is None:
        await message.answer(
            "⚠️ Онлайн-запись временно недоступна. Попробуйте позже."
        )
        return

    await message.answer("Нажмите кнопку ниже — откроется форма записи 👇", reply_markup=markup)


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
    # callback_data приходит от клиента: мусорный id иначе роняет asyncpg
    try:
        request_id = validate_uuid(callback.data.split(":", 1)[1], field="Заявка")
    except (IndexError, ValidationError):
        await callback.answer("❌ Заявка не найдена.", show_alert=True)
        return

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
