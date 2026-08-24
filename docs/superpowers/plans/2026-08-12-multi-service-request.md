# Несколько услуг в заявке и цена услуги — план реализации

> **Для агентов:** ОБЯЗАТЕЛЬНЫЙ САБ-СКИЛЛ: superpowers:subagent-driven-development
> (рекомендуется) либо superpowers:executing-plans — выполнять план задача за задачей.
> Шаги отмечаются чекбоксами (`- [ ]`).

**Спека:** `docs/superpowers/specs/2026-08-12-multi-service-request-design.md`

**Цель:** клиент выбирает в заявке несколько услуг сразу и видит ориентировочную сумму; управляющий задаёт цену каждой услуги.

**Архитектура:** позиции заявки живут в отдельной таблице `request_services` — одна строка на выбранную услугу, со снимками названия и цены на момент оформления. Каталог получает необязательное поле `price_rub`. Суммы нигде не хранятся, складываются при отображении.

**Стек:** Python 3.12, aiogram 3.27, FastAPI, asyncpg, PostgreSQL (Supabase), pytest.

## Глобальные ограничения

- Весь текст интерфейса, комментарии и docstring'и — на русском языке.
- Удаления только мягкие: `idrecstatus = 0` активна, `-1` удалена. Жёсткого `DELETE` в `database.py` быть не должно ни в одном методе.
- Ни один SQL-запрос не пишется вне `database.py`. Исключение — тестовые фикстуры и тесты, которые готовят состояние и убирают за собой.
- `parse_mode=HTML`; любое пользовательское значение экранируется через `validators.h()`. Названия услуг вводит управляющий — это пользовательский ввод. В подписях inline-кнопок и в alert-ответах экранирование не нужно и вредно: Telegram их как HTML не парсит.
- Идентификаторы из недоверенных источников (тело запроса, `callback_data`) проходят `validate_uuid` до похода в базу.
- Цена — целое число рублей, `NULL` значит «не указана» и отличается от нуля. Показывается всегда как «от N ₽».
- Длительность услуги не делаем: она появится вместе с расписанием.
- Права не меняются: каталог правит только управляющий (`services.owner_id`).
- Форма отправляет только идентификаторы услуг. Цены сервер берёт из своей базы — иначе подменённый запрос запишет «полировка, 1 рубль».

---

### Задача 1: Валидаторы и форматирование цены

**Файлы:**
- Изменить: `validators.py` (добавить `validate_price` и `validate_catalog_ids` перед `validate_request_fields`)
- Изменить: `render.py` (добавить `price_label` рядом с `status_label`)
- Изменить: `tests/test_validators.py`
- Создать: `tests/test_render.py`

**Интерфейсы:**
- Потребляет: `validators.clean_text`, `validators.validate_uuid`, `validators.ValidationError`
- Производит: `validators.validate_price(raw) -> int | None`, `validators.validate_catalog_ids(raw) -> list[str]`, `render.price_label(price_rub: int | None) -> str`

- [ ] **Шаг 1: Написать падающие тесты валидаторов**

Дописать в конец `tests/test_validators.py`:

```python
# ── Цена услуги ──────────────────────────────────────────────────────────────

def test_price_accepts_plain_number():
    assert validate_price("3000") == 3000


def test_price_accepts_spaces_inside():
    """«3 000» — обычный способ записи, придираться к нему нельзя."""
    assert validate_price("3 000") == 3000


def test_price_accepts_currency_suffix():
    assert validate_price("3000 ₽") == 3000
    assert validate_price("3000 руб") == 3000


def test_price_dash_means_not_set():
    assert validate_price("-") is None
    assert validate_price("—") is None


def test_price_rejects_text():
    with pytest.raises(ValidationError):
        validate_price("дорого")


def test_price_rejects_negative():
    with pytest.raises(ValidationError):
        validate_price("-500")


def test_price_rejects_absurd_value():
    with pytest.raises(ValidationError):
        validate_price("10000001")


def test_price_allows_zero():
    """Ноль — это «бесплатно», осмысленное значение для акции."""
    assert validate_price("0") == 0


# ── Список выбранных услуг ───────────────────────────────────────────────────

def test_catalog_ids_keeps_order():
    first = "3f8b6c1e-9d2a-4b7c-8e1f-0a5b6c7d8e9f"
    second = "11111111-2222-3333-4444-555555555555"
    assert validate_catalog_ids([first, second]) == [first, second]


def test_catalog_ids_drops_duplicates():
    value = "3f8b6c1e-9d2a-4b7c-8e1f-0a5b6c7d8e9f"
    assert validate_catalog_ids([value, value]) == [value]


def test_catalog_ids_rejects_empty():
    with pytest.raises(ValidationError):
        validate_catalog_ids([])


def test_catalog_ids_rejects_not_a_list():
    with pytest.raises(ValidationError):
        validate_catalog_ids("3f8b6c1e-9d2a-4b7c-8e1f-0a5b6c7d8e9f")


def test_catalog_ids_rejects_garbage_inside():
    with pytest.raises(ValidationError):
        validate_catalog_ids(["3f8b6c1e-9d2a-4b7c-8e1f-0a5b6c7d8e9f", "'; DROP TABLE--"])
```

Импорт в начале файла дополнить: `from validators import ValidationError, validate_catalog_ids, validate_price, validate_service_title, validate_uuid`

- [ ] **Шаг 2: Запустить и убедиться, что тесты падают**

```bash
.venv/Scripts/python.exe -m pytest tests/test_validators.py -q
```

Ожидается: `ImportError: cannot import name 'validate_price' from 'validators'`

- [ ] **Шаг 3: Реализовать валидаторы**

Вставить в `validators.py` перед `def validate_request_fields`:

```python
def validate_price(raw: object) -> int | None:
    """
    Цена услуги в рублях. None — цена не указана.

    Управляющий вводит её текстом, поэтому принимаем и «3 000», и «3000 ₽»:
    отказ из-за пробела внутри числа выглядит придиркой, а не проверкой.
    """
    text = clean_text(raw, field="Цена", max_len=20)
    if text in {"-", "—", "–", ""}:
        return None

    compact = re.sub(r"\s", "", text)
    compact = re.sub(r"(₽|руб\.?|р\.?)$", "", compact, flags=re.IGNORECASE)
    if not compact.isdigit():
        raise ValidationError(
            "Введите число рублей, например 3000, или «-», чтобы не указывать цену."
        )

    value = int(compact)
    if value > 10_000_000:
        raise ValidationError("Цена не может быть больше 10 000 000 ₽.")
    return value


def validate_catalog_ids(raw: object) -> list[str]:
    """
    Список выбранных клиентом услуг. Порядок сохраняется — в нём заявка
    показывается администратору. Дубликаты схлопываются: повторный элемент
    ничего не добавляет, а в базе на него стоит уникальный индекс.
    """
    if not isinstance(raw, (list, tuple)):
        raise ValidationError("Выберите хотя бы одну услугу.")

    chosen: list[str] = []
    for value in raw:
        item = validate_uuid(value, field="Услуга")
        if item not in chosen:
            chosen.append(item)

    if not chosen:
        raise ValidationError("Выберите хотя бы одну услугу.")
    return chosen
```

