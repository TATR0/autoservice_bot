"""
Регистрация сервиса. Базы не требует — слой БД и меню подменены.

Проверяется шаг услуг и последний шаг: сервис создан со списком услуг
управляющего, и об этом узнаёт владелец бота.
Проверка полей ввода живёт в test_validators.
"""

import pytest

import handlers.register as register

pytestmark = pytest.mark.asyncio

SERVICE_ID = "11111111-1111-1111-1111-111111111111"
OWNER_ID = 999_000_001


class FakeMessage:
    def __init__(self, text: str = "✅ Готово"):
        self.text = text
        self.from_user = type("User", (), {"id": OWNER_ID})()
        self.bot = object()
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


class FakeState:
    def __init__(self, data: dict):
        self._data = data

    async def get_data(self):
        return dict(self._data)

    async def set_state(self, _state):
        pass

    async def update_data(self, **kwargs):
        self._data.update(kwargs)


@pytest.fixture
def registered(monkeypatch):
    """Регистрация проходит успешно. Возвращает список писем владельцу бота."""
    alerts = []

    async def _create(**kwargs):
        created.append(kwargs)
        return SERVICE_ID

    async def _service(_id):
        return {
            "idservice": SERVICE_ID,
            "service_name": "Гараж №1",
            "service_number": "+79990000000",
            "city": "Тестоград",
            "location_service": "ул. Тестовая, 1",
            "timezone": "Europe/Moscow",
            "paid_until": None,
        }

    async def _user(_id):
        return None

    async def _alert(_bot, text):
        alerts.append(text)
        return 1

    async def _noop(*args, **kwargs):
        pass

    monkeypatch.setattr(register.db, "create_service", _create)
    monkeypatch.setattr(register.db, "get_service", _service)
    monkeypatch.setattr(register.db, "get_user", _user)
    monkeypatch.setattr(register, "alert_owners", _alert)
    monkeypatch.setattr(register, "set_active_service", _noop)
    monkeypatch.setattr(register, "show_main_menu", _noop)
    return alerts


created: list[dict] = []


@pytest.fixture(autouse=True)
def _clear_created():
    created.clear()


def _state(**extra):
    return FakeState({
        "name": "Гараж №1", "phone": "+79990000000", "city": "Тестоград",
        "address": "ул. Тестовая, 1",
        "items": [["Дубликат ключа", 1500, 60, 120], ["Прошивка чипа", None, None, None]],
        **extra,
    })


async def test_service_is_created_with_owners_own_services(registered):
    """Шаблона нет: в каталоге ровно то, что ввёл управляющий, с ценой и временем."""
    await register.reg_finish(FakeMessage(), _state())

    assert created[0]["address"] == "ул. Тестовая, 1"
    assert created[0]["catalog"] == [
        register.CatalogEntry("Дубликат ключа", 1500, (60, 120)),
        register.CatalogEntry("Прошивка чипа", None, None),
    ]


async def test_services_step_collects_title_price_and_duration(registered):
    state = FakeState({"items": []})
    await register.reg_item_title(FakeMessage("Дубликат ключа"), state)
    await register.reg_item_price(FakeMessage("1 500"), state)
    message = FakeMessage("1-2")
    await register.reg_item_duration(message, state)

    assert state._data["items"] == [["Дубликат ключа", 1500, 60, 120]]
    assert "Дубликат ключа — 1–2 ч, от 1 500 ₽" in message.answers[-1]


async def test_same_service_twice_is_refused(registered):
    state = FakeState({"items": [["Дубликат ключа", None, None, None]]})
    message = FakeMessage("дубликат ключа")
    await register.reg_item_title(message, state)

    assert "item_title" not in state._data
    assert message.answers[-1].startswith("❌")


async def test_new_service_is_announced_to_the_bot_owner(registered):
    """
    Регистрация — единственное событие, после которого в системе появляется
    чужой бизнес. Узнать о нём надо в тот же день, а не при разборе базы.
    """
    message = FakeMessage()
    await register.reg_finish(message, _state())

    assert len(registered) == 1, "новый сервис обязан быть замечен"
    alert = registered[0]
    assert "Гараж №1" in alert
    assert "Тестоград" in alert
    assert SERVICE_ID in alert, "без id сервиса продлить его не получится"
    assert str(OWNER_ID) in alert, "к кому идти с вопросами — часть новости"


async def test_failed_registration_is_not_announced(registered, monkeypatch):
    """Сервиса нет — и новости нет: иначе владелец пойдёт искать несуществующее."""
    async def _boom(**kwargs):
        raise RuntimeError("db connection lost")

    monkeypatch.setattr(register.db, "create_service", _boom)
    message = FakeMessage()
    await register.reg_finish(message, _state())

    assert registered == []
    assert message.answers and message.answers[0].startswith("❌")


async def test_announcement_failure_does_not_break_registration(registered, monkeypatch):
    """Сервис уже создан — упавшее письмо не повод показывать управляющему ошибку."""
    async def _boom(_bot, _text):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(register, "alert_owners", _boom)
    message = FakeMessage()
    await register.reg_finish(message, _state())

    assert message.answers, "управляющий обязан увидеть карточку сервиса"
    assert message.answers[0].startswith("✅")


# ── Второй сервис ────────────────────────────────────────────────────────────

from datetime import datetime, timedelta, timezone

import config


@pytest.fixture
def owner_has(monkeypatch):
    """Подменяет сервисы управляющего: передаются их сроки подписки."""
    def _set(*paid_until):
        async def _count(_owner):
            return len(paid_until)

        async def _owned(_owner):
            return [{"paid_until": p} for p in paid_until]

        monkeypatch.setattr(register.db, "count_owned_services", _count)
        monkeypatch.setattr(register.db, "get_owned_services", _owned)
    monkeypatch.setattr(config, "FREE_PLAN_SERVICE_LIMIT", 1)
    monkeypatch.setattr(config, "SUBSCRIPTION_ENFORCED", True)
    return _set


class StateSpy(FakeState):
    def __init__(self):
        super().__init__({})
        self.state = None

    async def set_state(self, state):
        self.state = state


async def test_second_service_is_sold_not_sent_to_support(owner_has):
    """Лишний сервис — ещё одна подписка: регистрация идёт, а не упирается в поддержку."""
    owner_has(datetime.now(timezone.utc) + timedelta(days=10))
    message, state = FakeMessage("/register_service"), StateSpy()
    await register.register_start(message, state)

    assert state.state == register.RegService.name
    assert "поддержк" not in message.answers[0]
    assert "после оплаты подписки" in message.answers[0]


async def test_unpaid_service_must_be_paid_first(owner_has):
    """Не больше одного неоплаченного сервиса, иначе брошенные регистрации копятся."""
    owner_has(datetime.now(timezone.utc) + timedelta(days=10), None)
    message, state = FakeMessage("/register_service"), StateSpy()
    await register.register_start(message, state)

    assert state.state is None
    assert "Оплатите" in message.answers[0]
    assert "поддержк" not in message.answers[0]


async def test_without_subscription_the_limit_still_goes_to_support(owner_has, monkeypatch):
    """Пока подписка не введена, платить нечем — остаётся поддержка."""
    owner_has(None)
    monkeypatch.setattr(config, "SUBSCRIPTION_ENFORCED", False)
    message, state = FakeMessage("/register_service"), StateSpy()
    await register.register_start(message, state)

    assert state.state is None
    assert "поддержк" in message.answers[0]
