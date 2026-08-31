"""
Письма владельцу бота об авариях. Базы не требует — отправка подменена.

Проверяется адресация: кому уходит письмо и уходит ли оно вообще. Тексты
самих аварий проверяются там, где эти аварии случаются.
"""

import pytest

import config
import notifications

pytestmark = pytest.mark.asyncio


@pytest.fixture
def sent(monkeypatch):
    """Отправку подменяем на список: safe_send сам по себе проверен отдельно."""
    calls = []

    async def _send(_bot, chat_id, text, **kwargs):
        calls.append((chat_id, text))
        return True

    monkeypatch.setattr(notifications, "safe_send", _send)
    return calls


async def test_alert_reaches_every_owner(sent, monkeypatch):
    monkeypatch.setattr(config, "BOT_OWNER_IDS", (11, 22))
    assert await notifications.alert_owners(None, "Авария") == 2
    assert [chat_id for chat_id, _ in sent] == [11, 22]


async def test_alert_falls_back_to_the_master_chat(sent, monkeypatch):
    """
    Пустой BOT_OWNER_IDS — это не повод потерять письмо об аварии с деньгами.

    Мастер-чат заведён ровно для недоставленного, и это последний адрес,
    по которому вообще есть кому прочитать.
    """
    monkeypatch.setattr(config, "BOT_OWNER_IDS", ())
    monkeypatch.setattr(config, "MASTER_CHAT_ID", -100500)
    await notifications.alert_owners(None, "Авария")
    assert [chat_id for chat_id, _ in sent] == [-100500]


async def test_owners_win_over_the_master_chat(sent, monkeypatch):
    """
    Мастер-чат может оказаться группой, где сидят посторонние. Пока есть
    личный адрес владельца, дублировать туда незачем.
    """
    monkeypatch.setattr(config, "BOT_OWNER_IDS", (11,))
    monkeypatch.setattr(config, "MASTER_CHAT_ID", -100500)
    await notifications.alert_owners(None, "Авария")
    assert [chat_id for chat_id, _ in sent] == [11]


async def test_no_addresses_is_not_a_crash(sent, monkeypatch):
    """Некому писать — это настройка, а не исключение посреди обработки платежа."""
    monkeypatch.setattr(config, "BOT_OWNER_IDS", ())
    monkeypatch.setattr(config, "MASTER_CHAT_ID", 0)
    assert await notifications.alert_owners(None, "Авария") == 0
    assert sent == []


async def test_undelivered_alert_is_not_counted(monkeypatch):
    """Заблокировавший бота владелец не должен считаться оповещённым."""
    async def _fails(_bot, _chat_id, _text, **kwargs):
        return False

    monkeypatch.setattr(notifications, "safe_send", _fails)
    monkeypatch.setattr(config, "BOT_OWNER_IDS", (11, 22))
    assert await notifications.alert_owners(None, "Авария") == 0
