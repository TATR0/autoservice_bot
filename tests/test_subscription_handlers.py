"""Команда /extend. Базы не требуют — слой БД подменён."""

import logging
from datetime import datetime, timedelta, timezone

import pytest

import handlers.subscription as handler

pytestmark = pytest.mark.asyncio

OWNER_ID = 999_000_100
STRANGER_ID = 999_000_200
SERVICE_ID = "11111111-1111-1111-1111-111111111111"


class FakeMessage:
    def __init__(self, text: str, user_id: int):
        self.text = text
        self.from_user = type("User", (), {"id": user_id})()
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


@pytest.fixture
def extended(monkeypatch):
    """Слой БД подменён: проверяем разбор команды и права, а не SQL."""
    calls = []

    async def _extend(idservice, *, days, granted_by=None, **kwargs):
        calls.append((idservice, days, granted_by))
        return datetime.now(timezone.utc) + timedelta(days=days)

    async def _service(_id):
        return {"service_name": "Тест", "timezone": "Europe/Moscow"}

    monkeypatch.setattr(handler.config, "BOT_OWNER_IDS", (OWNER_ID,))
    monkeypatch.setattr(handler.db, "extend_subscription", _extend)
    monkeypatch.setattr(handler.db, "get_service", _service)
    return calls


async def test_owner_extends_a_service(extended):
    message = FakeMessage(f"/extend {SERVICE_ID} 30", OWNER_ID)
    await handler.extend_command(message)
    assert extended == [(SERVICE_ID, 30, OWNER_ID)]
    assert message.answers


async def test_stranger_gets_no_answer_at_all(extended):
    """
    Молча: рассказывать постороннему, что такая команда существует, незачем.
    """
    message = FakeMessage(f"/extend {SERVICE_ID} 30", STRANGER_ID)
    await handler.extend_command(message)
    assert extended == []
    assert message.answers == []


async def test_broken_arguments_are_explained(extended):
    message = FakeMessage("/extend 30", OWNER_ID)
    await handler.extend_command(message)
    assert extended == []
    assert message.answers, "владельцу бота нужен текст, а не молчание"


async def test_zero_days_is_rejected_before_the_database(extended):
    """Констрейнт это тоже поймает, но ответит языком драйвера."""
    message = FakeMessage(f"/extend {SERVICE_ID} 0", OWNER_ID)
    await handler.extend_command(message)
    assert extended == []
    assert message.answers == [handler.USAGE]


async def test_non_ascii_digits_do_not_crash(extended):
    """isdigit() истинно для «²», а int() его не парсит — хендлер падал."""
    message = FakeMessage(f"/extend {SERVICE_ID} ²", OWNER_ID)
    await handler.extend_command(message)
    assert extended == []
    assert message.answers == [handler.USAGE]


async def test_missing_service_is_reported(extended, monkeypatch):
    async def _none(*args, **kwargs):
        return None

    monkeypatch.setattr(handler.db, "extend_subscription", _none)
    message = FakeMessage(f"/extend {SERVICE_ID} 30", OWNER_ID)
    await handler.extend_command(message)
    assert "не найден" in " ".join(message.answers).lower()


async def test_owner_can_grant_a_century(extended):
    """Бессрочная подписка — это очень длинный срок, а не отдельное состояние."""
    message = FakeMessage(f"/extend {SERVICE_ID} 36500", OWNER_ID)
    await handler.extend_command(message)
    assert extended == [(SERVICE_ID, 36500, OWNER_ID)]


async def test_owner_can_take_days_back(extended):
    """Необратимая операция без отмены рано или поздно случается."""
    message = FakeMessage(f"/extend {SERVICE_ID} -30", OWNER_ID)
    await handler.extend_command(message)
    assert extended == [(SERVICE_ID, -30, OWNER_ID)]


async def test_zero_is_still_rejected(extended):
    message = FakeMessage(f"/extend {SERVICE_ID} 0", OWNER_ID)
    await handler.extend_command(message)
    assert extended == []


