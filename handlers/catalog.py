"""
handlers/catalog.py — услуги сервиса.

Список услуг у каждого сервиса свой: при регистрации копируется шаблонный
набор, дальше управляющий правит его под себя. Администраторы каталог не
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
    ValidationError, h, validate_price, validate_service_title, validate_uuid
)

logger = logging.getLogger(__name__)
router = Router()

MAX_CATALOG_ITEMS = 30


class ServiceCatalog(StatesGroup):
    title = State()
    price = State()


class ServicePrice(StatesGroup):
    """Правка цены уже заведённой услуги — отдельный поток, без ветвлений."""
    value = State()


def _catalog_text(svc, items) -> str:
    lines = "".join(
        f"{i}. {render.titled_price(h(item['title']), item['price_rub'])}\n"
        for i, item in enumerate(items, 1)
    )
    return (
        f"🔧 <b>Услуги — {h(svc['service_name'])}</b>\n\n"
        f"{lines}\n"
        f"Всего {len(items)} из {MAX_CATALOG_ITEMS}. "
        "Нажмите на услугу, чтобы открыть её карточку."
    )


def _item_text(item) -> str:
    """Без цены строку о ней не выводим — задать её можно кнопкой ниже."""
    price = render.price_label(item["price_rub"])
    text = f"🔧 <b>{h(item['title'])}</b>"
    if price:
        text += f"\n\n💰 Цена: {price}"
    return text


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

    await state.set_state(ServiceCatalog.title)
    await callback.message.answer(
        "Введите название услуги, например: <i>Полировка кузова</i>",
        reply_markup=kb.kb_cancel(),
    )
    await callback.answer()


@router.message(ServiceCatalog.title, F.text == kb.BTN_CANCEL)
@router.message(ServiceCatalog.price, F.text == kb.BTN_CANCEL)
@router.message(ServicePrice.value, F.text == kb.BTN_CANCEL)
async def add_cancel(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await show_main_menu(message, state, greeting="Отменено.")


@router.message(ServiceCatalog.title)
async def add_title(message: Message, state: FSMContext) -> None:
    svc = await require_owner_service(message, state)
    if svc is None:
        await state.set_state(None)
        await show_main_menu(message, state)
        return

    try:
        title = validate_service_title(message.text)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}\nПопробуйте ещё раз:")
        return

    await state.update_data(new_title=title)
    await state.set_state(ServiceCatalog.price)
    await message.answer(
        f"Услуга: <b>{h(title)}</b>\n\n"
        "Введите цену в рублях — например <i>3000</i>.\n"
        "Отправьте <b>-</b>, если цену показывать не нужно.",
        reply_markup=kb.kb_cancel(),
    )


@router.message(ServiceCatalog.price)
async def add_price(message: Message, state: FSMContext) -> None:
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
    item = await db.add_catalog_item(str(svc["idservice"]), data["new_title"], price)
    if item is None:
        await state.set_state(None)
        await show_main_menu(message, state, greeting="❌ Такая услуга уже есть в списке.")
        await _show_catalog(message, svc)
        return

    await state.set_state(None)
    await show_main_menu(
        message, state, greeting=f"✅ Услуга «{h(item['title'])}» добавлена."
    )
    await _show_catalog(message, svc)


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
            greeting="✅ " + render.titled_price(h(item["title"]), item["price_rub"]),
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
