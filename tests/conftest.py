"""
Общие фикстуры.

Тесты слоя БД идут против настоящей базы из DATABASE_URL: подделывать asyncpg
моками бессмысленно — проверяем мы как раз поведение SQL (частичные уникальные
индексы, условный UPDATE). Без DATABASE_URL такие тесты пропускаются.
"""

import uuid
import warnings

import pytest
import pytest_asyncio

import config
from database import db

# Telegram ID, которого нет у живых пользователей
TEST_OWNER_ID = 999_000_001


@pytest.fixture(autouse=True)
def subscription_enforced(monkeypatch):
    """
    Механизм подписки проверяется во включённом виде, каким он поедет в бой.

    В .env он пока выключен: платить нечем, отключать некого. Если бы тесты
    брали это значение, вся проверка гейтов молча превратилась бы в проверку
    того, что гейтов нет. Выключенное состояние проверяется отдельно и явно.
    """
    monkeypatch.setattr(config, "SUBSCRIPTION_ENFORCED", True)


@pytest.fixture(scope="session", autouse=True)
def warn_when_tests_share_the_live_database():
    """
    Тесты чистят за собой настоящим DELETE — в боевой базе это вопрос времени.

    Молча падать без TEST_DATABASE_URL нельзя: у проекта уже есть прогоны в
    общей базе, и запретить их сегодня значит оставить сюиту без единого
    зелёного прогона. Поэтому предупреждение, а не отказ.
    """
    if not config.TEST_DATABASE_URL and config.DATABASE_URL:
        warnings.warn(
            "TEST_DATABASE_URL не задан: тесты идут в ту же базу, что и бот. "
            "Заведите отдельную базу и примените к ней schema.sql.",
            stacklevel=1,
        )


@pytest_asyncio.fixture
async def db_ready():
    dsn = config.TEST_DATABASE_URL or config.DATABASE_URL
    if not dsn:
        pytest.skip("Ни TEST_DATABASE_URL, ни DATABASE_URL не заданы — тесты БД пропущены")
    await db.connect(dsn)
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


@pytest_asyncio.fixture
async def make_request(db_ready):
    """Заявка на конкретное время. Услуга создаётся своя, чтобы не мешать тестам."""
    async def _make(
        idservice: str,
        moment,
        *,
        client_uid: str | None = None,
        client_tg_id: int = TEST_OWNER_ID,
    ):
        item = await db.add_catalog_item(idservice, f"Работа {uuid.uuid4().hex[:6]}")
        request, _ = await db.create_request(
            idservice=idservice,
            client_tg_id=client_tg_id,
            client_name="Тест",
            phone="+79990000000",
            brand="Toyota",
            model="Camry",
            plate="А777АА777",
            services=[{
                "idcatalog": str(item["idcatalog"]),
                "title": item["title"],
                "price_rub": item["price_rub"],
            }],
            comment="",
            scheduled_at=moment,
            client_uid=client_uid or str(uuid.uuid4()),
        )
        return request
    return _make
