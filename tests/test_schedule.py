"""Тесты расписания в слое БД. Идут против настоящей базы."""

from datetime import datetime, time, timedelta, timezone

import uuid

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


async def test_narrowing_hours_under_lunch_hits_the_constraint(service):
    """
    Ради чего hours_finish сверяется с обедом: часы и обед правятся порознь, и
    сужение часов оставляет обед снаружи. База это ловит — но текстом драйвера,
    поэтому решение принимается выше, а сюда доходить не должно.
    """
    await db.update_schedule(service, lunch_from=time(13), lunch_to=time(14))
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await db.update_schedule(service, work_from=time(9), work_to=time(12))


async def test_taken_slots_groups_by_moment(service, make_request):
    """Занятость привязана к своему моменту и не приписывается соседнему окну."""
    moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    await make_request(service, moment)

    taken = await db.get_taken_slots(
        service, moment - timedelta(hours=2), moment + timedelta(hours=2)
    )
    assert taken[moment] == 1
    assert moment + timedelta(hours=1) not in taken


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


import asyncio

from database import SlotTaken


async def test_slot_capacity_is_enforced(service, make_request):
    moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    await make_request(service, moment)
    with pytest.raises(SlotTaken):
        await make_request(service, moment)


async def test_parallel_booking_gives_one_request(service, make_request):
    """
    Двое жмут «Отправить» на одно окно одновременно.

    Без блокировки оба проходят проверку занятости до того, как любой вставит
    строку, и одно место продаётся дважды.
    """
    moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=2)
    results = await asyncio.gather(
        make_request(service, moment),
        make_request(service, moment),
        return_exceptions=True,
    )
    rejected = [r for r in results if isinstance(r, SlotTaken)]
    created = [r for r in results if not isinstance(r, Exception)]
    assert len(created) == 1
    assert len(rejected) == 1


async def test_second_place_is_free_when_capacity_allows(service, make_request):
    await db.update_schedule(service, capacity=2)
    moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=3)
    await make_request(service, moment)
    await make_request(service, moment)  # не бросает

    taken = await db.get_taken_slots(
        service, moment - timedelta(hours=1), moment + timedelta(hours=1)
    )
    assert taken[moment] == 2


async def test_repeat_tap_returns_the_same_request(service, make_request):
    """Повторный тап «Отправить» не должен упираться в собственную же заявку."""
    moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=4)
    uid = str(uuid.uuid4())
    first = await make_request(service, moment, client_uid=uid)
    second = await make_request(service, moment, client_uid=uid)
    assert second["idrequests"] == first["idrequests"]
