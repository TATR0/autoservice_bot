"""
«Удалите мои данные». Базы не требует — слой БД подменён.

Проверяется, что удаление спрашивают, а не делают с первого слова, и что
человеку говорят правду о том, что именно стёрли.
"""

import pytest

import handlers.privacy as privacy

pytestmark = pytest.mark.asyncio

CLIENT_ID = 999_000_003


class FakeMessage:
    def __init__(self):
        self.from_user = type("User", (), {"id": CLIENT_ID})()
        self.answers = []
        self.edits = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs.get("reply_markup")))

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)


class FakeCallback:
    def __init__(self):
        self.from_user = type("User", (), {"id": CLIENT_ID})()
        self.message = FakeMessage()
        self.answered = []

    async def answer(self, text=None, **kwargs):
        self.answered.append(text)


@pytest.fixture
def forgotten(monkeypatch):
    """db.forget_client отвечает «обезличено 3, активных нет»."""
    calls = []

    async def _forget(client_tg_id):
        calls.append(client_tg_id)
        return 3, 0

    monkeypatch.setattr(privacy.db, "forget_client", _forget)
    return calls


async def test_deletion_is_asked_before_it_happens(forgotten):
    """
    Стереть свою историю случайным сообщением человек не должен: отменить
    это нельзя, а команду легко набрать не в тот чат.
    """
    message = FakeMessage()
    await privacy.forget_me(message)

    assert forgotten == [], "по одной команде ничего не стирается"
    text, markup = message.answers[0]
    assert markup is not None, "без кнопки подтверждения команда ничего не значит"
    assert "отмен" in text.lower(), "необратимость надо назвать до, а не после"


async def test_confirmation_wipes_and_reports(forgotten):
    callback = FakeCallback()
    await privacy.forget_confirm(callback)

    assert forgotten == [CLIENT_ID]
    assert callback.message.edits, "молчать после удаления нельзя"
    assert "3" in callback.message.edits[0], "сколько заявок обезличено — это ответ"


async def test_active_requests_stop_the_wipe(monkeypatch):
    """
    Человека ждут в сервисе завтра. Стереть телефон и машину сейчас — значит
    сорвать его же запись, а не защитить его данные.
    """
    async def _forget(_id):
        return 0, 2

    monkeypatch.setattr(privacy.db, "forget_client", _forget)
    callback = FakeCallback()
    await privacy.forget_confirm(callback)

    assert callback.message.edits
    said = callback.message.edits[0]
    assert "2" in said, "сколько заявок мешает — часть отказа"
    assert "✅" not in said, "ничего не удалено, галочка тут врёт"


async def test_failure_is_not_silence(monkeypatch):
    """Обрыв базы посреди удаления не должен выглядеть как выполненная просьба."""
    async def _boom(_id):
        raise RuntimeError("db connection lost")

    monkeypatch.setattr(privacy.db, "forget_client", _boom)
    callback = FakeCallback()
    await privacy.forget_confirm(callback)

    assert callback.message.edits, "человек обязан узнать, что не получилось"
    assert "✅" not in callback.message.edits[0]
