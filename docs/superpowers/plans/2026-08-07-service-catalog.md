# Справочник услуг сервиса — план реализации

> **Для агентов:** ОБЯЗАТЕЛЬНЫЙ САБ-СКИЛЛ: superpowers:subagent-driven-development
> (рекомендуется) либо superpowers:executing-plans — выполнять план задача за задачей.
> Шаги отмечаются чекбоксами (`- [ ]`).

**Спека:** `docs/superpowers/specs/2026-08-07-service-catalog-design.md`

**Цель:** управляющий автосервиса добавляет и удаляет услуги своего сервиса; клиент видит в форме записи именно этот список.

**Архитектура:** отдельная таблица `service_catalog` (одна строка = одна услуга одного сервиса, мягкое удаление через `idrecstatus`). Заявка хранит и ссылку `idcatalog`, и снимок названия `service_title` — благодаря снимку карточки заявок и статистика читаются без JOIN'ов, а удаление услуги не ломает историю. Редактирование живёт в боте, WebApp получает список услуг из API.

**Стек:** Python 3.12, aiogram 3.27, FastAPI, asyncpg, PostgreSQL (Supabase), pytest.

## Глобальные ограничения

- Весь текст интерфейса, комментарии и docstring'и — на русском языке.
- Удаления только мягкие: `idrecstatus = 0` активна, `-1` удалена. Жёсткого `DELETE` в `database.py` быть не должно ни в одном методе.
- `parse_mode=HTML`; любое пользовательское значение экранируется через `validators.h()`.
- Ни один SQL-запрос не пишется вне `database.py`. Единственное исключение — тестовые фикстуры в `tests/conftest.py`: они убирают за собой созданные записи жёстким `DELETE`, и заводить ради этого метод в `database.py` нельзя — им тут же кто-нибудь снесёт живой сервис вместо мягкого удаления.
- Права: править каталог может только управляющий (`services.owner_id`), проверка обязательна и в message-, и в callback-хендлерах.
- Лимиты: до 30 услуг на сервис, название 2–40 символов, в одном сервисе нет двух активных услуг с одинаковым названием (без учёта регистра).
- В сервисе всегда остаётся минимум одна активная услуга.
- `DATABASE_URL` уже настроен в `.env`; тесты слоя БД идут против этой же базы и убирают за собой созданные записи.

---

### Задача 1: Инфраструктура тестов и валидаторы

**Файлы:**
- Создать: `requirements-dev.txt`
- Создать: `pytest.ini`
- Создать: `tests/conftest.py`
- Создать: `tests/test_validators.py`
- Изменить: `validators.py` (добавить две функции в конец, перед `validate_request_fields`)

**Отклонение от спеки (осознанное):** спека говорила положить pytest в `requirements.txt`. Кладём в отдельный `requirements-dev.txt`, потому что `requirements.txt` ставится в продакшен-образ (`Dockerfile`) и на Render — тестовые зависимости там лишний вес.

**Интерфейсы:**
- Производит: `validators.validate_service_title(raw: object) -> str`, `validators.validate_uuid(raw: object, *, field: str) -> str`
- Производит: фикстуры `db_ready` и `service` в `tests/conftest.py` (используются задачами 3–7)

- [ ] **Шаг 1: Установить тестовые зависимости и записать точные версии**

```bash
pip install pytest pytest-asyncio
pip freeze | grep -Ei "^(pytest|pytest-asyncio)=="
```

Вывод последней команды — две строки вида `pytest==8.x.y`. Создать `requirements-dev.txt` ровно с этими двумя строками и шапкой:

```
# Только для разработки. В продакшен-образ не попадает — там requirements.txt.
# Установка:  pip install -r requirements-dev.txt
```

- [ ] **Шаг 2: Создать `pytest.ini`**

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

`asyncio_mode = auto` избавляет от `@pytest.mark.asyncio` над каждым тестом.

- [ ] **Шаг 3: Написать падающие тесты валидаторов**

Создать `tests/test_validators.py`:

```python
"""Тесты валидаторов справочника услуг."""

import pytest

from validators import ValidationError, validate_service_title, validate_uuid


def test_title_strips_and_collapses_spaces():
    assert validate_service_title("  Полировка   кузова  ") == "Полировка кузова"


def test_title_too_short_rejected():
    with pytest.raises(ValidationError):
        validate_service_title("П")


def test_title_truncated_to_40():
    # clean_text не бросает ошибку на длинном вводе, а обрезает — здесь так же
    assert len(validate_service_title("П" * 50)) == 40


def test_title_drops_control_chars():
    assert validate_service_title("Поли\x00ровка") == "Полировка"


def test_uuid_accepted():
    value = "3f8b6c1e-9d2a-4b7c-8e1f-0a5b6c7d8e9f"
    assert validate_uuid(value, field="Услуга") == value


def test_uuid_rejects_garbage():
    with pytest.raises(ValidationError):
        validate_uuid("'; DROP TABLE requests--", field="Услуга")


def test_uuid_rejects_empty():
    with pytest.raises(ValidationError):
        validate_uuid(None, field="Услуга")
```

- [ ] **Шаг 4: Запустить и убедиться, что тесты падают**

```bash
pytest tests/test_validators.py -v
```

Ожидается: `ImportError: cannot import name 'validate_service_title' from 'validators'`

- [ ] **Шаг 5: Добавить функции в `validators.py`**

Вставить перед `def validate_request_fields`. Заодно рядом с `_SPACES_RE` добавить регулярку:

```python
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
```

```python
def validate_uuid(raw: object, *, field: str) -> str:
    """
    Проверить UUID из недоверенного источника — тела запроса или callback_data.

    Без проверки asyncpg бросает DataError на мусорной строке, и вместо
    понятного отказа клиент получает 500.
    """
    value = str(raw or "").strip()
    if not _UUID_RE.match(value):
        raise ValidationError(f"«{field}»: некорректный идентификатор.")
    return value


def validate_service_title(raw: object) -> str:
    """Название услуги, которое вводит управляющий."""
    return clean_text(raw, field="Название услуги", min_len=2, max_len=40)
```

- [ ] **Шаг 6: Запустить тесты — должны пройти**

```bash
pytest tests/test_validators.py -v
```

Ожидается: 7 passed

- [ ] **Шаг 7: Создать `tests/conftest.py` с фикстурами для тестов БД**

```python
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
```

- [ ] **Шаг 8: Проверить, что conftest не ломает сборку тестов**

