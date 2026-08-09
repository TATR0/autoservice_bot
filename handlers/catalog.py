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
from database import db
from handlers.common import require_active_service, show_main_menu
from validators import ValidationError, h, validate_service_title, validate_uuid

logger = logging.getLogger(__name__)
router = Router()

MAX_CATALOG_ITEMS = 30


class ServiceCatalog(StatesGroup):
    title = State()


async def _owner_service(
    message: Message, state: FSMContext, user_id: int | None = None
):
    """Активный сервис, если пользователь — его управляющий. Иначе None."""
    user_id = user_id or message.from_user.id
    svc = await require_active_service(message, state, user_id)
    if svc is None:
        return None
    if svc["owner_id"] != user_id:
        await message.answer("❌ Управлять услугами может только управляющий.")
        return None
    return svc


def _catalog_text(svc, items) -> str:
    lines = "".join(f"{i}. {h(item['title'])}\n" for i, item in enumerate(items, 1))
    return (
        f"🔧 <b>Услуги — {h(svc['service_name'])}</b>\n\n"
        f"{lines}\n"
        f"Всего {len(items)} из {MAX_CATALOG_ITEMS}. "
        "Нажмите на услугу, чтобы удалить."
    )


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
    svc = await _owner_service(message, state)
    if svc is None:
        return
    await _show_catalog(message, svc)


# ── Добавление услуги ────────────────────────────────────────────────────────

@router.callback_query(F.data == "svcadd")
async def add_start(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await _owner_service(callback.message, state, callback.from_user.id)
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
async def add_cancel(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await show_main_menu(message, state, greeting="Отменено.")


@router.message(ServiceCatalog.title)
async def add_finish(message: Message, state: FSMContext) -> None:
    svc = await _owner_service(message, state)
    if svc is None:
        await state.set_state(None)
        await show_main_menu(message, state)
        return

    try:
        title = validate_service_title(message.text)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}\nПопробуйте ещё раз:")
        return

    item = await db.add_catalog_item(str(svc["idservice"]), title)
    if item is None:
        await message.answer(
            "❌ Такая услуга уже есть в списке. Введите другое название:"
        )
        return

    await state.set_state(None)
    await show_main_menu(
        message, state, greeting=f"✅ Услуга «{h(item['title'])}» добавлена."
    )
    await _show_catalog(message, svc)


# ── Удаление услуги ──────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("svcdel:"))
async def delete_ask(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await _owner_service(callback.message, state, callback.from_user.id)
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
    svc = await _owner_service(callback.message, state, callback.from_user.id)
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
