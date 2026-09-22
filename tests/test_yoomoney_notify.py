"""
Уведомление ЮMoney о переводе: подпись, разбор и зачисление дней.

Ни сети, ни базы: слой БД и рассылка подменены. Подпись считается в тесте
руками по строке из документации ЮMoney — иначе проверялось бы лишь то, что
код сходится сам с собой, и перестановка полей осталась бы незамеченной.
"""

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qsl, quote, urlencode

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


def signed(**overrides) -> dict:
    """
    Уведомление с действующей подписью sign.

    Набор полей списан с боевого стенда: sha1_hash ЮMoney больше не шлёт,
    зато появились bill_id и operation_label — а подпись считается по всем
    полям подряд, так что лишнее поле ломает её так же, как переставленное.
    """
    data = {
        "notification_type": "p2p-incoming",
        "operation_id": OPERATION,
        "amount": "590.00",
        "currency": "643",
        "datetime": "2026-09-20T12:00:00Z",
        "sender": "410011112222333",
        "codepro": "false",
        "label": f"sub:{SERVICE_ID}:30",
        "bill_id": "",
        "operation_label": "8433227333052831",
        "test_notification": "false",
    }
    data.update(overrides)
    data["sign"] = expected_sign(data)
    return data


def expected_sign(data: dict) -> str:
    """
    Подпись по документации: все параметры, кроме sign, по алфавиту, в виде
    «ключ=значение» через «&», значения в URL-кодировке; HMAC-SHA256 в HEX.
    """
    base = "&".join(
        f"{key}={quote(str(value), safe='')}"
        for key, value in sorted(data.items()) if key != "sign"
    )
    return hmac.new(
        SECRET.encode("utf-8"), base.encode("utf-8"), hashlib.sha256,
    ).hexdigest()


# ── Подпись ──────────────────────────────────────────────────────────────────

def test_current_signature_is_hmac_over_every_field():
    data = signed()
    assert yoomoney.hmac_signature(data, SECRET) == data["sign"]


def test_notification_signed_the_new_way_is_accepted():
    """Действующая подпись ЮMoney — sign, и деньги приходят именно с ней."""
    notice = yoomoney.parse_notification(signed(), SECRET)
    assert notice.operation_id == OPERATION


def test_extra_field_is_part_of_the_current_signature():
    """
    sign считается по всем полям, а не по списку: подменить operation_label
    или bill_id по дороге нельзя, даже если сами мы их не читаем.
    """
    data = signed()
    data["operation_label"] = "другая"
    with pytest.raises(yoomoney.NotificationError):
        yoomoney.parse_notification(data, SECRET)


def test_forged_sign_is_rejected():
    data = signed()
    data["sign"] = "0" * 64
    with pytest.raises(yoomoney.NotificationError):
        yoomoney.parse_notification(data, SECRET)


def test_changed_sum_breaks_the_current_signature():
    data = signed()
    data["amount"] = "1.00"
    with pytest.raises(yoomoney.NotificationError):
        yoomoney.parse_notification(data, SECRET)


def test_plus_sent_as_is_does_not_break_the_signature():
    """
    Разбор формы обращает плюс в пробел, а подписан он был плюсом — так
    приходит смещение часового пояса. Считать только по разобранным
    значениям значит отвергать настоящие уведомления.
    """
    data = signed(datetime="2026-09-20T12:00:00+04:00")
    del data["sign"]
    # Тело, в котором значения оставлены как есть: плюс остался плюсом
    raw = "&".join(f"{key}={value}" for key, value in sorted(data.items()))
    raw += "&sign=" + hmac.new(
        SECRET.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256,
    ).hexdigest()

    form = dict(parse_qsl(raw, keep_blank_values=True))
    assert form["datetime"].endswith(" 04:00"), "плюс должен был стать пробелом"
    assert yoomoney.parse_notification(form, SECRET, raw).operation_id == OPERATION


def test_unsigned_notification_is_rejected():
    """Ни sign, ни sha1_hash — значит уведомление прислал кто угодно."""
    data = signed()
    del data["sign"]
    with pytest.raises(yoomoney.NotificationError):
        yoomoney.parse_notification(data, SECRET, urlencode(data))


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


# ── Объяснение несошедшейся подписи ──────────────────────────────────────────
#
# Эти проверки — про диагностику живой установки, а не про приём денег:
# принимает платёж по-прежнему одна документированная формула.

def body(data: dict) -> str:
    """Тело запроса так, как его шлёт ЮMoney: значения в процентах."""
    return urlencode(data)


def test_diagnosis_names_undecoded_values():
    """
    Если ЮMoney подписывает значения до кодирования, наш разбор формы даёт
    другую строку — и владелец должен прочесть об этом словами, а не гадать.
    """
    data = form()
    # Подпись, посчитанная по сырым значениям: двоеточия метки в процентах
    encoded = {k: quote(str(v), safe="") for k, v in data.items()}
    data["sha1_hash"] = yoomoney.signature(encoded, SECRET)

    assert "не раскодированы" in yoomoney.diagnose(data, body(data), SECRET)


def test_diagnosis_names_a_missing_signature():
    """
    Установка, настроенная по старой документации, ждёт sha1_hash и получает
    отказ на каждом переводе. Имя поля в журнале — это вся разгадка.
    """
    data = signed()
    del data["sign"]
    assert "ни sign" in yoomoney.diagnose(data, body(data), SECRET)


def test_diagnosis_admits_when_it_has_no_answer():
    """Выдуманная подпись объяснения не имеет, и придумывать его нельзя."""
    data = form()
    data["sha1_hash"] = "0" * 40
    assert yoomoney.diagnose(data, body(data), SECRET) == ""


def test_diagnosis_says_when_the_signature_was_fine():
    """
    Уведомление отвергают не только подписью: без operation_id оно тоже не
    наше. Свалить это на подпись значит отправить владельца искать секрет,
    с которым всё в порядке.
    """
    data = form()
    assert "сошлась" in yoomoney.diagnose(data, body(data), SECRET)


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


def test_endpoint_accepts_the_current_signature(client):
    """Ровно то, что приходит с боевого стенда: sign и ни одного sha1_hash."""
    assert client.post("/yoomoney/notify", data=signed()).status_code == 200


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
