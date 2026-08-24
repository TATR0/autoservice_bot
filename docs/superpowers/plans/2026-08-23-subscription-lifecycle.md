# Подписка сервиса: срок, гейты и напоминания — план реализации

> **Для агентов-исполнителей:** ОБЯЗАТЕЛЬНЫЙ СУБ-НАВЫК — используйте
> `superpowers:subagent-driven-development` (рекомендуется) или
> `superpowers:executing-plans`. Шаги отмечаются чекбоксами `- [ ]`.

**Цель:** сервис, не оплативший подписку, перестаёт продавать новое время —
пропадает из поиска, не отдаёт форму и не принимает заявки, — но сохраняет
кабинет управляющего и уже записанных клиентов. Владельцу заранее приходят
напоминания, продлевает подписку владелец бота командой.

**Архитектура:** подписка — это одно поле `services.paid_until`. Активна ⇔
`paid_until > now()`. Отдельного статуса нет: состояние выводится, а не
переключается, поэтому рассинхронизироваться нечему. Арифметика дат живёт в
чистом модуле `subscription.py` (ни базы, ни сети, ни системных часов — как
`slots.py`), запросы — в `database.py`, гейты — в четырёх уже существующих
точках. Напоминания будит внешний cron; тик состояние подписки не меняет,
поэтому молчащий cron стоит писем, а не логики.

**Стек:** Python 3.14, aiogram 3, FastAPI, asyncpg, PostgreSQL (Neon). Тесты —
pytest + pytest-asyncio.

**Спека:** `docs/superpowers/specs/2026-08-23-subscription-lifecycle-design.md` —
читайте её вместе с планом, план не спорит со спекой, а исполняет её.

## Глобальные ограничения

- Жёсткий `DELETE` в `database.py` запрещён: удаление мягкое, через
  `idrecstatus = -1`. Исключение — только чистка в тестовых фикстурах.
- **Клиент никогда не слышит слов «подписка» и «оплата».** Ему — «сервис не
  принимает онлайн-запись» и телефон сервиса. Тексты про оплату видит только
  владелец сервиса.
- `db.get_service` фильтра по подписке **не получает**: иначе управляющий
  потеряет доступ к своему кабинету и не сможет заплатить.
- Пустое значение показывается пустотой. Заглушек вроде «не указано» не пишем,
  строку скрываем целиком.
- Все тексты для пользователя — по-русски, с ёфикацией как в остальном коде.
- Время в базе — `timestamptz`. «Сейчас» в чистых функциях приходит аргументом,
  а не берётся из системных часов.
- Каждая задача заканчивается коммитом и работающим приложением.
- Тесты слоя БД идут против настоящей базы из `DATABASE_URL` и пропускаются,
  если он не задан. Запуск — `.venv/Scripts/python.exe -m pytest`.
- Сюита целиком идёт ~7 минут (удалённая база). Во время работы гоняйте свой
  файл точечно, полный прогон — в последней задаче.

## Две поправки к спеке

Обе всплыли при раскладке на шаги, обе делают реализацию согласованной с тем,
что спека уже утверждает.

1. **Пробный период и бэкфил тоже пишутся в журнал** (`source = 'trial'` и
   `'backfill'`). Спека называет `subscription_payments` единственным местом,
   где меняется `paid_until`; без этих строк утверждение было бы ложным, а на
   вопрос «почему у сервиса такой срок» журнал отвечал бы «не знаю».
2. **Колонка `source` без CHECK-констрейнта.** Набор источников открыт: завтра
   появится провайдер, и его имя не должно требовать миграции.

---

### Задача 1: Чистый расчёт подписки

**Файлы:**
- Создать: `subscription.py`
- Тест: `tests/test_subscription.py`

**Интерфейсы:**
- Потребляет: ничего (модуль ни от чего в проекте не зависит)
- Производит:
  - `subscription.REMIND_LEAD_DAYS: int`
  - `subscription.STAGE_5D / STAGE_24H / STAGE_EXPIRED: str`
  - `subscription.is_active(paid_until: datetime | None, now: datetime) -> bool`
  - `subscription.extend(paid_until: datetime | None, now: datetime, days: int) -> datetime`
  - `subscription.due_stage(paid_until: datetime | None, now: datetime) -> str | None`

- [ ] **Шаг 1: Написать падающие тесты**

Создайте `tests/test_subscription.py`:

```python
"""Тесты подписки. Базы не требуют — модуль чистый."""

from datetime import datetime, timedelta, timezone

import subscription

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def test_future_date_is_active():
    assert subscription.is_active(NOW + timedelta(seconds=1), NOW)


def test_past_date_is_not_active():
    assert not subscription.is_active(NOW - timedelta(seconds=1), NOW)


def test_expiring_exactly_now_is_not_active():
    """Граница закрыта в пользу отключения: срок «до» — значит до."""
    assert not subscription.is_active(NOW, NOW)


def test_never_paid_is_not_active():
    assert not subscription.is_active(None, NOW)


def test_extend_before_expiry_adds_to_remainder():
    """Заплатил заранее — оплаченные дни не сгорают."""
    paid_until = NOW + timedelta(days=3)
    assert subscription.extend(paid_until, NOW, 30) == paid_until + timedelta(days=30)


def test_extend_after_expiry_counts_from_now():
    """Иначе оплата уходит в оплату уже прошедшего простоя."""
    paid_until = NOW - timedelta(days=10)
    assert subscription.extend(paid_until, NOW, 30) == NOW + timedelta(days=30)


def test_extend_from_scratch_counts_from_now():
    assert subscription.extend(None, NOW, 5) == NOW + timedelta(days=5)


def test_no_reminder_while_expiry_is_far():
    assert subscription.due_stage(NOW + timedelta(days=6), NOW) is None


def test_five_days_stage_inside_the_lead():
    assert subscription.due_stage(NOW + timedelta(days=4), NOW) == subscription.STAGE_5D


def test_last_day_gives_the_24h_stage():
    assert subscription.due_stage(NOW + timedelta(hours=23), NOW) == subscription.STAGE_24H


def test_expired_stage_right_after_the_deadline():
    assert subscription.due_stage(NOW - timedelta(seconds=1), NOW) == subscription.STAGE_EXPIRED


def test_long_silent_cron_does_not_send_stale_stages():
    """
    Крон молчал двое суток, срок за это время прошёл.

    Досылать «осталось 24 часа» вдогонку нельзя: это неправда в момент
    получения. Отдаётся только самая поздняя подошедшая стадия.
    """
    assert subscription.due_stage(NOW - timedelta(days=2), NOW) == subscription.STAGE_EXPIRED


def test_never_paid_gets_no_reminder():
    """Напоминать не о чем: срока никогда не было."""
    assert subscription.due_stage(None, NOW) is None
```

