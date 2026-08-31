"""
Обезличивание заявок. Идёт против настоящей базы.

Проверяется, что чистка стирает ровно персональные данные и ровно там, где
должна: заявка остаётся историей загрузки сервиса, человек из неё исчезает.
"""

from datetime import datetime, timedelta, timezone

import pytest

from database import db

pytestmark = pytest.mark.asyncio

CLIENT_ID = 999_000_777
PII_FIELDS = ("client_name", "phone", "brand", "model", "plate", "comment")


async def _age(idrequests: str, *, days: int, status: str = "done") -> None:
    """Состарить заявку и закрыть её: настройка сцены, а не проверяемый код."""
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE requests SET createdate = now() - make_interval(days => $2),"
            " status=$3 WHERE idrequests=$1",
            idrequests, days, status,
        )


async def _row(idrequests: str):
    async with db.pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM requests WHERE idrequests=$1", idrequests
        )


@pytest.fixture
async def client_cleanup(db_ready):
    """Профиль тестового клиента после теста убираем: это фикстура, не данные."""
    yield CLIENT_ID
    async with db.pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE idusertg=$1", CLIENT_ID)


async def test_old_closed_request_loses_its_personal_data(service, make_request):
    req = await make_request(service, datetime.now(timezone.utc) + timedelta(days=1))
    await _age(str(req["idrequests"]), days=400)

    assert await db.anonymize_old_requests(365) >= 1
    row = await _row(str(req["idrequests"]))
    assert all(row[field] == "" for field in PII_FIELDS), "данные обязаны исчезнуть"
    assert row["idclienttg"] is None, "по Telegram id человека нашли бы и без имени"
    assert row["anonymized_at"] is not None
    assert row["seq"] is not None, "история загрузки сервиса остаётся"


async def test_a_recent_request_is_left_alone(service, make_request):
    req = await make_request(service, datetime.now(timezone.utc) + timedelta(days=1))
    await _age(str(req["idrequests"]), days=10)

    await db.anonymize_old_requests(365)
    assert (await _row(str(req["idrequests"])))["client_name"] == "Тест"


async def test_an_active_request_survives_any_age(service, make_request):
    """
    Годовалая заявка в работе — редкость, но человека по ней ещё ждут.
    Стереть его телефон значит сорвать работу, а не защитить данные.
    """
    req = await make_request(service, datetime.now(timezone.utc) + timedelta(days=1))
    await _age(str(req["idrequests"]), days=400, status="new")

    await db.anonymize_old_requests(365)
    assert (await _row(str(req["idrequests"])))["phone"] != ""


async def test_anonymizing_twice_keeps_the_first_moment(service, make_request):
    """Повторный круг чистки не должен переписывать отметку: она — дата события."""
    req = await make_request(service, datetime.now(timezone.utc) + timedelta(days=1))
    await _age(str(req["idrequests"]), days=400)

    await db.anonymize_old_requests(365)
    first = (await _row(str(req["idrequests"])))["anonymized_at"]
    await db.anonymize_old_requests(365)
    assert (await _row(str(req["idrequests"])))["anonymized_at"] == first


async def test_forget_client_wipes_requests_and_profile(
    service, make_request, client_cleanup
):
    req = await make_request(
        service,
        datetime.now(timezone.utc) + timedelta(days=1),
        client_tg_id=CLIENT_ID,
    )
    await _age(str(req["idrequests"]), days=1)
    await db.upsert_user(CLIENT_ID, username="ivan", first_name="Иван")
    await db.set_user_phone(CLIENT_ID, "+79990000001")

    anonymized, active = await db.forget_client(CLIENT_ID)
    assert (anonymized, active) == (1, 0)

    row = await _row(str(req["idrequests"]))
    assert row["client_name"] == "" and row["idclienttg"] is None
    user = await db.get_user(CLIENT_ID)
    assert user["phone"] is None and user["username"] is None


async def test_forget_client_refuses_while_a_request_is_active(
    service, make_request, client_cleanup
):
    """Отказ обязан быть полным: наполовину стёртый человек — худшее из двух."""
    req = await make_request(
        service,
        datetime.now(timezone.utc) + timedelta(days=1),
        client_tg_id=CLIENT_ID,
    )
    assert await db.forget_client(CLIENT_ID) == (0, 1)
    assert (await _row(str(req["idrequests"])))["client_name"] == "Тест"
