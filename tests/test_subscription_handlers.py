"""Команда /extend. Базы не требуют — слой БД подменён."""

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


async def test_missing_service_is_reported(extended, monkeypatch):
    async def _none(*args, **kwargs):
        return None

    monkeypatch.setattr(handler.db, "extend_subscription", _none)
    message = FakeMessage(f"/extend {SERVICE_ID} 30", OWNER_ID)
    await handler.extend_command(message)
    assert "не найден" in " ".join(message.answers).lower()