- [ ] **Шаг 4: Запустить тесты валидаторов — должны пройти**

```bash
.venv/Scripts/python.exe -m pytest tests/test_validators.py -q
```

Ожидается: все тесты файла проходят.

- [ ] **Шаг 5: Написать падающий тест форматирования**

Создать `tests/test_render.py`:

```python
"""Тесты сборки текстов. Базы не требуют."""

from render import price_label


def test_price_label_shows_from_prefix():
    """Цена всегда ориентировочная: точную сервис назовёт после осмотра."""
    assert price_label(3000) == "от 3 000 ₽"


def test_price_label_groups_thousands():
    assert price_label(1234567) == "от 1 234 567 ₽"


def test_price_label_without_price():
    assert price_label(None) == "цена по запросу"


def test_price_label_zero_is_a_price():
    assert price_label(0) == "от 0 ₽"
```

- [ ] **Шаг 6: Запустить и убедиться, что тест падает**

```bash
.venv/Scripts/python.exe -m pytest tests/test_render.py -q
```

Ожидается: `ImportError: cannot import name 'price_label' from 'render'`

- [ ] **Шаг 7: Реализовать форматирование**

Добавить в `render.py` после `def status_label`:

```python
def price_label(price_rub: int | None) -> str:
    """Цена для показа. Всегда «от»: точную сервис назовёт после осмотра."""
    if price_rub is None:
        return "цена по запросу"
    return "от " + f"{price_rub:,}".replace(",", " ") + " ₽"
```

- [ ] **Шаг 8: Запустить оба файла тестов**

```bash
.venv/Scripts/python.exe -m pytest tests/test_render.py tests/test_validators.py -q
```

Ожидается: все проходят, вывод чистый.

- [ ] **Шаг 9: Коммит**

```bash
git add validators.py render.py tests/test_validators.py tests/test_render.py
git commit -m "Валидатор цены услуги и списка выбранных услуг"
```

---

### Задача 2: Схема БД — цена в каталоге и позиции заявки

**Файлы:**
- Изменить: `schema.sql`

**Интерфейсы:**
- Производит: колонка `service_catalog.price_rub`; таблица `request_services (idrequestservice, idrequests, idcatalog, title, price_rub, position, createdate)`

- [ ] **Шаг 1: Добавить цену в каталог**

В `schema.sql`, в блоке `-- ── Каталог услуг сервиса ──`, после `CREATE UNIQUE INDEX ... idx_catalog_unique_title`:

```sql
ALTER TABLE service_catalog
    ADD COLUMN IF NOT EXISTS price_rub int;

-- Цена в целых рублях: копейки в прайсе автосервиса не встречаются, а numeric
-- тянет округления на ровном месте. NULL — «не указана», это не то же самое,
-- что 0 («бесплатно»).
ALTER TABLE service_catalog DROP CONSTRAINT IF EXISTS chk_catalog_price;
ALTER TABLE service_catalog ADD  CONSTRAINT chk_catalog_price
    CHECK (price_rub IS NULL OR price_rub BETWEEN 0 AND 10000000);
```

- [ ] **Шаг 2: Добавить таблицу позиций заявки**

Вставить сразу после блока `-- ── История смены статусов ──` (позиции ссылаются на `requests`, значит объявляются после неё):

```sql
-- ── Позиции заявки ───────────────────────────────────────────────────────
-- Одна строка на одну выбранную клиентом услугу. title и price_rub —
-- снимки на момент оформления: управляющий вправе поменять цену завтра, но
-- клиенту обещали сегодняшнюю. position хранит порядок, в котором клиент
-- видел услуги, чтобы карточка администратору совпадала с формой.
CREATE TABLE IF NOT EXISTS request_services (
    idrequestservice uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    idrequests       uuid        NOT NULL REFERENCES requests(idrequests) ON DELETE CASCADE,
    idcatalog        uuid        REFERENCES service_catalog(idcatalog) ON DELETE SET NULL,
    title            text        NOT NULL,
    price_rub        int,
    position         int         NOT NULL DEFAULT 0,
    createdate       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_request_services_request
    ON request_services (idrequests);
CREATE INDEX IF NOT EXISTS idx_request_services_catalog
    ON request_services (idcatalog);

-- одну и ту же услугу нельзя добавить в заявку дважды
CREATE UNIQUE INDEX IF NOT EXISTS idx_request_services_unique
    ON request_services (idrequests, idcatalog) WHERE idcatalog IS NOT NULL;
```

В блоке Row Level Security дописать:

```sql
ALTER TABLE request_services        ENABLE ROW LEVEL SECURITY;
```

- [ ] **Шаг 3: Убрать давно мёртвую колонку `service_type`**

В `CREATE TABLE IF NOT EXISTS requests` удалить строку:

```sql
    service_type text        NOT NULL DEFAULT 'other', -- legacy: не читается приложением, ждёт удаления после проверки бэкфила
```

Ниже, рядом с прочими `ALTER TABLE requests`, добавить:

```sql
-- Колонка пережила прошлую фичу как страховка на случай отката. Бэкфил
-- проверен, приложение её не читает и не пишет — удаляем.
ALTER TABLE requests DROP COLUMN IF EXISTS service_type;
```

- [ ] **Шаг 4: Добавить бэкфил позиций**

В блок `-- ── Бэкфил каталога услуг ──`, в самый конец, дописать:

```sql
-- Существующие заявки: у каждой ровно одна услуга, она становится позицией.
INSERT INTO request_services (idrequests, idcatalog, title, position)
SELECT r.idrequests, r.idcatalog, r.service_title, 0
  FROM requests r
 WHERE r.service_title <> ''
   AND NOT EXISTS (
        SELECT 1 FROM request_services rs WHERE rs.idrequests = r.idrequests);
```

- [ ] **Шаг 5: Применить схему к базе**

```bash
.venv/Scripts/python.exe -c "
import asyncio, asyncpg, config, pathlib
async def main():
    conn = await asyncpg.connect(config.DATABASE_URL, ssl='require', statement_cache_size=0)
    await conn.execute(pathlib.Path('schema.sql').read_text(encoding='utf-8'))
    await conn.close()
    print('schema.sql применён')
asyncio.run(main())
"
```

- [ ] **Шаг 6: Проверить результат**