```bash
pytest --collect-only -q
```

Ожидается: 7 тестов собрано, ошибок импорта нет.

- [ ] **Шаг 9: Коммит**

```bash
git add requirements-dev.txt pytest.ini tests/ validators.py
git commit -m "Тесты: инфраструктура pytest, валидаторы названия услуги и uuid"
```

---

### Задача 2: Схема БД — таблица каталога, поля заявки, бэкфил

**Файлы:**
- Изменить: `schema.sql` (новый блок после блока «Администраторы», правки блока «Заявки клиентов», строка RLS в конце)

**Интерфейсы:**
- Производит: таблица `service_catalog (idcatalog, idservice, title, sort_order, idrecstatus, createdate, deletedate)`; колонки `requests.idcatalog`, `requests.service_title`

- [ ] **Шаг 1: Добавить блок каталога в `schema.sql`**

Вставить после блока `-- ── Администраторы ──` (после строки с `idx_admins_unique`):

```sql
-- ── Каталог услуг сервиса ────────────────────────────────────────────────
-- Список услуг у каждого сервиса свой: детейлинг-студии не нужны «Ремонт
-- двигателя» и «Коробка передач». При регистрации копируется шаблонный
-- набор, дальше управляющий правит его сам.
CREATE TABLE IF NOT EXISTS service_catalog (
    idcatalog   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    idservice   uuid        NOT NULL REFERENCES services(idservice) ON DELETE CASCADE,
    title       text        NOT NULL,
    sort_order  int         NOT NULL DEFAULT 100,
    idrecstatus smallint    NOT NULL DEFAULT 0, -- 0 активна / -1 удалена
    createdate  timestamptz NOT NULL DEFAULT now(),
    deletedate  timestamptz
);

CREATE INDEX IF NOT EXISTS idx_catalog_service
    ON service_catalog (idservice) WHERE idrecstatus = 0;

-- в одном сервисе не может быть двух активных услуг с одинаковым названием
CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_unique_title
    ON service_catalog (idservice, lower(trim(title))) WHERE idrecstatus = 0;
```

- [ ] **Шаг 2: Добавить колонки заявки**

В блоке `-- ── Заявки клиентов ──`, в существующий `ALTER TABLE requests ADD COLUMN IF NOT EXISTS ...` (тот, где `seq`, `client_uid`, `handled_by`, `updatedate`) дописать две строки:

```sql
    ADD COLUMN IF NOT EXISTS idcatalog     uuid REFERENCES service_catalog(idcatalog) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS service_title text NOT NULL DEFAULT '',
```

И индекс рядом с остальными индексами `requests`:

```sql
CREATE INDEX IF NOT EXISTS idx_requests_catalog ON requests (idcatalog);
```

Над колонкой `service_type` в `CREATE TABLE requests` добавить комментарий:

```sql
    service_type text        NOT NULL DEFAULT 'other', -- legacy: не читается приложением, ждёт удаления после проверки бэкфила
```

- [ ] **Шаг 3: Добавить бэкфил в конец `schema.sql`, перед блоком Row Level Security**

```sql
-- ── Бэкфил каталога услуг ────────────────────────────────────────────────
-- Идемпотентно: сервисам без каталога раздаётся шаблонный набор, старым
-- заявкам проставляются название услуги и ссылка на строку каталога.

INSERT INTO service_catalog (idservice, title, sort_order)
SELECT s.idservice, t.title, t.ord
  FROM services s
 CROSS JOIN (VALUES
        ('Диагностика',       10),
        ('Замена масла',      20),
        ('Шины и диски',      30),
        ('Тормозная система', 40),
        ('Ремонт двигателя',  50),
        ('Коробка передач',   60),
        ('Подвеска',          70),
        ('Кузовные работы',   80),
        ('Другое',            90)
      ) AS t(title, ord)
 WHERE s.idrecstatus = 0
   AND NOT EXISTS (SELECT 1 FROM service_catalog c WHERE c.idservice = s.idservice);

UPDATE requests r
   SET service_title = m.title
  FROM (VALUES
        ('diagnostic',   'Диагностика'),
        ('oil-change',   'Замена масла'),
        ('tires',        'Шины и диски'),
        ('brake',        'Тормозная система'),
        ('engine',       'Ремонт двигателя'),
        ('transmission', 'Коробка передач'),
        ('suspension',   'Подвеска'),
        ('body',         'Кузовные работы'),
        ('other',        'Другое')
       ) AS m(key, title)
 WHERE r.service_title = '' AND r.service_type = m.key;

-- ключа не нашлось в списке выше — заявка всё равно должна быть читаемой
UPDATE requests SET service_title = 'Другое' WHERE service_title = '';

UPDATE requests r
   SET idcatalog = c.idcatalog
  FROM service_catalog c
 WHERE r.idcatalog IS NULL
   AND c.idservice = r.idservice
   AND c.idrecstatus = 0
   AND lower(trim(c.title)) = lower(trim(r.service_title));
```

В блок Row Level Security дописать:

```sql
ALTER TABLE service_catalog         ENABLE ROW LEVEL SECURITY;
```

- [ ] **Шаг 4: Применить схему к базе**

Вариант через Supabase SQL Editor: открыть проект → SQL Editor → вставить содержимое `schema.sql` → Run.

Вариант из консоли (не требует psql):

```bash
python -c "
import asyncio, asyncpg, config, pathlib
async def main():
    conn = await asyncpg.connect(config.DATABASE_URL, ssl='require', statement_cache_size=0)
    await conn.execute(pathlib.Path('schema.sql').read_text(encoding='utf-8'))
    await conn.close()
    print('schema.sql применён')
asyncio.run(main())
"
```

- [ ] **Шаг 5: Проверить, что схема встала**

```bash
python -c "
import asyncio, asyncpg, config
async def main():
    conn = await asyncpg.connect(config.DATABASE_URL, ssl='require', statement_cache_size=0)
    print('услуг в каталоге:', await conn.fetchval('SELECT count(*) FROM service_catalog'))
    print('заявок без названия услуги:', await conn.fetchval(\"SELECT count(*) FROM requests WHERE service_title=''\"))
    await conn.close()
asyncio.run(main())
"
```

Ожидается: услуг = 9 × число активных сервисов, заявок без названия = 0.

- [ ] **Шаг 6: Прогнать скрипт повторно — убедиться в идемпотентности**

