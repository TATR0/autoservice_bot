"""Тесты каталога услуг — идут против настоящей базы (см. tests/conftest.py)."""

import config
from database import db


async def test_new_service_gets_default_catalog(service):
    items = await db.get_catalog(service)
    assert [i["title"] for i in items] == list(config.DEFAULT_SERVICE_TITLES)
    assert all(i["idrecstatus"] == 0 for i in items)


async def test_get_catalog_item_returns_own_item(service):
    items = await db.get_catalog(service)
    found = await db.get_catalog_item(service, str(items[0]["idcatalog"]))
    assert found is not None
    assert found["title"] == items[0]["title"]


async def test_get_catalog_item_rejects_foreign_service(service, db_ready):
    """Услугу чужого сервиса подставить в заявку нельзя."""
    other = await db.create_service(
        name="Тест чужой", phone="+79990000001", city="Тестоград",
        address="ул. Чужая, 2", owner_tg_id=999_000_002,
    )
    try:
        foreign = (await db.get_catalog(other))[0]
        assert await db.get_catalog_item(service, str(foreign["idcatalog"])) is None
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM services WHERE idservice=$1", other)


async def test_add_catalog_item_creates_new(service):
    item = await db.add_catalog_item(service, "Полировка кузова")
    assert item is not None
    assert item["title"] == "Полировка кузова"
    assert len(await db.get_catalog(service)) == len(config.DEFAULT_SERVICE_TITLES) + 1


async def test_add_duplicate_returns_none_ignoring_case(service):
    assert await db.add_catalog_item(service, "  диагностика ") is None
    assert len(await db.get_catalog(service)) == len(config.DEFAULT_SERVICE_TITLES)


async def test_add_after_manual_deactivation_revives_same_row(service):
    """Воскрешение: услуга с тем же названием переиспользует старую строку,
    поэтому оформленные на неё заявки остаются слинкованы."""
    target = (await db.get_catalog(service))[0]
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE service_catalog SET idrecstatus=-1, deletedate=now() WHERE idcatalog=$1",
            target["idcatalog"],
        )

    revived = await db.add_catalog_item(service, target["title"])
    assert str(revived["idcatalog"]) == str(target["idcatalog"])
    assert revived["idrecstatus"] == 0


async def test_delete_is_soft(service):
    items = await db.get_catalog(service)
    removed = await db.delete_catalog_item(service, str(items[0]["idcatalog"]))
    assert removed is not None
    assert removed["idrecstatus"] == -1
    assert len(await db.get_catalog(service)) == len(items) - 1


async def test_cannot_delete_last_item(service):
    items = await db.get_catalog(service)
    for item in items[:-1]:
        assert await db.delete_catalog_item(service, str(item["idcatalog"])) is not None

    last = items[-1]
    assert await db.delete_catalog_item(service, str(last["idcatalog"])) is None
    assert len(await db.get_catalog(service)) == 1


async def test_delete_twice_returns_none(service):
    item = (await db.get_catalog(service))[0]
    assert await db.delete_catalog_item(service, str(item["idcatalog"])) is not None
    assert await db.delete_catalog_item(service, str(item["idcatalog"])) is None


async def test_count_requests_by_catalog_zero_for_fresh_item(service):
    item = (await db.get_catalog(service))[0]
    assert await db.count_requests_by_catalog(service, str(item["idcatalog"])) == 0
