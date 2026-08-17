"""
handlers/schedule.py — расписание сервиса.

Часы, обед, вместимость и горизонт правятся текстовым вводом, как цена услуги.
Шаг и рабочие дни — inline-кнопками: набор вариантов конечен, печатать и потом
разбирать опечатки незачем.
"""

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.types import CallbackQuery, Message
from asyncpg.exceptions import CheckViolationError

import keyboards as kb
import render
from database import db
from handlers.common import require_owner_service, show_main_menu
from validators import (
    ValidationError,
    lunch_fits_hours,
    snap_lunch_to_grid,
    validate_capacity,
    validate_horizon,
    validate_lunch,
    validate_lunch_on_grid,
    validate_time_range,
)

router = Router()

# Часы, обед и шаг связаны: обед лежит внутри часов и занимает целое число окон.
# Правится каждое поле по отдельности, поэтому согласованность держит не одна
# проверка на вводе, а каждый редактор — и снизу их страхует констрейнт базы.
SAVE_FAILED = (
    "❌ Не получилось сохранить: часы, обед и шаг противоречат друг другу. "
    "Проверьте обед — он должен лежать внутри рабочих часов."
)


class ScheduleEdit(StatesGroup):
    hours = State()
    lunch = State()
    capacity = State()
    horizon = State()


async def _free_count(svc) -> int:
    """Сколько окон увидит клиент прямо сейчас — считаем ровно тем же расчётом."""
    free = await db.free_slots(svc)
    return sum(len(times) for times in free.values())


async def _save(idservice: str, **fields) -> bool:
    """
    Записать поля расписания. False — база отвергла набор.

    Констрейнты задуманы последней линией обороны, но говорят они языком
    драйвера: без этого перехвата управляющий увидел бы текст asyncpg про
    chk_schedule_lunch. Сюда попадать не должны — каждый редактор проверяет
    связку сам, — но если проверка и база разойдутся, разойдутся они молча.
    """
    try:
        await db.update_schedule(idservice, **fields)
    except CheckViolationError:
        return False
    return True


async def _show_schedule(message: Message, svc, *, edit: bool = False) -> None:
    idservice = str(svc["idservice"])
    schedule = await db.get_schedule(idservice)
    text = render.schedule_card(svc, schedule, await _free_count(svc))
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
        # Без ответа спиннер на кнопке крутится до таймаута Telegram, и отказ
        # выглядит зависанием, а не отказом
        await callback.answer()
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

    idservice = str(svc["idservice"])
    schedule = await db.get_schedule(idservice)
    fields = {"work_from": work_from, "work_to": work_to}
    note = ""
    lunch = (
        (schedule["lunch_from"], schedule["lunch_to"]) if schedule["lunch_from"] else None
    )
    if lunch:
        # Новые часы могут не вместить обед. Убрать его молча нельзя: управляющий
        # просил сменить часы, а не отменить обед, и пропажу он заметит нескоро
        if not lunch_fits_hours(lunch, work_from=work_from, work_to=work_to):
            await message.answer(
                f"❌ В такие часы не помещается обед {lunch[0]:%H:%M}-{lunch[1]:%H:%M}. "
                "Сначала измените или уберите обед."
            )
            return
        # Сетка окон отсчитывается от начала дня, так что сдвиг часов сдвигает и её
        aligned = snap_lunch_to_grid(
            lunch,
            work_from=work_from,
            work_to=work_to,
            slot_minutes=schedule["slot_minutes"],
        )
        if aligned != lunch:
            fields["lunch_from"], fields["lunch_to"] = aligned
            note = f" Обед сдвинут на {aligned[0]:%H:%M}-{aligned[1]:%H:%M}."

    if not await _save(idservice, **fields):
        await message.answer(SAVE_FAILED)
        return

    await state.set_state(None)
    await show_main_menu(message, state, greeting=f"✅ Часы работы обновлены.{note}")
    await _show_schedule(message, svc)


