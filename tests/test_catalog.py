"""Тесты каталога услуг — идут против настоящей базы (см. tests/conftest.py)."""

import config
from database import db


async def test_new_service_gets_default_catalog(service):
    items = await db.get_catalog(service)
    assert [i["title"] for i in items] == list(config.DEFAULT_SERVICE_TITLES)
    assert all(i["idrecstatus"] == 0 for i in items)