Повторить шаг 4, затем шаг 5. Числа должны не измениться, ошибок быть не должно.

- [ ] **Шаг 7: Коммит**

```bash
git add schema.sql
git commit -m "Схема: таблица service_catalog, поля заявки idcatalog и service_title, бэкфил"
```

---

### Задача 3: Шаблонный набор услуг при регистрации сервиса

**Файлы:**
- Изменить: `config.py:60-70` (добавить `DEFAULT_SERVICE_TITLES`, `SERVICE_TYPES` пока оставить)
- Изменить: `database.py` (метод `_seed_catalog`, вызов из `create_service`)
- Создать: `tests/test_catalog.py`

**Интерфейсы:**
- Потребляет: фикстуры `db_ready`, `service` из `tests/conftest.py` (задача 1)
- Производит: `config.DEFAULT_SERVICE_TITLES: tuple[str, ...]`; `Database._seed_catalog(conn, idservice) -> None`; `Database.get_catalog(idservice) -> list[asyncpg.Record]`

- [ ] **Шаг 1: Написать падающий тест**

Создать `tests/test_catalog.py`:

```python
"""Тесты каталога услуг — идут против настоящей базы (см. tests/conftest.py)."""

import config
from database import db


async def test_new_service_gets_default_catalog(service):
    items = await db.get_catalog(service)
    assert [i["title"] for i in items] == list(config.DEFAULT_SERVICE_TITLES)
    assert all(i["idrecstatus"] == 0 for i in items)
```

- [ ] **Шаг 2: Запустить и убедиться, что тест падает**

```bash
pytest tests/test_catalog.py -v
```

Ожидается: `AttributeError: module 'config' has no attribute 'DEFAULT_SERVICE_TITLES'`

- [ ] **Шаг 3: Добавить справочник в `config.py`**

Рядом с существующим `SERVICE_TYPES` (он ещё нужен `render.py` и `validators.py`, удалим в задаче 8):

```python
# Шаблонный набор услуг: копируется сервису при регистрации, дальше
# управляющий правит список сам (handlers/catalog.py).
DEFAULT_SERVICE_TITLES: tuple[str, ...] = (
    "Диагностика",
    "Замена масла",
    "Шины и диски",
    "Тормозная система",
    "Ремонт двигателя",
    "Коробка передач",
    "Подвеска",
    "Кузовные работы",
    "Другое",
)
```

Порядок обязан совпадать с бэкфилом из задачи 2, иначе `sort_order` у старых и новых сервисов разъедется.

- [ ] **Шаг 4: Добавить блок каталога в `database.py`**

Вставить новый раздел сразу после метода `get_services_by_city` (конец блока `# ── services ──`):

```python
    # ── service_catalog ──────────────────────────────────────────────────────

    async def _seed_catalog(self, conn: asyncpg.Connection, idservice: str) -> None:
        """Раздать сервису шаблонный набор услуг. Вызывается внутри транзакции."""
        await conn.executemany(
            """
            INSERT INTO service_catalog (idcatalog, idservice, title, sort_order, idrecstatus)
            VALUES ($1,$2,$3,$4,0)
            """,
            [
                (_new_id(), idservice, title, (i + 1) * 10)
                for i, title in enumerate(config.DEFAULT_SERVICE_TITLES)
            ],
        )

    async def get_catalog(self, idservice: str) -> list[asyncpg.Record]:
        """Активные услуги сервиса в порядке показа."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT * FROM service_catalog
                WHERE idservice=$1 AND idrecstatus=0
                ORDER BY sort_order, title
                """,
                idservice,
            )
```

- [ ] **Шаг 5: Вызвать сид из `create_service`**

В `database.py`, в `create_service`, сразу после `INSERT INTO admins ...` и до `return idservice` (всё внутри того же `async with ... conn.transaction()`):

```python
            # Сервис без услуг не должен существовать даже мгновение:
            # в форме записи клиенту было бы нечего выбрать.
            await self._seed_catalog(conn, idservice)
```

- [ ] **Шаг 6: Запустить тест — должен пройти**

```bash
pytest tests/test_catalog.py -v
```

Ожидается: 1 passed

- [ ] **Шаг 7: Коммит**

```bash
git add config.py database.py tests/test_catalog.py
git commit -m "Каталог: шаблонный набор услуг при регистрации сервиса"
```

---

### Задача 4: Чтение одной услуги с проверкой принадлежности сервису

**Файлы:**
- Изменить: `database.py` (метод `get_catalog_item` в блок `# ── service_catalog ──`)
- Изменить: `tests/test_catalog.py`

**Интерфейсы:**
- Производит: `Database.get_catalog_item(idservice: str, idcatalog: str) -> asyncpg.Record | None`

- [ ] **Шаг 1: Написать падающие тесты**

Дописать в `tests/test_catalog.py`:

```python
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
```

- [ ] **Шаг 2: Запустить и убедиться, что тесты падают**

```bash
pytest tests/test_catalog.py -v
```

Ожидается: `AttributeError: 'Database' object has no attribute 'get_catalog_item'`

- [ ] **Шаг 3: Реализовать метод**

Добавить в блок `# ── service_catalog ──` после `get_catalog`:

```python
    async def get_catalog_item(
        self, idservice: str, idcatalog: str
    ) -> asyncpg.Record | None:
        """
        Одна активная услуга сервиса.

        Фильтр по idservice обязателен: без него клиент подставил бы в заявку
        idcatalog чужого сервиса.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT * FROM service_catalog
                WHERE idcatalog=$1 AND idservice=$2 AND idrecstatus=0
                """,
                idcatalog, idservice,
            )
```

- [ ] **Шаг 4: Запустить тесты — должны пройти**

```bash
pytest tests/test_catalog.py -v
```

Ожидается: 3 passed

- [ ] **Шаг 5: Коммит**

```bash
git add database.py tests/test_catalog.py
git commit -m "Каталог: чтение услуги с проверкой принадлежности сервису"
```

---

### Задача 5: Добавление услуги с дедупликацией и воскрешением

**Файлы:**
- Изменить: `database.py` (метод `add_catalog_item`)
- Изменить: `tests/test_catalog.py`

**Интерфейсы:**
- Производит: `Database.add_catalog_item(idservice: str, title: str) -> asyncpg.Record | None` (`None` — активная услуга с таким названием уже есть)

- [ ] **Шаг 1: Написать падающие тесты**

Дописать в `tests/test_catalog.py`:

