"""Оплата звёздами. Базы не требует — слой БД и Telegram подменены."""

import logging
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


def test_non_uuid_id_is_rejected():
    """
    Мусор вместо UUID не должен доходить до asyncpg — там DataError,
    потому что services.idservice имеет тип uuid.
    """
    assert payment.parse_payload("sub:not-a-uuid:30") is None


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


async def test_non_uuid_payload_is_refused_before_touching_the_database(monkeypatch):
    """
    Мусорный id не должен доходить до db.get_service — там asyncpg роняет
    DataError, потому что колонка идентификатора типа uuid.
    """
    calls = []

    async def _track(idservice):
        calls.append(idservice)
        return {"idservice": SERVICE_ID}

    monkeypatch.setattr(payment.db, "get_service", _track)
    query = FakePreCheckout("sub:not-a-uuid:30")
    await payment.pre_checkout(query)
    ok, message = query.answers[0]
    assert ok is False
    assert message, "отказ без текста человек не поймёт"
    assert calls == [], "мусорный id не должен доходить до db.get_service"


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
        # Через message.bot хендлер зовёт письмо владельцу бота
        self.bot = object()
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
    assert message.answers[0].startswith("⚠️"), (
        "деньги списаны, а счёт не распознан — галочка тут врёт"
    )


@pytest.fixture
def applied_missing_service(monkeypatch):
    """apply_stars_payment не нашёл сервис — вернул None."""
    calls = []

    async def _apply(idservice, *, days, charge_id, payer_id):
        calls.append((idservice, days, charge_id, payer_id))
        return None

    monkeypatch.setattr(payment.db, "apply_stars_payment", _apply)
    return calls


async def test_applied_is_none_answers_and_does_not_crash(applied_missing_service, caplog):
    message = FakePaidMessage(payment.make_payload(SERVICE_ID, 30), charge_id="ch_none")
    with caplog.at_level(logging.ERROR):
        await payment.paid(message)

    assert applied_missing_service, "apply_stars_payment обязан быть вызван"
    assert message.answers, "человек обязан получить ответ"
    assert message.answers[0].startswith("⚠️"), (
        "деньги списаны, сервис не найден — галочка тут врёт"
    )
    assert "ch_none" in caplog.text, "charge_id обязан попасть в лог для разбора инцидента"


@pytest.fixture
def applied_raises(monkeypatch):
    """db.apply_stars_payment падает — например, обрыв связи с базой."""
    async def _apply(idservice, *, days, charge_id, payer_id):
        raise RuntimeError("db connection lost")

    monkeypatch.setattr(payment.db, "apply_stars_payment", _apply)


async def test_apply_stars_payment_failure_does_not_crash(applied_raises, caplog):
    """
    Деньги списаны, зачисление могло не пройти. Хендлер не должен упасть
    необработанным исключением, а лог обязан содержать charge_id для разбора.
    """
    message = FakePaidMessage(payment.make_payload(SERVICE_ID, 30), charge_id="ch_boom")
    with caplog.at_level(logging.ERROR):
        await payment.paid(message)

    assert message.answers, "молчать после аварии нельзя"
    assert message.answers[0].startswith("⚠️")
    assert "ch_boom" in caplog.text, "charge_id обязан попасть в лог для разбора инцидента"


async def test_missing_service_after_credit_confirms_without_name(applied, monkeypatch, caplog):
    """
    Дни начислены успешно, но get_service вернул None: имени и часового пояса
    нет — render.payment_done на них упал бы TypeError. Подтверждение обязано
    уйти без названия сервиса, без выдуманных заглушек.
    """
    async def _gone(_id):
        return None

    monkeypatch.setattr(payment.db, "get_service", _gone)
    message = FakePaidMessage(payment.make_payload(SERVICE_ID, 30), charge_id="ch_gone")
    with caplog.at_level(logging.ERROR):
        await payment.paid(message)

    assert applied == [(SERVICE_ID, 30, "ch_gone", 999_000_001)], (
        "дни обязаны быть начислены до того, как читаем сервис обратно"
    )
    assert message.answers, "подтверждение обязано уйти — дни уже начислены"
    assert message.answers[0].startswith("✅"), "начисление прошло полностью успешно"
    assert "Тест" not in message.answers[0], (
        "имени сервиса нет — заглушку вместо него не выдумываем"
    )
    assert "ch_gone" in caplog.text, "charge_id обязан попасть в лог для разбора инцидента"


