"""
handlers/catalog.py — услуги сервиса.

Список услуг у каждого сервиса свой: первые управляющий вводит при
регистрации, дальше правит их здесь. Администраторы каталог не
меняют — состав услуг это про то, чем сервис вообще занимается, а не про
повседневную обработку заявок.
"""

import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.types import CallbackQuery, Message

import keyboards as kb
import render
from database import db
from handlers.common import require_owner_service, show_main_menu
from validators import (
    ValidationError,
    h,
    parse_service_lines,
    validate_duration,
    validate_price,
    validate_uuid,
)

logger = logging.getLogger(__name__)
router = Router()

MAX_CATALOG_ITEMS = 30


class ServiceCatalog(StatesGroup):
    lines = State()
    confirm = State()


class ServicePrice(StatesGroup):
    """Правка цены уже заведённой услуги — отдельный поток, без ветвлений."""
    value = State()


class ServiceDuration(StatesGroup):
    """Правка времени работы уже заведённой услуги."""
    value = State()


# Общие с регистрацией: там услуги вводятся и проверяются так же
SERVICES_HINT = (
    "Пришлите услуги одним сообщением, каждую с новой строки: название, "
    "цена и время работы через запятую. Цену и время можно не писать. "
    "Цена — в рублях, время — в часах.\n\n"
    "<i>Полировка кузова, от 2000р, 2-4 ч\n"
    "Химчистка салона, 5000р, 3 ч\n"
    "Дубликат ключа</i>"
)


def services_check_text(items: list) -> str:
    """
    Список на проверку: цена и время отдельными строками, с пометками.
    Так сразу видно, если бот понял строку не так, как задумано: цену
    временем или часть названия ценой.
    """
    lines = []
    for i, (title, price, low, high) in enumerate(items, 1):
        lines.append(f"{i}. <b>{h(title)}</b>")
        if price is not None:
            lines.append(f"     💰 {render.price_label(price)}")
        if low:
            lines.append(f"     ⏱ {render.duration_label(low, high)}")
    return "\n".join(lines)


def new_items_error(taken: list[str], parsed: list) -> str | None:
    """
    Почему новые услуги нельзя добавить к списку taken. None — можно.

    Сообщение не добавляется частично: иначе управляющему пришлось бы
    выяснять, какие строки прошли, а какие нет.
    """
    seen = {title.strip().lower() for title in taken}
    for title, _, _ in parsed:
        key = title.strip().lower()
        if key in seen:
            return (
                f"❌ Услуга «{h(title)}» уже есть в списке. Остальные из этого "
                "сообщения тоже не добавлены: пришлите их без неё."
            )
        seen.add(key)
    if len(taken) + len(parsed) > MAX_CATALOG_ITEMS:
        return (
            f"❌ Больше {MAX_CATALOG_ITEMS} услуг быть не может. "
            f"В списке уже {len(taken)}."
        )
    return None


def as_items(parsed: list) -> list[list]:
    """[название, цена, от, до] — списки, а не кортежи: данные FSM хранятся в JSON."""
    return [
        [title, price, *(duration or (None, None))]
        for title, price, duration in parsed
    ]


DURATION_PROMPT = (
    "Сколько часов занимает работа? Например <i>2-4</i> или <i>3</i>.\n"
    "Запись займёт бокс на верхнюю границу, чтобы следующая машина не ждала.\n"
    "Отправьте <b>-</b>, если время не указывать: запись займёт одно окно расписания."
)


def _catalog_text(svc, items) -> str:
    lines = "".join(
        f"{i}. {render.titled_item(h(item['title']), item)}\n"
        for i, item in enumerate(items, 1)
    )
    return (
        f"🔧 <b>Услуги — {h(svc['service_name'])}</b>\n\n"
        f"{lines}\n"
        f"Всего {len(items)} из {MAX_CATALOG_ITEMS}. "
        "Нажмите на услугу, чтобы открыть её карточку."
    )


def _item_text(item) -> str:
    """Без цены и времени строк о них не выводим — задать их можно кнопками ниже."""
    price = render.price_label(item["price_rub"])
    duration = render.duration_label(*(render.item_duration(item) or (None, None)))
    lines = [f"🔧 <b>{h(item['title'])}</b>"]
    if price or duration:
        lines.append("")
    if price:
        lines.append(f"💰 Цена: {price}")
    if duration:
        lines.append(f"⏱ Время работы: {duration}")
    return "\n".join(lines)


async def _show_catalog(message: Message, svc, *, edit: bool = False) -> None:
    items = await db.get_catalog(str(svc["idservice"]))
    text = _catalog_text(svc, items)
    markup = kb.kb_catalog(items)
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


def _parse_idcatalog(callback: CallbackQuery) -> str | None:
    """id из callback_data. None — данные подделаны или испорчены."""
    try:
        return validate_uuid(callback.data.split(":", 1)[1], field="Услуга")
    except (IndexError, ValidationError):
        return None


# ── Список услуг ─────────────────────────────────────────────────────────────

