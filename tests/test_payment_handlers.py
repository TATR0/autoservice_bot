"""Оплата звёздами. Базы не требует — слой БД и Telegram подменены."""

from datetime import datetime, timedelta, timezone

import pytest

import config
import handlers.payment as payment
from database import PaymentApplied

pytestmark = pytest.mark.asyncio

SERVICE_ID = "11111111-1111-1111-1111-111111111111"


def test_payload_round_trip():
    assert payment.parse_payload(payment.make_payload(SERVICE_ID, 30)) == (
        SERVICE_ID, 30
    )


def test_broken_payload_is_not_guessed():
    """Мусор в payload — это не повод угадывать, за что человек заплатил."""
    for raw in ("", "sub", "sub:x", f"sub:{SERVICE_ID}", f"sub:{SERVICE_ID}:x",
                f"sub:{SERVICE_ID}:30:extra", f"other:{SERVICE_ID}:30"):
        assert payment.parse_payload(raw) is None, raw


def test_non_ascii_digits_do_not_crash_the_parser():
    """isdigit() истинно для «²», int() его не парсит — уже ловили в /extend."""
    assert payment.parse_payload(f"sub:{SERVICE_ID}:²") is None


class FakeScreenMessage:
    def __init__(self):
        self.answers = []
        self.markups = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)
        self.markups.append(kwargs.get("reply_markup"))


async def test_expired_owner_can_still_reach_the_tariffs(monkeypatch):
    """
    Гейты подписки этот экран не закрывают.

    Иначе просрочка становится ловушкой: заплатить можно только изнутри,
    а внутрь не пускают, пока не заплатишь.
    """
    from datetime import datetime, timedelta, timezone

    async def _expired(_message, _state, user_id=None):
        return {
            "idservice": SERVICE_ID,
            "service_name": "Тест",
            "timezone": "Europe/Moscow",
            "paid_until": datetime.now(timezone.utc) - timedelta(days=10),
        }

    monkeypatch.setattr(payment, "require_owner_service", _expired)
    message = FakeScreenMessage()
    await payment.subscription_screen(message, None)

    assert message.answers, "просроченному управляющему экран обязан открыться"
    assert message.markups[0] is not None, "тарифы без кнопок бесполезны"


async def test_admin_is_refused_the_tariff_screen(monkeypatch):
    """
    Подписка сервиса — не дело администратора.

    Кнопку ему не показывают, но текст можно набрать руками, а callback —
    нажать в пересланном письме. Решает сервер, а не клавиатура.
    """
    refused = []

    async def _not_owner(message, _state, user_id=None):
        refused.append(user_id)
        return None

    monkeypatch.setattr(payment, "require_owner_service", _not_owner)
    message = FakeScreenMessage()
    await payment.subscription_screen(message, None)

    assert refused, "проверка владельца обязана быть вызвана"
    assert message.answers == [], "чужому экран не рисуем"


# ── Проверка перед списанием ─────────────────────────────────────────────────
# Telegram ждёт ответа десять секунд: не ответил — платёж не состоится. Отказ
# на этом шаге не стоит человеку ни звезды, деньги ещё не списаны.


class FakePreCheckout:
    def __init__(self, payload: str, total_amount: int = 150):
        self.invoice_payload = payload
        self.total_amount = total_amount
        self.answers = []

    async def answer(self, ok: bool, error_message: str | None = None):
        self.answers.append((ok, error_message))


@pytest.fixture
def live_service(monkeypatch):
    async def _service(_id):
        return {"idservice": SERVICE_ID, "service_name": "Тест"}

    monkeypatch.setattr(payment.db, "get_service", _service)


async def test_good_invoice_is_let_through(live_service):
    query = FakePreCheckout(payment.make_payload(SERVICE_ID, 30))
    await payment.pre_checkout(query)
    assert query.answers == [(True, None)]


async def test_broken_payload_is_refused_with_words(live_service):
    query = FakePreCheckout("мусор")
    await payment.pre_checkout(query)
    ok, message = query.answers[0]
    assert ok is False
    assert message, "отказ без текста человек не поймёт"


async def test_unknown_plan_is_refused(live_service):
    query = FakePreCheckout(payment.make_payload(SERVICE_ID, 7))
    await payment.pre_checkout(query)
    assert query.answers[0][0] is False


async def test_deleted_service_is_refused_before_the_money(monkeypatch):
    """
    Не взять денег лучше, чем взять и чинить последствия.

    Восстановление сервиса при оплате закрывает только щель между этой
    проверкой и списанием, а не заменяет её.
    """
    async def _gone(_id):
        return None

    monkeypatch.setattr(payment.db, "get_service", _gone)
    query = FakePreCheckout(payment.make_payload(SERVICE_ID, 30))
    await payment.pre_checkout(query)
    assert query.answers[0][0] is False


# ── Зачисление платежа ───────────────────────────────────────────────────────
# Деньги уже списаны: молчать или падать здесь нельзя ни при каком вводе.


class FakePayment:
    def __init__(self, payload: str, charge_id: str = "ch_1", amount: int = 150):
        self.invoice_payload = payload
        self.telegram_payment_charge_id = charge_id
        self.total_amount = amount
        self.currency = "XTR"


class FakePaidMessage:
    def __init__(self, payload: str, user_id: int = 999_000_001, **kwargs):
        self.successful_payment = FakePayment(payload, **kwargs)
        self.from_user = type("User", (), {"id": user_id})()
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


@pytest.fixture
def applied(monkeypatch):
    calls = []

    async def _apply(idservice, *, days, charge_id, payer_id):
        calls.append((idservice, days, charge_id, payer_id))
        # PaymentApplied — класс уровня модуля database, а не атрибут db:
        # db это экземпляр Database, и через него класс не достать
        return PaymentApplied(
            paid_until=datetime.now(timezone.utc) + timedelta(days=days),
            restored=False,
        )

    async def _service(_id):
        return {
            "idservice": SERVICE_ID,
            "service_name": "Тест",
            "timezone": "Europe/Moscow",
            "paid_until": datetime.now(timezone.utc) + timedelta(days=30),
        }

    monkeypatch.setattr(payment.db, "apply_stars_payment", _apply)
    monkeypatch.setattr(payment.db, "get_service", _service)
    return calls


async def test_payment_credits_the_service(applied):
    message = FakePaidMessage(payment.make_payload(SERVICE_ID, 30))
    await payment.paid(message)
    assert applied == [(SERVICE_ID, 30, "ch_1", 999_000_001)]
    assert message.answers, "человек обязан увидеть подтверждение"


async def test_broken_payload_after_payment_does_not_crash(applied):
    """
    Деньги уже списаны. Падение здесь означало бы платёж без товара и без следа.
    """
    message = FakePaidMessage("мусор")
    await payment.paid(message)
    assert applied == []
    assert message.answers, "молчать после списания нельзя"