```python
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
```

Третий тест деактивирует строку напрямую в SQL, а не через `delete_catalog_item`: этот метод появится только в задаче 6, а коммит должен остаться зелёным.

- [ ] **Шаг 2: Запустить и убедиться, что тесты падают**

```bash
pytest tests/test_catalog.py -v
```

Ожидается: `AttributeError: 'Database' object has no attribute 'add_catalog_item'`

- [ ] **Шаг 3: Реализовать метод**

Добавить после `get_catalog_item`:

```python
    async def add_catalog_item(
        self, idservice: str, title: str
    ) -> asyncpg.Record | None:
        """
        Добавить услугу. None — активная услуга с таким названием уже есть.

        Ранее удалённая услуга воскресает, а не создаётся заново: так заявки,
        оформленные на неё до удаления, снова оказываются слинкованы со своей
        строкой каталога.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            revived = await conn.fetchrow(
                """
                UPDATE service_catalog SET idrecstatus=0, deletedate=NULL
                WHERE idcatalog = (
                        SELECT idcatalog FROM service_catalog
                         WHERE idservice=$1 AND idrecstatus=-1
                           AND lower(trim(title))=lower(trim($2))
                         ORDER BY deletedate DESC NULLS LAST
                         LIMIT 1)
                  AND NOT EXISTS (
                        SELECT 1 FROM service_catalog
                         WHERE idservice=$1 AND idrecstatus=0
                           AND lower(trim(title))=lower(trim($2)))
                RETURNING *
                """,
                idservice, title,
            )
            if revived:
                return revived

            # DO NOTHING вместо исключения: активный дубликат — не ошибка
            # уровня БД, а понятный ответ пользователю «такая услуга уже есть»
            return await conn.fetchrow(
                """
                INSERT INTO service_catalog
                    (idcatalog, idservice, title, sort_order, idrecstatus)
                VALUES ($1,$2,$3,$4,0)
                ON CONFLICT (idservice, lower(trim(title))) WHERE idrecstatus = 0
                DO NOTHING
                RETURNING *
                """,
                _new_id(), idservice, title, 1000,
            )
```

`sort_order = 1000` ставит добавленные услуги после шаблонных (у тех 10–90), между собой они сортируются по названию.

- [ ] **Шаг 4: Запустить тесты**

```bash
pytest tests/test_catalog.py -v
```

Ожидается: 6 passed

- [ ] **Шаг 5: Коммит**

```bash
git add database.py tests/test_catalog.py
git commit -m "Каталог: добавление услуги с дедупликацией и воскрешением удалённой"
```

---

### Задача 6: Удаление услуги с защитой последней

**Файлы:**
- Изменить: `database.py` (методы `delete_catalog_item`, `count_requests_by_catalog`)
- Изменить: `tests/test_catalog.py`

**Интерфейсы:**
- Производит: `Database.delete_catalog_item(idservice: str, idcatalog: str) -> asyncpg.Record | None` (`None` — удалить нельзя, услуга последняя или уже удалена)
- Производит: `Database.count_requests_by_catalog(idservice: str, idcatalog: str) -> int`

- [ ] **Шаг 1: Написать падающие тесты**

Дописать в `tests/test_catalog.py`:

```python
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
```

- [ ] **Шаг 2: Запустить и убедиться, что тесты падают**

```bash
pytest tests/test_catalog.py -v
```

Ожидается: `AttributeError: 'Database' object has no attribute 'delete_catalog_item'`

- [ ] **Шаг 3: Реализовать методы**

Добавить после `add_catalog_item`:

```python
    async def delete_catalog_item(
        self, idservice: str, idcatalog: str
    ) -> asyncpg.Record | None:
        """
        Мягко удалить услугу. None — удалять нечего или она последняя.

        CTE с FOR UPDATE блокирует все активные услуги сервиса до подсчёта.
        Без блокировки правило «хотя бы одна услуга» не работает: в READ
        COMMITTED две транзакции читают свои снапшоты, не видят UPDATE друг
        друга, и управляющий с двух устройств удаляет две последние услуги
        одновременно — сервис остаётся пустым. ORDER BY задаёт единый порядок
        захвата строк и тем исключает взаимную блокировку.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                WITH locked AS (
                    SELECT idcatalog FROM service_catalog
                     WHERE idservice=$2 AND idrecstatus=0
                     ORDER BY idcatalog
                     FOR UPDATE
                )
                UPDATE service_catalog SET idrecstatus=-1, deletedate=now()
                WHERE idcatalog=$1 AND idservice=$2 AND idrecstatus=0
                  AND (SELECT count(*) FROM locked) > 1
                RETURNING *
                """,
                idcatalog, idservice,
            )

    async def count_requests_by_catalog(self, idservice: str, idcatalog: str) -> int:
        """Сколько заявок оформлено на эту услугу — для текста подтверждения."""
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT count(*) FROM requests
                WHERE idservice=$1 AND idcatalog=$2 AND idrecstatus=0
                """,
                idservice, idcatalog,
            )
        return value or 0
```

- [ ] **Шаг 4: Запустить весь файл**

```bash
pytest tests/test_catalog.py -v
```

Ожидается: 10 passed

- [ ] **Шаг 5: Коммит**

```bash
git add database.py tests/test_catalog.py
git commit -m "Каталог: мягкое удаление услуги с защитой последней"
```

---

### Задача 7: Заявка переезжает с service_type на каталог

Одна задача на всю цепочку «форма → API → валидация → БД»: если разбить, между коммитами останется нерабочее состояние — `create_request` уже ждёт `idcatalog`, а `create_request_flow` ещё шлёт `service_type`.

**Файлы:**
- Изменить: `database.py:402-452` (`create_request`)
- Изменить: `validators.py:107-120` (`validate_request_fields`)
- Изменить: `handlers/requests.py:39-121` (`create_request_flow`), `:128-149` (legacy `web_app_data`)
- Изменить: `app.py:161-173` (`RequestPayload`)
- Изменить: `tests/test_catalog.py`

**Интерфейсы:**
- Потребляет: `db.get_catalog_item`, `db.get_catalog` (задачи 3–4); `validators.validate_uuid` (задача 1)
- Производит: `Database.create_request(..., idcatalog: str, service_title: str, ...)` — параметр `service_type` исчезает
- Производит: заявка содержит поля `idcatalog` и `service_title`

- [ ] **Шаг 1: Написать падающий тест**