```bash
.venv/Scripts/python.exe -c "
import asyncio, asyncpg, config
async def main():
    conn = await asyncpg.connect(config.DATABASE_URL, ssl='require', statement_cache_size=0)
    print('позиций заявок:', await conn.fetchval('SELECT count(*) FROM request_services'))
    print('заявок всего  :', await conn.fetchval('SELECT count(*) FROM requests'))
    print('заявок без позиций:', await conn.fetchval('''
        SELECT count(*) FROM requests r WHERE NOT EXISTS (
            SELECT 1 FROM request_services rs WHERE rs.idrequests = r.idrequests)'''))
    print('колонка service_type осталась:', await conn.fetchval('''
        SELECT count(*) FROM information_schema.columns
         WHERE table_name='requests' AND column_name='service_type'''') )
    await conn.close()
asyncio.run(main())
"
```

Ожидается: позиций столько же, сколько заявок; заявок без позиций 0; колонка `service_type` — 0.

- [ ] **Шаг 7: Прогнать схему повторно и сверить числа**

Повторить шаги 5 и 6. Числа не должны измениться, ошибок быть не должно — это проверка идемпотентности.

- [ ] **Шаг 8: Коммит**

```bash
git add schema.sql
git commit -m "Схема: цена услуги, таблица позиций заявки, удаление legacy service_type"
```

---

### Задача 3: Цена услуги в слое БД

**Файлы:**
- Изменить: `database.py` (блок `# ── service_catalog ──`: `add_catalog_item`, новые `set_catalog_item_price` и `get_catalog_items`)
- Изменить: `tests/test_catalog.py`

**Интерфейсы:**
- Потребляет: фикстуры `db_ready`, `service` из `tests/conftest.py`
- Производит: `Database.add_catalog_item(idservice, title, price_rub=None) -> asyncpg.Record | None`; `Database.set_catalog_item_price(idservice, idcatalog, price_rub) -> asyncpg.Record | None`; `Database.get_catalog_items(idservice, idcatalogs, *, only_active=True) -> list[asyncpg.Record]`

- [ ] **Шаг 1: Написать падающие тесты**

Дописать в конец `tests/test_catalog.py`:

```python
# ── Цена услуги ──────────────────────────────────────────────────────────────

async def test_add_catalog_item_stores_price(service):
    item = await db.add_catalog_item(service, "Полировка кузова", price_rub=3000)
    assert item["price_rub"] == 3000


async def test_add_catalog_item_without_price(service):
    item = await db.add_catalog_item(service, "Химчистка салона")
    assert item["price_rub"] is None


async def test_set_catalog_item_price(service):
    item = (await db.get_catalog(service))[0]
    updated = await db.set_catalog_item_price(service, str(item["idcatalog"]), 1500)
    assert updated["price_rub"] == 1500


async def test_set_catalog_item_price_clears_it(service):
    item = await db.add_catalog_item(service, "Полировка фар", price_rub=1500)
    cleared = await db.set_catalog_item_price(service, str(item["idcatalog"]), None)
    assert cleared["price_rub"] is None


async def test_set_catalog_item_price_rejects_foreign_service(service, db_ready):
    """Цену чужой услуги менять нельзя."""
    other = await db.create_service(
        name="Тест чужой цены", phone="+79990000020", city="Тестоград",
        address="ул. Чужая, 3", owner_tg_id=999_000_020,
    )
    try:
        foreign = (await db.get_catalog(other))[0]
        assert await db.set_catalog_item_price(service, str(foreign["idcatalog"]), 100) is None
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM services WHERE idservice=$1", other)


async def test_get_catalog_items_returns_requested_only(service):
    catalog = await db.get_catalog(service)
    wanted = [str(catalog[0]["idcatalog"]), str(catalog[2]["idcatalog"])]
    items = await db.get_catalog_items(service, wanted)
    assert {str(i["idcatalog"]) for i in items} == set(wanted)


async def test_get_catalog_items_skips_deleted(service):
    catalog = await db.get_catalog(service)
    target = str(catalog[0]["idcatalog"])
    await db.delete_catalog_item(service, target)
    assert await db.get_catalog_items(service, [target]) == []


async def test_get_catalog_items_can_include_deleted(service):
    """Удалённую услугу нужно уметь назвать по имени в тексте отказа."""
    catalog = await db.get_catalog(service)
    target = str(catalog[0]["idcatalog"])
    await db.delete_catalog_item(service, target)
    items = await db.get_catalog_items(service, [target], only_active=False)
    assert [i["title"] for i in items] == [catalog[0]["title"]]


async def test_get_catalog_items_rejects_foreign_service(service, db_ready):
    other = await db.create_service(
        name="Тест чужой выборки", phone="+79990000021", city="Тестоград",
        address="ул. Чужая, 4", owner_tg_id=999_000_021,
    )
    try:
        foreign = (await db.get_catalog(other))[0]
        assert await db.get_catalog_items(service, [str(foreign["idcatalog"])]) == []
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM services WHERE idservice=$1", other)
```

- [ ] **Шаг 2: Запустить и убедиться, что тесты падают**

```bash
.venv/Scripts/python.exe -m pytest tests/test_catalog.py -q -k "price or catalog_items"
```

Ожидается: `TypeError: add_catalog_item() got an unexpected keyword argument 'price_rub'`

- [ ] **Шаг 3: Принять цену в `add_catalog_item`**

В `database.py` изменить сигнатуру и оба запроса метода:

```python
    async def add_catalog_item(
        self, idservice: str, title: str, price_rub: int | None = None
    ) -> asyncpg.Record | None:
```

Значение по умолчанию обязательно: существующие вызовы и тесты не должны сломаться.

В ветке воскрешения дописать установку цены — управляющий заводит услугу заново и ожидает, что укажет актуальную цену, а не унаследует прошлогоднюю:

```python
            revived = await conn.fetchrow(
                """
                UPDATE service_catalog SET idrecstatus=0, deletedate=NULL, price_rub=$3
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
                idservice, title, price_rub,
            )
```

В ветке вставки добавить колонку:

```python
            return await conn.fetchrow(
                """
                INSERT INTO service_catalog
                    (idcatalog, idservice, title, price_rub, sort_order, idrecstatus)
                VALUES ($1,$2,$3,$4,$5,0)
                ON CONFLICT (idservice, lower(trim(title))) WHERE idrecstatus = 0
                DO NOTHING
                RETURNING *
                """,
                _new_id(), idservice, title, price_rub, 1000,
            )
```

- [ ] **Шаг 4: Добавить методы правки цены и выборки пачкой**

Вставить в блок `# ── service_catalog ──` после `add_catalog_item`:

