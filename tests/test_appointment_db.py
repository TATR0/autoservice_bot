"""
Выборка напоминаний о записи. Идёт против настоящей базы.

Проверяется, кого выборка берёт и кого молча пропускает: ошибка здесь
означает либо разбуженного посреди ночи клиента, либо тишину вместо
напоминания — и то и другое видно только в бою.
"""

from datetime import datetime, timedelta, timezone

import pytest

import config
from database import db
from tests.conftest import TEST_OWNER_ID

pytestmark = pytest.mark.asyncio

LEAD = 3


async def _backdate(idrequests: str, hours: int = 24) -> None:
    """
    Отодвинуть момент приёма заявки в прошлое.

    Выборка не напоминает о записи, выбранной только что: свежая заявка в
    тесте иначе не прошла бы отбор по причине, к самому тесту не относящейся.
    """
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE requests SET createdate = now() - make_interval(hours => $2)"
            " WHERE idrequests=$1",
            idrequests, hours,
        )


async def _ids(lead: int = LEAD) -> set[str]:
    rows = await db.requests_for_appointment_reminders(lead)
    return {str(row["idrequests"]) for row in rows}


async def test_booking_inside_the_window_is_due(service, make_request):
    req = await make_request(service, datetime.now(timezone.utc) + timedelta(hours=2))
    await _backdate(str(req["idrequests"]))
    assert str(req["idrequests"]) in await _ids()


async def test_booking_beyond_the_window_waits(service, make_request):
    """Напоминание за сутки — это не напоминание, а ещё одно письмо в чате."""
    req = await make_request(service, datetime.now(timezone.utc) + timedelta(hours=30))
    await _backdate(str(req["idrequests"]), hours=48)
    assert str(req["idrequests"]) not in await _ids()


async def test_a_just_made_booking_is_not_reminded(service, make_request):
    """Человек сам выбрал ближайшее окно минуту назад — напоминать не о чем."""
    req = await make_request(service, datetime.now(timezone.utc) + timedelta(hours=2))
    assert str(req["idrequests"]) not in await _ids()


async def test_past_booking_is_not_reminded(service, make_request):
    """Время прошло. Письмо «не забудьте приехать» тут только оскорбит."""
    req = await make_request(service, datetime.now(timezone.utc) - timedelta(hours=1))
    await _backdate(str(req["idrequests"]))
    assert str(req["idrequests"]) not in await _ids()


async def test_cancelled_booking_is_not_reminded(service, make_request):
    req = await make_request(service, datetime.now(timezone.utc) + timedelta(hours=2))
    await _backdate(str(req["idrequests"]))
    await db.update_request_status(
        str(req["idrequests"]),
        "cancelled",
        changed_by=TEST_OWNER_ID,
        allowed_from=config.STATUS_TRANSITIONS["cancelled"],
    )
    assert str(req["idrequests"]) not in await _ids()


async def test_blocked_client_is_skipped(service, make_request):
    """
    Заблокировавшему бота письмо не дойдёт, а право на него сгорит.

    Пусть лучше выборка его не берёт: вернётся — получит напоминание.
    """
    req = await make_request(service, datetime.now(timezone.utc) + timedelta(hours=2))
    await _backdate(str(req["idrequests"]))
    await db.upsert_user(TEST_OWNER_ID, username=None, first_name="Тест", last_name=None)
    await db.set_user_blocked(TEST_OWNER_ID, True)
    try:
        assert str(req["idrequests"]) not in await _ids()
    finally:
        await db.set_user_blocked(TEST_OWNER_ID, False)


async def test_right_to_remind_is_taken_once(service, make_request):
    """Два процесса с циклом рассылки не должны разбудить клиента дважды."""
    req = await make_request(service, datetime.now(timezone.utc) + timedelta(hours=2))
    assert await db.claim_appointment_reminder(str(req["idrequests"])) is True
    assert await db.claim_appointment_reminder(str(req["idrequests"])) is False


async def test_claimed_booking_leaves_the_selection(service, make_request):
    req = await make_request(service, datetime.now(timezone.utc) + timedelta(hours=2))
    await _backdate(str(req["idrequests"]))
    assert str(req["idrequests"]) in await _ids()
    await db.claim_appointment_reminder(str(req["idrequests"]))
    assert str(req["idrequests"]) not in await _ids()
