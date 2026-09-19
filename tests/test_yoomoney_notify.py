"""
Уведомление ЮMoney о переводе: подпись, разбор и зачисление дней.

Ни сети, ни базы: слой БД и рассылка подменены. Подпись считается в тесте
руками по строке из документации ЮMoney — иначе проверялось бы лишь то, что
код сходится сам с собой, и перестановка полей осталась бы незамеченной.
"""

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import config
import handlers.payment as payment
import yoomoney

# Без pytestmark: в этом файле есть и синхронные проверки подписи, а
# asyncio_mode=auto из pytest.ini сам разберётся с асинхронными
SECRET = "тестовый-секрет"
SERVICE_ID = "11111111-1111-1111-1111-111111111111"
OWNER_ID = 777
OPERATION = "670000000000000001"


def form(**overrides) -> dict:
    """Уведомление о принятом переводе на 590 ₽ за месяц подписки."""
    data = {
        "notification_type": "p2p-incoming",
        "operation_id": OPERATION,
        "amount": "590.00",
        "withdraw_amount": "590.00",
        "currency": "643",
        "datetime": "2026-09-19T10:00:00Z",
        "sender": "410011112222333",
        "codepro": "false",
        "label": f"sub:{SERVICE_ID}:30",
        "unaccepted": "false",
    }
    data.update(overrides)
    data["sha1_hash"] = expected_hash(data)
    return data


