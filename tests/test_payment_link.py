"""
Оплата переводом: экран тарифов выдаёт ссылку вместо счёта Telegram.

Ни сети, ни базы: подменены слой БД, Telegram и рассылка владельцу.
"""

from urllib.parse import parse_qs, urlsplit

import pytest

import config
import handlers.payment as payment

pytestmark = pytest.mark.asyncio

SERVICE_ID = "11111111-1111-1111-1111-111111111111"
OWNER_ID = 500
WALLET = "4100111122223333"


class FakeMessage:
    def __init__(self):
        self.answers = []
        self.markups = []
        self.invoices = []
        self.bot = object()

    async def answer(self, text, **kwargs):
        self.answers.append(text)
        self.markups.append(kwargs.get("reply_markup"))

    async def answer_invoice(self, **kwargs):
        self.invoices.append(kwargs)


class FakeCallback:
    def __init__(self, days: int, method: str | None = None):
        self.data = (f"subscr:pay:{method}:{days}" if method
                     else f"subscr:buy:{days}")
        self.message = FakeMessage()
        self.from_user = type("User", (), {"id": OWNER_ID})()

    async def answer(self, *args, **kwargs):
        pass


@pytest.fixture
def paying(monkeypatch):
    """Оплата переводом настроена; письма владельцу собираются в список."""
    alerts = []

    async def _service(_message, _state, user_id=None):
        return {
            "idservice": SERVICE_ID,
            "service_name": "Тест",
            "timezone": "Europe/Moscow",
            "paid_until": None,
        }

    async def _alert_owners(_bot, text):
        alerts.append(text)

    monkeypatch.setattr(payment, "require_owner_service", _service)
    monkeypatch.setattr(payment, "alert_owners", _alert_owners)
    monkeypatch.setattr(payment.config, "PAYMENT_METHODS", (config.PAYMENT_YOOMONEY,))
    monkeypatch.setattr(payment.config, "YOOMONEY_WALLET", WALLET)
    return alerts


def link_from(markup) -> str:
    return markup.inline_keyboard[0][0].url


async def test_link_carries_the_price_of_the_chosen_plan(paying):
    plan = config.PLANS[0]
    callback = FakeCallback(plan.days)
    await payment.buy_plan(callback, state=None)

    url = link_from(callback.message.markups[-1])
    query = parse_qs(urlsplit(url).query)
    assert query["sum"] == [f"{plan.rubles}.00"]
    assert query["receiver"] == [WALLET]


async def test_every_plan_gets_its_own_sum(paying):
    """Срок выбирают кнопкой — цена обязана идти за ним, а не за первой."""
    sums = []
    for plan in config.PLANS:
        callback = FakeCallback(plan.days)
        await payment.buy_plan(callback, state=None)
        url = link_from(callback.message.markups[-1])
        sums.append(parse_qs(urlsplit(url).query)["sum"][0])

    assert sums == [f"{plan.rubles}.00" for plan in config.PLANS]


async def test_label_ties_the_payment_to_the_service(paying):
    """
    Деньги придут на кошелёк без единого слова о том, чей это платёж. Метка —
    единственная ниточка, и формат у неё тот же, что у счёта Telegram.
    """
    callback = FakeCallback(30)
    await payment.buy_plan(callback, state=None)

    url = link_from(callback.message.markups[-1])
    label = parse_qs(urlsplit(url).query)["label"][0]
    assert payment.parse_payload(label) == (SERVICE_ID, 30)


async def test_no_telegram_invoice_is_sent(paying):
    """Счёт и ссылка одновременно — это два способа заплатить дважды."""
    callback = FakeCallback(30)
    await payment.buy_plan(callback, state=None)
    assert callback.message.invoices == []


async def test_owner_gets_the_ready_command(paying):
    """
    Начисляет дни владелец бота руками, поэтому письмо должно содержать всё
    нужное: искать uuid по истории переводов в пять утра — плохая затея.
    """
    callback = FakeCallback(30)
    await payment.buy_plan(callback, state=None)

    assert paying, "владелец не узнал, что кто-то пошёл платить"
    assert f"/extend sub:{SERVICE_ID}:30" in paying[-1]


async def test_unset_wallet_does_not_send_anyone_to_a_broken_form(paying, monkeypatch):
    """
    Пустой кошелёк — это ссылка в никуда. Управляющий об этом не просил и
    починить не может, поэтому ему честный отказ, а владельцу — письмо.
    """
    monkeypatch.setattr(payment.config, "YOOMONEY_WALLET", "")
    callback = FakeCallback(30)
    await payment.buy_plan(callback, state=None)

    assert callback.message.markups == [None], "кнопки оплаты быть не должно"
    assert callback.message.answers, "молчать в ответ на нажатие нельзя"
    assert any("YOOMONEY_WALLET" in text for text in paying)


