"""
Регистрация сервиса. Базы не требует — слой БД и меню подменены.

Проверяется последний шаг: сервис создан, и об этом узнаёт владелец бота.
Проверка полей ввода живёт в test_validators.
"""

import pytest

import handlers.register as register

pytestmark = pytest.mark.asyncio

SERVICE_ID = "11111111-1111-1111-1111-111111111111"
OWNER_ID = 999_000_001


class FakeMessage:
    def __init__(self, text: str = "ул. Тестовая, 1"):
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


def _state():
    return FakeState({"name": "Гараж №1", "phone": "+79990000000", "city": "Тестоград"})


async def test_new_service_is_announced_to_the_bot_owner(registered):
    """
    Регистрация — единственное событие, после которого в системе появляется
    чужой бизнес. Узнать о нём надо в тот же день, а не при разборе базы.
    """
    message = FakeMessage()
    await register.reg_address(message, _state())

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
    await register.reg_address(message, _state())

    assert registered == []
    assert message.answers and message.answers[0].startswith("❌")


async def test_announcement_failure_does_not_break_registration(registered, monkeypatch):
    """Сервис уже создан — упавшее письмо не повод показывать управляющему ошибку."""
    async def _boom(_bot, _text):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(register, "alert_owners", _boom)
    message = FakeMessage()
    await register.reg_address(message, _state())

    assert message.answers, "управляющий обязан увидеть карточку сервиса"
    assert message.answers[0].startswith("✅")
