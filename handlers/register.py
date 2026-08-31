"""
handlers/register.py — FSM-регистрация автосервиса.

Четыре шага: название, телефон, город, адрес. Шага с вводом tg id
администратора нет: владелец сразу становится первым админом, остальных
подключает инвайт-ссылкой (handlers/admin_mgmt.py).
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.types import Message

import config
import keyboards as kb
import render
from database import db
from handlers.common import set_active_service, show_main_menu
from notifications import alert_owners
from validators import (
    ValidationError,
    clean_text,
    format_phone,
    h,
    normalize_city,
    normalize_phone,
)

logger = logging.getLogger(__name__)
router = Router()

TOTAL_STEPS = 4


class RegService(StatesGroup):
    name = State()
    phone = State()
    city = State()
    address = State()


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
            f"Телефон: {h(format_phone(svc['service_number']))}\n"
            f"Управляющий: {h(db.user_title(owner, message.from_user.id))}, "
            f"id <code>{message.from_user.id}</code>\n\n"
            f"<code>{svc['idservice']}</code>",
        )
    except Exception:
        logger.exception("Письмо владельцу бота о новом сервисе %s не ушло", svc["idservice"])


@router.message(Command("register_service"), StateFilter(default_state))
@router.message(F.text == kb.BTN_REGISTER, StateFilter(default_state))
async def register_start(message: Message, state: FSMContext) -> None:
    owned = await db.count_owned_services(message.from_user.id)
    if owned >= config.FREE_PLAN_SERVICE_LIMIT:
        await message.answer(
            f"⚠️ На текущем тарифе можно зарегистрировать "
            f"{config.FREE_PLAN_SERVICE_LIMIT} сервис(а).\n"
            f"У вас уже: {owned}.\n\n"
            "Чтобы добавить ещё один, обратитесь к поддержке.",
        )
        return

    await state.set_state(RegService.name)
    await message.answer(
        "🚗 <b>Регистрация автосервиса</b>\n\n"
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

    data = await state.get_data()
    await state.set_state(None)

    try:
        idservice = await db.create_service(
            name=data["name"],
            phone=data["phone"],
            city=data["city"],
            address=address,
            owner_tg_id=message.from_user.id,
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