```python
    async def set_catalog_item_price(
        self, idservice: str, idcatalog: str, price_rub: int | None
    ) -> asyncpg.Record | None:
        """
        Изменить цену услуги. None — услуги нет, она удалена или чужая.

        Отдельный метод, а не «обновить услугу целиком»: переименование услуг
        сознательно вне области, и смешивать эти операции незачем.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                UPDATE service_catalog SET price_rub=$3
                WHERE idcatalog=$1 AND idservice=$2 AND idrecstatus=0
                RETURNING *
                """,
                idcatalog, idservice, price_rub,
            )

    async def get_catalog_items(
        self,
        idservice: str,
        idcatalogs: Sequence[str],
        *,
        only_active: bool = True,
    ) -> list[asyncpg.Record]:
        """
        Услуги сервиса по списку идентификаторов, одним запросом.

        Фильтр по idservice обязателен: без него клиент подставил бы в заявку
        услуги чужого сервиса. only_active=False нужен, чтобы назвать удалённую
        услугу по имени в тексте отказа, а не отделаться безличным «одна из
        услуг недоступна».
        """
        if not idcatalogs:
            return []
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT * FROM service_catalog
                WHERE idservice=$1 AND idcatalog = ANY($2::uuid[])
                  AND (NOT $3::bool OR idrecstatus = 0)
                ORDER BY sort_order, title
                """,
                idservice, list(idcatalogs), only_active,
            )
```

`Sequence` уже импортирован в `database.py` (`from typing import Any, Iterable, Sequence`).

- [ ] **Шаг 5: Запустить тесты каталога**

```bash
.venv/Scripts/python.exe -m pytest tests/test_catalog.py -q
```

Ожидается: все тесты файла проходят, включая прежние.

- [ ] **Шаг 6: Коммит**

```bash
git add database.py tests/test_catalog.py
git commit -m "Каталог: цена услуги и выборка услуг пачкой"
```

---

### Задача 4: Заявка переезжает на список услуг

Одна задача на всю цепочку «форма → API → флоу → БД»: если разбить, между коммитами останется нерабочее состояние — `create_request` уже ждёт список, а `create_request_flow` ещё шлёт одну услугу.

**Файлы:**
- Изменить: `database.py` (`create_request`, новый `get_request_services`)
- Изменить: `handlers/requests.py` (`create_request_flow`, legacy `web_app_data`)
- Изменить: `app.py` (`RequestPayload`)
- Изменить: `tests/test_catalog.py`

**Интерфейсы:**
- Потребляет: `db.get_catalog_items` (задача 3), `validators.validate_catalog_ids` (задача 1)
- Производит: `Database.create_request(..., services: list[dict], ...)` — параметры `idcatalog` и `service_title` исчезают; элемент списка — словарь с ключами `idcatalog: str`, `title: str`, `price_rub: int | None`
- Производит: `Database.get_request_services(idrequests) -> list[asyncpg.Record]`
- Производит: `RequestPayload.idcatalogs: list[str]`

- [ ] **Шаг 1: Написать падающие тесты**

Дописать в конец `tests/test_catalog.py`:

```python
# ── Несколько услуг в заявке ─────────────────────────────────────────────────

async def _make_request_with(service, titles_and_prices):
    """Завести услуги и оформить на них заявку. Возвращает (заявка, услуги)."""
    items = []
    for title, price in titles_and_prices:
        item = await db.add_catalog_item(service, title, price_rub=price)
        items.append(item)

    request, _ = await db.create_request(
        idservice=service,
        client_tg_id=999_000_030,
        client_name="Клиент",
        phone="+79990000030",
        brand="Toyota",
        model="Camry",
        plate="А777АА77",
        services=[
            {"idcatalog": str(i["idcatalog"]), "title": i["title"], "price_rub": i["price_rub"]}
            for i in items
        ],
        urgency="low",
        comment="",
    )
    return request, items


async def test_request_stores_every_service(service):
    request, items = await _make_request_with(
        service, [("Полировка кузова", 3000), ("Полировка фар", 1500)]
    )
    positions = await db.get_request_services(str(request["idrequests"]))
    assert [p["title"] for p in positions] == ["Полировка кузова", "Полировка фар"]
    assert [p["price_rub"] for p in positions] == [3000, 1500]


async def test_request_keeps_order_of_selection(service):
    request, _ = await _make_request_with(
        service, [("Услуга Б", None), ("Услуга А", None)]
    )
    positions = await db.get_request_services(str(request["idrequests"]))
    assert [p["position"] for p in positions] == [0, 1]
    assert [p["title"] for p in positions] == ["Услуга Б", "Услуга А"]


async def test_price_snapshot_survives_catalog_change(service):
    """Цену подняли завтра — в уже оформленной заявке остаётся вчерашняя."""
    request, items = await _make_request_with(service, [("Полировка кузова", 3000)])
    await db.set_catalog_item_price(service, str(items[0]["idcatalog"]), 9000)

    positions = await db.get_request_services(str(request["idrequests"]))
    assert positions[0]["price_rub"] == 3000


async def test_snapshot_survives_service_deletion(service):
    request, items = await _make_request_with(service, [("Полировка фар", 1500)])
    await db.delete_catalog_item(service, str(items[0]["idcatalog"]))

    positions = await db.get_request_services(str(request["idrequests"]))
    assert positions[0]["title"] == "Полировка фар"
    assert positions[0]["price_rub"] == 1500
```

- [ ] **Шаг 2: Запустить и убедиться, что тесты падают**

```bash
.venv/Scripts/python.exe -m pytest tests/test_catalog.py -q -k "every_service or order_of_selection or snapshot"
```

Ожидается: `TypeError: create_request() got an unexpected keyword argument 'services'`

- [ ] **Шаг 3: Переписать `create_request`**

В сигнатуре заменить

```python
        idcatalog: str,
        service_title: str,
```

на

```python
        services: list[dict],
```

Заменить тело вставки заявки — колонки `idcatalog` и `service_title` из INSERT уходят, срабатывают дефолты:

```python
            row = await conn.fetchrow(
                """
                INSERT INTO requests
                    (idrequests, idservice, idclienttg, client_name, phone,
                     brand, model, plate, urgency, comment, client_uid,
                     status, idrecstatus)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'new',0)
                ON CONFLICT (client_uid) WHERE client_uid IS NOT NULL DO NOTHING
                RETURNING *
                """,
                _new_id(), idservice, client_tg_id, client_name, phone,
                brand, model, plate, urgency, comment, client_uid,
            )
```

Сразу после блока с `ForeignClientUid` и до вставки в `request_status_history` добавить запись позиций:

```python
            # Позиции пишутся в той же транзакции: заявки без услуг
            # существовать не должно даже мгновение
            await conn.executemany(
                """
                INSERT INTO request_services
                    (idrequestservice, idrequests, idcatalog, title, price_rub, position)
                VALUES ($1,$2,$3,$4,$5,$6)
                """,
                [
                    (
                        _new_id(),
                        row["idrequests"],
                        item["idcatalog"],
                        item["title"],
                        item.get("price_rub"),
                        index,
                    )
                    for index, item in enumerate(services)
                ],
            )
```