Дописать в `tests/test_catalog.py`:

```python
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
```

- [ ] **Шаг 2: Запустить и убедиться, что тест падает**

```bash
pytest tests/test_catalog.py::test_request_stores_title_snapshot -v
```

Ожидается: `TypeError: create_request() got an unexpected keyword argument 'idcatalog'`

- [ ] **Шаг 3: Переписать `create_request` в `database.py`**

В сигнатуре заменить `service_type: str,` на:

```python
        idcatalog: str,
        service_title: str,
```

В INSERT заменить список колонок и плейсхолдеров — `service_type` уходит, добавляются два новых поля (колонка `service_type` остаётся в таблице и заполняется своим дефолтом `'other'`):

```python
                """
                INSERT INTO requests
                    (idrequests, idservice, idclienttg, client_name, phone,
                     brand, model, plate, idcatalog, service_title, urgency,
                     comment, client_uid, status, idrecstatus)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'new',0)
                ON CONFLICT (client_uid) WHERE client_uid IS NOT NULL DO NOTHING
                RETURNING *
                """,
                _new_id(), idservice, client_tg_id, client_name, phone,
                brand, model, plate, idcatalog, service_title, urgency,
                comment, client_uid,
```

- [ ] **Шаг 4: Убрать услугу из `validate_request_fields` в `validators.py`**

Удалить из возвращаемого словаря строку:

```python
        "service_type": validate_service_type(payload.get("service_type")),
```

Функцию `validate_service_type` пока не трогаем — её удалит задача 8 вместе с `config.SERVICE_TYPES`.

- [ ] **Шаг 5: Проверять услугу в `create_request_flow` (`handlers/requests.py`)**

Импорт дополнить: `from validators import ValidationError, h, validate_request_fields, validate_uuid`

Заменить блок валидации (сейчас — `try: fields = validate_request_fields(payload)`) на:

```python
    try:
        service_id = validate_uuid(payload.get("service_id"), field="Сервис")
    except ValidationError as exc:
        raise RequestRejected(str(exc)) from exc

    service = await db.get_service(service_id)
    if not service:
        raise RequestRejected("Сервис не найден или больше не принимает заявки.")

    try:
        fields = validate_request_fields(payload)
        idcatalog = validate_uuid(payload.get("idcatalog"), field="Услуга")
    except ValidationError as exc:
        raise RequestRejected(str(exc)) from exc

    # Услуга обязана принадлежать этому сервису и быть активной: клиент мог
    # держать форму открытой, пока управляющий убирал услугу из списка
    item = await db.get_catalog_item(service_id, idcatalog)
    if item is None:
        raise RequestRejected("Эта услуга больше не оказывается — выберите другую.")
```

Существующие строки в начале функции, которые доставали `service_id` и `service`, удалить — их заменил блок выше.

В вызове `db.create_request(...)` дописать два аргумента:

```python
    request, is_duplicate = await db.create_request(
        idservice=service_id,
        client_tg_id=client_tg_id,
        client_uid=client_uid,
        idcatalog=idcatalog,
        service_title=item["title"],
        **fields,
    )
```

В подтверждении клиенту заменить строку с услугой:

```python
        f"<b>Услуга:</b> {h(item['title'])}\n"
```

- [ ] **Шаг 6: Обновить legacy-хендлер `web_app_data`**

Заменить строку `payload.setdefault("service_type", payload.get("service"))` на:

```python
    # Старая форма присылала ключ service_type из общего справочника. Услуги
    # теперь свои у каждого сервиса, сопоставить их со старыми ключами нельзя.
    if not payload.get("idcatalog"):
        await message.answer(
            "❌ Форма записи устарела — закройте её и откройте заново."
        )
        return
```

- [ ] **Шаг 7: Обновить `RequestPayload` в `app.py`**

Заменить `service_type: str = ""` на:

```python
    idcatalog: str = ""
```

- [ ] **Шаг 8: Запустить тесты**

```bash
pytest -v
```

Ожидается: 18 passed

- [ ] **Шаг 9: Проверить, что приложение импортируется**

```bash
python -c "import app; print('ok')"
```

Ожидается: `ok` (предупреждения конфига допустимы)

- [ ] **Шаг 10: Коммит**

```bash
git add database.py validators.py handlers/requests.py app.py tests/test_catalog.py
git commit -m "Заявка: услуга берётся из каталога сервиса, название сохраняется снимком"
```

---

### Задача 8: Отображение и статистика на снимке названия

**Файлы:**
- Изменить: `render.py:44-45` (удалить `service_type_label`), `:63`, `:87`, `:97`, `:174-177`
- Изменить: `database.py:586-594` (`get_service_type_breakdown` → `get_service_breakdown`)
- Изменить: `handlers/admin_actions.py:155`
- Изменить: `config.py` (удалить `SERVICE_TYPES`)
- Изменить: `validators.py` (удалить `validate_service_type`)

**Интерфейсы:**
- Потребляет: поле `requests.service_title` (задачи 2, 7)
- Производит: `Database.get_service_breakdown(idservice: str) -> list[asyncpg.Record]` со столбцами `title`, `cnt`

- [ ] **Шаг 1: Заменить подписи услуг в `render.py`**

Удалить функцию целиком:

```python
def service_type_label(key: str) -> str:
    return config.SERVICE_TYPES.get(key, key)
```

Заменить три места использования:

- `request_card_for_staff`: `f"🔧 <b>Услуга:</b> {h(service_type_label(req['service_type']))}\n"` → `f"🔧 <b>Услуга:</b> {h(req['service_title'])}\n"`
- `request_line_for_staff`: `f"  🔧 {h(service_type_label(req['service_type']))} | "` → `f"  🔧 {h(req['service_title'])} | "`
- `request_line_for_client`: `f"   Услуга: {h(service_type_label(req['service_type']))}\n"` → `f"   Услуга: {h(req['service_title'])}\n"`

В `stats_card` заменить строку разбивки:

```python
            text += f"• {h(row['title'])} — {row['cnt']}\n"
```

- [ ] **Шаг 2: Переписать разбивку по услугам в `database.py`**

Заменить метод целиком:

```python
    async def get_service_breakdown(self, idservice: str) -> list[asyncpg.Record]:
        """Сколько заявок по каждой услуге — группируем по снимку названия,
        чтобы удалённые услуги не выпадали из статистики."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT service_title AS title, count(*) AS cnt
                FROM requests
                WHERE idservice=$1 AND idrecstatus=0 AND service_title <> ''
                GROUP BY service_title ORDER BY cnt DESC
                """,
                idservice,
            )
```