async def test_stars_stay_available_when_chosen(monkeypatch, paying):
    """Способ оплаты — настройка: со звёздами всё работает как прежде."""
    monkeypatch.setattr(payment.config, "PAYMENT_METHODS", (config.PAYMENT_STARS,))
    callback = FakeCallback(30)
    await payment.buy_plan(callback, state=None)

    assert callback.message.invoices, "счёт Telegram не выставлен"
    assert callback.message.invoices[-1]["currency"] == "XTR"


# ── Два способа сразу ────────────────────────────────────────────────────────


@pytest.fixture
def both(paying, monkeypatch):
    """Включены оба способа: переводом и звёздами."""
    monkeypatch.setattr(
        payment.config, "PAYMENT_METHODS",
        (config.PAYMENT_YOOMONEY, config.PAYMENT_STARS),
    )
    return paying


async def test_choice_comes_before_the_payment(both):
    """
    Два способа — сначала спрашиваем, чем платить. Молча выбрать за человека
    нельзя: у одного нет карты, у другого — звёзд.
    """
    callback = FakeCallback(30)
    await payment.buy_plan(callback, state=None)

    assert callback.message.invoices == [], "счёт выставлен без выбора способа"
    buttons = [b for row in callback.message.markups[-1].inline_keyboard for b in row]
    assert {b.callback_data for b in buttons} == {
        "subscr:pay:yoomoney:30", "subscr:pay:stars:30",
    }


async def test_both_prices_are_on_the_buttons(both):
    """Цены разные, и разницу видно до нажатия, а не после списания."""
    plan = config.PLANS[0]
    callback = FakeCallback(plan.days)
    await payment.buy_plan(callback, state=None)

    texts = " ".join(
        b.text for row in callback.message.markups[-1].inline_keyboard for b in row
    )
    assert f"{plan.rubles} ₽" in texts
    assert f"{plan.stars} ⭐" in texts


async def test_chosen_stars_send_an_invoice(both):
    callback = FakeCallback(30, method=config.PAYMENT_STARS)
    await payment.pay_with(callback, state=None)

    assert callback.message.invoices, "счёт Telegram не выставлен"
    assert callback.message.invoices[-1]["currency"] == "XTR"


async def test_chosen_transfer_sends_a_link(both):
    plan = config.PLANS[0]
    callback = FakeCallback(plan.days, method=config.PAYMENT_YOOMONEY)
    await payment.pay_with(callback, state=None)

    assert callback.message.invoices == []
    query = parse_qs(urlsplit(link_from(callback.message.markups[-1])).query)
    assert query["sum"] == [f"{plan.rubles}.00"]


async def test_switched_off_method_does_not_charge(paying):
    """
    Кнопка из вчерашнего сообщения, а способ с тех пор выключили. Счёт по ней
    выставлять нельзя — предлагаем выбрать заново.
    """
    callback = FakeCallback(30, method=config.PAYMENT_STARS)
    await payment.pay_with(callback, state=None)

    assert callback.message.invoices == [], "счёт по выключенному способу"
    assert callback.message.answers, "нажатие осталось без ответа"


async def test_unknown_plan_is_not_paid_for(both):
    callback = FakeCallback(7, method=config.PAYMENT_STARS)
    await payment.pay_with(callback, state=None)

    assert callback.message.invoices == []
    assert "не действует" in callback.message.answers[-1]


# ── /paysupport ──────────────────────────────────────────────────────────────


class FakeUser:
    id = 777
    username = "payer"
    full_name = "Плательщик"


async def test_pay_support_answers_and_tells_the_owner(paying):
    """
    Вопрос о списанных деньгах: человеку — куда писать, владельцу бота —
    письмо сразу, с тем, как этого человека найти.
    """
    message = FakeMessage()
    message.from_user = FakeUser()
    await payment.pay_support(message)

    assert message.answers, "команда осталась без ответа"
    assert paying, "владелец бота не узнал о вопросе"
    assert "@payer" in paying[-1]
    assert "777" in paying[-1]


async def test_pay_support_escapes_the_name(paying):
    """Без ника в письмо идёт имя — а его человек пишет сам, как хочет."""
    message = FakeMessage()
    message.from_user = FakeUser()
    message.from_user.username = None
    message.from_user.full_name = "<b>Вася</b>"
    await payment.pay_support(message)

    assert "<b>Вася</b>" not in paying[-1]
    assert "&lt;b&gt;Вася" in paying[-1]