- [ ] **Шаг 2: Запустить тесты, убедиться, что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription.py -q`
Ожидается: FAIL — `ModuleNotFoundError: No module named 'subscription'`

- [ ] **Шаг 3: Написать модуль**

Создайте `subscription.py`:

```python
"""
subscription.py — срок подписки сервиса.

Модуль чистый: ни базы, ни сети, ни системных часов — «сейчас» приходит
аргументом, как в slots.py. Состояние подписки нигде не хранится отдельным
полем: активность выводится из paid_until. Переключать нечего, значит нечему
и разъехаться.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# За сколько предупреждаем о конце срока. Не то же самое, что TRIAL_DAYS:
# сегодня оба равны пяти, но означают разное — длину пробного периода и
# заблаговременность напоминания
REMIND_LEAD_DAYS = 5

STAGE_5D = "5d"
STAGE_24H = "24h"
STAGE_EXPIRED = "expired"


def is_active(paid_until: datetime | None, now: datetime) -> bool:
    """Продаёт ли сервис новое время прямо сейчас."""
    return paid_until is not None and paid_until > now


def extend(paid_until: datetime | None, now: datetime, days: int) -> datetime:
    """
    Новый срок после продления на days дней.

    Отсчёт от большего из «сейчас» и текущего срока: заплатил заранее — дни
    прибавляются к остатку и не сгорают; заплатил после просрочки — отсчёт с
    сегодня, иначе деньги уходят в оплату уже прошедшего простоя.
    """
    base = max(now, paid_until) if paid_until else now
    return base + timedelta(days=days)


def due_stage(paid_until: datetime | None, now: datetime) -> str | None:
    """
    Какое напоминание причитается прямо сейчас. None — ещё рано или не о чем.

    Отдаётся самая поздняя подошедшая стадия, пропущенные не досылаются: если
    крон молчал сутки и срок успел кончиться, «осталось 24 часа» — уже неправда.
    """
    if paid_until is None:
        return None
    left = paid_until - now
    if left <= timedelta(0):
        return STAGE_EXPIRED
    if left <= timedelta(hours=24):
        return STAGE_24H
    if left <= timedelta(days=REMIND_LEAD_DAYS):
        return STAGE_5D
    return None
```

- [ ] **Шаг 4: Запустить тесты, убедиться, что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription.py -q`
Ожидается: PASS, 13 тестов

- [ ] **Шаг 5: Коммит**

```bash
git add subscription.py tests/test_subscription.py
git commit -m "Срок подписки считается чистой функцией"
```

---

### Задача 2: Переменные окружения

**Файлы:**
- Изменить: `config.py` (секция `Misc`, после `DEFAULT_TIMEZONE`)
- Изменить: `.env.example` (секция `Misc`)
- Тест: `tests/test_config.py` (создать)

**Интерфейсы:**
- Потребляет: `subscription.REMIND_LEAD_DAYS` — задача 1
- Производит:
  - `config.TICK_SECRET: str`
  - `config.BOT_OWNER_IDS: tuple[int, ...]`
  - `config.SUPPORT_CONTACT: str`
  - `config.TRIAL_DAYS: int`
  - `config._int_list(raw: str) -> tuple[int, ...]`

- [ ] **Шаг 1: Написать падающие тесты**

Создайте `tests/test_config.py`:

```python
"""Разбор переменных окружения, где он не сводится к одному int()."""

import config


def test_owner_ids_parse_a_comma_list():
    assert config._int_list("1,2,3") == (1, 2, 3)


def test_owner_ids_tolerate_spaces_and_trailing_comma():
    """Список правится руками в .env — лишняя запятая не должна ронять старт."""
    assert config._int_list(" 42 , 77 , ") == (42, 77)


def test_empty_owner_ids_give_an_empty_tuple():
    assert config._int_list("") == ()


def test_owner_ids_are_a_tuple_by_default():
    """Значение конфига неизменяемо: подправить его из хендлера нельзя."""
    assert isinstance(config.BOT_OWNER_IDS, tuple)
```

- [ ] **Шаг 2: Запустить тесты, убедиться, что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_config.py -q`
Ожидается: FAIL — `AttributeError: module 'config' has no attribute '_int_list'`

- [ ] **Шаг 3: Добавить переменные в `config.py`**

В секцию `# ── Misc ───` после строки `DEFAULT_TIMEZONE = ...`:

```python
def _int_list(raw: str) -> tuple[int, ...]:
    """«1, 2 ,» → (1, 2). Пустые куски пропускаются: .env правят руками."""
    return tuple(int(part) for part in raw.split(",") if part.strip())


# Кому доступна команда /extend. Не MASTER_CHAT_ID: тот — чат для
# недоставленных уведомлений и вполне может оказаться группой, а у группы id
# отрицательный и с user id не совпадёт никогда
BOT_OWNER_IDS: tuple[int, ...] = _int_list(os.getenv("BOT_OWNER_IDS", ""))

# Секрет эндпоинта напоминаний. Пусто — эндпоинт закрыт совсем
TICK_SECRET: str = os.getenv("TICK_SECRET", "").strip()

# Куда управляющему писать за продлением. Пусто — строку не показываем
SUPPORT_CONTACT: str = os.getenv("SUPPORT_CONTACT", "").strip()

# Пробный период нового сервиса, дней
TRIAL_DAYS: int = int(os.getenv("TRIAL_DAYS") or 5)
```

- [ ] **Шаг 4: Дописать `.env.example`**

В секцию `# ── Misc ───`, после `DEFAULT_TIMEZONE`:

```
BOT_OWNER_IDS=                   # Telegram id владельцев бота через запятую, для /extend
TICK_SECRET=                     # секрет POST /internal/subscriptions/tick; пусто — закрыт
SUPPORT_CONTACT=@your_username   # куда управляющему писать за продлением подписки
TRIAL_DAYS=5                     # пробный период нового сервиса, дней
```

- [ ] **Шаг 5: Запустить тесты, убедиться, что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_config.py -q`
Ожидается: PASS, 4 теста

- [ ] **Шаг 6: Коммит**

```bash
git add config.py .env.example tests/test_config.py
git commit -m "Настройки подписки: владельцы бота, секрет тика, контакт, триал"
```

---

### Задача 3: Схема — срок, журнал, отметки, бэкфил

**Файлы:**
- Изменить: `schema.sql` (после секции заявок, перед `request_status_history`)
- Тест: `tests/test_subscription_db.py` (создать)

**Интерфейсы:**
- Потребляет: таблицу `services` — уже есть
- Производит: колонку `services.paid_until` типа `timestamptz`, таблицы
  `subscription_payments` и `subscription_reminders` с их индексами

**Важно:** схема накатывается руками в SQL Editor. После правки `schema.sql`
выполните её содержимое на тестовой базе, иначе тесты этой задачи не пройдут.

Фикстуру `service` править не нужно: обе таблицы ссылаются на `services`
через `ON DELETE CASCADE`, и её `DELETE FROM services` уносит их строки за
собой. Мягкого удаления это не нарушает — жёсткий `DELETE` живёт только в
фикстурах.

- [ ] **Шаг 1: Написать падающие тесты**

Создайте `tests/test_subscription_db.py`:

```python
"""
Схема подписки. Идут против настоящей базы.

