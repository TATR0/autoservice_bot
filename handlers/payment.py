"""
handlers/payment.py — оплата подписки: звёзды Telegram и перевод на ЮMoney.

Экран тарифов открывается двумя путями (кнопкой меню и кнопкой из письма) и
ведёт в одно место. Счёт формируется в момент нажатия, поэтому кнопка из
старого письма не устаревает.

Гейты подписки этот экран не закрывают: просроченный управляющий обязан иметь
возможность заплатить, иначе просрочка становится ловушкой без выхода.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import default_state
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

import config
import keyboards as kb
import render
import yoomoney
from database import db
from handlers.common import require_owner_service
from notifications import alert_owners, safe_send
from validators import h, is_uuid

logger = logging.getLogger(__name__)
router = Router()

PAYLOAD_PREFIX = "sub"

# Две ветки после успешного начисления отвечают одинаково — дни начислены, а
# названия сервиса нет. Текст один на обе, чтобы они не разошлись правками
CREDITED_WITHOUT_NAME = "✅ Оплата прошла, дни начислены."


async def _alert_bot(bot: Bot, text: str) -> None:
    """
    Позвать владельца бота руками разбирать платёж.

    Своей аварией это письмо не должно ронять обработку платежа: плательщику
    уже ответили, и последнее, чем стоит заканчивать оплату, — исключение
    из-за того, что владелец заблокировал собственного бота.
    """
    try:
        await alert_owners(bot, text)
    except Exception:
        logger.exception("Письмо владельцу бота об аварии платежа не ушло")


async def _alert(message: Message, text: str) -> None:
    """То же письмо, когда под рукой сообщение, а не бот."""
    await _alert_bot(message.bot, text)


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
    if not is_uuid(idservice) or not days.isdecimal():
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


def _plan_from(raw: str):
    """Тариф по строке из callback_data. None — такого тарифа больше нет."""
    return config.plan_by_days(int(raw)) if raw.isdecimal() else None


@router.callback_query(F.data.startswith("subscr:buy:"))
async def buy_plan(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    plan = _plan_from(callback.data.rsplit(":", 1)[-1])
    if plan is None:
        # Тариф убрали из конфига, пока письмо лежало в чате
        await callback.message.answer("Этот тариф больше не действует.")
        return

    svc = await require_owner_service(
        callback.message, state, user_id=callback.from_user.id
    )
    if svc is None:
        return

    # Способ один — спрашивать не о чем: лишний экран между «хочу продлить» и
    # оплатой люди читают как поломку
    if len(config.PAYMENT_METHODS) == 1:
        await _start_payment(callback.message, svc, plan, config.PAYMENT_METHODS[0])
        return

    await callback.message.answer(
        render.payment_method_screen(plan),
        reply_markup=kb.kb_payment_methods(plan),
    )


@router.callback_query(F.data.startswith("subscr:pay:"))
async def pay_with(callback: CallbackQuery, state: FSMContext) -> None:
    """Способ оплаты выбран. Дальше — счёт в звёздах или ссылка на перевод."""
    await callback.answer()
    parts = callback.data.split(":")
    if len(parts) != 4:
        return
    method, plan = parts[2], _plan_from(parts[3])
    if plan is None:
        await callback.message.answer("Этот тариф больше не действует.")
        return
    if not config.pays_with(method):
        # Способ выключили, пока сообщение лежало в чате. Открываем тарифы
        # заново, а не молчим: человек нажал «оплатить» и ждёт оплаты
        await callback.message.answer("Этот способ оплаты больше не доступен.")
        svc = await require_owner_service(
            callback.message, state, user_id=callback.from_user.id
        )
        if svc is not None:
            await _show_tariffs(callback.message, svc)
        return

    svc = await require_owner_service(
        callback.message, state, user_id=callback.from_user.id
    )
    if svc is None:
        return

    await _start_payment(callback.message, svc, plan, method)


async def _start_payment(message: Message, svc, plan, method: str) -> None:
    """Оплата выбранным способом. Счёт собирается здесь и сейчас, по цене из
    конфига, — поэтому кнопка из старого сообщения не выставит старую цену."""
    if method == config.PAYMENT_YOOMONEY:
        await _send_payment_link(message, svc, plan)
        return

    await message.answer_invoice(
        title=render.invoice_title(svc),
        description=render.invoice_description(plan),
        payload=make_payload(str(svc["idservice"]), plan.days),
        currency="XTR",
        prices=[LabeledPrice(label=plan.label, amount=plan.stars)],
    )


async def _send_payment_link(message: Message, svc, plan) -> None:
    """
    Оплата переводом: ссылка с подставленной суммой вместо счёта Telegram.

    Telegram о таком платеже не узнаёт, поэтому дни начисляет владелец бота
    руками. Чтобы это не превращалось в расследование, письмо ему уходит
    сразу — с готовой командой, которую останется выполнить, когда деньги
    придут на кошелёк.
    """
    label = make_payload(str(svc["idservice"]), plan.days)
    try:
        url = yoomoney.quickpay_link(
            config.YOOMONEY_WALLET,
            rubles=plan.rubles,
            label=label,
            target=render.payment_target(svc, plan),
        )
    except ValueError:
        # Кошелёк не настроен. Управляющий в этом не виноват и починить не
        # может — отправляем его к людям, а не к форме с пустым получателем
        logger.exception("Оплата переводом не настроена: YOOMONEY_WALLET пуст")
        await message.answer(
            "Оплата сейчас недоступна — мы уже чиним. "
            "Подписка не прервётся, пока разбираемся."
        )
        await _alert(
            message,
            "🆘 <b>Оплата не настроена</b>\n"
            "Управляющий открыл тарифы, а ссылку собрать не из чего: "
            "не задан <code>YOOMONEY_WALLET</code>.",
        )
        return

    await message.answer(
        render.payment_link_screen(svc, plan),
        reply_markup=kb.kb_pay_link(url),
        disable_web_page_preview=True,
    )
    await _alert(
        message,
        "💳 <b>Открыта оплата переводом</b>\n"
        f"Сервис: «{h(svc['service_name'])}»\n"
        f"Тариф: {plan.label} — {plan.rubles} ₽\n"
        f"Метка платежа: <code>{h(label)}</code>\n\n"
        f"Когда перевод придёт: <code>/extend {h(label)}</code>",
    )


@router.message(Command("paysupport"))
async def pay_support(message: Message) -> None:
    """
    Куда идти со спорным платежом. Telegram требует такую команду от ботов,
    которые продают за звёзды, — и правильно: списание без адреса для жалобы
    выглядит как ловушка.

    Без фильтра состояния: вопрос о списанных деньгах не должен упираться в
    то, что человек на середине регистрации. Состояние при этом не трогаем.
    Владельцу бота уходит письмо сразу — ждать, пока плательщик сам найдёт
    контакт, значит отвечать на жалобу через неделю.
    """
    await message.answer(render.pay_support())

    user = message.from_user
    who = f"@{h(user.username)}" if user.username else h(user.full_name)
    await _alert(
        message,
        "🙋 <b>Вопрос по оплате</b>\n"
        f"От: {who}, id <code>{user.id}</code> — "
        f'<a href="tg://user?id={user.id}">написать</a>\n\n'
        "Человек вызвал /paysupport. Звёзды возвращаются командой "
        "<code>/refund &lt;id платежа&gt;</code>, пока Telegram это позволяет — "
        "около трёх недель.",
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
        await _alert(
            message,
            "🆘 <b>Оплата без зачисления</b>\n"
            f"Списание: <code>{h(charge_id)}</code>\n"
            f"Счёт не распознан: <code>{h(payment_info.invoice_payload or '')}</code>\n\n"
            f"Дни не начислены. Вернуть звёзды: <code>/refund {h(charge_id)}</code>",
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
    except Exception:
        # Начислено или нет — отсюда не видно, и обещать начисление нельзя
        logger.exception(
            "Платёж %s: сбой зачисления, idservice=%s, дней=%d",
            charge_id, idservice, days,
        )
        await message.answer(
            "⚠️ Оплата прошла, зачисление задержалось. "
            "Мы уже видим платёж и разберёмся."
        )
        await _alert(
            message,
            "🆘 <b>Оплата без зачисления</b>\n"
            f"Списание: <code>{h(charge_id)}</code>\n"
            f"Сервис: <code>{h(idservice)}</code>, дней: {days}\n\n"
            "Зачисление упало, и начислено или нет — по этому месту не видно. "
            "Сверьте срок сервиса и при необходимости продлите "
            f"<code>/extend {h(idservice)} {days}</code>.",
        )
        return

    if applied is None:
        logger.error(
            "Платёж %s за несуществующий сервис %s, дней=%d",
            charge_id, idservice, days,
        )
        await message.answer(
            "⚠️ Оплата прошла, но сервис не найден. "
            "Напишите нам — вернём звёзды."
        )
        await _alert(
            message,
            "🆘 <b>Оплата за несуществующий сервис</b>\n"
            f"Списание: <code>{h(charge_id)}</code>\n"
            f"Сервис: <code>{h(idservice)}</code>, дней: {days}\n\n"
            f"Начислять некуда — вернуть звёзды: <code>/refund {h(charge_id)}</code>",
        )
        return

    # Дальше дни начислены наверняка, и провалиться может только сборка текста.
    # Отдельный try именно поэтому: назвать это «задержкой зачисления» значило
    # бы соврать человеку про его же деньги
    try:
        svc = await db.get_service(idservice)
    except Exception:
        logger.exception(
            "Платёж %s: дни начислены, но сервис %s не прочитать",
            charge_id, idservice,
        )
        await message.answer(CREDITED_WITHOUT_NAME)
        return

    if svc is None:
        # Без имени сервиса и часового пояса render.payment_done не собрать.
        # Заглушку вместо имени не выдумываем, отвечаем коротким подтверждением
        logger.error(
            "Платёж %s: дни начислены, но get_service(%s) вернул None",
            charge_id, idservice,
        )
        await message.answer(CREDITED_WITHOUT_NAME)
        return

    await message.answer(
        render.payment_done(svc, days=days, restored=applied.restored)
    )


# ── Перевод на кошелёк ───────────────────────────────────────────────────────

# Сколько от цены тарифа должно дойти, чтобы считать его оплаченным. ЮMoney
# берёт комиссию с некоторых способов оплаты, и требовать копейка в копейку
# значило бы отбивать честные платежи. Заметно меньшую сумму зачитывать нельзя:
# ссылку на оплату видно целиком, и сумму в ней подменяют одной правкой адреса
MIN_PAID_SHARE = Decimal("0.9")


async def credit_transfer(bot: Bot, notice: yoomoney.Notification) -> str:
    """
    Зачесть перевод на кошелёк. Возвращает короткий итог для лога.

    Деньги уже на кошельке, поэтому молча закончить нельзя ни на одной ветке:
    либо начисляем дни, либо зовём владельца бота разобрать руками. Повторную
    доставку того же уведомления отсекает db.extend_subscription: право на
    начисление занято уникальным индексом по (source, external_id).
    """
    if notice.test:
        # Кнопка «Проверить» в настройках ЮMoney: денег нет, метки нет
        logger.info("ЮMoney: тестовое уведомление")
        return "test"

    if notice.codepro or notice.unaccepted:
        # Перевод с защитным кодом или ждущий принятия: на кошелёк он ещё не
        # лёг, и начислять дни за него значит выдать товар до оплаты
        logger.warning("ЮMoney: перевод %s не принят", notice.operation_id)
        await _alert_bot(
            bot,
            "⏳ <b>Перевод ждёт принятия</b>\n"
            f"Операция: <code>{h(notice.operation_id)}</code>\n"
            f"Метка: <code>{h(notice.label)}</code>\n\n"
            "Дни не начислены. Примите перевод в ЮMoney — уведомление придёт "
            "заново, и начисление пройдёт само.",
        )
        return "unaccepted"

    parsed = parse_payload(notice.label)
    if parsed is None:
        # Перевод без нашей метки: например, человек отправил деньги сам, не
        # по ссылке. Кому начислять — отсюда не видно
        logger.error("ЮMoney: перевод %s с чужой меткой %r",
                     notice.operation_id, notice.label)
        await _alert_bot(
            bot,
            "🆘 <b>Перевод без метки</b>\n"
            f"Операция: <code>{h(notice.operation_id)}</code>\n"
            f"Сумма: {notice.amount} ₽\n"
            f"Метка: <code>{h(notice.label)}</code>\n\n"
            "Начислять некуда: метка не наша. Разберитесь по истории переводов.",
        )
        return "unknown-label"

    idservice, days = parsed
    plan = config.plan_by_days(days)
    if plan is not None and notice.amount < plan.rubles * MIN_PAID_SHARE:
        logger.error("ЮMoney: за тариф %d ₽ пришло %s", plan.rubles, notice.amount)
        await _alert_bot(
            bot,
            "🆘 <b>Пришло меньше цены тарифа</b>\n"
            f"Операция: <code>{h(notice.operation_id)}</code>\n"
            f"Тариф: {plan.label} — {plan.rubles} ₽, пришло {notice.amount} ₽\n"
            f"Метка: <code>{h(notice.label)}</code>\n\n"
            "Дни не начислены. Если платёж честный: "
            f"<code>/extend {h(notice.label)}</code>",
        )
        return "underpaid"

    try:
        paid_until = await db.extend_subscription(
            idservice, days=days, source="yoomoney", external_id=notice.operation_id,
        )
    except Exception:
        logger.exception("ЮMoney: сбой зачисления перевода %s", notice.operation_id)
        await _alert_bot(
            bot,
            "🆘 <b>Перевод без зачисления</b>\n"
            f"Операция: <code>{h(notice.operation_id)}</code>\n"
            f"Сумма: {notice.amount} ₽\n\n"
            "Зачисление упало. Сверьте срок сервиса и при необходимости: "
            f"<code>/extend {h(notice.label)}</code>",
        )
        return "failed"

    if paid_until is None:
        # Сервис удалён. Воскрешать его, как при оплате звёздами, нельзя:
        # звёзды возвращаются командой, а перевод — только руками
        logger.error("ЮMoney: перевод %s за несуществующий сервис %s",
                     notice.operation_id, idservice)
        await _alert_bot(
            bot,
            "🆘 <b>Перевод за несуществующий сервис</b>\n"
            f"Операция: <code>{h(notice.operation_id)}</code>\n"
            f"Сервис: <code>{h(idservice)}</code>, дней: {days}\n\n"
            "Начислять некуда — вернуть деньги придётся переводом.",
        )
        return "no-service"

    # Дни начислены. Дальше — только письма, и их сбой ничего не отменяет
    svc = await db.get_service(idservice)
    if svc is None:
        logger.error("ЮMoney: дни начислены, но сервис %s не читается", idservice)
        return "ok"

    await safe_send(bot, svc["owner_id"],
                    render.payment_done(svc, days=days, restored=False))
    await _alert_bot(
        bot,
        "✅ <b>Перевод зачислен</b>\n"
        f"Сервис: «{h(svc['service_name'])}»\n"
        f"Сумма: {notice.amount} ₽, дней: {days}\n"
        f"Операция: <code>{h(notice.operation_id)}</code>",
    )
    return "ok"
