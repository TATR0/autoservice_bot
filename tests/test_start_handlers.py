"""Переход по ссылке сервиса. Базы не требуют — соседи подменены."""

from datetime import datetime, timedelta, timezone

import pytest

import handlers.start as start

pytestmark = pytest.mark.asyncio


class FakeMessage:
    def __init__(self):
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


def _service(paid_until):
    return {
        "idservice": "11111111-1111-1111-1111-111111111111",
        "service_name": "Тест",
        "service_number": "+79990000000",
        "city": "Тестоград",
        "location_service": "ул. Тестовая, 1",
        "paid_until": paid_until,
    }


@pytest.fixture
def expired(monkeypatch):
    async def _get(_id):
        return _service(datetime.now(timezone.utc) - timedelta(days=1))

    monkeypatch.setattr(start.db, "get_service", _get)
    monkeypatch.setattr(start.kb, "webapp_url", lambda _id: "https://example.test/app")


async def test_expired_service_link_offers_the_phone(expired):
    """Онлайн-записи нет — но заказ сервис получить должен, голосом."""
    message = FakeMessage()
    await start._handle_service_link(message, "11111111-1111-1111-1111-111111111111")

    text = "\n".join(message.answers)
    assert "+79990000000" in text
    assert "подписк" not in text.lower() and "оплат" not in text.lower()


async def test_expired_service_link_does_not_open_the_form(expired):
    message = FakeMessage()
    await start._handle_service_link(message, "11111111-1111-1111-1111-111111111111")
    assert "Вы открыли форму записи" not in "\n".join(message.answers)