- [ ] **Шаг 4: Добавить чтение позиций**

Вставить в `database.py` рядом с `get_request`:

```python
    async def get_request_services(self, idrequests: str) -> list[asyncpg.Record]:
        """Позиции заявки в том порядке, в каком их выбирал клиент."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM request_services WHERE idrequests=$1 ORDER BY position",
                idrequests,
            )
```

- [ ] **Шаг 5: Перевести `create_request_flow` на список**

В `handlers/requests.py` импорт дополнить: `from validators import ValidationError, h, validate_catalog_ids, validate_request_fields, validate_uuid`

Заменить блок проверки услуги (сейчас — `idcatalog = validate_uuid(...)` и `item = await db.get_catalog_item(...)`) на:

```python
    try:
        fields = validate_request_fields(payload)
        idcatalogs = validate_catalog_ids(payload.get("idcatalogs"))
    except ValidationError as exc:
        raise RequestRejected(str(exc)) from exc

    # Услуги обязаны принадлежать этому сервису и быть активными: клиент мог
    # держать форму открытой, пока управляющий убирал что-то из списка
    items = await db.get_catalog_items(service_id, idcatalogs)
    if len(items) != len(idcatalogs):
        found = {str(item["idcatalog"]) for item in items}
        missing = [cid for cid in idcatalogs if cid not in found]
        # Называем услугу по имени: «одна из услуг недоступна» не подсказывает,
        # что именно переснимать в форме
        gone = await db.get_catalog_items(service_id, missing, only_active=False)
        names = ", ".join(f"«{row['title']}»" for row in gone)
        raise RequestRejected(
            f"Услуга {names} больше не оказывается — выберите заново."
            if names
            else "Одна из выбранных услуг недоступна. Откройте форму заново."
        )

    by_id = {str(item["idcatalog"]): item for item in items}
    services = [
        {
            "idcatalog": cid,
            "title": by_id[cid]["title"],
            "price_rub": by_id[cid]["price_rub"],
        }
        for cid in idcatalogs
    ]
```

В вызове `db.create_request(...)` заменить пару аргументов на `services=services`.

Подтверждение клиенту — вместо строки с одной услугой:

```python
    services_line = "\n".join(
        f"• {h(item['title'])} — {render.price_label(item['price_rub'])}"
        for item in services
    )
```

и в тексте сообщения `f"<b>Услуги:</b>\n{services_line}\n"` вместо прежней строки `<b>Услуга:</b> ...`.

- [ ] **Шаг 6: Обновить legacy-хендлер `web_app_data`**

Заменить проверку `if not payload.get("idcatalog")` на:

```python
    if not payload.get("idcatalogs"):
        await message.answer(
            "❌ Форма записи устарела — закройте её и откройте заново."
        )
        return
```

- [ ] **Шаг 7: Обновить `RequestPayload` в `app.py`**

Заменить `idcatalog: str = ""` на:

```python
    idcatalogs: list[str] = Field(default_factory=list)
```

- [ ] **Шаг 7б: Починить существующие тесты потока**

В `tests/test_catalog.py` уже есть два теста, написанных под одну услугу:
`test_create_request_flow_rejects_foreign_catalog_item` и
`test_create_request_flow_rejects_deleted_own_catalog_item`. Они собирают
payload с ключом `idcatalog` и после смены контракта начнут падать. В обоих
заменить строку вида

```python
        "idcatalog": str(foreign["idcatalog"]),
```

на список:

```python
        "idcatalogs": [str(foreign["idcatalog"])],
```

Тесты должны продолжать проверять ровно то же — отказ на чужой и на удалённой
услуге. Это единственная защита от подстановки чужой услуги, ослаблять её
нельзя.

- [ ] **Шаг 8: Прогнать тесты и проверку импорта**

```bash
.venv/Scripts/python.exe -m pytest tests/ -q -x
.venv/Scripts/python.exe -c "import app; print('ok')"
```

Ожидается: все тесты проходят; печатается `ok`.

- [ ] **Шаг 9: Коммит**

```bash
git add database.py handlers/requests.py app.py tests/test_catalog.py
git commit -m "Заявка: несколько услуг со снимками названия и цены"
```

---

### Задача 5: Карточка заявки и статистика на позициях

**Файлы:**
- Изменить: `render.py` (`request_card_for_staff`, `request_line_for_staff`, `request_line_for_client`, `stats_card`)
- Изменить: `database.py` (`get_service_requests`, `get_client_requests`, `get_service_breakdown`, `count_requests_by_catalog`)
- Изменить: `handlers/admin_actions.py` (три места), `handlers/requests.py` (два места)
- Изменить: `tests/test_catalog.py`

**Интерфейсы:**
- Потребляет: `db.get_request_services` (задача 4), `render.price_label` (задача 1)
- Производит: `render.request_card_for_staff(req, services, *, tz=None, title=...)` — вторым позиционным аргументом идут позиции заявки
- Производит: колонка `services_summary` в выборках списков заявок

- [ ] **Шаг 1: Написать падающие тесты**

Дописать в конец `tests/test_catalog.py`:

```python
async def test_breakdown_counts_every_service_of_request(service):
    """Заявка с тремя услугами — это три строки в статистике, а не одна."""
    await _make_request_with(
        service, [("Работа А", None), ("Работа Б", None), ("Работа В", None)]
    )
    breakdown = await db.get_service_breakdown(service)
    counted = {row["title"]: row["cnt"] for row in breakdown}
    assert counted["Работа А"] == 1
    assert counted["Работа Б"] == 1
    assert counted["Работа В"] == 1


async def test_count_requests_by_catalog_uses_positions(service):
    request, items = await _make_request_with(service, [("Полировка кузова", 3000)])
    used = await db.count_requests_by_catalog(service, str(items[0]["idcatalog"]))
    assert used == 1


async def test_request_list_carries_services_summary(service):
    await _make_request_with(service, [("Работа А", None), ("Работа Б", None)])
    rows = await db.get_service_requests(service, limit=5)
    assert "Работа А, Работа Б" in rows[0]["services_summary"]
```

- [ ] **Шаг 2: Запустить и убедиться, что тесты падают**

```bash
.venv/Scripts/python.exe -m pytest tests/test_catalog.py -q -k "breakdown_counts or by_catalog_uses or services_summary"
```

Ожидается: `KeyError: 'services_summary'` и расхождения в подсчёте статистики.

- [ ] **Шаг 3: Переписать статистику и подсчёт в `database.py`**

