"""Тесты каталога услуг — идут против настоящей базы (см. tests/conftest.py)."""

import asyncio
import uuid

import pytest

import config
from database import ForeignClientUid, db
from handlers.requests import RequestRejected, create_request_flow


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


async def test_create_request_flow_rejects_foreign_catalog_item(service, db_ready):
    """Единственная защита от подстановки чужой услуги — проверка внутри
    create_request_flow, а не в форме на клиенте. Дёргаем сам флоу, а не
    db.create_request напрямую, иначе эта защита остаётся непокрытой."""
    other = await db.create_service(
        name="Тест чужой 2", phone="+79990000004", city="Тестоград",
        address="ул. Чужая, 4", owner_tg_id=999_000_004,
    )
    try:
        foreign = (await db.get_catalog(other))[0]
        payload = {
            "service_id": service,
            "idcatalog": str(foreign["idcatalog"]),
            "client_name": "Иван Тестов",
            "phone": "+79990000004",
            "brand": "Toyota",
            "model": "Camry",
            "plate": "А777АА777",
            "urgency": "low",
            "comment": "",
            "consent": True,
        }
        with pytest.raises(RequestRejected):
            await create_request_flow(None, client_tg_id=999_000_004, payload=payload)
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM services WHERE idservice=$1", other)


async def test_create_request_flow_rejects_deleted_own_catalog_item(service):
    """Та же защита срабатывает и на мягко удалённую услугу своего сервиса —
    клиент мог держать форму открытой, пока управляющий убрал услугу."""
    item = (await db.get_catalog(service))[0]
    await db.delete_catalog_item(service, str(item["idcatalog"]))

    payload = {
        "service_id": service,
        "idcatalog": str(item["idcatalog"]),
        "client_name": "Иван Тестов",
        "phone": "+79990000005",
        "brand": "Toyota",
        "model": "Camry",
        "plate": "А777АА777",
        "urgency": "low",
        "comment": "",
        "consent": True,
    }
    with pytest.raises(RequestRejected):
        await create_request_flow(None, client_tg_id=999_000_005, payload=payload)


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


async def test_delete_last_two_concurrently_only_one_succeeds(service):
    """
    Регрессия на write skew: без блокировки строк подзапрос-подсчёт
    в двух параллельных транзакциях мог не увидеть чужой незакоммиченный
    UPDATE, обе проверки «услуга не последняя» проходили одновременно,
    и сервис оставался вовсе без активных услуг.

    Сам race window — доли миллисекунды, поэтому одной попытки мало, чтобы
    надёжно поймать регрессию: повторяем гонку несколько раз подряд,
    возвращая обе услуги активными между попытками.
    """
    items = await db.get_catalog(service)
    keep_ids = [item["idcatalog"] for item in items[:2]]
    drop_ids = [item["idcatalog"] for item in items[2:]]
    async with db.pool.acquire() as conn:
        await conn.executemany(
            "UPDATE service_catalog SET idrecstatus=-1, deletedate=now() WHERE idcatalog=$1",
            [(idcatalog,) for idcatalog in drop_ids],
        )

    # Прогреваем пул двумя соединениями заранее: иначе одна из двух попыток
    # ниже тратит время на установление нового TCP/TLS-соединения и де-факто
    # стартует уже после того, как первая успела закоммититься — гонка не
    # воспроизводится не из-за корректности SQL, а из-за задержки коннекта.
    async with db.pool.acquire(), db.pool.acquire():
        pass

    for _ in range(30):
        results = await asyncio.gather(
            db.delete_catalog_item(service, str(keep_ids[0])),
            db.delete_catalog_item(service, str(keep_ids[1])),
        )
        succeeded = [r for r in results if r is not None]
        assert len(succeeded) == 1
        assert len(await db.get_catalog(service)) == 1

        async with db.pool.acquire() as conn:
            await conn.executemany(
                "UPDATE service_catalog SET idrecstatus=0, deletedate=NULL WHERE idcatalog=$1",
                [(idcatalog,) for idcatalog in keep_ids],
            )


async def test_count_requests_by_catalog_zero_for_fresh_item(service):
    item = (await db.get_catalog(service))[0]
    assert await db.count_requests_by_catalog(service, str(item["idcatalog"])) == 0