async def test_minus_zero_is_rejected_too(extended):
    message = FakeMessage(f"/extend {SERVICE_ID} -0", OWNER_ID)
    await handler.extend_command(message)
    assert extended == []


async def test_absurd_number_is_rejected(extended):
    message = FakeMessage(f"/extend {SERVICE_ID} 999999", OWNER_ID)
    await handler.extend_command(message)
    assert extended == []


# ── «/refund» ────────────────────────────────────────────────────────────────

PAYMENT_ID = "22222222-2222-2222-2222-222222222222"
PAYER_ID = 999_000_300


class FakeBot:
    """Подменяет aiogram.Bot.refund_star_payment: до Telegram тест не ходит."""

    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.refund_calls = []

    async def refund_star_payment(self, *, user_id, telegram_payment_charge_id):
        self.refund_calls.append((user_id, telegram_payment_charge_id))
        if self.exc is not None:
            raise self.exc


def _payment_record(refunded_at=None, days=5):
    return {
        "idpayment": PAYMENT_ID,
        "idservice": SERVICE_ID,
        "granted_by": PAYER_ID,
        "refunded_at": refunded_at,
        "days": days,
    }


@pytest.fixture
def refundable(monkeypatch):
    """Слой БД и уведомления подменены: проверяем разбор и права, а не Telegram."""
    sent = []

    async def _safe_send(bot, chat_id, text, **kwargs):
        sent.append((chat_id, text))
        return True

    async def _get_payment(_charge_id):
        return _payment_record()

    async def _revoke(_idpayment):
        return datetime.now(timezone.utc) + timedelta(days=5)

    async def _service(_id):
        return {"service_name": "Тест", "timezone": "Europe/Moscow"}

    monkeypatch.setattr(handler.config, "BOT_OWNER_IDS", (OWNER_ID,))
    monkeypatch.setattr(handler.db, "get_stars_payment", _get_payment)
    monkeypatch.setattr(handler.db, "revoke_payment", _revoke)
    monkeypatch.setattr(handler.db, "get_service", _service)
    monkeypatch.setattr(handler, "safe_send", _safe_send)
    return sent


async def test_stranger_cannot_refund():
    """Знать о существовании команды постороннему незачем."""
    message = FakeMessage("/refund ch_1", STRANGER_ID)
    await handler.refund_command(message, None)
    assert message.answers == []


async def test_owner_can_refund_a_payment(refundable):
    bot = FakeBot()
    message = FakeMessage(f"/refund {PAYMENT_ID}", OWNER_ID)
    await handler.refund_command(message, bot)
    assert bot.refund_calls == [(PAYER_ID, PAYMENT_ID)]
    assert message.answers, "владельцу бота нужно подтверждение"
    assert len(refundable) == 1 and refundable[0][0] == PAYER_ID, (
        "плательщику должно уйти уведомление"
    )


async def test_refund_for_deleted_service_does_not_crash(refundable, monkeypatch):
    """
    Поправка контролёра: db.get_service фильтрует idrecstatus=0 и для
    удалённого сервиса вернёт None. Деньги и дни к этому моменту уже отобраны
    обратно — хендлер обязан ответить владельцу, а не упасть TypeError-ом.
    """

    async def _none(_id):
        return None

    monkeypatch.setattr(handler.db, "get_service", _none)
    bot = FakeBot()
    message = FakeMessage(f"/refund {PAYMENT_ID}", OWNER_ID)
    await handler.refund_command(message, bot)

    assert bot.refund_calls == [(PAYER_ID, PAYMENT_ID)], "деньги должны вернуться"
    assert message.answers, "владелец бота должен получить подтверждение"
    assert "удал" in " ".join(message.answers).lower()
    assert refundable == [], "плательщику про удалённый сервис слать нечего"