```python
    async def get_service_breakdown(self, idservice: str) -> list[asyncpg.Record]:
        """
        Сколько раз заказывали каждую услугу. Считаем по позициям, а не по
        заявкам: заявка с тремя работами — это три заказанные услуги.
        Группировка по снимку названия оставляет в статистике и те услуги,
        которые из каталога уже удалили.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT rs.title AS title, count(*) AS cnt
                FROM request_services rs
                JOIN requests r ON r.idrequests = rs.idrequests
                WHERE r.idservice=$1 AND r.idrecstatus=0
                GROUP BY rs.title ORDER BY cnt DESC
                """,
                idservice,
            )

    async def count_requests_by_catalog(self, idservice: str, idcatalog: str) -> int:
        """Сколько заявок содержит эту услугу — для текста подтверждения."""
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT count(DISTINCT rs.idrequests)
                FROM request_services rs
                JOIN requests r ON r.idrequests = rs.idrequests
                WHERE r.idservice=$1 AND rs.idcatalog=$2 AND r.idrecstatus=0
                """,
                idservice, idcatalog,
            )
        return value or 0
```

- [ ] **Шаг 4: Добавить сводку услуг в выборки списков**

Прочитать оба метода и в каждом дописать в список выбираемых полей — сразу после существующего перечисления, перед `FROM` — подзапрос:

```sql
                       ,(SELECT string_agg(rs.title, ', ' ORDER BY rs.position)
                           FROM request_services rs
                          WHERE rs.idrequests = r.idrequests) AS services_summary
```

Псевдоним таблицы заявок в этих запросах может отличаться: если он не `r`,
подставить используемый. Проверить это чтением, а не предположением.

Подзапрос, а не JOIN с группировкой: списки заявок читаются на каждый показ меню, и лишняя группировка по всем колонкам заявки здесь ни к чему. Тянуть позиции отдельным запросом на каждую заявку нельзя — это N+1 на самом частом экране.

- [ ] **Шаг 5: Перевести `render.py` на позиции**

`request_card_for_staff` принимает позиции вторым аргументом и печатает их списком:

```python
def request_card_for_staff(
    req, services, *, tz: str | None = None, title: str = "🚗 <b>НОВАЯ ЗАЯВКА</b>"
) -> str:
```

Строка `f"🔧 <b>Услуга:</b> {h(req['service_title'])}\n"` живёт внутри первого
f-string, которым собирается `text`. Её оттуда нужно **удалить**, а сразу после
присваивания `text = (...)` вставить блок:

```python
    priced = [item for item in services if item["price_rub"] is not None]
    lines = "".join(
        f"  • {h(item['title'])} — {price_label(item['price_rub'])}\n"
        for item in services
    )
    text += f"🔧 <b>Услуги:</b>\n{lines}"

    if priced:
        total = sum(item["price_rub"] for item in priced)
        # Оговорка обязательна: без неё сумма выглядит полной, хотя часть
        # работ в неё не вошла
        note = "" if len(priced) == len(services) else " <i>(часть работ — по запросу)</i>"
        text += "💰 <b>Итого:</b> от " + f"{total:,}".replace(",", " ") + f" ₽{note}\n"
```

Порядок важен: блок встаёт до строки со срочностью, чтобы услуги в карточке шли
там же, где раньше стояла одна услуга.

`request_line_for_staff` и `request_line_for_client` берут готовую сводку:

```python
        f"  🔧 {h(req['services_summary'] or '—')} | "
```

и

```python
        f"   Услуги: {h(req['services_summary'] or '—')}\n"
```

- [ ] **Шаг 6: Обновить вызовы**

`handlers/requests.py`, отправка карточки персоналу:

```python
    positions = await db.get_request_services(summary["request_id"])
    await notify_staff(
        bot,
        service_id,
        render.request_card_for_staff(request, positions, tz=service["timezone"]),
        ...
    )
```

`handlers/admin_actions.py` — в обоих местах, где строится карточка, перед вызовом добавить `positions = await db.get_request_services(request_id)` и передать их вторым аргументом.

- [ ] **Шаг 7: Прогнать тесты и проверку импорта**

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
.venv/Scripts/python.exe -c "import app; print('ok')"
```

- [ ] **Шаг 8: Убедиться, что ссылок на снимок одной услуги не осталось**

```bash
grep -rn "service_title" --include="*.py" .
```

Ожидается: пусто. Поиск ограничен `*.py`, поэтому упоминание колонки в
`schema.sql` (она остаётся legacy на один релиз) в выдачу не попадает.

- [ ] **Шаг 9: Коммит**

```bash
git add render.py database.py handlers/admin_actions.py handlers/requests.py tests/test_catalog.py
git commit -m "Карточки заявок и статистика считают все услуги заявки"
```

---

### Задача 6: Экран услуг — карточка услуги и цена

**Файлы:**
- Изменить: `keyboards.py` (`kb_catalog`, новая `kb_catalog_item`)
- Изменить: `handlers/catalog.py`
- Изменить: `tests/test_keyboards.py`

**Интерфейсы:**
- Потребляет: `db.set_catalog_item_price`, `db.add_catalog_item(..., price_rub)` (задача 3), `validators.validate_price`, `render.price_label` (задача 1)
- Производит: `keyboards.kb_catalog_item(idcatalog) -> InlineKeyboardMarkup`
- Callback-данные: `svcopen:<idcatalog>` открыть карточку, `svcprice:<idcatalog>` изменить цену, `svcdel:<idcatalog>` удалить (как было), `svcadd` добавить, `svclist` вернуться к списку

- [ ] **Шаг 1: Написать падающий тест клавиатуры**

Дописать в `tests/test_keyboards.py`:

```python
def test_catalog_list_opens_item_card(webapp_configured):
    """Тап по услуге ведёт в её карточку, а не сразу в удаление."""
    items = [{"idcatalog": SERVICE_ID, "title": "Полировка", "price_rub": 3000}]
    markup = kb.kb_catalog(items)
    assert markup.inline_keyboard[0][0].callback_data == f"svcopen:{SERVICE_ID}"


def test_catalog_item_card_has_price_and_delete(webapp_configured):
    markup = kb.kb_catalog_item(SERVICE_ID)
    actions = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"svcprice:{SERVICE_ID}" in actions
    assert f"svcdel:{SERVICE_ID}" in actions
    assert "svclist" in actions