Проверяются те свойства схемы, на которые опирается код выше: тип колонки
срока, однократность напоминания и запрет продления «на ноль дней».
"""

from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from database import db

pytestmark = pytest.mark.asyncio

LATER = datetime(2027, 1, 1, tzinfo=timezone.utc)


async def test_paid_until_keeps_the_time_of_day(service):
    """
    Колонка обязана быть timestamptz.

    С date «за 24 часа» превратилось бы в «в полночь накануне по серверному
    времени», а у сервисов свои зоны.
    """
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE services SET paid_until=$2 WHERE idservice=$1", service, LATER
        )
        stored = await conn.fetchval(
            "SELECT paid_until FROM services WHERE idservice=$1", service
        )
    assert stored == LATER


async def test_reminder_is_claimed_once(service):
    """Ключ отметки — сервис, срок и стадия. Второй такой же не пройдёт."""
    async with db.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO subscription_reminders (idservice, paid_until, stage)"
            " VALUES ($1,$2,'24h')",
            service, LATER,
        )
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await conn.execute(
                "INSERT INTO subscription_reminders (idservice, paid_until, stage)"
                " VALUES ($1,$2,'24h')",
                service, LATER,
            )


async def test_new_deadline_gets_its_own_reminders(service):
    """После продления срок другой — значит, напоминания по нему свои."""
    async with db.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO subscription_reminders (idservice, paid_until, stage)"
            " VALUES ($1,$2,'24h')",
            service, LATER,
        )
        await conn.execute(  # не бросает
            "INSERT INTO subscription_reminders (idservice, paid_until, stage)"
            " VALUES ($1,$2,'24h')",
            service, LATER + timedelta(days=30),
        )


async def test_payment_of_zero_days_is_rejected(service):
    """Продление, ничего не продлевающее, — это ошибка вызывающего."""
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                "INSERT INTO subscription_payments (idservice, days, paid_until)"
                " VALUES ($1, 0, $2)",
                service, LATER,
            )


async def test_same_external_payment_lands_once(service):
    """
    Ради этого журнал заводится сейчас, а не вместе с провайдером: повторный
    вебхук того же платежа не должен продлить подписку дважды.
    """
    async with db.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO subscription_payments"
            " (idservice, days, paid_until, source, external_id)"
            " VALUES ($1, 30, $2, 'yookassa', 'pay-1')",
            service, LATER,
        )
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await conn.execute(
                "INSERT INTO subscription_payments"
                " (idservice, days, paid_until, source, external_id)"
                " VALUES ($1, 30, $2, 'yookassa', 'pay-1')",
                service, LATER,
            )


async def test_manual_payments_do_not_collide(service):
    """У ручных продлений external_id пуст — индекс частичный и их не трогает."""
    async with db.pool.acquire() as conn:
        for _ in range(2):
            await conn.execute(
                "INSERT INTO subscription_payments (idservice, days, paid_until)"
                " VALUES ($1, 30, $2)",
                service, LATER,
            )
```

- [ ] **Шаг 2: Запустить тесты, убедиться, что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -q`
Ожидается: FAIL — `UndefinedTableError: relation "subscription_reminders" does not exist`

- [ ] **Шаг 3: Дописать `schema.sql`**

После блока констрейнтов `requests`, перед `-- ── История смены статусов ──`:

```sql
-- ── Подписка сервиса ─────────────────────────────────────────
-- Состояние подписки нигде не хранится: активна ⇔ paid_until > now().
-- Переключать нечего, поэтому нечему и разъехаться с реальностью.

-- Срок — момент, а не дата: «за 24 часа» иначе превратилось бы в «в полночь
-- накануне по серверному времени», а у сервисов свои зоны. Колонка пуста во
-- всех строках, поэтому смена типа сегодня бесплатна
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'services' AND column_name = 'paid_until'
                  AND data_type = 'date') THEN
        ALTER TABLE services ALTER COLUMN paid_until TYPE timestamptz;
    END IF;
END $$;

-- Журнал продлений: единственное место, где меняется paid_until.
-- Источник — открытый набор: 'trial', 'backfill', 'manual', завтра провайдер
CREATE TABLE IF NOT EXISTS subscription_payments (
    idpayment   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    idservice   uuid        NOT NULL REFERENCES services(idservice) ON DELETE CASCADE,
    days        integer     NOT NULL,
    paid_until  timestamptz NOT NULL,   -- каким срок стал после этого продления
    source      text        NOT NULL DEFAULT 'manual',
    external_id text,                   -- id платежа у провайдера, NULL для ручных
    granted_by  bigint,                 -- Telegram id продлившего вручную
    createdate  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_subscription_payments_service
    ON subscription_payments (idservice, createdate DESC);

-- Повторная доставка того же платежа упрётся сюда и не продлит подписку
-- дважды. Вебхуки повторяются всегда, это не редкий случай
CREATE UNIQUE INDEX IF NOT EXISTS idx_subscription_payments_external
    ON subscription_payments (source, external_id) WHERE external_id IS NOT NULL;

ALTER TABLE subscription_payments DROP CONSTRAINT IF EXISTS chk_subscription_payments_days;
ALTER TABLE subscription_payments ADD  CONSTRAINT chk_subscription_payments_days
    CHECK (days > 0);

-- Отметки об отправленных напоминаниях. В ключ входит сам срок: после
-- продления новый срок получает свои напоминания, а напомнить повторно про
-- старый невозможно
CREATE TABLE IF NOT EXISTS subscription_reminders (
    idreminder uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    idservice  uuid        NOT NULL REFERENCES services(idservice) ON DELETE CASCADE,
    paid_until timestamptz NOT NULL,
    stage      text        NOT NULL,
    createdate timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_subscription_reminders_once
    ON subscription_reminders (idservice, paid_until, stage);

ALTER TABLE subscription_reminders DROP CONSTRAINT IF EXISTS chk_subscription_reminders_stage;
ALTER TABLE subscription_reminders ADD  CONSTRAINT chk_subscription_reminders_stage
    CHECK (stage IN ('5d','24h','expired'));

-- Бэкфил. Без него в момент выката каждый живой сервис окажется просроченным:
-- пропадёт из поиска и перестанет принимать заявки, тихо, среди рабочего дня.
-- WHERE paid_until IS NULL — повторный прогон схемы никому ничего не перепишет
WITH granted AS (
    UPDATE services SET paid_until = now() + interval '30 days'
     WHERE idrecstatus = 0 AND paid_until IS NULL
 RETURNING idservice, paid_until
)
INSERT INTO subscription_payments (idservice, days, paid_until, source)
SELECT idservice, 30, paid_until, 'backfill' FROM granted;
```

- [ ] **Шаг 4: Накатить схему и запустить тесты**

Выполните `schema.sql` в SQL Editor тестовой базы, затем:
Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -q`
Ожидается: PASS, 6 тестов

- [ ] **Шаг 5: Коммит**

```bash
git add schema.sql tests/test_subscription_db.py
git commit -m "Схема подписки: срок моментом, журнал продлений, отметки напоминаний"
```

---

### Задача 4: Продление в слое БД

**Файлы:**
- Изменить: `database.py` (новая секция после `close_service`)
- Тест: `tests/test_subscription_db.py`

**Интерфейсы:**
- Потребляет: `subscription.extend` — задача 1; таблицы — задача 3
- Производит:
  - `db.extend_subscription(idservice: str, *, days: int, source: str = "manual", external_id: str | None = None, granted_by: int | None = None) -> datetime | None`
    (`None` — активного сервиса с таким id нет)

- [ ] **Шаг 1: Написать падающие тесты**

Допишите в `tests/test_subscription_db.py`:

```python
# ── Продление ────────────────────────────────────────────────────────────────

import uuid


async def test_extension_sets_the_deadline_and_logs_it(service):
    """Срок и журнал меняются вместе — иначе на «почему» отвечать нечем."""
    paid_until = await db.extend_subscription(service, days=30, granted_by=42)
    assert paid_until is not None

    async with db.pool.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT paid_until FROM services WHERE idservice=$1", service
        )
        logged = await conn.fetchrow(
            "SELECT * FROM subscription_payments WHERE idservice=$1"
            " ORDER BY createdate DESC LIMIT 1",
            service,
        )
    assert stored == paid_until
    assert logged["days"] == 30
    assert logged["paid_until"] == paid_until
    assert logged["source"] == "manual"
    assert logged["granted_by"] == 42


async def test_second_extension_counts_from_the_first(service):
    """Два продления подряд не должны считать от одного и того же остатка."""
    first = await db.extend_subscription(service, days=30)
    second = await db.extend_subscription(service, days=30)
    assert second == first + timedelta(days=30)


async def test_extension_of_a_missing_service_changes_nothing(service):
    assert await db.extend_subscription(str(uuid.uuid4()), days=30) is None
```

- [ ] **Шаг 2: Запустить тесты, убедиться, что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -k extension -q`
Ожидается: FAIL — `AttributeError: 'Database' object has no attribute 'extend_subscription'`

- [ ] **Шаг 3: Реализовать**

В `database.py` добавьте `import subscription` к остальным импортам, а после
`close_service` — новую секцию:

```python
    # ── Подписка ─────────────────────────────────────────────────────────────

    async def extend_subscription(
        self,
        idservice: str,
        *,
        days: int,
        source: str = "manual",
        external_id: str | None = None,
        granted_by: int | None = None,
    ) -> datetime | None:
        """
        Продлить подписку и записать продление в журнал — одной транзакцией.

        Порознь нельзя: однажды появится либо срок без следа в журнале, либо
        запись о платеже, который ничего не продлил.

        Строка сервиса блокируется на время расчёта: два продления подряд не
        должны посчитать новый срок от одного и того же остатка.
        """
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT paid_until FROM services"
                " WHERE idservice=$1 AND idrecstatus=0 FOR UPDATE",
                idservice,
            )
            if row is None:
                return None
            paid_until = subscription.extend(row["paid_until"], now, days)
            await conn.execute(
                "UPDATE services SET paid_until=$2 WHERE idservice=$1",
                idservice, paid_until,
            )
            await conn.execute(
                """
                INSERT INTO subscription_payments
                    (idservice, days, paid_until, source, external_id, granted_by)
                VALUES ($1,$2,$3,$4,$5,$6)
                """,
                idservice, days, paid_until, source, external_id, granted_by,
            )
        return paid_until
```

- [ ] **Шаг 4: Запустить тесты, убедиться, что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -q`
Ожидается: PASS, 9 тестов

- [ ] **Шаг 5: Коммит**

```bash
git add database.py tests/test_subscription_db.py
git commit -m "Продление подписки пишет срок и журнал одной транзакцией"
```

---

### Задача 5: Пробный период при регистрации

**Файлы:**
- Изменить: `database.py` (`create_service`, около строки 155)
- Тест: `tests/test_subscription_db.py`

**Интерфейсы:**
- Потребляет: `config.TRIAL_DAYS` — задача 2; `subscription.extend` — задача 1
- Производит: новый сервис со сроком `now + TRIAL_DAYS` и предзанятой отметкой
  напоминания `5d`

**Почему отметка ставится сразу:** триал длится ровно `REMIND_LEAD_DAYS` дней,
поэтому стадия `5d` подошла бы в секунду регистрации. Дату окончания называет
приветствие при регистрации (задача 10), а тик про неё больше не вспомнит.

- [ ] **Шаг 1: Написать падающие тесты**

Допишите в `tests/test_subscription_db.py`:

```python
# ── Пробный период ───────────────────────────────────────────────────────────

import config
import subscription


async def test_new_service_starts_with_a_trial(service):
    """Управляющий должен увидеть, как это работает, прежде чем платить."""
    svc = await db.get_service(service)
    now = datetime.now(timezone.utc)
    assert subscription.is_active(svc["paid_until"], now)
    left = svc["paid_until"] - now
    assert timedelta(days=config.TRIAL_DAYS - 1) < left <= timedelta(days=config.TRIAL_DAYS)


async def test_trial_is_written_to_the_journal(service):
    """Журнал отвечает на «почему у сервиса такой срок» — в том числе про триал."""
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM subscription_payments WHERE idservice=$1", service
        )
    assert row["source"] == "trial"
    assert row["days"] == config.TRIAL_DAYS


async def test_trial_does_not_trigger_the_five_day_reminder(service):
    """
    Триал длится ровно столько, за сколько мы предупреждаем.

    Без предзанятой отметки управляющий получил бы «осталось 5 дней» в секунду
    регистрации, сразу после приветствия, где эта дата уже названа.
    """
    svc = await db.get_service(service)
    claimed = await db.claim_reminder(service, svc["paid_until"], subscription.STAGE_5D)
    assert claimed is False
```

- [ ] **Шаг 2: Запустить тесты, убедиться, что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -k trial -q`
Ожидается: FAIL — `assert None is not None` (у нового сервиса срок пуст)

- [ ] **Шаг 3: Реализовать**

В `create_service`, внутри существующей транзакции, после `INSERT INTO admins`:

```python
            # Пробный период — это просто срок, проставленный при регистрации:
            # отдельной сущности «триал» нет и заводить её незачем
            paid_until = subscription.extend(
                None, datetime.now(timezone.utc), config.TRIAL_DAYS
            )
            await conn.execute(
                "UPDATE services SET paid_until=$2 WHERE idservice=$1",
                idservice, paid_until,
            )
            await conn.execute(
                """
                INSERT INTO subscription_payments (idservice, days, paid_until, source)
                VALUES ($1,$2,$3,'trial')
                """,
                idservice, config.TRIAL_DAYS, paid_until,
            )
            # Триал длится ровно столько, за сколько предупреждаем, поэтому
            # стадия «5d» подошла бы прямо сейчас. Дату называет приветствие
            await conn.execute(
                """
                INSERT INTO subscription_reminders (idservice, paid_until, stage)
                VALUES ($1,$2,$3)
                """,
                idservice, paid_until, subscription.STAGE_5D,
            )
```

- [ ] **Шаг 4: Запустить тесты**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -q`
Ожидается: FAIL на `test_trial_does_not_trigger_the_five_day_reminder` —
`claim_reminder` появится в задаче 7. Остальные — PASS.

Отметьте этот тест `@pytest.mark.xfail(reason="claim_reminder — задача 7", strict=True)`
и снимите пометку в задаче 7.

- [ ] **Шаг 5: Коммит**

```bash
git add database.py tests/test_subscription_db.py
git commit -m "Новый сервис получает пробный период"
```

---

### Задача 6: Просроченный сервис не находится в поиске

**Файлы:**
- Изменить: `database.py` (`get_services_by_city`, около строки 205)
- Тест: `tests/test_subscription_db.py`

**Интерфейсы:**
- Потребляет: колонку `paid_until` — задача 3
- Производит: `db.get_services_by_city` отдаёт только оплаченные сервисы

- [ ] **Шаг 1: Написать падающие тесты**

Допишите в `tests/test_subscription_db.py`:

```python
# ── Поиск ────────────────────────────────────────────────────────────────────


async def _expire(idservice: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE services SET paid_until=now() - interval '1 day' WHERE idservice=$1",
            idservice,
        )


async def test_paid_service_is_found_by_city(service):
    svc = await db.get_service(service)
    found = await db.get_services_by_city(svc["city"])
    assert any(str(row["idservice"]) == service for row in found)


async def test_expired_service_disappears_from_search(service):
    svc = await db.get_service(service)
    await _expire(service)
    found = await db.get_services_by_city(svc["city"])
    assert all(str(row["idservice"]) != service for row in found)


async def test_expired_service_is_still_available_to_its_owner(service):
    """
    Гейт стоит на продаже времени, а не на существовании сервиса.

    Спрячь сервис от get_service — и управляющий потеряет кабинет вместе с
    возможностью заплатить. Ровно та ошибка, из-за которой отвергнут вариант
    с idrecstatus = -1.
    """
    await _expire(service)
    assert await db.get_service(service) is not None
    assert await db.get_owner_services(999_000_001)
```

- [ ] **Шаг 2: Запустить тесты, убедиться, что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -k search -q`
Ожидается: FAIL на `test_expired_service_disappears_from_search`

- [ ] **Шаг 3: Реализовать**

В `get_services_by_city`:

```python
                """
                SELECT * FROM services
                WHERE LOWER(TRIM(city))=LOWER(TRIM($1)) AND idrecstatus=0
                  -- Не оплатил — не продаёт новое время. Кабинет управляющего
                  -- при этом открыт: get_service такого фильтра не получает
                  AND paid_until > now()
                ORDER BY service_name
                """,
```

- [ ] **Шаг 4: Запустить тесты, убедиться, что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -q`
Ожидается: PASS (кроме помеченного xfail из задачи 5)

- [ ] **Шаг 5: Коммит**

```bash
git add database.py tests/test_subscription_db.py
git commit -m "Просроченный сервис пропадает из поиска, но не из кабинета"
```

---

### Задача 7: Кандидаты на напоминание и захват отметки

**Файлы:**
- Изменить: `database.py` (секция «Подписка», после `extend_subscription`)
- Изменить: `tests/test_subscription_db.py` (снять `xfail` из задачи 5)
- Тест: `tests/test_subscription_db.py`

**Интерфейсы:**
- Потребляет: `subscription.REMIND_LEAD_DAYS` — задача 1
- Производит:
  - `db.services_for_reminders() -> list[asyncpg.Record]` (поля `idservice`,
    `service_name`, `owner_id`, `timezone`, `paid_until`)
  - `db.claim_reminder(idservice: str, paid_until: datetime, stage: str) -> bool`
    (`True` — отметка наша, отправлять; `False` — уже отправляли)

- [ ] **Шаг 1: Написать падающие тесты**

Допишите в `tests/test_subscription_db.py`:

```python
# ── Напоминания ──────────────────────────────────────────────────────────────


async def test_service_close_to_expiry_becomes_a_candidate(service):
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE services SET paid_until=now() + interval '2 days' WHERE idservice=$1",
            service,
        )
    candidates = await db.services_for_reminders()
    assert any(str(row["idservice"]) == service for row in candidates)


async def test_service_with_a_distant_deadline_is_not_a_candidate(service):
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE services SET paid_until=now() + interval '90 days' WHERE idservice=$1",
            service,
        )
    candidates = await db.services_for_reminders()
    assert all(str(row["idservice"]) != service for row in candidates)


async def test_long_expired_service_is_not_a_candidate(service):
    """Своё он уже получил, перебирать его каждый час незачем."""
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE services SET paid_until=now() - interval '60 days' WHERE idservice=$1",
            service,
        )
    candidates = await db.services_for_reminders()
    assert all(str(row["idservice"]) != service for row in candidates)


async def test_reminder_is_claimed_by_the_first_caller_only(service):
    """
    Главное свойство всей конструкции: повторный тик, два тика внахлёст и
    перезапуск посреди рассылки дают одно письмо.
    """
    moment = datetime.now(timezone.utc) + timedelta(days=1)
    assert await db.claim_reminder(service, moment, "24h") is True
    assert await db.claim_reminder(service, moment, "24h") is False


async def test_new_deadline_can_be_claimed_again(service):
    """После продления срок другой — напоминания по нему свои."""
    moment = datetime.now(timezone.utc) + timedelta(days=1)
    await db.claim_reminder(service, moment, "24h")
    assert await db.claim_reminder(service, moment + timedelta(days=30), "24h") is True
```

- [ ] **Шаг 2: Запустить тесты, убедиться, что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -k "candidate or claimed" -q`
Ожидается: FAIL — `AttributeError: 'Database' object has no attribute 'services_for_reminders'`

- [ ] **Шаг 3: Реализовать**

После `extend_subscription`:

```python
    async def services_for_reminders(self) -> list[asyncpg.Record]:
        """
        Сервисы, которым может причитаться напоминание о подписке.

        Стадию считает subscription.due_stage: здесь только сужаем выборку.
        Нижняя граница отсекает давно истёкших — своё они получили, а
        перебирать их каждый час незачем.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                f"""
                SELECT idservice, service_name, owner_id, timezone, paid_until
                FROM services
                WHERE idrecstatus = 0
                  AND paid_until IS NOT NULL
                  AND paid_until > now() - interval '30 days'
                  AND paid_until < now() + interval '{subscription.REMIND_LEAD_DAYS} days'
                """
            )

    async def claim_reminder(
        self, idservice: str, paid_until: datetime, stage: str
    ) -> bool:
        """
        Занять право отправить напоминание. False — его уже отправляли.

        Отметка ставится до отправки: при сбое теряется одно письмо, а не
        управляющий получает его заново на каждом тике.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO subscription_reminders (idservice, paid_until, stage)
                VALUES ($1,$2,$3)
                ON CONFLICT DO NOTHING
                RETURNING idreminder
                """,
                idservice, paid_until, stage,
            )
        return row is not None
