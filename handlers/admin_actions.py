"""
handlers/admin_actions.py — работа сотрудников с заявками.

Смена статуса идёт условным UPDATE: если два администратора одновременно
нажали «Принять», второй получит «заявку уже взял другой сотрудник»,
а история статусов не задвоится.
"""

import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import default_state
from aiogram.types import CallbackQuery, Message

import config
import keyboards as kb
import render
from database import db
from handlers.common import require_active_service, show_main_menu
from notifications import safe_send
from validators import ValidationError, h, validate_uuid

logger = logging.getLogger(__name__)
router = Router()


# ── Смена статуса заявки ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("req:"))
async def request_status_cb(callback: CallbackQuery) -> None:
    # callback_data приходит от клиента и может быть подделан: без проверки
    # формата мусорный id уходит в asyncpg и роняет хендлер DataError'ом
    try:
        _, status, raw_id = callback.data.split(":", 2)
        request_id = validate_uuid(raw_id, field="Заявка")
    except (ValueError, ValidationError):
        await callback.answer("❌ Некорректная кнопка.", show_alert=True)
        return

    if status not in config.STATUS_TRANSITIONS:
        await callback.answer("❌ Неизвестный статус.", show_alert=True)
        return

    req = await db.get_request(request_id)
    if not req:
        await callback.answer("❌ Заявка не найдена.", show_alert=True)
        return
    if not req["idservice"]:
        await callback.answer("❌ Сервис заявки удалён.", show_alert=True)
        return
    if not await db.has_access(str(req["idservice"]), callback.from_user.id):
        await callback.answer("❌ У вас нет прав изменять эту заявку.", show_alert=True)
        return

    updated = await db.update_request_status(
        request_id,
        status,
        changed_by=callback.from_user.id,
        allowed_from=config.STATUS_TRANSITIONS[status],
    )
    if updated is None:
        current = render.status_label(req["status"])
        await callback.answer(
            f"Заявку уже обработал другой сотрудник.\nТекущий статус: {current}",
            show_alert=True,
        )
        # Подтягиваем актуальные кнопки
        try:
            await callback.message.edit_reply_markup(
                reply_markup=kb.kb_request_actions(request_id, req["status"])
            )
        except Exception:
            pass
        return

    label = render.status_label(status)
    handler_user = await db.get_user(callback.from_user.id)
    svc = await db.get_service(str(updated["idservice"]))

    positions = await db.get_request_services(request_id)
    card = render.request_card_for_staff(
        updated, positions, tz=svc["timezone"] if svc else None
    )
    card += f"\n👤 <b>Обработал:</b> {h(db.user_title(handler_user, callback.from_user.id))}"

    try:
        await callback.message.edit_text(
            card, reply_markup=kb.kb_request_actions(request_id, status)
        )
    except Exception:
        logger.debug("Не удалось обновить карточку заявки", exc_info=True)
    await callback.answer(f"✅ Статус: {label}")

    # Уведомляем клиента
    notify = config.CLIENT_NOTIFICATIONS.get(status)
    if notify and updated["idclienttg"]:
        svc_name = svc["service_name"] if svc else "Автосервис"
        markup = (
            kb.kb_client_request_actions(request_id)
            if status in ("accepted", "called")
            else None
        )
        await safe_send(
            callback.bot,
            updated["idclienttg"],
            f"{notify}\n\n"
            f"<b>Сервис:</b> {h(svc_name)}\n"
            f"<b>Заявка:</b> {render.request_number(updated['seq'])}",
            reply_markup=markup,
        )


# ── 📋 Заявки сервиса ────────────────────────────────────────────────────────

@router.message(F.text == kb.BTN_SERVICE_REQS, StateFilter(default_state))
async def service_requests(message: Message, state: FSMContext) -> None:
    svc = await require_active_service(message, state)
    if svc is None:
        return

    reqs = await db.get_service_requests(str(svc["idservice"]), limit=20)
    if not reqs:
        await message.answer(
            f"<b>📋 {h(svc['service_name'])}</b>\n\n<i>Заявок пока нет.</i>"
        )
        return

    text = f"<b>📋 {h(svc['service_name'])} — последние {len(reqs)} заявок</b>\n"
    text += "".join(render.request_line_for_staff(r, svc["timezone"]) for r in reqs)
    for chunk in render.split_text(text):
        await message.answer(chunk)

    # Новые заявки отправляем отдельными карточками с кнопками действий
    for req in [r for r in reqs if r["status"] == "new"][:5]:
        positions = await db.get_request_services(str(req["idrequests"]))
        await message.answer(
            render.request_card_for_staff(
                req, positions, tz=svc["timezone"], title="🆕 <b>ТРЕБУЕТ РЕАКЦИИ</b>"
            ),
            reply_markup=kb.kb_request_actions(str(req["idrequests"]), req["status"]),
        )


# ── 📊 Статистика ────────────────────────────────────────────────────────────

@router.message(F.text == kb.BTN_STATS, StateFilter(default_state))
async def service_stats(message: Message, state: FSMContext) -> None:
    svc = await require_active_service(message, state)
    if svc is None:
        return

    idservice = str(svc["idservice"])
    stats = await db.get_service_stats(idservice)
    if not stats:
        await message.answer("❌ Не удалось собрать статистику.")
        return

    breakdown = await db.get_service_breakdown(idservice)
    avg_reaction = await db.get_avg_reaction_seconds(idservice)

    await message.answer(render.stats_card(svc, stats, breakdown, avg_reaction))


# ── ℹ️ О сервисе ─────────────────────────────────────────────────────────────

@router.message(F.text == kb.BTN_ABOUT, StateFilter(default_state))
async def about_service(message: Message, state: FSMContext) -> None:
    svc = await require_active_service(message, state)
    if svc is None:
        return

    admins = await db.get_active_admins(str(svc["idservice"]))
    admins_text = (
        ", ".join(h(db.user_title(a, a["idusertg"])) for a in admins) or "<i>нет</i>"
    )
    await message.answer(
        render.service_card(
            svc,
            link=db.service_link(str(svc["idservice"])),
            role=svc["role"],
            admins_text=admins_text,
        )
    )


# ── Fallback ─────────────────────────────────────────────────────────────────
# Регистрируется последним роутером: сюда попадает всё, что не разобрали выше.

@router.message(StateFilter(default_state))
async def fallback(message: Message, state: FSMContext) -> None:
    await show_main_menu(
        message,
        state,
        greeting="❓ Не понял команду. Воспользуйтесь кнопками меню 👇",
    )
