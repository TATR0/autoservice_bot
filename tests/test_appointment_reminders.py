"""
Напоминание клиенту о записи. Базы не требует — слой БД подменён.

Проверяется круг рассылки: кому уходит письмо, кому не уходит и что
происходит, когда право на письмо уже занято. Выборку из базы проверяет
test_appointment_db, текст — test_render.
"""

from datetime import datetime, timedelta, timezone

import pytest

import config
import notifications

pytestmark = pytest.mark.asyncio

REQUEST_ID = "22222222-2222-2222-2222-222222222222"
CLIENT_ID = 999_000_002


def _request(idrequests: str = REQUEST_ID, **over):
    row = {
        "idrequests": idrequests,
        "seq": 142,
        "idclienttg": CLIENT_ID,
        "scheduled_at": datetime.now(timezone.utc) + timedelta(hours=2),
        "brand": "Toyota",
        "model": "Camry",
        "service_name": "Гараж №1",
        "timezone": "Europe/Moscow",
        "service_number": "+79990000000",
        "city": "Тестоград",
        "location_service": "ул. Тестовая, 1",
        "services_summary": "Замена масла",
    }
    row.update(over)
    return row


@pytest.fixture
def round_(monkeypatch):
    """Один круг рассылки: что отдала база, кому ушло письмо, что занято."""
    state = {"due": [_request()], "claimed": [], "sent": []}

    async def _due(lead_hours):
        state["lead"] = lead_hours
        return state["due"]

    async def _claim(idrequests):
        state["claimed"].append(idrequests)
        return idrequests not in state.get("taken", ())

    async def _send(_bot, chat_id, text, **kwargs):
        state["sent"].append((chat_id, text, kwargs.get("reply_markup")))
        return True

    monkeypatch.setattr(config, "APPOINTMENT_REMINDER_HOURS", 3)
    monkeypatch.setattr(notifications.db, "requests_for_appointment_reminders", _due)
    monkeypatch.setattr(notifications.db, "claim_appointment_reminder", _claim)
    monkeypatch.setattr(notifications, "safe_send", _send)
    return state


async def test_client_gets_the_reminder(round_):
    assert await notifications.send_appointment_reminders(None) == 1
    assert round_["lead"] == 3, "окно берётся из настройки, а не из кода рассылки"
    chat_id, text, markup = round_["sent"][0]
    assert chat_id == CLIENT_ID
    assert "Гараж №1" in text
    assert markup is not None, "отменить запись надо тут же, а не искать в меню"


async def test_right_to_send_is_claimed_before_sending(round_):
    """
    Иначе сбой отправки стоил бы клиенту письма на каждом тике: раз в час
    до самой записи.
    """
    await notifications.send_appointment_reminders(None)
    assert round_["claimed"] == [REQUEST_ID]


async def test_taken_reminder_is_not_repeated(round_):
    """Право занято другим процессом — письмо не дублируется."""
    round_["taken"] = (REQUEST_ID,)
    assert await notifications.send_appointment_reminders(None) == 0
    assert round_["sent"] == []


async def test_zero_hours_turns_reminders_off(round_, monkeypatch):
    """Ноль в настройке — это выключатель, а не «напомнить прямо сейчас»."""
    monkeypatch.setattr(config, "APPOINTMENT_REMINDER_HOURS", 0)
    assert await notifications.send_appointment_reminders(None) == 0
    assert round_["claimed"] == [], "выключенная рассылка в базу не ходит"


async def test_undelivered_reminder_is_not_counted(round_, monkeypatch):
    """Заблокировавший бота клиент письма не получил — считать его нельзя."""
    async def _fails(*args, **kwargs):
        return False

    monkeypatch.setattr(notifications, "safe_send", _fails)
    assert await notifications.send_appointment_reminders(None) == 0


async def test_every_due_request_is_handled(round_):
    other = "33333333-3333-3333-3333-333333333333"
    round_["due"] = [_request(), _request(other, seq=143)]
    assert await notifications.send_appointment_reminders(None) == 2
    assert round_["claimed"] == [REQUEST_ID, other]