```

- [ ] **Шаг 4: Снять `xfail` с теста из задачи 5**

Уберите строку `@pytest.mark.xfail(...)` над
`test_trial_does_not_trigger_the_five_day_reminder`.

- [ ] **Шаг 5: Запустить тесты, убедиться, что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -q`
Ожидается: PASS, 20 тестов, ни одного xfail

- [ ] **Шаг 6: Коммит**

```bash
git add database.py tests/test_subscription_db.py
git commit -m "Напоминание отправляется один раз на срок и стадию"
```

---

### Задача 8: Гейты формы и приёма заявки

**Файлы:**
- Изменить: `app.py` (`/api/service/{service_id}`, около строки 259)
- Изменить: `handlers/requests.py` (`create_request_flow`, около строки 60)
- Тест: `tests/test_subscription_db.py`, `tests/test_api_security.py`

**Интерфейсы:**
- Потребляет: `subscription.is_active` — задача 1
- Производит: `config.CLOSED_FOR_BOOKING: str` — текст отказа клиенту

- [ ] **Шаг 1: Написать падающие тесты**

Допишите в `tests/test_subscription_db.py`:

```python
# ── Гейт приёма заявки ───────────────────────────────────────────────────────

import uuid as _uuid

from handlers.requests import RequestRejected, create_request_flow

CLIENT_ID = 999_000_008


def _payload(service_id: str, item, **extra) -> dict:
    """Свой, а не импортированный из test_schedule: тестовые файлы друг друга
    не импортируют — в tests нет пакета, и порядок сборки sys.path не наш."""
    return {
        "service_id": service_id,
        "idcatalogs": [str(item["idcatalog"])],
        "client_name": "Иван Тестов",
        "phone": "+79990000008",
        "brand": "Toyota",
        "model": "Camry",
        "plate": "А777АА777",
        "comment": "",
        "consent": True,
        "client_uid": str(_uuid.uuid4()),
        **extra,
    }


async def test_expired_service_does_not_take_requests(service):
    """Форму можно отправить в обход интерфейса — решает сервер."""
    item = (await db.get_catalog(service))[0]
    svc = await db.get_service(service)
    free = await db.free_slots(svc)
    day = sorted(free)[0]
    moment = free[day][0]
    await _expire(service)

    with pytest.raises(RequestRejected) as exc:
        await create_request_flow(
            None,
            client_tg_id=CLIENT_ID,
            payload=_payload(service, item, scheduled_at=f"{day} {moment:%H:%M}"),
        )
    text = str(exc.value).lower()
    assert "подписк" not in text and "оплат" not in text, "клиенту про оплату не говорим"
```