# ── Обед ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "schedlunch")
async def lunch_ask(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        # Без ответа спиннер на кнопке крутится до таймаута Telegram, и отказ
        # выглядит зависанием, а не отказом
        await callback.answer()
        return
    schedule = await db.get_schedule(str(svc["idservice"]))
    await state.set_state(ScheduleEdit.lunch)
    await callback.message.answer(
        "Введите обед: <b>13-14</b>, или <b>-</b>, чтобы убрать.\n"
        f"Обед занимает целое число окон по {schedule['slot_minutes']} мин.",
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
    schedule = await db.get_schedule(str(svc["idservice"]))
    try:
        lunch = validate_lunch(message.text)
        # Обед меряется окнами, а не минутами: 45 минут при часовом шаге всё
        # равно убрали бы целый час, только незаметно для управляющего
        lunch = validate_lunch_on_grid(
            lunch,
            work_from=schedule["work_from"],
            work_to=schedule["work_to"],
            slot_minutes=schedule["slot_minutes"],
        )
    except ValidationError as exc:
        await message.answer(f"❌ {exc}")
        return

    saved = await _save(
        str(svc["idservice"]),
        lunch_from=lunch[0] if lunch else None,
        lunch_to=lunch[1] if lunch else None,
    )
    if not saved:
        await message.answer(SAVE_FAILED)
        return

    await state.set_state(None)
    await show_main_menu(message, state, greeting="✅ Обед обновлён.")
    await _show_schedule(message, svc)


# ── Вместимость ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "schedcap")
async def capacity_ask(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        # Без ответа спиннер на кнопке крутится до таймаута Telegram, и отказ
        # выглядит зависанием, а не отказом
        await callback.answer()
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
        # Без ответа спиннер на кнопке крутится до таймаута Telegram, и отказ
        # выглядит зависанием, а не отказом
        await callback.answer()
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
        # Без ответа спиннер на кнопке крутится до таймаута Telegram, и отказ
        # выглядит зависанием, а не отказом
        await callback.answer()
        return
    await callback.message.edit_reply_markup(reply_markup=kb.kb_schedule_step())
    await callback.answer()


@router.callback_query(F.data.startswith("schedstep:"))
async def step_set(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        # Без ответа спиннер на кнопке крутится до таймаута Telegram, и отказ
        # выглядит зависанием, а не отказом
        await callback.answer()
        return
    minutes = int(callback.data.split(":", 1)[1])
    idservice = str(svc["idservice"])
    schedule = await db.get_schedule(idservice)

    # Новый шаг может не поделить уже заданный обед. Раздвигаем обед до целого
    # числа окон здесь же: иначе правило «обед меряется окнами» держалось бы
    # только на вводе и тихо ломалось при смене шага
    fields = {"slot_minutes": minutes}
    note = ""
    if schedule["lunch_from"]:
        lunch = (schedule["lunch_from"], schedule["lunch_to"])
        aligned = snap_lunch_to_grid(
            lunch,
            work_from=schedule["work_from"],
            work_to=schedule["work_to"],
            slot_minutes=minutes,
        )
        if aligned != lunch:
            fields["lunch_from"], fields["lunch_to"] = aligned
            note = f", обед раздвинут до {aligned[0]:%H:%M}-{aligned[1]:%H:%M}"

    if not await _save(idservice, **fields):
        await callback.answer(SAVE_FAILED, show_alert=True)
        return

    await _show_schedule(callback.message, svc, edit=True)
    await callback.answer(f"Шаг записи: {minutes} минут{note}", show_alert=bool(note))


@router.callback_query(F.data == "schedback")
async def step_back(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        # Без ответа спиннер на кнопке крутится до таймаута Telegram, и отказ
        # выглядит зависанием, а не отказом
        await callback.answer()
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
        # Без ответа спиннер на кнопке крутится до таймаута Telegram, и отказ
        # выглядит зависанием, а не отказом
        await callback.answer()
        return
    schedule = await db.get_schedule(str(svc["idservice"]))
    chosen = list(schedule["weekdays"])
    await state.update_data(weekdays=chosen)
    await callback.message.edit_reply_markup(reply_markup=kb.kb_schedule_days(chosen))
    await callback.answer()


@router.callback_query(F.data.startswith("scheddaytoggle:"))
async def days_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        # Без ответа спиннер на кнопке крутится до таймаута Telegram, и отказ
        # выглядит зависанием, а не отказом
        await callback.answer()
        return
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
        # Без ответа спиннер на кнопке крутится до таймаута Telegram, и отказ
        # выглядит зависанием, а не отказом
        await callback.answer()
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