def expected_hash(data: dict) -> str:
    """Подпись по документации: поля через «&», секрет перед меткой."""
    raw = "&".join([
        data.get("notification_type", ""),
        data.get("operation_id", ""),
        data.get("amount", ""),
        data.get("currency", ""),
        data.get("datetime", ""),
        data.get("sender", ""),
        data.get("codepro", ""),
        SECRET,
        data.get("label", ""),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ── Подпись ──────────────────────────────────────────────────────────────────

def test_signature_follows_the_documented_field_order():
    data = form()
    assert yoomoney.signature(data, SECRET) == expected_hash(data)


def test_forged_notification_is_rejected():
    """Адрес приёмника публичный: без проверки подписи подписку продлит любой."""
    data = form()
    data["sha1_hash"] = "0" * 40
    with pytest.raises(yoomoney.NotificationError):
        yoomoney.parse_notification(data, SECRET)


def test_changed_sum_breaks_the_signature():
    """Подпись считается и по сумме — переписать её по дороге не выйдет."""
    data = form()
    data["amount"] = "1.00"
    with pytest.raises(yoomoney.NotificationError):
        yoomoney.parse_notification(data, SECRET)


def test_empty_secret_accepts_nothing():
    """Незаполненная переменная не должна означать «принимать всё подряд»."""
    with pytest.raises(yoomoney.NotificationError):
        yoomoney.parse_notification(form(), "")


def test_card_payment_without_sender_is_valid():
    """При оплате картой ЮMoney не присылает sender — подпись считается по пустому."""
    data = form(sender="")
    notice = yoomoney.parse_notification(data, SECRET)
    assert notice.sender == ""


def test_amount_is_decimal_not_float():
    """Деньги в двоичной дроби — способ однажды не досчитаться копейки."""
    notice = yoomoney.parse_notification(form(amount="1490.00"), SECRET)
    assert notice.amount == Decimal("1490.00")
    assert isinstance(notice.amount, Decimal)


# ── Зачисление ───────────────────────────────────────────────────────────────

class FakeDB:
    def __init__(self, paid_until=None, service=True):
        self.calls = []
        self._paid_until = paid_until or datetime.now(timezone.utc) + timedelta(days=30)
        self._service = service

    async def extend_subscription(self, idservice, **kwargs):
        self.calls.append((idservice, kwargs))
        return self._paid_until if self._service else None

    async def get_service(self, idservice):
        if not self._service:
            return None
        return {
            "idservice": idservice,
            "service_name": "Тест",
            "owner_id": OWNER_ID,
            "timezone": "Europe/Moscow",
            "paid_until": self._paid_until,
        }


@pytest.fixture
def credited(monkeypatch):
    """Подменённое окружение: письма собираются в списки, база — в FakeDB."""
    alerts, sent = [], []

    async def _alert_owners(_bot, text):
        alerts.append(text)

    async def _safe_send(_bot, chat_id, text, **kwargs):
        sent.append((chat_id, text))
        return True

    fake_db = FakeDB()
    monkeypatch.setattr(payment, "db", fake_db)
    monkeypatch.setattr(payment, "alert_owners", _alert_owners)
    monkeypatch.setattr(payment, "safe_send", _safe_send)
    return fake_db, alerts, sent


def notice(**overrides):
    return yoomoney.parse_notification(form(**overrides), SECRET)


async def test_transfer_credits_the_days(credited):
    fake_db, _, _ = credited
    assert await payment.credit_transfer(None, notice()) == "ok"

    idservice, kwargs = fake_db.calls[0]
    assert idservice == SERVICE_ID
    assert kwargs["days"] == 30


async def test_repeat_delivery_is_tied_to_the_operation(credited):
    """
    ЮMoney повторяет доставку, пока не получит 200. Второй раз начислять
    нечего: деньги те же. Отсекает это уникальный индекс в базе, поэтому
    номер операции обязан доехать до него неизменным.
    """
    fake_db, _, _ = credited
    await payment.credit_transfer(None, notice())
    _, kwargs = fake_db.calls[0]
    assert kwargs["external_id"] == OPERATION
    assert kwargs["source"] == "yoomoney"


async def test_manager_learns_that_payment_went_through(credited):
    _, _, sent = credited
    await payment.credit_transfer(None, notice())
    assert sent, "управляющему не сказали, что оплата прошла"
    assert sent[0][0] == OWNER_ID


async def test_test_notification_credits_nothing(credited):
    """Кнопка «Проверить» в настройках ЮMoney — это не деньги."""
    fake_db, _, _ = credited
    assert await payment.credit_transfer(None, notice(test_notification="true")) == "test"
    assert fake_db.calls == []


async def test_unaccepted_transfer_credits_nothing(credited):
    """Перевод с защитным кодом ещё не на кошельке — товар вперёд денег."""
    fake_db, alerts, _ = credited
    assert await payment.credit_transfer(None, notice(codepro="true")) == "unaccepted"
    assert fake_db.calls == []
    assert alerts, "владелец бота не узнал о непринятом переводе"


async def test_foreign_label_credits_nothing_but_calls_the_owner(credited):
    """Перевод мимо ссылки: кому начислять — не видно, молчать нельзя."""
    fake_db, alerts, _ = credited
    result = await payment.credit_transfer(None, notice(label="привет"))
    assert result == "unknown-label"
    assert fake_db.calls == []
    assert alerts


async def test_underpaid_transfer_credits_nothing(credited):
    """
    Сумма стоит в адресе ссылки открытым текстом, и правится он одной рукой.
    Год подписки за рубль — ровно то, что случится без этой проверки.
    """
    fake_db, alerts, _ = credited
    plan = config.PLANS[0]
    result = await payment.credit_transfer(None, notice(amount="1.00"))
    assert result == "underpaid"
    assert fake_db.calls == []
    assert any(str(plan.rubles) in text for text in alerts)


async def test_commission_does_not_turn_a_payment_into_underpayment(credited):
    """ЮMoney берёт комиссию: до 10% недостачи — всё ещё честная оплата."""
    fake_db, _, _ = credited
    assert await payment.credit_transfer(None, notice(amount="560.00")) == "ok"
    assert fake_db.calls


async def test_missing_service_alerts_instead_of_silence(credited, monkeypatch):
    """Сервис удалили, а деньги пришли: вернуть их можно только руками."""
    _, alerts, _ = credited
    monkeypatch.setattr(payment, "db", FakeDB(service=False))
    assert await payment.credit_transfer(None, notice()) == "no-service"
    assert alerts


# ── Эндпоинт ─────────────────────────────────────────────────────────────────
#
# TestClient без контекстного менеджера: lifespan не запускается, база не
# нужна — всё, что проверяется здесь, срабатывает до обращения к ней.

@pytest.fixture
def client(monkeypatch):
    from starlette.testclient import TestClient

    import app as app_module

    async def _credited(_bot, _notice):
        return "ok"

    monkeypatch.setattr(app_module.config, "YOOMONEY_NOTIFY_SECRET", SECRET)
    monkeypatch.setattr(app_module.payment, "credit_transfer", _credited)
    return TestClient(app_module.app)


def test_endpoint_accepts_a_signed_notification(client):
    response = client.post("/yoomoney/notify", data=form())
    assert response.status_code == 200


def test_endpoint_refuses_a_forged_notification(client):
    data = form()
    data["sha1_hash"] = "0" * 40
    assert client.post("/yoomoney/notify", data=data).status_code == 403


def test_endpoint_is_closed_without_a_secret(monkeypatch):
    """
    Пустая переменная не должна означать открытый приём: иначе стенд, где её
    забыли заполнить, продлевает подписку любому, кто пришлёт форму.
    """
    from starlette.testclient import TestClient

    import app as app_module

    monkeypatch.setattr(app_module.config, "YOOMONEY_NOTIFY_SECRET", "")
    response = TestClient(app_module.app).post("/yoomoney/notify", data=form())
    assert response.status_code == 404