И в `tests/test_api_security.py`:

```python
def test_expired_service_gets_no_slots(client, monkeypatch):
    """Форма просроченного сервиса не отдаётся: отказ, а не пустой календарь."""
    from datetime import datetime, timedelta, timezone

    service_id = "11111111-1111-1111-1111-111111111111"

    async def _expired(_id):
        return {
            "idservice": service_id,
            "service_name": "Тест",
            "service_number": "+79990000000",
            "city": "Тестоград",
            "location_service": "ул. Тестовая, 1",
            "timezone": "Europe/Moscow",
            "paid_until": datetime.now(timezone.utc) - timedelta(days=1),
        }

    monkeypatch.setattr(app_module.db, "get_service", _expired)
    response = client.get(f"/api/service/{service_id}")
    # 403, а не 404: «сервис не найден» — неправда, а неправда стоит отладки
    assert response.status_code == 403
    assert "подписк" not in response.json()["detail"].lower()
```

- [ ] **Шаг 2: Запустить тесты, убедиться, что падают**

Запустить:
`.venv/Scripts/python.exe -m pytest tests/test_api_security.py -k expired tests/test_subscription_db.py -k "does_not_take" -q`
Ожидается: FAIL — заявка создаётся, `/api/service` отвечает 200

- [ ] **Шаг 3: Добавить текст отказа в `config.py`**

Рядом с `CLIENT_NOTIFICATIONS`:

```python
# Клиент не должен знать, что у сервиса с оплатой: это не его дело и для
# сервиса унизительно. Телефон подставляется на месте
CLOSED_FOR_BOOKING = (
    "Сервис сейчас не принимает онлайн-запись. "
    "Позвоните, пожалуйста, по телефону: {phone}"
)
```

- [ ] **Шаг 4: Реализовать гейт в `app.py`**

Добавьте `import subscription` и `from datetime import datetime, timezone`
(если их нет), затем в `api_service`, сразу после проверки `if not svc:`:

```python
        # Просрочка отключает продажу нового времени, а не сам сервис.
        # 403, а не 404: «не найден» — неправда, а неправда стоит вечера отладки
        if not subscription.is_active(svc["paid_until"], datetime.now(timezone.utc)):
            raise HTTPException(
                status_code=403,
                detail=config.CLOSED_FOR_BOOKING.format(phone=svc["service_number"]),
            )
```

- [ ] **Шаг 5: Реализовать гейт в `handlers/requests.py`**

Добавьте `import subscription` и `from datetime import datetime, timezone`,
затем сразу после `if not service: raise RequestRejected(...)`:

```python
    # Та же линия обороны, что проверка услуг и проверка окна: форму можно
    # отправить в обход интерфейса, поэтому решает сервер. Заодно закрывает
    # клиента, у которого форма была открыта в момент истечения срока
    if not subscription.is_active(service["paid_until"], datetime.now(timezone.utc)):
        raise RequestRejected(
            config.CLOSED_FOR_BOOKING.format(phone=service["service_number"])
        )
```

- [ ] **Шаг 6: Запустить тесты, убедиться, что проходят**

Запустить:
`.venv/Scripts/python.exe -m pytest tests/test_api_security.py tests/test_subscription_db.py -q`
Ожидается: PASS

- [ ] **Шаг 7: Коммит**

```bash
git add config.py app.py handlers/requests.py tests/test_api_security.py tests/test_subscription_db.py
git commit -m "Просроченный сервис не отдаёт форму и не принимает заявки"
```

---

### Задача 9: Гейт прямой ссылки на сервис

