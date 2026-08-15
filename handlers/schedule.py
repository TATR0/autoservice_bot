"""
handlers/schedule.py — расписание сервиса.

Часы, обед, вместимость и горизонт правятся текстовым вводом, как цена услуги.
Шаг и рабочие дни — inline-кнопками: набор вариантов конечен, печатать и потом
разбирать опечатки незачем.
"""

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.types import CallbackQuery, Message

import keyboards as kb
import render
import slots
from database import db
from handlers.common import require_owner_service, show_main_menu
from validators import (
    ValidationError,
    validate_capacity,
    validate_horizon,
    validate_lunch,
    validate_time_range,
)

router = Router()


class ScheduleEdit(StatesGroup):
    hours = State()
    lunch = State()
    capacity = State()
    horizon = State()


async def _free_count(idservice: str, schedule, tz: str) -> int:
    """Сколько окон увидит клиент прямо сейчас."""
    now = datetime.now(timezone.utc)
    taken = await db.get_taken_slots(
        idservice, now, now + timedelta(days=schedule["horizon_days"] + 1)
    )
    free = slots.free_slots(schedule, tz, now, taken)
    return sum(len(times) for times in free.values())


async def _show_schedule(message: Message, svc, *, edit: bool = False) -> None:
    idservice = str(svc["idservice"])
    schedule = await db.get_schedule(idservice)
    text = render.schedule_card(
        svc, schedule, await _free_count(idservice, schedule, svc["timezone"])
    )
    if edit:
        await message.edit_text(text, reply_markup=kb.kb_schedule())
    else:
        await message.answer(text, reply_markup=kb.kb_schedule())


@router.message(F.text == kb.BTN_SCHEDULE, StateFilter(default_state))
async def schedule_open(message: Message, state: FSMContext) -> None:
    svc = await require_owner_service(message, state)
    if svc is None:
        return
    await _show_schedule(message, svc)


