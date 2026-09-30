"""
handlers/register.py — FSM-регистрация автосервиса.

Пять шагов: название, телефон, город, адрес и услуги. Шаблонного списка
услуг нет: он не подходит никому целиком, а лишнее в нём клиент выбирает
и едет не туда. Поэтому хотя бы одну услугу управляющий вводит сам.

Шага с вводом tg id администратора нет: владелец сразу становится первым
админом, остальных подключает инвайт-ссылкой (handlers/admin_mgmt.py).
"""

import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.types import Message

import config
import keyboards as kb
import render
import subscription
from database import CatalogEntry, db
from handlers.catalog import DURATION_PROMPT, MAX_CATALOG_ITEMS
from handlers.common import set_active_service, show_main_menu
from notifications import alert_owners
from validators import (
    ValidationError,
    clean_text,
    h,
    normalize_city,
    normalize_phone,
    validate_duration,
    validate_price,
    validate_service_title,
)

logger = logging.getLogger(__name__)
router = Router()

TOTAL_STEPS = 5


class RegService(StatesGroup):
    name = State()
    phone = State()
    city = State()
    address = State()
    item_title = State()
    item_price = State()
    item_duration = State()
    more_items = State()


async def _announce(message: Message, svc) -> None:
    """
    Сообщить владельцу бота о новом сервисе.

    Это единственное событие, после которого в системе заводится чужой
    бизнес: увидеть его надо в тот же день, а не при следующем разборе базы.
    Персональных данных клиентов тут нет — только карточка самого сервиса и
    к кому идти с вопросами.

    Своей аварией письмо регистрацию не портит: сервис уже создан, и
    управляющий не должен из-за недоставленной новости увидеть ошибку.
    """
    try:
        owner = await db.get_user(message.from_user.id)
        await alert_owners(
            message.bot,
            "🆕 <b>Новый сервис</b>\n"
            f"{h(svc['service_name'])}, {h(svc['city'])}\n"
            f"Телефон: {render.callable_phone(svc['service_number'])}\n"
            f"Управляющий: {h(db.user_title(owner, message.from_user.id))}, "
            f"id <code>{message.from_user.id}</code>\n\n"
            f"<code>{svc['idservice']}</code>",
        )
    except Exception:
        logger.exception("Письмо владельцу бота о новом сервисе %s не ушло", svc["idservice"])


async def _paid_extra_note(owner_tg_id: int) -> str | None:
    """
    Можно ли завести ещё один сервис сверх бесплатного. None — нельзя;
    строка — можно, это предупреждение перед первым шагом.

    Пока подписка действует, лишний сервис — это ещё одна подписка:
    пробный период даётся человеку один раз, и новый сервис заработает после
    оплаты. Упираться в поддержку тут незачем. Держим одно ограничение: не
    больше одного неоплаченного сервиса за раз, иначе брошенные регистрации
    копились бы без конца.
    """
    now = datetime.now(timezone.utc)
    unpaid = [
        svc for svc in await db.get_owned_services(owner_tg_id)
        if not subscription.is_active(svc["paid_until"], now)
    ]
    if unpaid:
        return None
    return (
        "ℹ️ Пробный период уже использован, поэтому новый сервис заработает "
        f"после оплаты подписки: «{kb.BTN_SUBSCRIPTION}» в его меню.\n\n"
    )