- [ ] **Шаг 3: Обновить вызов в `handlers/admin_actions.py`**

```python
    breakdown = await db.get_service_breakdown(idservice)
```

- [ ] **Шаг 4: Удалить мёртвый справочник**

В `config.py` удалить весь словарь `SERVICE_TYPES` (`DEFAULT_SERVICE_TITLES` остаётся).
В `validators.py` удалить функцию `validate_service_type`.

- [ ] **Шаг 5: Убедиться, что ссылок на удалённое не осталось**

```bash
grep -rn "SERVICE_TYPES\|service_type_label\|validate_service_type\|get_service_type_breakdown" --include="*.py" .
```

Ожидается: пусто. Поиск ограничен `*.py`, поэтому legacy-колонка `service_type` в `schema.sql` и старый массив в `webapp/index.html` (правится в задаче 12) в выдачу не попадают.

- [ ] **Шаг 6: Прогнать тесты и импорт**

```bash
pytest -v && python -c "import app; print('ok')"
```

Ожидается: 18 passed, затем `ok`

- [ ] **Шаг 7: Коммит**

```bash
git add render.py database.py handlers/admin_actions.py config.py validators.py
git commit -m "Отображение и статистика заявок переведены на снимок названия услуги"
```

---

### Задача 9: API отдаёт каталог услуг сервиса

**Файлы:**
- Изменить: `app.py:147-158` (`api_service`)

**Интерфейсы:**
- Потребляет: `db.get_catalog` (задача 3), `validators.validate_uuid` (задача 1)
- Производит: `GET /api/service/{id}` возвращает поле `catalog: [{idcatalog, title}]`

- [ ] **Шаг 1: Переписать эндпоинт**

```python
@app.get("/api/service/{service_id}")
async def api_service(service_id: str):
    # Без проверки формата asyncpg бросит DataError на мусорном id,
    # и клиент получит 500 вместо понятного «сервис не найден»
    try:
        service_id = validate_uuid(service_id, field="Сервис")
    except ValidationError:
        raise HTTPException(status_code=404, detail="Сервис не найден")

    svc = await db.get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail="Сервис не найден")

    catalog = await db.get_catalog(service_id)
    return {
        "idservice": str(svc["idservice"]),
        "service_name": svc["service_name"],
        "service_number": svc["service_number"],
        "city": svc["city"],
        "location_service": svc["location_service"],
        "catalog": [
            {"idcatalog": str(c["idcatalog"]), "title": c["title"]} for c in catalog
        ],
    }
```

Импорт в `app.py` дополнить: `from validators import ValidationError, validate_uuid`

- [ ] **Шаг 2: Поднять приложение локально**

```bash
python -m uvicorn app:app --port 8080
```

- [ ] **Шаг 3: Проверить ответ эндпоинта**

В другом терминале (подставить реальный id сервиса из базы):

```bash
curl -s http://localhost:8080/api/service/<idservice> | python -m json.tool
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/api/service/not-a-uuid
```

Ожидается: в первом ответе массив `catalog` из 9 объектов с `idcatalog` и `title`; во втором — `404`.

- [ ] **Шаг 4: Остановить сервер и закоммитить**

```bash
git add app.py
git commit -m "API: /api/service отдаёт каталог услуг, мусорный id даёт 404"
```

---

### Задача 10: Кнопки и клавиатуры каталога

**Файлы:**
- Изменить: `keyboards.py:18-32` (подписи), `:76-87` (`kb_owner_main`), добавить `kb_catalog` в блок inline-клавиатур

**Интерфейсы:**
- Производит: `keyboards.BTN_SERVICES`, `keyboards.kb_catalog(items) -> InlineKeyboardMarkup`
- Callback-данные: `svcdel:<idcatalog>` (удалить), `svcadd` (добавить), `svcdelok:<idcatalog>` (подтверждение, собирается существующим `kb_confirm`)

- [ ] **Шаг 1: Добавить подпись кнопки**

В блок подписей, после `BTN_REMOVE_ADMIN`:

```python
BTN_SERVICES       = "🔧 Услуги"
```

- [ ] **Шаг 2: Добавить кнопку в меню управляющего**

В `kb_owner_main`, между строкой `[BTN_ADMINS, BTN_ABOUT]` и строкой с `_webapp_button`:

```python
        [KeyboardButton(text=BTN_SERVICES)],
```

`kb_admin_main` не трогаем: администратор каталог не правит.

- [ ] **Шаг 3: Добавить inline-клавиатуру каталога**

В блок `# ── Inline-клавиатуры ──`, после `kb_select_admin`:

```python
def kb_catalog(items: list) -> InlineKeyboardMarkup:
    """Список услуг: тап по услуге ведёт к её удалению."""
    rows = [
        [InlineKeyboardButton(
            text=f"❌ {item['title']}",
            callback_data=f"svcdel:{item['idcatalog']}",
        )]
        for item in items
    ]
    rows.append([InlineKeyboardButton(
        text="➕ Добавить услугу", callback_data="svcadd"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)
```

`svcdel:` + uuid — 43 байта, лимит Telegram на `callback_data` (64) не превышен.

- [ ] **Шаг 4: Проверить импорт**

```bash
python -c "import keyboards; print(keyboards.kb_catalog([{'title':'Диагностика','idcatalog':'3f8b6c1e-9d2a-4b7c-8e1f-0a5b6c7d8e9f'}]))"
```

Ожидается: печатается объект `InlineKeyboardMarkup` с двумя строками кнопок.

- [ ] **Шаг 5: Коммит**

```bash
git add keyboards.py
git commit -m "Клавиатуры: кнопка «Услуги» в меню управляющего и список услуг"
```

---

### Задача 11: Хендлеры управления услугами

**Файлы:**
- Создать: `handlers/catalog.py`
- Изменить: `app.py:52-58` (регистрация роутера)

**Интерфейсы:**
- Потребляет: `db.get_catalog`, `db.get_catalog_item`, `db.add_catalog_item`, `db.delete_catalog_item`, `db.count_requests_by_catalog` (задачи 3–6); `kb.BTN_SERVICES`, `kb.kb_catalog`, `kb.kb_confirm`, `kb.kb_cancel` (задача 10); `validators.validate_service_title`, `validators.validate_uuid` (задача 1); `handlers.common.require_active_service`, `show_main_menu`
- Производит: `handlers.catalog.router`

