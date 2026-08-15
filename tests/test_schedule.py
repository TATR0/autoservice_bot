"""Тесты расписания в слое БД. Идут против настоящей базы."""

from datetime import datetime, time, timedelta, timezone

import asyncpg
import pytest

import config
from database import db

pytestmark = pytest.mark.asyncio


async def test_new_service_gets_default_schedule(service):
    """Сервис без расписания не должен существовать: иначе запись невозможна."""
    row = await db.get_schedule(service)
    assert row is not None
    assert row["work_from"] == time(9)
    assert row["work_to"] == time(18)
    assert row["slot_minutes"] == 60
    assert list(row["weekdays"]) == [1, 2, 3, 4, 5]
    assert row["capacity"] == 1


async def test_update_schedule_saves_fields(service):
    updated = await db.update_schedule(
        service, work_from=time(8), work_to=time(20), capacity=3
    )
    assert updated["work_from"] == time(8)
    assert updated["capacity"] == 3

    reread = await db.get_schedule(service)
    assert reread["work_to"] == time(20)


async def test_update_schedule_clears_lunch(service):
    await db.update_schedule(service, lunch_from=time(13), lunch_to=time(14))
    cleared = await db.update_schedule(service, lunch_from=None, lunch_to=None)
    assert cleared["lunch_from"] is None


async def test_update_schedule_rejects_reversed_hours(service):
    """Констрейнт — последняя линия обороны, даже если валидатор обойдут."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await db.update_schedule(service, work_from=time(20), work_to=time(8))


async def test_taken_slots_counts_live_requests(service, make_request):
    moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    await make_request(service, moment)
    await make_request(service, moment)

    taken = await db.get_taken_slots(
        service, moment - timedelta(hours=1), moment + timedelta(hours=1)
    )
    assert taken[moment] == 2


async def test_cancelled_request_frees_the_slot(service, make_request):
    moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    request = await make_request(service, moment)
    await db.update_request_status(
        str(request["idrequests"]), "cancelled",
        changed_by=None, allowed_from=config.STATUS_TRANSITIONS["cancelled"],
    )

    taken = await db.get_taken_slots(
        service, moment - timedelta(hours=1), moment + timedelta(hours=1)
    )
    assert taken.get(moment, 0) == 0


async def test_done_request_keeps_the_slot(service, make_request):
    """Выполненная заявка — история, это время больше не продаётся."""
    moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    request = await make_request(service, moment)
    # Прямо из «новой» в «выполнена» нельзя — так устроен STATUS_TRANSITIONS
    for status in ("accepted", "done"):
        await db.update_request_status(
            str(request["idrequests"]), status,
            changed_by=None, allowed_from=config.STATUS_TRANSITIONS[status],
        )

    taken = await db.get_taken_slots(
        service, moment - timedelta(hours=1), moment + timedelta(hours=1)
    )
    assert taken[moment] == 1
