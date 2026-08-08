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