@router.message(ScheduleEdit.hours, F.text == kb.BTN_CANCEL)
@router.message(ScheduleEdit.lunch, F.text == kb.BTN_CANCEL)
@router.message(ScheduleEdit.capacity, F.text == kb.BTN_CANCEL)
@router.message(ScheduleEdit.horizon, F.text == kb.BTN_CANCEL)
async def edit_cancel(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await show_main_menu(message, state, greeting="Отменено.")


# ── Часы работы ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "schedhours")
async def hours_ask(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        return
    await state.set_state(ScheduleEdit.hours)
    await callback.message.answer(
        "Введите часы работы: <b>9-18</b> или <b>09:00-18:00</b>.",
        reply_markup=kb.kb_cancel(),
    )
    await callback.answer()


@router.message(ScheduleEdit.hours)
async def hours_finish(message: Message, state: FSMContext) -> None:
    svc = await require_owner_service(message, state)
    if svc is None:
        await state.set_state(None)
        await show_main_menu(message, state)
        return
    try:
        work_from, work_to = validate_time_range(message.text, field="Часы")
    except ValidationError as exc:
        await message.answer(f"❌ {exc}")
        return

    await db.update_schedule(str(svc["idservice"]), work_from=work_from, work_to=work_to)
    await state.set_state(None)
    await show_main_menu(message, state, greeting="✅ Часы работы обновлены.")
    await _show_schedule(message, svc)


# ── Обед ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "schedlunch")
async def lunch_ask(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        return
    await state.set_state(ScheduleEdit.lunch)
    await callback.message.answer(
        "Введите обед: 13-14, или <b>-</b>, чтобы убрать",
        reply_markup=kb.kb_cancel(),
    )
    await callback.answer()


@router.message(ScheduleEdit.lunch)
async def lunch_finish(message: Message, state: FSMContext) -> None:
    svc = await require_owner_service(message, state)
    if svc is None:
        await state.set_state(None)
        await show_main_menu(message, state)
        return
    try:
        lunch = validate_lunch(message.text)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}")
        return

    await db.update_schedule(
        str(svc["idservice"]),
        lunch_from=lunch[0] if lunch else None,
        lunch_to=lunch[1] if lunch else None,
    )
    await state.set_state(None)
    await show_main_menu(message, state, greeting="✅ Обед обновлён.")
    await _show_schedule(message, svc)


# ── Вместимость ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "schedcap")
async def capacity_ask(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        return
    await state.set_state(ScheduleEdit.capacity)
    await callback.message.answer(
        "Сколько машин принимаете в одно время? Число от 1 до 20",
        reply_markup=kb.kb_cancel(),
    )
    await callback.answer()


@router.message(ScheduleEdit.capacity)
async def capacity_finish(message: Message, state: FSMContext) -> None:
    svc = await require_owner_service(message, state)
    if svc is None:
        await state.set_state(None)
        await show_main_menu(message, state)
        return
    try:
        capacity = validate_capacity(message.text)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}")
        return

    await db.update_schedule(str(svc["idservice"]), capacity=capacity)
    await state.set_state(None)
    await show_main_menu(message, state, greeting="✅ Вместимость обновлена.")
    await _show_schedule(message, svc)


# ── Горизонт записи ──────────────────────────────────────────────────────────

@router.callback_query(F.data == "schedhorizon")
async def horizon_ask(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        return
    await state.set_state(ScheduleEdit.horizon)
    await callback.message.answer(
        "На сколько дней вперёд открыта запись? Число от 1 до 60",
        reply_markup=kb.kb_cancel(),
    )
    await callback.answer()


@router.message(ScheduleEdit.horizon)
async def horizon_finish(message: Message, state: FSMContext) -> None:
    svc = await require_owner_service(message, state)
    if svc is None:
        await state.set_state(None)
        await show_main_menu(message, state)
        return
    try:
        horizon_days = validate_horizon(message.text)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}")
        return

    await db.update_schedule(str(svc["idservice"]), horizon_days=horizon_days)
    await state.set_state(None)
    await show_main_menu(message, state, greeting="✅ Горизонт записи обновлён.")
    await _show_schedule(message, svc)


# ── Шаг записи ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == "schedstep")
async def step_ask(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        return
    await callback.message.edit_reply_markup(reply_markup=kb.kb_schedule_step())
    await callback.answer()


@router.callback_query(F.data.startswith("schedstep:"))
async def step_set(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        return
    minutes = int(callback.data.split(":", 1)[1])
    await db.update_schedule(str(svc["idservice"]), slot_minutes=minutes)
    await _show_schedule(callback.message, svc, edit=True)
    await callback.answer(f"Шаг записи: {minutes} минут")


@router.callback_query(F.data == "schedback")
async def step_back(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        return
    await _show_schedule(callback.message, svc, edit=True)
    await callback.answer()


# ── Рабочие дни ──────────────────────────────────────────────────────────────
# Выбор копится в FSM, а не в базе: иначе каждый тап писал бы в неё, и снятый
# последний день на мгновение нарушал бы констрейнт непустого набора.

@router.callback_query(F.data == "scheddays")
async def days_ask(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        return
    schedule = await db.get_schedule(str(svc["idservice"]))
    chosen = list(schedule["weekdays"])
    await state.update_data(weekdays=chosen)
    await callback.message.edit_reply_markup(reply_markup=kb.kb_schedule_days(chosen))
    await callback.answer()


@router.callback_query(F.data.startswith("scheddaytoggle:"))
async def days_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    day = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    chosen = set(data.get("weekdays", []))
    chosen.symmetric_difference_update({day})
    await state.update_data(weekdays=sorted(chosen))
    await callback.message.edit_reply_markup(
        reply_markup=kb.kb_schedule_days(sorted(chosen))
    )
    await callback.answer()


@router.callback_query(F.data == "scheddaysdone")
async def days_save(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        return
    data = await state.get_data()
    chosen = sorted(data.get("weekdays", []))
    if not chosen:
        # Констрейнт это тоже поймает, но управляющему нужен текст, а не
        # ошибка драйвера
        await callback.answer("Оставьте хотя бы один рабочий день", show_alert=True)
        return

    await db.update_schedule(str(svc["idservice"]), weekdays=chosen)
    await state.update_data(weekdays=None)
    await _show_schedule(callback.message, svc, edit=True)
    await callback.answer("Рабочие дни сохранены")