@router.message(Command("register_service"), StateFilter(default_state))
@router.message(F.text == kb.BTN_REGISTER, StateFilter(default_state))
async def register_start(message: Message, state: FSMContext) -> None:
    note = ""
    owned = await db.count_owned_services(message.from_user.id)
    if owned >= config.FREE_PLAN_SERVICE_LIMIT:
        if not config.SUBSCRIPTION_ENFORCED:
            # Платить пока нечем — остаётся только поддержка
            await message.answer(
                f"⚠️ На текущем тарифе можно зарегистрировать "
                f"{config.FREE_PLAN_SERVICE_LIMIT} сервис(а).\n"
                f"У вас уже: {owned}.\n\n"
                "Чтобы добавить ещё один, обратитесь к поддержке.",
            )
            return
        note = await _paid_extra_note(message.from_user.id)
        if note is None:
            await message.answer(
                "⚠️ У вас уже есть сервис без оплаченной подписки.\n\n"
                f"Оплатите его подписку в «{kb.BTN_SUBSCRIPTION}», и можно будет "
                "зарегистрировать следующий. Если этот сервис больше не нужен, "
                f"удалите его: «{kb.BTN_DELETE_SERVICE}».",
            )
            return

    await state.set_state(RegService.name)
    await message.answer(
        "🚗 <b>Регистрация автосервиса</b>\n\n"
        f"{note}"
        f"<b>Шаг 1/{TOTAL_STEPS}.</b> Введите <b>название</b> автосервиса:",
        reply_markup=kb.kb_cancel(),
    )


@router.message(StateFilter(RegService), F.text == kb.BTN_CANCEL)
async def register_cancel(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await show_main_menu(message, state, greeting="↩️ Регистрация отменена.")


@router.message(RegService.name)
async def reg_name(message: Message, state: FSMContext) -> None:
    try:
        name = clean_text(message.text, field="Название", min_len=2, max_len=80)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}\nПопробуйте ещё раз:")
        return

    await state.update_data(name=name)
    await state.set_state(RegService.phone)
    await message.answer(
        f"<b>Шаг 2/{TOTAL_STEPS}.</b> Введите <b>номер телефона</b> сервиса:\n"
        "<i>Пример: +7 999 123-45-67</i>",
        reply_markup=kb.kb_cancel(),
    )


@router.message(RegService.phone)
async def reg_phone(message: Message, state: FSMContext) -> None:
    try:
        phone = normalize_phone(message.text)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}\nПопробуйте ещё раз:")
        return

    await state.update_data(phone=phone)
    await state.set_state(RegService.city)
    await message.answer(
        f"<b>Шаг 3/{TOTAL_STEPS}.</b> Введите <b>город</b>:\n<i>Пример: Москва</i>",
        reply_markup=kb.kb_cancel(),
    )


@router.message(RegService.city)
async def reg_city(message: Message, state: FSMContext) -> None:
    try:
        city = normalize_city(message.text)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}\nПопробуйте ещё раз:")
        return

    data = await state.get_data()
    duplicate = await db.find_duplicate_service(message.from_user.id, data["name"], city)
    if duplicate:
        await state.set_state(None)
        await show_main_menu(
            message,
            state,
            greeting=(
                f"⚠️ Сервис «{data['name']}» в городе {city} у вас уже зарегистрирован.\n"
                "Повторная регистрация не нужна."
            ),
        )
        return

    await state.update_data(city=city)
    await state.set_state(RegService.address)
    await message.answer(
        f"<b>Шаг 4/{TOTAL_STEPS}.</b> Введите <b>адрес</b> (улица и дом):\n"
        "<i>Пример: ул. Пушкина, д. 10</i>",
        reply_markup=kb.kb_cancel(),
    )


@router.message(RegService.address)
async def reg_address(message: Message, state: FSMContext) -> None:
    try:
        address = clean_text(message.text, field="Адрес", min_len=2, max_len=120)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}\nПопробуйте ещё раз:")
        return

    await state.update_data(address=address, items=[])
    await state.set_state(RegService.item_title)
    await message.answer(
        f"<b>Шаг 5/{TOTAL_STEPS}.</b> Какие <b>услуги</b> вы делаете?\n"
        "Клиент выберет их при записи. Добавим по одной: цену и время работы "
        "спрошу следом, позже всё можно поправить в «🔧 Услуги».\n\n"
        "Введите название первой услуги, например: <i>Полировка кузова</i>",
        reply_markup=kb.kb_cancel(),
    )