**Файлы:**
- Изменить: `handlers/start.py` (`_handle_service_link`, около строки 48)
- Тест: `tests/test_start_handlers.py` (создать)

**Интерфейсы:**
- Потребляет: `subscription.is_active` — задача 1; `config.CLOSED_FOR_BOOKING` —
  задача 8
- Производит: ничего для следующих задач

**Зачем отдельно:** ссылку `?start=SVC_…` клиент мог сохранить или получить от
знакомого — фильтр поиска её не прикрывает. Телефон здесь даётся ровно как в
соседней ветке, где не сконфигурирована форма: клиент записывается голосом, и
сервис не теряет заказ из-за биллинговой механики.

- [ ] **Шаг 1: Написать падающие тесты**

Создайте `tests/test_start_handlers.py`:

```python
"""Переход по ссылке сервиса. Базы не требуют — соседи подменены."""

from datetime import datetime, timedelta, timezone

import pytest

import handlers.start as start

pytestmark = pytest.mark.asyncio


class FakeMessage:
    def __init__(self):
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


def _service(paid_until):
    return {
        "idservice": "11111111-1111-1111-1111-111111111111",
        "service_name": "Тест",
        "service_number": "+79990000000",
        "city": "Тестоград",
        "location_service": "ул. Тестовая, 1",
        "paid_until": paid_until,
    }


@pytest.fixture
def expired(monkeypatch):
    async def _get(_id):
        return _service(datetime.now(timezone.utc) - timedelta(days=1))

    monkeypatch.setattr(start.db, "get_service", _get)


async def test_expired_service_link_offers_the_phone(expired):
    """Онлайн-записи нет — но заказ сервис получить должен, голосом."""
    message = FakeMessage()
    await start._handle_service_link(message, "11111111-1111-1111-1111-111111111111")

    text = "\n".join(message.answers)
    assert "+79990000000" in text
    assert "подписк" not in text.lower() and "оплат" not in text.lower()


async def test_expired_service_link_does_not_open_the_form(expired):
    message = FakeMessage()
    await start._handle_service_link(message, "11111111-1111-1111-1111-111111111111")
    assert "Вы открыли форму записи" not in "\n".join(message.answers)
```

- [ ] **Шаг 2: Запустить тесты, убедиться, что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_start_handlers.py -q`
Ожидается: FAIL — форма открывается

- [ ] **Шаг 3: Реализовать**

В `handlers/start.py` добавьте `import subscription` и
`from datetime import datetime, timezone`, затем в `_handle_service_link`
после проверки `if not service:`:

```python
    # Ссылку могли сохранить или переслать — фильтр поиска её не прикрывает.
    # Телефон даём, как в ветке ниже: сервис не должен терять заказ из-за
    # того, что не заплатил нам
    if not subscription.is_active(service["paid_until"], datetime.now(timezone.utc)):
        await message.answer(
            "⚠️ " + config.CLOSED_FOR_BOOKING.format(
                phone=h(format_phone(service["service_number"]))
            ),
            reply_markup=kb.kb_client_main(),
        )
        return
```

Проверьте, что `config`, `h` и `format_phone` в модуле уже импортированы;
если нет — добавьте.

- [ ] **Шаг 4: Запустить тесты, убедиться, что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_start_handlers.py -q`
Ожидается: PASS, 2 теста

- [ ] **Шаг 5: Коммит**

```bash
git add handlers/start.py tests/test_start_handlers.py
git commit -m "Ссылка на просроченный сервис ведёт к телефону, а не к форме"
```

---

### Задача 10: Управляющий видит своё состояние

**Файлы:**
- Изменить: `render.py` (после `schedule_card`), `handlers/common.py`
  (`show_main_menu`), `render.py` (`registration_summary`)
- Тест: `tests/test_render.py`

**Интерфейсы:**
- Потребляет: `subscription.is_active`, `subscription.due_stage` — задача 1;
  `config.SUPPORT_CONTACT` — задача 2
- Производит:
  - `render.subscription_line(svc) -> str` (пустая строка — показывать нечего)
  - `render.subscription_reminder(stage: str, svc) -> str`

- [ ] **Шаг 1: Написать падающие тесты**

Допишите в `tests/test_render.py`:

```python
# ── Подписка ─────────────────────────────────────────────────────────────────

from datetime import datetime, timedelta, timezone

import subscription


def _svc(paid_until):
    return {
        "service_name": "Тест",
        "service_number": "+79990000000",
        "timezone": "Europe/Moscow",
        "paid_until": paid_until,
    }


def test_no_subscription_line_while_the_deadline_is_far():
    """Лишняя строка в меню тратит внимание на то, что не требует действий."""
    assert render.subscription_line(_svc(datetime.now(timezone.utc) + timedelta(days=90))) == ""


def test_subscription_line_warns_before_the_deadline():
    line = render.subscription_line(_svc(datetime.now(timezone.utc) + timedelta(days=2)))
    assert line != ""


def test_expired_line_says_what_stopped_and_what_did_not():
    """
    Без второй половины управляющий решит, что теряет всю запись на неделю
    вперёд, — и будет спасать несуществующую беду.
    """
    line = render.subscription_line(_svc(datetime.now(timezone.utc) - timedelta(days=1)))
    assert "скрыт из поиска" in line
    assert "уже записанные" in line.lower()


def test_reminder_mentions_the_deadline_and_the_survivors():
    svc = _svc(datetime.now(timezone.utc) + timedelta(hours=20))
    text = render.subscription_reminder(subscription.STAGE_24H, svc)
    assert "уже записанные" in text.lower()
    assert svc["service_name"] in text


def test_support_contact_is_omitted_when_unset(monkeypatch):
    """Пустое поле — пустое место, а не «обратитесь в поддержку: »."""
    monkeypatch.setattr(render.config, "SUPPORT_CONTACT", "")
    line = render.subscription_line(_svc(datetime.now(timezone.utc) - timedelta(days=1)))
    assert "Продлить" not in line
```

- [ ] **Шаг 2: Запустить тесты, убедиться, что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_render.py -k subscription -q`
Ожидается: FAIL — `AttributeError: module 'render' has no attribute 'subscription_line'`

- [ ] **Шаг 3: Реализовать в `render.py`**

Добавьте `import subscription` и после `schedule_card`:

```python
# ── Подписка ─────────────────────────────────────────────────────────────────
# Тексты видит только управляющий: клиенту про оплату не говорим ни слова.

_SURVIVORS = "Уже записанные клиенты придут в своё время — их заявки на месте."


def _renew_hint() -> str:
    """Пустой контакт — пустое место, а не «Продлить: »."""
    return f"\nПродлить: {h(config.SUPPORT_CONTACT)}" if config.SUPPORT_CONTACT else ""


def subscription_line(svc) -> str:
    """
    Строка о подписке для главного меню. Пустая, пока до конца срока далеко:
    напоминать не о чем, а лишняя строка тратит внимание.
    """
    now = datetime.now(timezone.utc)
    paid_until = svc["paid_until"]
    if subscription.is_active(paid_until, now):
        if subscription.due_stage(paid_until, now) is None:
            return ""
        return (
            f"💳 Подписка действует до {local_dt(paid_until, svc['timezone'])}."
            + _renew_hint()
        )
    return (
        "⛔️ <b>Подписка истекла.</b> Сервис скрыт из поиска, "
        "новые записи не принимаются.\n"
        f"{_SURVIVORS}" + _renew_hint()
    )


def subscription_reminder(stage: str, svc) -> str:
    """Письмо владельцу сервиса. Стадию считает subscription.due_stage."""
    name = h(svc["service_name"])
    until = local_dt(svc["paid_until"], svc["timezone"])
    if stage == subscription.STAGE_EXPIRED:
        head = (
            f"⛔️ <b>Подписка сервиса «{name}» истекла.</b>\n"
            "Сервис скрыт из поиска, новые записи не принимаются."
        )
    elif stage == subscription.STAGE_24H:
        head = (
            f"⏳ <b>Подписка сервиса «{name}» заканчивается завтра</b> — {until}.\n"
            "После этого сервис пропадёт из поиска и перестанет принимать записи."
        )
    else:
        head = (
            f"💳 <b>Подписка сервиса «{name}» действует до {until}.</b>\n"
            "Дальше сервис пропадёт из поиска и перестанет принимать записи."
        )
    return f"{head}\n{_SURVIVORS}" + _renew_hint()