```

- [ ] **Шаг 2: Запустить и убедиться, что тесты падают**

```bash
.venv/Scripts/python.exe -m pytest tests/test_keyboards.py -q
```

Ожидается: `AssertionError` на `svcopen` и `AttributeError: module 'keyboards' has no attribute 'kb_catalog_item'`

- [ ] **Шаг 3: Переписать клавиатуры каталога**

```python
def kb_catalog(items: list) -> InlineKeyboardMarkup:
    """Список услуг: тап открывает карточку услуги."""
    rows = [
        [InlineKeyboardButton(
            text=f"{item['title']} — {render.price_label(item['price_rub'])}",
            callback_data=f"svcopen:{item['idcatalog']}",
        )]
        for item in items
    ]
    rows.append([InlineKeyboardButton(
        text="➕ Добавить услугу", callback_data="svcadd"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_catalog_item(idcatalog: str) -> InlineKeyboardMarkup:
    """Карточка услуги: правка цены, удаление, возврат к списку."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Изменить цену", callback_data=f"svcprice:{idcatalog}"),
         InlineKeyboardButton(text="❌ Удалить услугу", callback_data=f"svcdel:{idcatalog}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="svclist")],
    ])
```

В начале `keyboards.py` добавить `import render`. Проверить, что это не создаёт цикл импорта: `render.py` импортирует `config` и `validators`, но не `keyboards`.

- [ ] **Шаг 4: Прогнать тесты клавиатур**

```bash
.venv/Scripts/python.exe -m pytest tests/test_keyboards.py -q
```

Ожидается: все проходят.

- [ ] **Шаг 5: Добавить в `handlers/catalog.py` цену при создании услуги**

Состояние FSM дополняется шагом цены:

```python
class ServiceCatalog(StatesGroup):
    title = State()
    price = State()


class ServicePrice(StatesGroup):
    """Правка цены уже заведённой услуги — отдельный поток, без ветвлений."""
    value = State()
```

В `add_finish` (ввод названия) вместо создания услуги сохраняем название и спрашиваем цену:

```python
@router.message(ServiceCatalog.title)
async def add_title(message: Message, state: FSMContext) -> None:
    svc = await _owner_service(message, state)
    if svc is None:
        await state.set_state(None)
        await show_main_menu(message, state)
        return

    try:
        title = validate_service_title(message.text)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}\nПопробуйте ещё раз:")
        return

    await state.update_data(new_title=title)
    await state.set_state(ServiceCatalog.price)
    await message.answer(
        f"Услуга: <b>{h(title)}</b>\n\n"
        "Введите цену в рублях — например <i>3000</i>.\n"
        "Отправьте <b>-</b>, если цену показывать не нужно.",
        reply_markup=kb.kb_cancel(),
    )


@router.message(ServiceCatalog.price)
async def add_price(message: Message, state: FSMContext) -> None:
    svc = await _owner_service(message, state)
    if svc is None:
        await state.set_state(None)
        await show_main_menu(message, state)
        return

    try:
        price = validate_price(message.text)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}")
        return

    data = await state.get_data()
    item = await db.add_catalog_item(str(svc["idservice"]), data["new_title"], price)
    if item is None:
        await state.set_state(None)
        await show_main_menu(message, state, greeting="❌ Такая услуга уже есть в списке.")
        await _show_catalog(message, svc)
        return

    await state.set_state(None)
    await show_main_menu(
        message, state, greeting=f"✅ Услуга «{h(item['title'])}» добавлена."
    )
    await _show_catalog(message, svc)
```

Кнопку отмены обработать для обоих состояний:

```python
@router.message(ServiceCatalog.title, F.text == kb.BTN_CANCEL)
@router.message(ServiceCatalog.price, F.text == kb.BTN_CANCEL)
@router.message(ServicePrice.value, F.text == kb.BTN_CANCEL)
async def add_cancel(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await show_main_menu(message, state, greeting="Отменено.")
```

- [ ] **Шаг 6: Добавить карточку услуги и правку цены**

```python
def _item_text(item) -> str:
    return (
        f"🔧 <b>{h(item['title'])}</b>\n\n"
        f"💰 Цена: {render.price_label(item['price_rub'])}"
    )


@router.callback_query(F.data.startswith("svcopen:"))
async def open_item(callback: CallbackQuery, state: FSMContext) -> None:
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

    await callback.message.edit_text(
        _item_text(item), reply_markup=kb.kb_catalog_item(idcatalog)
    )
    await callback.answer()


@router.callback_query(F.data == "svclist")
async def back_to_list(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await _owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        await callback.answer()
        return
    await _show_catalog(callback.message, svc, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("svcprice:"))
async def price_start(callback: CallbackQuery, state: FSMContext) -> None:
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

    await state.update_data(price_for=idcatalog)
    await state.set_state(ServicePrice.value)
    await callback.message.answer(
        f"Услуга: <b>{h(item['title'])}</b>\n"
        f"Сейчас: {render.price_label(item['price_rub'])}\n\n"
        "Введите новую цену в рублях или <b>-</b>, чтобы убрать её.",
        reply_markup=kb.kb_cancel(),
    )
    await callback.answer()


@router.message(ServicePrice.value)
async def price_finish(message: Message, state: FSMContext) -> None:
    svc = await _owner_service(message, state)
    if svc is None:
        await state.set_state(None)
        await show_main_menu(message, state)
        return

    try:
        price = validate_price(message.text)
    except ValidationError as exc:
        await message.answer(f"❌ {exc}")
        return

    data = await state.get_data()
    item = await db.set_catalog_item_price(
        str(svc["idservice"]), data["price_for"], price
    )
    await state.set_state(None)

    if item is None:
        await show_main_menu(message, state, greeting="❌ Услуга уже удалена.")
    else:
        await show_main_menu(
            message,
            state,
            greeting=f"✅ {h(item['title'])} — {render.price_label(item['price_rub'])}",
        )
    await _show_catalog(message, svc)
```

Импорты файла дополнить: `import render`, `from validators import ValidationError, h, validate_price, validate_service_title, validate_uuid`.

Функцию `_catalog_text` дополнить ценой в строках списка:

```python
    lines = "".join(
        f"{i}. {h(item['title'])} — {render.price_label(item['price_rub'])}\n"
        for i, item in enumerate(items, 1)
    )
```

- [ ] **Шаг 7: Проверить, что роутер собирается и тесты зелёные**

```bash
.venv/Scripts/python.exe -c "import app; print(len(app.dp.sub_routers), 'роутеров')"
.venv/Scripts/python.exe -m pytest tests/ -q
```

Ожидается: `6 роутеров`, все тесты проходят.

- [ ] **Шаг 8: Коммит**

```bash
git add keyboards.py handlers/catalog.py tests/test_keyboards.py
git commit -m "Экран услуг: карточка услуги и правка цены"
```

---

### Задача 7: API и форма записи

**Файлы:**
- Изменить: `app.py` (`api_service`)
- Изменить: `webapp/index.html`
- Изменить: `tests/test_api_security.py`

**Интерфейсы:**
- Потребляет: `RequestPayload.idcatalogs` (задача 4), `price_rub` в каталоге (задача 3)
- Производит: `GET /api/service/{id}` отдаёт в каждом элементе `catalog` поле `price_rub`; форма шлёт `idcatalogs: string[]`

- [ ] **Шаг 1: Отдать цену в API**

В `app.py`, в `api_service`, в сборке ответа:

```python
        "catalog": [
            {
                "idcatalog": str(c["idcatalog"]),
                "title": c["title"],
                "price_rub": c["price_rub"],
            }
            for c in items
        ],
```

- [ ] **Шаг 2: Убрать одиночный селект из формы**

В `webapp/index.html` заменить блок поля «Тип работы» на список с галочками:

```html
      <div class="field">
        <label>Услуги *</label>
        <div id="catalogList" class="catalog-list">
          ${catalog.map(s => `
            <label class="catalog-item">
              <input type="checkbox" class="catalog-cb" value="${esc(s.idcatalog)}">
              <span class="catalog-title">${esc(s.title)}</span>
              <span class="catalog-price">${esc(priceLabel(s.price_rub))}</span>
            </label>
          `).join("")}
        </div>
        <div class="catalog-total" id="catalogTotal"></div>
      </div>
```

- [ ] **Шаг 3: Добавить расчёт итога**

Рядом с `esc()` добавить помощники и пересчёт:

```js
function priceLabel(price) {
  if (price === null || price === undefined) return "цена по запросу";
  return "от " + String(price).replace(/\B(?=(\d{3})+(?!\d))/g, " ") + " ₽";
}

function selectedCatalogIds() {
  return [...document.querySelectorAll(".catalog-cb:checked")].map(cb => cb.value);
}

function updateTotal() {
  const chosen = selectedCatalogIds();
  const catalog = state.selectedService?.catalog || [];
  const picked = catalog.filter(s => chosen.includes(s.idcatalog));
  const priced = picked.filter(s => s.price_rub !== null && s.price_rub !== undefined);
  const box = el("catalogTotal");
  if (!box) return;

  if (picked.length === 0) { box.innerHTML = ""; return; }
  if (priced.length === 0) { box.innerHTML = "Итого: цена по запросу"; return; }

  const total = priced.reduce((sum, s) => sum + s.price_rub, 0);
  // Оговорка обязательна: без неё сумма выглядит полной, хотя часть работ
  // в неё не вошла
  const note = priced.length === picked.length ? "" : "<br><small>часть работ — по запросу</small>";
  box.innerHTML = "Итого: " + priceLabel(total) + note;
}
```

После отрисовки формы повесить обработчик:

```js
  document.querySelectorAll(".catalog-cb").forEach(cb =>
    cb.addEventListener("change", updateTotal)
  );
  updateTotal();
```

- [ ] **Шаг 4: Отправлять список идентификаторов**

В теле `POST /api/requests` заменить `idcatalog: get("f_catalog"),` на:

```js
        idcatalogs:   selectedCatalogIds(),
```

Перед отправкой добавить проверку — рядом с остальными проверками формы:

```js
  if (selectedCatalogIds().length === 0) {
    el("formError").innerHTML = `<div class="error-banner">Выберите хотя бы одну услугу.</div>`;
    return;
  }
```

- [ ] **Шаг 5: Добавить стили списка**

В `<style>` рядом с существующими правилами:

```css
  .catalog-list { display: flex; flex-direction: column; gap: 2px; }
  .catalog-item { display: flex; align-items: center; gap: 8px;
                  padding: 10px 8px; border-radius: 8px; cursor: pointer; }
  .catalog-item:active { background: var(--tg-theme-secondary-bg-color, #f1f1f1); }
  .catalog-title { flex: 1; }
  .catalog-price { opacity: .7; font-size: 13px; white-space: nowrap; }
  .catalog-total { margin-top: 10px; font-weight: 600; }
```

- [ ] **Шаг 6: Проверить, что старых упоминаний не осталось**

```bash
grep -n "f_catalog\|idcatalog:" webapp/index.html
```

Ожидается: пусто (поле `idcatalog` в значениях чекбоксов пишется как `s.idcatalog`, а не как ключ тела запроса).

- [ ] **Шаг 7: Проверить синтаксис JavaScript**

```bash
.venv/Scripts/python.exe -c "
import re, pathlib
html = pathlib.Path('webapp/index.html').read_text(encoding='utf-8')
pathlib.Path('app_extracted.js').write_text(re.findall(r'<script>(.*?)</script>', html, re.S)[-1], encoding='utf-8')
"
node --check app_extracted.js && rm app_extracted.js
```

Ожидается: без ошибок.

- [ ] **Шаг 8: Прогнать тесты и поднять приложение**

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
.venv/Scripts/python.exe -c "import app; print('ok')"
```

- [ ] **Шаг 9: Коммит**

```bash
git add app.py webapp/index.html tests/test_api_security.py
git commit -m "Форма записи: выбор нескольких услуг и ориентировочная сумма"
```

---

### Задача 8: Сквозная проверка

**Файлы:** изменений нет — только проверка.

- [ ] **Шаг 1: Прогнать всю сюиту**

```bash
.venv/Scripts/python.exe -m pytest -q
```

Ожидается: все тесты проходят, вывод чистый.

- [ ] **Шаг 2: Поднять приложение с публичным адресом**

Запустить `uvicorn app:app --port 8080` и туннель, подставить адрес в `BASE_URL`, перезапустить приложение.

- [ ] **Шаг 3: Пройти ручной чек-лист**

- [ ] «🔧 Услуги» → список показывает цены, у услуг без цены «цена по запросу»
- [ ] Тап по услуге → карточка с ценой и кнопками
- [ ] «💰 Изменить цену» → ввести `4500` → в списке появилось «от 4 500 ₽»
- [ ] Изменить цену на `-` → стало «цена по запросу»
- [ ] «➕ Добавить услугу» → название → цена `3000` → услуга в списке с ценой
- [ ] Добавить услугу, на шаге цены отправить `-` → услуга без цены
- [ ] На шаге цены ввести `абв` → понятная ошибка, шаг не потерян
- [ ] Открыть форму записи → список с галочками и ценами
- [ ] Отметить две услуги → итог пересчитался
- [ ] Отметить услугу без цены вместе с платной → появилась оговорка «часть работ — по запросу»
- [ ] Снять все галочки и отправить → «Выберите хотя бы одну услугу»
- [ ] Отправить заявку на две услуги → в карточке администратору обе позиции с ценами и итог
- [ ] «📋 Мои заявки» у клиента → обе услуги перечислены
- [ ] «📊 Статистика» → обе услуги в «Популярных работах»
- [ ] Поднять цену услуги в каталоге → в уже созданной заявке цена прежняя
- [ ] Удалить услугу, по которой есть заявка → подтверждение показывает число заявок, заявка после удаления читается

- [ ] **Шаг 4: Финальный коммит, если правки были**

```bash
git add -A
git commit -m "Правки по итогам сквозной проверки"
```

---

## Что осталось за рамками

- Расписание и запись на время — отдельная спека, следующий цикл.
- Длительность услуги: появится вместе с расписанием, где заработает.
- Поле «срочность» — потеряет смысл вместе с появлением выбора времени, тогда и уберём.
- Переименование услуг и ручная сортировка.
- Колонки `requests.idcatalog` и `requests.service_title` остаются legacy на один релиз.