@router.message(F.text == kb.BTN_SERVICES, StateFilter(default_state))
async def show_services(message: Message, state: FSMContext) -> None:
    svc = await require_owner_service(message, state)
    if svc is None:
        return
    await _show_catalog(message, svc)


# ── Добавление услуги ────────────────────────────────────────────────────────

@router.callback_query(F.data == "svcadd")
async def add_start(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        await callback.answer()
        return

    items = await db.get_catalog(str(svc["idservice"]))
    if len(items) >= MAX_CATALOG_ITEMS:
        await callback.answer(
            f"❌ Больше {MAX_CATALOG_ITEMS} услуг добавить нельзя.", show_alert=True
        )
        return

    await state.update_data(new_items=[])
    await state.set_state(ServiceCatalog.lines)
    await callback.message.answer(SERVICES_HINT, reply_markup=kb.kb_cancel())
    await callback.answer()


@router.message(ServiceCatalog.lines, F.text == kb.BTN_CANCEL)
@router.message(ServiceCatalog.confirm, F.text == kb.BTN_CANCEL)
@router.message(ServicePrice.value, F.text == kb.BTN_CANCEL)
@router.message(ServiceDuration.value, F.text == kb.BTN_CANCEL)
async def add_cancel(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await show_main_menu(message, state, greeting="Отменено.")


@router.message(ServiceCatalog.confirm, F.text == kb.BTN_SERVICES_RESET)
async def add_reset(message: Message, state: FSMContext) -> None:
    await state.update_data(new_items=[])
    await state.set_state(ServiceCatalog.lines)
    await message.answer(
        f"Список очищен.\n\n{SERVICES_HINT}", reply_markup=kb.kb_cancel()
    )


@router.message(ServiceCatalog.confirm, F.text == kb.BTN_SERVICES_OK)
async def add_save(message: Message, state: FSMContext) -> None:
    svc = await require_owner_service(message, state)
    if svc is None:
        await state.set_state(None)
        await show_main_menu(message, state)
        return

    data = await state.get_data()
    added, skipped = [], []
    for title, price, low, high in data.get("new_items", []):
        item = await db.add_catalog_item(
            str(svc["idservice"]), title, price, (low, high) if low else None
        )
        (added if item else skipped).append(title)

    # Дубликаты отсеяны ещё при вводе; сюда попадает только то, что успели
    # завести параллельно, например со второго устройства
    lines = []
    if added:
        lines.append(f"✅ Добавлено услуг: {len(added)}.")
    if skipped:
        lines.append(
            "❌ Уже есть в списке, не добавлены: "
            + ", ".join(f"«{h(title)}»" for title in skipped) + "."
        )
    await state.set_state(None)
    await show_main_menu(message, state, greeting="\n".join(lines))
    await _show_catalog(message, svc)


@router.message(StateFilter(ServiceCatalog.lines, ServiceCatalog.confirm))
async def add_lines(message: Message, state: FSMContext) -> None:
    """Услуги строками. На проверке новые строки дописываются к списку."""
    svc = await require_owner_service(message, state)
    if svc is None:
        await state.set_state(None)
        await show_main_menu(message, state)
        return

    try:
        parsed = parse_service_lines(message.text)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}\n\n{SERVICES_HINT}")
        return

    data = await state.get_data()
    items = list(data.get("new_items", []))
    catalog = await db.get_catalog(str(svc["idservice"]))
    taken = [item["title"] for item in catalog] + [item[0] for item in items]
    error = new_items_error(taken, parsed)
    if error:
        await message.answer(error)
        return

    items += as_items(parsed)
    await state.update_data(new_items=items)
    await state.set_state(ServiceCatalog.confirm)
    await message.answer(
        "Проверьте <b>новые услуги</b>:\n\n"
        f"{services_check_text(items)}\n\n"
        f"Всё так — нажмите «{kb.BTN_SERVICES_OK}».\n"
        "Забыли услугу — пришлите её следующим сообщением, она добавится.\n"
        f"Что-то не так — «{kb.BTN_SERVICES_RESET}».",
        reply_markup=kb.kb_services_check(),
    )


# ── Карточка услуги и цена ───────────────────────────────────────────────────

@router.callback_query(F.data.startswith("svcopen:"))
async def open_item(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        await callback.answer()
        return

    idcatalog = _parse_idcatalog(callback)
    if idcatalog is None:
        await callback.answer("❌ Услуга не найдена.", show_alert=True)
        return

    item = await db.get_catalog_item(str(svc["idservice"]), idcatalog)
    if item is None:
        await callback.answer("❌ Услуга уже удалена.", show_alert=True)
        await _show_catalog(callback.message, svc, edit=True)
        return

    await callback.message.edit_text(
        _item_text(item), reply_markup=kb.kb_catalog_item(idcatalog)
    )
    await callback.answer()


@router.callback_query(F.data == "svclist")
async def back_to_list(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        await callback.answer()
        return
    await _show_catalog(callback.message, svc, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("svcprice:"))
async def price_start(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        await callback.answer()
        return

    idcatalog = _parse_idcatalog(callback)
    if idcatalog is None:
        await callback.answer("❌ Услуга не найдена.", show_alert=True)
        return

    item = await db.get_catalog_item(str(svc["idservice"]), idcatalog)
    if item is None:
        await callback.answer("❌ Услуга уже удалена.", show_alert=True)
        await _show_catalog(callback.message, svc, edit=True)
        return

    await state.update_data(price_for=idcatalog)
    await state.set_state(ServicePrice.value)
    current = render.price_label(item["price_rub"])
    await callback.message.answer(
        f"Услуга: <b>{h(item['title'])}</b>\n"
        + (f"Сейчас: {current}\n" if current else "")
        + "\nВведите новую цену в рублях или <b>-</b>, чтобы убрать её.",
        reply_markup=kb.kb_cancel(),
    )
    await callback.answer()


@router.message(ServicePrice.value)
async def price_finish(message: Message, state: FSMContext) -> None:
    svc = await require_owner_service(message, state)
    if svc is None:
        await state.set_state(None)
        await show_main_menu(message, state)
        return

    try:
        price = validate_price(message.text)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}")
        return

    data = await state.get_data()
    item = await db.set_catalog_item_price(
        str(svc["idservice"]), data["price_for"], price
    )
    await state.set_state(None)

    if item is None:
        await show_main_menu(message, state, greeting="❌ Услуга уже удалена.")
    else:
        await show_main_menu(
            message,
            state,
            greeting="✅ " + render.titled_item(h(item["title"]), item),
        )
    await _show_catalog(message, svc)


@router.callback_query(F.data.startswith("svctime:"))
async def duration_start(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        await callback.answer()
        return

    idcatalog = _parse_idcatalog(callback)
    if idcatalog is None:
        await callback.answer("❌ Услуга не найдена.", show_alert=True)
        return

    item = await db.get_catalog_item(str(svc["idservice"]), idcatalog)
    if item is None:
        await callback.answer("❌ Услуга уже удалена.", show_alert=True)
        await _show_catalog(callback.message, svc, edit=True)
        return

    await state.update_data(duration_for=idcatalog)
    await state.set_state(ServiceDuration.value)
    current = render.duration_label(*(render.item_duration(item) or (None, None)))
    await callback.message.answer(
        f"Услуга: <b>{h(item['title'])}</b>\n"
        + (f"Сейчас: {current}\n" if current else "")
        + "\n" + DURATION_PROMPT,
        reply_markup=kb.kb_cancel(),
    )
    await callback.answer()


@router.message(ServiceDuration.value)
async def duration_finish(message: Message, state: FSMContext) -> None:
    svc = await require_owner_service(message, state)
    if svc is None:
        await state.set_state(None)
        await show_main_menu(message, state)
        return

    try:
        duration = validate_duration(message.text)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}")
        return

    data = await state.get_data()
    item = await db.set_catalog_item_duration(
        str(svc["idservice"]), data["duration_for"], duration
    )
    await state.set_state(None)

    if item is None:
        await show_main_menu(message, state, greeting="❌ Услуга уже удалена.")
    else:
        await show_main_menu(
            message, state, greeting="✅ " + render.titled_item(h(item["title"]), item)
        )
    await _show_catalog(message, svc)


# ── Удаление услуги ──────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("svcdel:"))
async def delete_ask(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        await callback.answer()
        return

    idcatalog = _parse_idcatalog(callback)
    if idcatalog is None:
        await callback.answer("❌ Услуга не найдена.", show_alert=True)
        return

    item = await db.get_catalog_item(str(svc["idservice"]), idcatalog)
    if item is None:
        await callback.answer("❌ Услуга уже удалена.", show_alert=True)
        await _show_catalog(callback.message, svc, edit=True)
        return

    used = await db.count_requests_by_catalog(str(svc["idservice"]), idcatalog)
    used_line = (
        f"По ней уже {used} заявок — они останутся в истории и статистике.\n"
        if used else ""
    )
    await callback.message.edit_text(
        f"🗑 <b>Удалить услугу «{h(item['title'])}»?</b>\n\n"
        f"{used_line}"
        "Клиенты больше не смогут выбрать её при записи.",
        reply_markup=kb.kb_confirm("svcdelok", idcatalog),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("svcdelok:"))
async def delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        await callback.answer()
        return

    idcatalog = _parse_idcatalog(callback)
    if idcatalog is None:
        await callback.answer("❌ Услуга не найдена.", show_alert=True)
        return

    removed = await db.delete_catalog_item(str(svc["idservice"]), idcatalog)
    if removed is None:
        # None приходит и на «последняя услуга», и на «уже удалили с другого
        # устройства» — различаем повторным чтением, иначе покажем неверную причину
        if await db.get_catalog_item(str(svc["idservice"]), idcatalog) is None:
            await callback.answer("Услуга уже удалена.", show_alert=True)
        else:
            await callback.answer(
                "❌ Нельзя удалить последнюю услугу — сначала добавьте другую.",
                show_alert=True,
            )
        await _show_catalog(callback.message, svc, edit=True)
        return

    await callback.answer(f"Услуга «{removed['title']}» удалена.")
    await _show_catalog(callback.message, svc, edit=True)