- [ ] **Шаг 1: Создать `handlers/catalog.py`**

```python
"""
handlers/catalog.py — услуги сервиса.

Список услуг у каждого сервиса свой: при регистрации копируется шаблонный
набор, дальше управляющий правит его под себя. Администраторы каталог не
меняют — состав услуг это про то, чем сервис вообще занимается, а не про
повседневную обработку заявок.
"""

import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.types import CallbackQuery, Message

import keyboards as kb
from database import db
from handlers.common import require_active_service, show_main_menu
from validators import ValidationError, h, validate_service_title, validate_uuid

logger = logging.getLogger(__name__)
router = Router()

MAX_CATALOG_ITEMS = 30


class ServiceCatalog(StatesGroup):
    title = State()


async def _owner_service(
    message: Message, state: FSMContext, user_id: int | None = None
):
    """Активный сервис, если пользователь — его управляющий. Иначе None."""
    user_id = user_id or message.from_user.id
    svc = await require_active_service(message, state, user_id)
    if svc is None:
        return None
    if svc["owner_id"] != user_id:
        await message.answer("❌ Управлять услугами может только управляющий.")
        return None
    return svc


def _catalog_text(svc, items) -> str:
    lines = "".join(f"{i}. {h(item['title'])}\n" for i, item in enumerate(items, 1))
    return (
        f"🔧 <b>Услуги — {h(svc['service_name'])}</b>\n\n"
        f"{lines}\n"
        f"Всего {len(items)} из {MAX_CATALOG_ITEMS}. "
        "Нажмите на услугу, чтобы удалить."
    )


async def _show_catalog(message: Message, svc, *, edit: bool = False) -> None:
    items = await db.get_catalog(str(svc["idservice"]))
    text = _catalog_text(svc, items)
    markup = kb.kb_catalog(items)
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


def _parse_idcatalog(callback: CallbackQuery) -> str | None:
    """id из callback_data. None — данные подделаны или испорчены."""
    try:
        return validate_uuid(callback.data.split(":", 1)[1], field="Услуга")
    except (IndexError, ValidationError):
        return None


# ── Список услуг ─────────────────────────────────────────────────────────────

@router.message(F.text == kb.BTN_SERVICES, StateFilter(default_state))
async def show_services(message: Message, state: FSMContext) -> None:
    svc = await _owner_service(message, state)
    if svc is None:
        return
    await _show_catalog(message, svc)


# ── Добавление услуги ────────────────────────────────────────────────────────

@router.callback_query(F.data == "svcadd")
async def add_start(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await _owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        await callback.answer()
        return

    items = await db.get_catalog(str(svc["idservice"]))
    if len(items) >= MAX_CATALOG_ITEMS:
        await callback.answer(
            f"❌ Больше {MAX_CATALOG_ITEMS} услуг добавить нельзя.", show_alert=True
        )
        return

    await state.set_state(ServiceCatalog.title)
    await callback.message.answer(
        "Введите название услуги, например: <i>Полировка кузова</i>",
        reply_markup=kb.kb_cancel(),
    )
    await callback.answer()


@router.message(ServiceCatalog.title, F.text == kb.BTN_CANCEL)
async def add_cancel(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await show_main_menu(message, state, greeting="Отменено.")


@router.message(ServiceCatalog.title)
async def add_finish(message: Message, state: FSMContext) -> None:
    svc = await _owner_service(message, state)
    if svc is None:
        await state.set_state(None)
        return

    try:
        title = validate_service_title(message.text)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}\nПопробуйте ещё раз:")
        return

    item = await db.add_catalog_item(str(svc["idservice"]), title)
    if item is None:
        await message.answer(
            "❌ Такая услуга уже есть в списке. Введите другое название:"
        )
        return

    await state.set_state(None)
    await show_main_menu(
        message, state, greeting=f"✅ Услуга «{h(item['title'])}» добавлена."
    )
    await _show_catalog(message, svc)


# ── Удаление услуги ──────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("svcdel:"))
async def delete_ask(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await _owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        await callback.answer()
        return

    idcatalog = _parse_idcatalog(callback)
    if idcatalog is None:
        await callback.answer("❌ Услуга не найдена.", show_alert=True)
        return

    item = await db.get_catalog_item(str(svc["idservice"]), idcatalog)
    if item is None:
        await callback.answer("❌ Услуга уже удалена.", show_alert=True)
        await _show_catalog(callback.message, svc, edit=True)
        return

    used = await db.count_requests_by_catalog(str(svc["idservice"]), idcatalog)
    used_line = (
        f"По ней уже {used} заявок — они останутся в истории и статистике.\n"
        if used else ""
    )
    await callback.message.edit_text(
        f"🗑 <b>Удалить услугу «{h(item['title'])}»?</b>\n\n"
        f"{used_line}"
        "Клиенты больше не смогут выбрать её при записи.",
        reply_markup=kb.kb_confirm("svcdelok", idcatalog),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("svcdelok:"))
async def delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await _owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        await callback.answer()
        return

    idcatalog = _parse_idcatalog(callback)
    if idcatalog is None:
        await callback.answer("❌ Услуга не найдена.", show_alert=True)
        return

    removed = await db.delete_catalog_item(str(svc["idservice"]), idcatalog)
    if removed is None:
        await callback.answer(
            "❌ Нельзя удалить последнюю услугу — сначала добавьте другую.",
            show_alert=True,
        )
        await _show_catalog(callback.message, svc, edit=True)
        return

    await callback.answer(f"Услуга «{removed['title']}» удалена.")
    await _show_catalog(callback.message, svc, edit=True)
```

- [ ] **Шаг 2: Зарегистрировать роутер в `app.py`**

В импорте: `from handlers import admin_actions, admin_mgmt, catalog, register, requests, start`

В `dp.include_routers(...)` добавить `catalog.router` перед `admin_mgmt.router`:

```python
dp.include_routers(
    requests.router,
    start.router,
    register.router,
    catalog.router,
    admin_mgmt.router,
    admin_actions.router,
)
```

Порядок важен: `admin_actions` содержит fallback и обязан остаться последним.

- [ ] **Шаг 3: Проверить импорт и отсутствие конфликта хендлеров**

```bash
python -c "import app; print(len(app.dp.sub_routers), 'роутеров подключено')"
```

Ожидается: `6 роутеров подключено`

