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
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

import config
import keyboards as kb
import render
from database import db
from handlers.common import require_owner_service
from validators import _UUID_RE

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
    # services.idservice — колонка типа uuid: не-UUID строка дошла бы до
    # db.get_service и уронила бы asyncpg DataError вместо понятного отказа
    if not _UUID_RE.match(idservice) or not days.isdecimal():
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


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    """
    Последняя проверка перед списанием. Telegram ждёт ответа десять секунд.

    Поэтому здесь только разбор payload и одно чтение сервиса — ничего
    тяжёлого. Отказ на этом шаге не стоит человеку ни звезды.
    """
    parsed = parse_payload(query.invoice_payload)
    if parsed is None:
        await query.answer(False, error_message="Счёт испорчен. Откройте оплату заново.")
        return

    idservice, days = parsed
    if config.plan_by_days(days) is None:
        await query.answer(False, error_message="Этот тариф больше не действует.")
        return

    # Удалён заранее — не берём денег вовсе. Воскрешение при оплате (см.
    # db.apply_stars_payment) закрывает только щель между этой проверкой и
    # списанием, а не заменяет её
    if await db.get_service(idservice) is None:
        await query.answer(False, error_message="Сервис недоступен. Оплата отменена.")
        return

    await query.answer(True)


@router.message(F.successful_payment)
async def paid(message: Message) -> None:
    """
    Деньги уже списаны — отсюда нельзя уйти молча ни при какой ошибке.

    Повторную доставку того же платежа отсекает db.extend_subscription: право
    на начисление занимается уникальным индексом по (source, external_id).
    """
    payment_info = message.successful_payment
    charge_id = payment_info.telegram_payment_charge_id
    parsed = parse_payload(payment_info.invoice_payload)
    if parsed is None:
        logger.error(
            "Платёж %s с неразбираемым payload %r",
            charge_id, payment_info.invoice_payload,
        )
        await message.answer(
            "⚠️ Оплата прошла, но счёт не удалось распознать. "
            "Напишите нам — разберёмся вручную."
        )
        return

    idservice, days = parsed
    plan = config.plan_by_days(days)
    if plan is not None and plan.stars != payment_info.total_amount:
        # Цену поменяли, пока счёт лежал в чате. Дни начисляем — деньги уже
        # у нас, — но расхождение должно быть видно в логе
        logger.warning(
            "Платёж %s: заплачено %d звёзд, тариф стоит %d",
            charge_id, payment_info.total_amount, plan.stars,
        )

    # Деньги уже списаны — необработанное исключение отсюда ловит только
    # ErrorLoggingMiddleware, а в его логе нет ни charge_id, ни idservice, ни
    # days: разбирать инцидент вручную было бы не по чему
    try:
        applied = await db.apply_stars_payment(
            idservice,
            days=days,
            charge_id=charge_id,
            payer_id=message.from_user.id,
        )
        if applied is None:
            logger.error(
                "Платёж %s за несуществующий сервис %s, дней=%d",
                charge_id, idservice, days,
            )
            await message.answer(
                "⚠️ Оплата прошла, но сервис не найден. "
                "Напишите нам — вернём звёзды."
            )
            return

        svc = await db.get_service(idservice)
    except Exception:
        logger.exception(
            "Платёж %s: сбой зачисления, idservice=%s, дней=%d",
            charge_id, idservice, days,
        )
        await message.answer(
            "⚠️ Оплата прошла, зачисление задержалось. "
            "Мы уже видим платёж и разберёмся."
        )
        return

    if svc is None:
        # Дни начислены — apply_stars_payment вернул не None, — но без имени
        # сервиса и часового пояса render.payment_done не собрать. Заглушку
        # вместо имени не выдумываем, отвечаем коротким подтверждением
        logger.error(
            "Платёж %s: дни начислены, но get_service(%s) вернул None",
            charge_id, idservice,
        )
        await message.answer("✅ Оплата прошла, дни начислены.")
        return

    await message.answer(
        render.payment_done(svc, days=days, restored=applied.restored)
    )