```

- [ ] **Шаг 4: Показать строку в главном меню**

В `handlers/common.py`, в `show_main_menu`, после сборки `text`:

```python
    # Только к обычному приветствию: подшивать предупреждение о подписке к
    # «✅ Часы работы обновлены» значит показывать его на каждый чих
    if greeting is None and role == "owner":
        note = render.subscription_line(svc)
        if note:
            text = f"{text}\n\n{note}"
```

Убедитесь, что `render` в модуле импортирован.

- [ ] **Шаг 5: Назвать дату окончания триала при регистрации**

В `render.registration_summary`, перед строкой про ссылку:

```python
        f"<b>Пробный период:</b> до {local_dt(svc['paid_until'], svc['timezone'])}\n\n"
```

- [ ] **Шаг 6: Запустить тесты, убедиться, что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_render.py -q`
Ожидается: PASS

- [ ] **Шаг 7: Коммит**

```bash
git add render.py handlers/common.py tests/test_render.py
git commit -m "Управляющий видит срок подписки и что именно отключится"
```

---

### Задача 11: Команда продления

**Файлы:**
- Создать: `handlers/subscription.py`
- Изменить: `app.py` (`dp.include_routers`, около строки 73)
- Тест: `tests/test_subscription_handlers.py` (создать)

**Интерфейсы:**
- Потребляет: `db.extend_subscription` — задача 4; `config.BOT_OWNER_IDS` — задача 2
- Производит: `handlers.subscription.router`, команду `/extend <idservice> <дней>`

**В `set_my_commands` команда не публикуется:** в меню у всех она не нужна, а
работает и без публикации.

- [ ] **Шаг 1: Написать падающие тесты**

Создайте `tests/test_subscription_handlers.py`:

```python
"""Команда /extend. Базы не требуют — слой БД подменён."""

from datetime import datetime, timedelta, timezone

import pytest

import handlers.subscription as handler

pytestmark = pytest.mark.asyncio

OWNER_ID = 999_000_100
STRANGER_ID = 999_000_200
SERVICE_ID = "11111111-1111-1111-1111-111111111111"


class FakeMessage:
    def __init__(self, text: str, user_id: int):
        self.text = text
        self.from_user = type("User", (), {"id": user_id})()
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


@pytest.fixture
def extended(monkeypatch):
    """Слой БД подменён: проверяем разбор команды и права, а не SQL."""
    calls = []

    async def _extend(idservice, *, days, granted_by=None, **kwargs):
        calls.append((idservice, days, granted_by))
        return datetime.now(timezone.utc) + timedelta(days=days)

    monkeypatch.setattr(handler.config, "BOT_OWNER_IDS", (OWNER_ID,))
    monkeypatch.setattr(handler.db, "extend_subscription", _extend)
    return calls


async def test_owner_extends_a_service(extended):
    message = FakeMessage(f"/extend {SERVICE_ID} 30", OWNER_ID)
    await handler.extend_command(message)
    assert extended == [(SERVICE_ID, 30, OWNER_ID)]
    assert message.answers


async def test_stranger_gets_no_answer_at_all(extended):
    """
    Молча: рассказывать постороннему, что такая команда существует, незачем.
    """
    message = FakeMessage(f"/extend {SERVICE_ID} 30", STRANGER_ID)
    await handler.extend_command(message)
    assert extended == []
    assert message.answers == []


async def test_broken_arguments_are_explained(extended):
    message = FakeMessage("/extend 30", OWNER_ID)
    await handler.extend_command(message)
    assert extended == []
    assert message.answers, "владельцу бота нужен текст, а не молчание"


async def test_zero_days_is_rejected_before_the_database(extended):
    """Констрейнт это тоже поймает, но ответит языком драйвера."""
    message = FakeMessage(f"/extend {SERVICE_ID} 0", OWNER_ID)
    await handler.extend_command(message)
    assert extended == []


async def test_missing_service_is_reported(extended, monkeypatch):
    async def _none(*args, **kwargs):
        return None

    monkeypatch.setattr(handler.db, "extend_subscription", _none)
    message = FakeMessage(f"/extend {SERVICE_ID} 30", OWNER_ID)
    await handler.extend_command(message)
    assert "не найден" in " ".join(message.answers).lower()
```