async def test_count_requests_by_catalog_counts_only_matching_item(service):
    """Проверяем реальный подсчёт, а не то, что запрос всегда возвращает 0:
    заявка привязана к одной услуге каталога — счётчик другой услуги того же
    сервиса должен остаться нулевым."""
    target, other = (await db.get_catalog(service))[:2]
    async with db.pool.acquire() as conn:
        idrequest = await conn.fetchval(
            """
            INSERT INTO requests (idservice, client_name, phone, idcatalog, service_title)
            VALUES ($1,$2,$3,$4,$5)
            RETURNING idrequests
            """,
            service, "Тест Тестов", "+79990000003",
            target["idcatalog"], target["title"],
        )
    try:
        assert await db.count_requests_by_catalog(service, str(target["idcatalog"])) == 1
        assert await db.count_requests_by_catalog(service, str(other["idcatalog"])) == 0
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM requests WHERE idrequests=$1", idrequest)


async def test_request_stores_title_snapshot(service):
    """Название услуги сохраняется в заявке — удаление услуги не ломает историю."""
    item = (await db.get_catalog(service))[0]

    request, is_duplicate = await db.create_request(
        idservice=service,
        client_tg_id=999_000_003,
        client_name="Иван Тестов",
        phone="+79990000003",
        brand="Toyota",
        model="Camry",
        plate="А777АА777",
        idcatalog=str(item["idcatalog"]),
        service_title=item["title"],
        urgency="low",
        comment="",
    )
    assert not is_duplicate
    assert request["service_title"] == item["title"]
    assert str(request["idcatalog"]) == str(item["idcatalog"])

    await db.delete_catalog_item(service, str(item["idcatalog"]))
    again = await db.get_request(str(request["idrequests"]))
    assert again["service_title"] == item["title"]


# ── Безопасность ─────────────────────────────────────────────────────────────

async def test_foreign_client_uid_is_rejected(service):
    """Чужой client_uid не должен возвращать чужую заявку."""
    item = (await db.get_catalog(service))[0]
    shared_uid = "uid-" + uuid.uuid4().hex

    await db.create_request(
        idservice=service, client_tg_id=999_000_010, client_name="Первый клиент",
        phone="+79990000010", brand="Toyota", model="Camry", plate="А111АА11",
        idcatalog=str(item["idcatalog"]), service_title=item["title"],
        urgency="low", comment="", client_uid=shared_uid,
    )

    # Второй клиент подставляет чужой client_uid
    with pytest.raises(ForeignClientUid):
        await db.create_request(
            idservice=service, client_tg_id=999_000_011, client_name="Второй клиент",
            phone="+79990000011", brand="Kia", model="Rio", plate="В222ВВ22",
            idcatalog=str(item["idcatalog"]), service_title=item["title"],
            urgency="low", comment="", client_uid=shared_uid,
        )


async def test_own_client_uid_still_deduplicates(service):
    """Повторный тап «Отправить» у самого клиента по-прежнему не плодит дубли."""
    item = (await db.get_catalog(service))[0]
    uid = "uid-" + uuid.uuid4().hex
    fields = dict(
        idservice=service, client_tg_id=999_000_012, client_name="Клиент",
        phone="+79990000012", brand="Lada", model="Vesta", plate="С333СС33",
        idcatalog=str(item["idcatalog"]), service_title=item["title"],
        urgency="low", comment="", client_uid=uid,
    )
    first, dup_first = await db.create_request(**fields)
    second, dup_second = await db.create_request(**fields)

    assert dup_first is False and dup_second is True
    assert str(first["idrequests"]) == str(second["idrequests"])


async def test_invite_token_is_not_stored_in_plaintext(service):
    """В базе лежит отпечаток приглашения, а сама ссылка работает."""
    token = await db.create_invite(service, 999_000_013)

    async with db.pool.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT token FROM admin_invites WHERE idservice=$1", service
        )
    assert stored != token, "токен приглашения сохранён открытым текстом"
    assert len(stored) == 64, "ожидался sha256-отпечаток"

    assert await db.get_valid_invite(token) is not None
    assert await db.get_valid_invite("подобранный-токен") is None
