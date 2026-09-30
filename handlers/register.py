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
from handlers.catalog import MAX_CATALOG_ITEMS
from handlers.common import set_active_service, show_main_menu
from notifications import alert_owners
from validators import (
    ValidationError,
    clean_text,
    h,
    normalize_city,
    normalize_phone,
    parse_service_lines,
)

logger = logging.getLogger(__name__)
router = Router()

TOTAL_STEPS = 5


class RegService(StatesGroup):
    name = State()
    phone = State()
    city = State()
    address = State()
    items = State()


SERVICES_HINT = (
    "Пришлите услуги одним сообщением, каждую с новой строки: название, "
    "цена и время работы через запятую. Цену и время можно не писать.\n\n"
    "<i>Полировка кузова, от 2000р, 2-4 ч\n"
    "Химчистка салона, 5000р, 3 ч\n"
    "Дубликат ключа</i>\n\n"
    "Позже всё можно поправить в «🔧 Услуги»."
)


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
    await state.set_state(RegService.items)
    await message.answer(
        f"<b>Шаг 5/{TOTAL_STEPS}.</b> Какие <b>услуги</b> вы делаете?\n"
        "Клиент выберет их при записи.\n\n"
        f"{SERVICES_HINT}",
        reply_markup=kb.kb_cancel(),
    )


def _items_text(items: list) -> str:
    return "".join(
        f"{i}. {render.titled_price(h(title), price, (low, high) if low else None)}\n"
        for i, (title, price, low, high) in enumerate(items, 1)
    )


@router.message(RegService.items, F.text == kb.BTN_SERVICES_DONE)
async def reg_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("items"):
        await message.answer(f"Сначала добавьте хотя бы одну услугу.\n\n{SERVICES_HINT}")
        return
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


@router.message(RegService.items)
async def reg_items(message: Message, state: FSMContext) -> None:
    try:
        parsed = parse_service_lines(message.text)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}\n\n{SERVICES_HINT}")
        return

    data = await state.get_data()
    items = list(data.get("items", []))
    taken = {item[0].strip().lower() for item in items}
    for title, _, _ in parsed:
        key = title.strip().lower()
        if key in taken:
            await message.answer(
                f"❌ Услуга «{h(title)}» уже есть в списке. Остальные из этого "
                "сообщения тоже не добавлены: пришлите их без неё."
            )
            return
        taken.add(key)

    if len(items) + len(parsed) > MAX_CATALOG_ITEMS:
        await message.answer(
            f"❌ Больше {MAX_CATALOG_ITEMS} услуг сразу добавить нельзя. "
            f"В списке уже {len(items)}."
        )
        return

    # Списки, а не кортежи: данные FSM хранятся в JSON
    items += [
        [title, price, *(duration or (None, None))]
        for title, price, duration in parsed
    ]
    await state.update_data(items=items)
    await message.answer(
        f"<b>Ваши услуги:</b>\n{_items_text(items)}\n"
        "Если всё, нажмите «✅ Готово». Если нет, пришлите следующие услуги "
        "так же, строками.",
        reply_markup=kb.kb_reg_services(),
    )