- [ ] **Шаг 2: Запустить тесты, убедиться, что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_handlers.py -q`
Ожидается: FAIL — `ModuleNotFoundError: No module named 'handlers.subscription'`

- [ ] **Шаг 3: Реализовать**

Создайте `handlers/subscription.py`:

```python
"""
handlers/subscription.py — продление подписки владельцем бота.

Пока приёма денег нет, продлевает человек: /extend <idservice> <дней>. Когда
появится провайдер, он позовёт тот же db.extend_subscription, только с другим
source и с external_id для идемпотентности.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

import config
import render
from database import db
from validators import ValidationError, validate_uuid

router = Router()

MAX_DAYS = 366

USAGE = (
    "Продление подписки:\n"
    "<code>/extend &lt;idservice&gt; &lt;дней&gt;</code>\n"
    f"Дней — от 1 до {MAX_DAYS}."
)


@router.message(Command("extend"))
async def extend_command(message: Message) -> None:
    # Постороннему не отвечаем вовсе: знать о существовании команды ему незачем
    if message.from_user.id not in config.BOT_OWNER_IDS:
        return

    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer(USAGE)
        return

    try:
        idservice = validate_uuid(parts[1], field="Сервис")
    except ValidationError as exc:
        await message.answer(f"❌ {exc}")
        return

    if not parts[2].isdigit() or not 1 <= int(parts[2]) <= MAX_DAYS:
        await message.answer(USAGE)
        return

    days = int(parts[2])
    paid_until = await db.extend_subscription(
        idservice, days=days, granted_by=message.from_user.id
    )
    if paid_until is None:
        await message.answer("❌ Сервис не найден или удалён.")
        return

    svc = await db.get_service(idservice)
    await message.answer(
        f"✅ «{svc['service_name']}» продлён на {days} дн.\n"
        f"Подписка действует до {render.local_dt(paid_until, svc['timezone'])}."
    )
```

- [ ] **Шаг 4: Зарегистрировать роутер в `app.py`**

В `dp.include_routers`, перед `admin_mgmt.router`:

```python
    subscription.router,
```

И в импорт: `from handlers import ..., subscription, ...` (модуль
`handlers.subscription`, не путать с чистым `subscription.py` в корне — в
`app.py` последний не импортируется).

- [ ] **Шаг 5: Запустить тесты, убедиться, что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_handlers.py -q`
Ожидается: PASS, 5 тестов

- [ ] **Шаг 6: Коммит**

```bash
git add handlers/subscription.py app.py tests/test_subscription_handlers.py
git commit -m "Владелец бота продлевает подписку командой"
```

---

### Задача 12: Рассылка напоминаний и тик

**Файлы:**
- Изменить: `notifications.py` (в конец), `app.py` (новый эндпоинт после
  `/api/me`), `README.md` (раздел про крон)
- Тест: `tests/test_api_security.py`

**Интерфейсы:**
- Потребляет: `db.services_for_reminders`, `db.claim_reminder` — задача 7;
  `render.subscription_reminder` — задача 10; `subscription.due_stage` — задача 1
- Производит:
  - `notifications.send_subscription_reminders(bot) -> int`
  - `POST /internal/subscriptions/tick`

**Почему рассылка в `notifications.py`:** чистая часть (какая стадия) — в
`subscription.py`, запросы — в `database.py`, а здесь остаётся то, чем модуль и
занят: разослать, не превысив лимиты Telegram и не уронив процесс.

- [ ] **Шаг 1: Написать падающие тесты**

Допишите в `tests/test_api_security.py`:

```python
# ── Тик напоминаний ──────────────────────────────────────────────────────────


@pytest.fixture
def tick(monkeypatch):
    """Секрет задан, рассылка подменена: проверяется дверь, а не письма."""
    calls = []

    async def _send(_bot):
        calls.append(1)
        return 3

    monkeypatch.setattr(app_module.config, "TICK_SECRET", "s3cret")
    monkeypatch.setattr(app_module, "send_subscription_reminders", _send)
    return calls


def test_tick_runs_with_the_right_secret(client, tick):
    response = client.post(
        "/internal/subscriptions/tick", headers={"X-Tick-Secret": "s3cret"}
    )
    assert response.status_code == 200
    assert response.json()["sent"] == 3
    assert tick == [1]


def test_tick_without_a_secret_is_rejected(client, tick):
    assert client.post("/internal/subscriptions/tick").status_code == 401
    assert tick == []


def test_tick_with_a_wrong_secret_is_rejected(client, tick):
    response = client.post(
        "/internal/subscriptions/tick", headers={"X-Tick-Secret": "guess"}
    )
    assert response.status_code == 401
    assert tick == []


def test_tick_is_closed_when_no_secret_is_configured(client, monkeypatch):
    """
    Иначе стенд с незаполненной переменной оказался бы открыт всем: пустой
    секрет совпал бы с пустым заголовком.
    """
    monkeypatch.setattr(app_module.config, "TICK_SECRET", "")
    response = client.post(
        "/internal/subscriptions/tick", headers={"X-Tick-Secret": ""}
    )
    assert response.status_code == 401
```

- [ ] **Шаг 2: Запустить тесты, убедиться, что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_api_security.py -k tick -q`
Ожидается: FAIL — 404, эндпоинта нет

- [ ] **Шаг 3: Реализовать рассылку в `notifications.py`**

Добавьте `from datetime import datetime, timezone`, `import render`,
`import subscription` и в конец файла:

```python
async def send_subscription_reminders(bot: Bot) -> int:
    """
    Разослать причитающиеся напоминания о подписке. Возвращает число писем.

    Право на письмо занимается до отправки: при сбое теряется одно
    напоминание, а не управляющий получает его заново на каждом тике.

    Состояние подписки здесь не меняется — оно выводится из paid_until.
    Поэтому молчавший неделю крон стоит писем, а не поехавших гейтов.
    """
    now = datetime.now(timezone.utc)
    sent = 0
    for svc in await db.services_for_reminders():
        stage = subscription.due_stage(svc["paid_until"], now)
        if stage is None:
            continue
        if not await db.claim_reminder(
            str(svc["idservice"]), svc["paid_until"], stage
        ):
            continue
        if await safe_send(
            bot, svc["owner_id"], render.subscription_reminder(stage, svc)
        ):
            sent += 1
        await asyncio.sleep(BROADCAST_DELAY)
    return sent
```

- [ ] **Шаг 4: Добавить эндпоинт в `app.py`**

Добавьте `import hmac` и
`from notifications import send_subscription_reminders`, затем после `/api/me`:

```python
@app.post("/internal/subscriptions/tick")
async def subscriptions_tick(x_tick_secret: str | None = Header(default=None)):
    """
    Рассылка напоминаний о подписке. Дёргается внешним кроном.

    Состояние подписки тик не меняет: он только шлёт письма и помечает
    отправленное. Два тика внахлёст безопасны — право на письмо занимается
    уникальным индексом в базе.
    """
    # Пустой секрет закрывает эндпоинт совсем: иначе стенд с незаполненной
    # переменной оказался бы открыт всем
    if not config.TICK_SECRET or not hmac.compare_digest(
        x_tick_secret or "", config.TICK_SECRET
    ):
        raise HTTPException(status_code=401, detail="unauthorized")

    async with _db_gate:
        sent = await send_subscription_reminders(bot)
    return {"sent": sent}
```

- [ ] **Шаг 5: Описать крон в `README.md`**

В раздел про деплой:

```markdown
### Напоминания о подписке

Рассылку будит внешний крон — раз в час, точности до часа хватает и для
суточного напоминания:

    0 * * * * curl -fsS -X POST -H "X-Tick-Secret: <TICK_SECRET>" \
        https://<host>/internal/subscriptions/tick

Секрет — переменная `TICK_SECRET`. Пока она пуста, эндпоинт закрыт.
Пропущенные запуски не ломают подписку: срок считается из `paid_until`, тик
только рассылает письма.
```

- [ ] **Шаг 6: Запустить тесты, убедиться, что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_api_security.py -q`
Ожидается: PASS

- [ ] **Шаг 7: Коммит**

```bash
git add notifications.py app.py README.md tests/test_api_security.py
git commit -m "Крон будит рассылку напоминаний о подписке"
```

---

### Задача 13: Сквозная проверка

**Файлы:**
- Тест: `tests/test_subscription_db.py` (сквозной сценарий)

**Интерфейсы:**
- Потребляет: всё выше
- Производит: ничего

- [ ] **Шаг 1: Написать сквозной тест**

Допишите в `tests/test_subscription_db.py`:

```python
# ── Путь подписки целиком ────────────────────────────────────────────────────


async def test_expiry_and_renewal_round_trip(service, make_request):
    """
    Полный круг: истёк → пропал из поиска и не принимает записи → продлён →
    вернулся. И записанный клиент всё это время остаётся записанным.
    """
    svc = await db.get_service(service)
    moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    booked = await make_request(service, moment)

    await _expire(service)
    assert all(
        str(row["idservice"]) != service
        for row in await db.get_services_by_city(svc["city"])
    )
    # Заявка на месте: просрочка отключает продажу нового времени, а не сервис
    taken = await db.get_taken_slots(
        service, moment - timedelta(hours=1), moment + timedelta(hours=1)
    )
    assert taken[moment] == 1

    await db.extend_subscription(service, days=30)
    assert any(
        str(row["idservice"]) == service
        for row in await db.get_services_by_city(svc["city"])
    )
    assert booked["idrequests"]


async def test_two_ticks_send_one_letter(service, monkeypatch):
    """Главное свойство конструкции, проверенное на настоящей базе."""
    import notifications

    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE services SET paid_until=now() + interval '20 hours'"
            " WHERE idservice=$1",
            service,
        )

    sent = []

    async def _fake_send(bot, chat_id, text, **kwargs):
        sent.append(chat_id)
        return True

    monkeypatch.setattr(notifications, "safe_send", _fake_send)
    await notifications.send_subscription_reminders(None)
    await notifications.send_subscription_reminders(None)
    assert sent.count(999_000_001) == 1
```

- [ ] **Шаг 2: Запустить полную сюиту**

Запустить: `.venv/Scripts/python.exe -m pytest -q`
Ожидается: PASS. Прогон ~7 минут — база удалённая. `TimeoutError` в одиночном
файле перепроверяйте точечным запуском, прежде чем считать падением.

- [ ] **Шаг 3: Коммит**

```bash
git add tests/test_subscription_db.py
git commit -m "Сквозная проверка: истечение, отключение, продление, возврат"
```

- [ ] **Шаг 4: Ручная проверка**

Поднимите приложение и туннель, затем:

Управляющий:
- [ ] Регистрация нового сервиса → в сводке названа дата окончания пробного периода
- [ ] `/start` → в меню строки о подписке нет (до конца триала больше суток)
- [ ] `/extend <id> 30` от постороннего аккаунта → бот молчит
- [ ] `/extend <id> 30` от владельца бота → ответ с новым сроком
- [ ] `/extend` без аргументов → подсказка по формату
- [ ] Выставить `paid_until` в прошлое руками → `/start` показывает, что сервис
      скрыт из поиска, и что записанные клиенты остаются
- [ ] При этом «🗓 Расписание», «📋 Заявки», «🛠 Услуги» открываются и работают

Клиент (сервис просрочен):
- [ ] Поиск по городу → сервиса в списке нет
- [ ] Переход по сохранённой ссылке → телефон сервиса, форма не открывается
- [ ] Открытая до просрочки форма, отправка → отказ без слов «подписка» и «оплата»
- [ ] Заявка, созданная до просрочки, видна в «📋 Мои заявки» и отменяется

Напоминания:
- [ ] Выставить `paid_until = now() + 20 часов`, дёрнуть тик curl-ом → письмо владельцу
- [ ] Дёрнуть тик повторно → письма нет, `{"sent": 0}`
- [ ] `/extend <id> 30`, снова выставить срок близко и дёрнуть тик → письмо приходит
- [ ] Тик без заголовка и с чужим секретом → 401
