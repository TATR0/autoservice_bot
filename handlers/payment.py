"""
handlers/payment.py — оплата подписки звёздами Telegram.

Экран тарифов открывается двумя путями (кнопкой меню и кнопкой из письма) и
ведёт в одно место. Счёт формируется в момент нажатия, поэтому кнопка из
старого письма не устаревает.

Гейты подписки этот экран не закрывают: просроченный управляющий обязан иметь
возможность заплатить, иначе просрочка становится ловушкой без выхода.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import default_state
from aiogram.types import CallbackQuery, LabeledPrice, Message

import config
import keyboards as kb
import render
from database import db
from handlers.common import require_owner_service

logger = logging.getLogger(__name__)
router = Router()

PAYLOAD_PREFIX = "sub"


def make_payload(idservice: str, days: int) -> str:
    """Что именно оплачено. Telegram вернёт эту строку в неизменном виде."""
    return f"{PAYLOAD_PREFIX}:{idservice}:{days}"


def parse_payload(raw: str) -> tuple[str, int] | None:
    """
    Разобрать payload счёта. None — строка не наша или испорчена.

    isdecimal, а не isdigit: isdigit истинно для не-ASCII цифр вроде «²»,
    которые int() не парсит, и хендлер падал бы необработанным исключением.
    """
    parts = (raw or "").split(":")
    if len(parts) != 3 or parts[0] != PAYLOAD_PREFIX:
        return None
    idservice, days = parts[1], parts[2]
    if not idservice or not days.isdecimal():
        return None
    return idservice, int(days)


async def _show_tariffs(message: Message, svc) -> None:
    await message.answer(render.tariff_screen(svc), reply_markup=kb.kb_tariffs())


@router.message(F.text == kb.BTN_SUBSCRIPTION, StateFilter(default_state))
async def subscription_screen(message: Message, state: FSMContext) -> None:
    svc = await require_owner_service(message, state)
    if svc is None:
        return
    await _show_tariffs(message, svc)


@router.callback_query(F.data == "subscr:open")
async def open_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    # user_id обязателен: у callback.message автор — бот, а не человек, и без
    # этого аргумента сервис искался бы по id бота и не находился никогда
    svc = await require_owner_service(
        callback.message, state, user_id=callback.from_user.id
    )
    if svc is None:
        return
    await _show_tariffs(callback.message, svc)


@router.callback_query(F.data.startswith("subscr:buy:"))
async def buy_plan(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    days = callback.data.rsplit(":", 1)[-1]
    plan = config.plan_by_days(int(days)) if days.isdecimal() else None
    if plan is None:
        # Тариф убрали из конфига, пока письмо лежало в чате
        await callback.message.answer("Этот тариф больше не действует.")
        return

    svc = await require_owner_service(
        callback.message, state, user_id=callback.from_user.id
    )
    if svc is None:
        return

    await callback.message.answer_invoice(
        title=render.invoice_title(svc),
        description=render.invoice_description(plan),
        payload=make_payload(str(svc["idservice"]), plan.days),
        currency="XTR",
        prices=[LabeledPrice(label=plan.label, amount=plan.stars)],
    )
