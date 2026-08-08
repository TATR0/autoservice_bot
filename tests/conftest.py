"""
Общие фикстуры.

Тесты слоя БД идут против настоящей базы из DATABASE_URL: подделывать asyncpg
моками бессмысленно — проверяем мы как раз поведение SQL (частичные уникальные
индексы, условный UPDATE). Без DATABASE_URL такие тесты пропускаются.
"""

import uuid

import pytest
import pytest_asyncio

import config
from database import db

# Telegram ID, которого нет у живых пользователей
TEST_OWNER_ID = 999_000_001


@pytest_asyncio.fixture
async def db_ready():
    if not config.DATABASE_URL:
        pytest.skip("DATABASE_URL не задан — тесты слоя БД пропущены")
    await db.connect()
    yield
    await db.close()


@pytest_asyncio.fixture
async def service(db_ready) -> str:
    """Временный сервис. Удаляется вместе с каталогом и заявками после теста."""
    idservice = await db.create_service(
        name=f"Тест {uuid.uuid4().hex[:8]}",
        phone="+79990000000",
        city="Тестоград",
        address="ул. Тестовая, 1",
        owner_tg_id=TEST_OWNER_ID,
    )
    yield idservice
    async with db.pool.acquire() as conn:
        # requests ссылается на services через ON DELETE SET NULL — чистим руками
        await conn.execute("DELETE FROM requests WHERE idservice=$1", idservice)
        await conn.execute("DELETE FROM services WHERE idservice=$1", idservice)