- [ ] **Шаг 4: Прогнать тесты**

```bash
pytest -v
```

Ожидается: 18 passed

- [ ] **Шаг 5: Коммит**

```bash
git add handlers/catalog.py app.py
git commit -m "Бот: экран управления услугами для управляющего"
```

---

### Задача 12: Форма записи берёт услуги с сервера

**Файлы:**
- Изменить: `webapp/index.html:220-230` (удалить `SERVICE_TYPES`), `:281-317` (`init`), `:404-418` (клик по сервису), `:468-476` (селект услуги), `:548-562` (тело запроса)

**Интерфейсы:**
- Потребляет: поле `catalog` из `GET /api/service/{id}` (задача 9)
- Производит: тело `POST /api/requests` содержит `idcatalog` вместо `service_type`

- [ ] **Шаг 1: Удалить захардкоженный список услуг**

Удалить целиком константу `const SERVICE_TYPES = [ ... ];` (константа `URGENCY` остаётся — срочность общая для всех сервисов).

- [ ] **Шаг 2: Свести обе ветки выбора сервиса к одной функции**

Добавить перед `async function init()`:

```js
// Единственный путь к форме: карточку сервиса вместе с каталогом услуг
// всегда берём с сервера — в результатах поиска по городу услуг нет.
async function openService(serviceId) {
  renderSpinner();
  try {
    state.selectedService = await api(`/api/service/${encodeURIComponent(serviceId)}`);
    state.serviceId = serviceId;
    document.title = state.selectedService.service_name + " — запись";
  } catch (e) {
    render(`
      <div class="header"><div class="logo">⚙ АВТОСЕРВИС</div></div>
      <div class="card">
        <div class="error-banner">${esc(e.message)}</div>
        <button class="btn btn-secondary" onclick="location.href=location.pathname">
          Выбрать другой сервис
        </button>
      </div>
    `);
    return;
  }
  renderForm();
}
```

В `init()` заменить весь блок `if (state.serviceId) { ... } else { renderCityPicker(); }` на:

```js
  if (state.serviceId) {
    await openService(state.serviceId);
  } else {
    renderCityPicker();
  }
```

В `renderServicesList` заменить обработчик клика:

```js
  container.querySelectorAll(".svc-item").forEach(el => {
    el.addEventListener("click", () => {
      openService(list[Number(el.dataset.index)].idservice);
    });
  });
```

- [ ] **Шаг 3: Строить селект услуг из каталога**

В `renderForm()`, после строки `const svc = state.selectedService || {};` добавить:

```js
  const catalog = svc.catalog || [];
```

Заменить блок поля «Тип работы»:

```html
      <div class="field">
        <label>Тип работы *</label>
        <select id="f_catalog">
          ${catalog.map(s => `<option value="${esc(s.idcatalog)}">${esc(s.title)}</option>`).join("")}
        </select>
      </div>
```

Перед `render(...)` в `renderForm()` добавить защиту на случай пустого каталога (по правилу «минимум одна услуга» это невозможно, но пустой селект молча ломал бы отправку):

```js
  if (catalog.length === 0) {
    render(`
      <div class="header"><div class="logo">${esc(svc.service_name || "Автосервис")}</div></div>
      <div class="card">
        <div class="error-banner">
          Сервис пока не настроил список услуг.<br>Попробуйте позже.
        </div>
      </div>
    `);
    return;
  }
```

- [ ] **Шаг 4: Отправлять `idcatalog`**

В теле `POST /api/requests` заменить строку `service_type: get("f_service_type"),` на:

```js
        idcatalog:    get("f_catalog"),
```

- [ ] **Шаг 5: Убедиться, что старых упоминаний не осталось**

```bash
grep -n "SERVICE_TYPES\|f_service_type\|service_type" webapp/index.html
```

Ожидается: пусто.

- [ ] **Шаг 6: Коммит**

```bash
git add webapp/index.html
git commit -m "WebApp: список услуг приходит с сервера, заявка ссылается на idcatalog"
```

---

### Задача 13: Сквозная проверка

**Файлы:** изменений нет — только проверка.

- [ ] **Шаг 1: Прогнать все тесты**

```bash
pytest -v
```

Ожидается: 18 passed

- [ ] **Шаг 2: Поднять приложение с публичным адресом**

```bash
python -m uvicorn app:app --port 8080
```

В отдельном терминале: `ngrok http 8080`, полученный `https://...` подставить в `BASE_URL` в `.env`, перезапустить приложение.

- [ ] **Шаг 3: Пройти ручной чек-лист в Telegram**

- [ ] Зарегистрировать новый сервис → в меню управляющего есть «🔧 Услуги»
- [ ] Открыть «🔧 Услуги» → список из 9 услуг, снизу «➕ Добавить услугу»
- [ ] Удалить 8 услуг подряд → каждый раз список обновляется в том же сообщении
- [ ] Попробовать удалить девятую → алерт «Нельзя удалить последнюю услугу»
- [ ] Добавить «Полировка кузова» → появляется в списке
- [ ] Добавить «полировка кузова» ещё раз → «Такая услуга уже есть в списке»
- [ ] Добавить услугу из одной буквы → «минимум 2 символа»
- [ ] Открыть форму записи → в селекте ровно две услуги
- [ ] Отправить заявку на «Полировка кузова» → в карточке администратору стоит «Полировка кузова»
- [ ] «📊 Статистика» → в «Популярные работы» есть «Полировка кузова»
- [ ] Удалить «Полировка кузова» → подтверждение упоминает 1 заявку
- [ ] «📋 Мои заявки» у клиента → услуга по-прежнему видна
- [ ] Зайти вторым аккаунтом как администратор → кнопки «🔧 Услуги» в меню нет

- [ ] **Шаг 4: Вернуть `.env` в исходное состояние**

Убрать временный `BASE_URL` от ngrok, если он там не был раньше.

- [ ] **Шаг 5: Финальный коммит (если правки по итогам проверки были)**

```bash
git add -A
git commit -m "Правки по итогам сквозной проверки каталога услуг"
```

---

## Что осталось за рамками

Перечислено в спеке, отдельными задачами не реализуется:

- переименование услуг и ручная сортировка (`sort_order` заполняется, UI нет);
- цена и длительность услуги;
- редактирование каталога из WebApp;
- удаление legacy-колонки `requests.service_type` — отдельным шагом после того, как бэкфил подтвердится на боевых данных.