async def test_revoke_payment_failure_after_refund_does_not_crash(refundable, monkeypatch, caplog):
    """
    Деньги уже вернулись плательщику — это необратимо. Если revoke_payment
    падает (обрыв связи, дедлок), хендлер не должен рухнуть необработанным
    исключением, а обязан честно сказать: деньги вернулись, дни — нет, и
    назвать рабочее средство — /extend с реальными id и отрицательным числом
    дней, скопировать и выполнить.
    """

    async def _boom(_idpayment):
        raise RuntimeError("db connection lost")

    monkeypatch.setattr(handler.db, "revoke_payment", _boom)
    bot = FakeBot()
    message = FakeMessage(f"/refund {PAYMENT_ID}", OWNER_ID)
    with caplog.at_level(logging.ERROR):
        await handler.refund_command(message, bot)

    assert bot.refund_calls == [(PAYER_ID, PAYMENT_ID)], "деньги уже должны были уйти"
    assert message.answers, "владелец бота обязан получить ответ, а не тишину"
    answer = " ".join(message.answers)
    assert f"/extend {SERVICE_ID} -5" in answer, (
        "рабочее средство — /extend с id сервиса и отрицательным числом дней, "
        "готовое к копированию"
    )
    assert PAYMENT_ID in caplog.text, "charge_id обязан попасть в лог для разбора инцидента"
    assert refundable == [], "плательщику после аварии слать нечего"


async def test_get_service_failure_after_revoke_does_not_crash(refundable, monkeypatch, caplog):
    """
    К этому моменту и деньги, и дни уже списаны обратно — падать нельзя.
    get_service нужен только для текста ответа.
    """

    async def _boom(_id):
        raise RuntimeError("db connection lost")

    monkeypatch.setattr(handler.db, "get_service", _boom)
    bot = FakeBot()
    message = FakeMessage(f"/refund {PAYMENT_ID}", OWNER_ID)
    with caplog.at_level(logging.ERROR):
        await handler.refund_command(message, bot)

    assert bot.refund_calls == [(PAYER_ID, PAYMENT_ID)], "деньги уже должны были уйти"
    assert message.answers, "владелец бота обязан получить ответ, а не тишину"
    assert PAYMENT_ID in caplog.text, "charge_id обязан попасть в лог для разбора инцидента"


# ── «Записаться в свой сервис» ───────────────────────────────────────────────
# Пятый вход в форму, и единственный, который открывает её сотрудник. Гейты
# ниже его поймают, но покажут клиентский текст: предложат позвонить самому
# себе, ни словом не объяснив причину.

import handlers.requests as requests_handler  # noqa: E402
import keyboards as kb  # noqa: E402


def _own_service(paid_until):
    return {
        "idservice": SERVICE_ID,
        "service_name": "Тест",
        "service_number": "+79990000000",
        "timezone": "Europe/Moscow",
        "paid_until": paid_until,
    }


@pytest.fixture
def own_service(monkeypatch):
    """Подменяем поиск своего сервиса: проверяется гейт, а не права."""
    box = {}

    async def _require(_message, _state):
        return box.get("svc")

    monkeypatch.setattr(requests_handler, "require_active_service", _require)
    return box


async def test_own_form_is_refused_when_the_subscription_expired(own_service):
    own_service["svc"] = _own_service(datetime.now(timezone.utc) - timedelta(days=1))
    message = FakeMessage(kb.BTN_BOOK_OWN, OWNER_ID)
    await requests_handler.open_booking_form(message, None)
    answer = " ".join(message.answers)
    assert "истекла" in answer, "сотруднику называем причину, а не «позвоните»"
    assert "Позвоните" not in answer, "клиентский текст сотруднику не показываем"


async def test_own_form_opens_while_the_subscription_holds(own_service):
    own_service["svc"] = _own_service(datetime.now(timezone.utc) + timedelta(days=10))
    message = FakeMessage(kb.BTN_BOOK_OWN, OWNER_ID)
    await requests_handler.open_booking_form(message, None)
    assert "истекла" not in " ".join(message.answers)