async def test_unreadable_service_after_credit_is_not_called_a_delay(
    applied, monkeypatch, caplog
):
    """
    Дни начислены наверняка — упало только чтение сервиса ради текста.

    Сказать тут «зачисление задержалось» значит соврать человеку про его же
    деньги в единственном месте фичи, где они уже списаны.
    """
    async def _boom(_id):
        raise RuntimeError("db connection lost")

    monkeypatch.setattr(payment.db, "get_service", _boom)
    message = FakePaidMessage(payment.make_payload(SERVICE_ID, 30), charge_id="ch_read")
    with caplog.at_level(logging.ERROR):
        await payment.paid(message)

    assert applied == [(SERVICE_ID, 30, "ch_read", 999_000_001)], (
        "дни начислены до чтения сервиса — это и есть суть случая"
    )
    assert message.answers, "молчать после списания нельзя"
    assert "задерж" not in message.answers[0].lower(), (
        "зачисление состоялось, задержкой его называть нельзя"
    )
    assert message.answers[0].startswith("✅")
    assert "ch_read" in caplog.text, "charge_id обязан попасть в лог для разбора инцидента"


# ── Владелец бота узнаёт о деньгах без товара ────────────────────────────────
# Все три аварии ниже кончаются одним: человек заплатил, а дней не получил.
# Лог об этом знает, но в лог не смотрят без повода — а повод как раз в логе.


@pytest.fixture
def alerts(monkeypatch):
    """Письмо владельцу бота подменяем списком: адресация проверена в test_alerts."""
    texts = []

    async def _alert(_bot, text):
        texts.append(text)
        return 1

    monkeypatch.setattr(payment, "alert_owners", _alert)
    return texts


async def test_broken_payload_alerts_the_owner(applied, alerts):
    message = FakePaidMessage("мусор", charge_id="ch_junk")
    await payment.paid(message)
    assert len(alerts) == 1, "деньги списаны, счёт не распознан — владелец обязан узнать"
    assert "ch_junk" in alerts[0], "без id списания звёзды не вернуть"


async def test_credit_failure_alerts_the_owner(applied_raises, alerts):
    message = FakePaidMessage(payment.make_payload(SERVICE_ID, 30), charge_id="ch_boom")
    await payment.paid(message)
    assert len(alerts) == 1
    assert "ch_boom" in alerts[0]
    assert SERVICE_ID in alerts[0], "чинить придётся конкретному сервису"
    assert "30" in alerts[0], "сколько дней доначислить — часть задачи"


async def test_missing_service_alerts_the_owner(applied_missing_service, alerts):
    """Платёж за несуществующий сервис — единственный случай, где надо вернуть звёзды."""
    message = FakePaidMessage(payment.make_payload(SERVICE_ID, 30), charge_id="ch_none")
    await payment.paid(message)
    assert len(alerts) == 1
    assert "ch_none" in alerts[0]


async def test_successful_payment_does_not_alert(applied, alerts):
    """
    Успешная оплата — не авария. Письмо на каждую покупку приучает
    пролистывать письма, и настоящую аварию пролистают вместе с ними.
    """
    await payment.paid(FakePaidMessage(payment.make_payload(SERVICE_ID, 30)))
    assert alerts == []


async def test_alert_failure_does_not_cost_the_payer_an_answer(applied_raises, monkeypatch):
    """
    Владелец бота может заблокировать своего же бота, а Telegram — ответить
    ошибкой. Плательщик тут ни при чём: ответ ему уже нельзя не отправить.
    """
    async def _boom(_bot, _text):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(payment, "alert_owners", _boom)
    message = FakePaidMessage(payment.make_payload(SERVICE_ID, 30), charge_id="ch_x")
    await payment.paid(message)
    assert message.answers, "ответ плательщику важнее письма владельцу"