@router.message(RegService.item_title)
async def reg_item_title(message: Message, state: FSMContext) -> None:
    try:
        title = validate_service_title(message.text)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}\nПопробуйте ещё раз:")
        return

    data = await state.get_data()
    taken = {item[0].strip().lower() for item in data.get("items", [])}
    if title.strip().lower() in taken:
        await message.answer("❌ Такая услуга уже есть в списке. Введите другую:")
        return

    await state.update_data(item_title=title)
    await state.set_state(RegService.item_price)
    await message.answer(
        f"Услуга: <b>{h(title)}</b>\n\n"
        "Введите цену в рублях — например <i>3000</i>. Клиент увидит «от 3 000 ₽».\n"
        "Отправьте <b>-</b>, если цену показывать не нужно.",
        reply_markup=kb.kb_cancel(),
    )


@router.message(RegService.item_price)
async def reg_item_price(message: Message, state: FSMContext) -> None:
    try:
        price = validate_price(message.text)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}")
        return

    await state.update_data(item_price=price)
    await state.set_state(RegService.item_duration)
    await message.answer(DURATION_PROMPT, reply_markup=kb.kb_cancel())


def _items_text(items: list) -> str:
    return "".join(
        f"{i}. {render.titled_price(h(title), price, (low, high) if low else None)}\n"
        for i, (title, price, low, high) in enumerate(items, 1)
    )


@router.message(RegService.item_duration)
async def reg_item_duration(message: Message, state: FSMContext) -> None:
    try:
        duration = validate_duration(message.text)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}")
        return

    data = await state.get_data()
    low, high = duration or (None, None)
    # Список, а не кортеж: данные FSM хранятся в JSON
    items = [*data.get("items", []), [data["item_title"], data["item_price"], low, high]]
    await state.update_data(items=items)
    await state.set_state(RegService.more_items)

    can_add = len(items) < MAX_CATALOG_ITEMS
    await message.answer(
        f"<b>Ваши услуги:</b>\n{_items_text(items)}\n"
        + ("Добавить ещё одну или закончить регистрацию?" if can_add
           else "Больше услуг сразу добавить нельзя. Заканчиваем регистрацию?"),
        reply_markup=kb.kb_reg_services(can_add=can_add),
    )


@router.message(RegService.more_items, F.text == kb.BTN_MORE_SERVICE)
async def reg_more_items(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if len(data.get("items", [])) >= MAX_CATALOG_ITEMS:
        await message.answer(
            "Больше услуг сразу добавить нельзя.",
            reply_markup=kb.kb_reg_services(can_add=False),
        )
        return
    await state.set_state(RegService.item_title)
    await message.answer("Введите название следующей услуги:", reply_markup=kb.kb_cancel())


@router.message(RegService.more_items, F.text == kb.BTN_SERVICES_DONE)
async def reg_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(None)

    try:
        idservice = await db.create_service(
            name=data["name"],
            phone=data["phone"],
            city=data["city"],
            address=data["address"],
            owner_tg_id=message.from_user.id,
            catalog=[
                CatalogEntry(title, price, (low, high) if low else None)
                for title, price, low, high in data["items"]
            ],
        )
    except Exception:
        logger.exception("Ошибка при регистрации сервиса")
        await message.answer(
            "❌ Не удалось сохранить сервис. Попробуйте позже.",
            reply_markup=kb.kb_client_main(),
        )
        return

    svc = await db.get_service(idservice)
    await set_active_service(state, idservice)

    await message.answer(render.registration_summary(svc, db.service_link(idservice)))
    await _announce(message, svc)
    await show_main_menu(
        message, state, greeting="Меню управляющего готово к работе 👇"
    )


@router.message(RegService.more_items)
async def reg_more_items_unknown(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await message.answer(
        "Нажмите кнопку ниже: добавить ещё услугу или закончить.",
        reply_markup=kb.kb_reg_services(
            can_add=len(data.get("items", [])) < MAX_CATALOG_ITEMS
        ),
    )
