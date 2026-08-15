# Запись на время: расписание и календарь — план реализации

> **Для агентов-исполнителей:** ОБЯЗАТЕЛЬНЫЙ СУБ-НАВЫК — используйте
> `superpowers:subagent-driven-development` (рекомендуется) или
> `superpowers:executing-plans`. Шаги отмечаются чекбоксами `- [ ]`.

**Цель:** заменить поле «Срочность» настоящей записью на время: управляющий
задаёт шаблон рабочего дня, клиент выбирает день и час в календаре, занятое
время не показывается.

**Архитектура:** слоты нигде не хранятся. Есть шаблон (`service_schedule`, одна
строка на сервис) и факт занятости (`requests.scheduled_at`). Свободные окна —
разность между нарезкой шаблона и занятыми моментами, считается чистой функцией
в `slots.py`. Гонка за окно закрывается блокировкой строки расписания в той же
транзакции, что создаёт заявку.

**Стек:** Python 3.14, aiogram 3, FastAPI, asyncpg, PostgreSQL (Neon), WebApp —
один файл на ванильном JS без сборки. Тесты — pytest + pytest-asyncio.

**Спека:** `docs/superpowers/specs/2026-08-15-booking-calendar-design.md` —
читайте её вместе с планом, план не спорит со спекой, а исполняет её.

## Глобальные ограничения

- Жёсткий `DELETE` в `database.py` запрещён: удаление мягкое, через
  `idrecstatus = -1`. Исключение — только чистка в тестовых фикстурах.
- Форму записи открывает **только inline-кнопка**: reply-кнопка не передаёт
  `initData`, и заявка не проходит проверку на сервере.
- Пустое значение показывается пустотой. Заглушек вроде «не указано» в
  интерфейсе не пишем, строку скрываем целиком.
- Все тексты для пользователя — по-русски, с ёфикацией как в остальном коде.
- Время в базе — `timestamptz`. Локальное время сервиса берётся из
  `services.timezone`, а не из системных часов.
- Каждая задача заканчивается коммитом и работающим приложением: промежуточных
  состояний, где форма записи сломана, в плане нет.
- Тесты слоя БД идут против настоящей базы из `DATABASE_URL` и пропускаются,
  если он не задан. Запуск — `.venv/Scripts/python.exe -m pytest`.

---

### Задача 1: Валидаторы расписания

**Файлы:**
- Изменить: `validators.py` (добавить после `validate_price`)
- Тест: `tests/test_validators.py`

**Интерфейсы:**
- Потребляет: `clean_text`, `ValidationError` — уже есть в `validators.py`
- Производит:
  - `validators.validate_time_range(raw: object, *, field: str) -> tuple[time, time]`
  - `validators.validate_lunch(raw: object) -> tuple[time, time] | None`
  - `validators.validate_capacity(raw: object) -> int`
  - `validators.validate_horizon(raw: object) -> int`

- [ ] **Шаг 1: Написать падающие тесты**

Добавьте в конец `tests/test_validators.py`:

```python
# ── Расписание ───────────────────────────────────────────────────────────────

from datetime import time

from validators import (
    validate_capacity,
    validate_horizon,
    validate_lunch,
    validate_time_range,
)


def test_time_range_accepts_short_form():
    """«9-18» — то, как человек пишет часы работы в переписке."""
    assert validate_time_range("9-18", field="Часы") == (time(9), time(18))


def test_time_range_accepts_full_form():
    assert validate_time_range("09:00-18:00", field="Часы") == (time(9), time(18))


def test_time_range_tolerates_spaces_and_dashes():
    assert validate_time_range(" 9:30 — 17:45 ", field="Часы") == (
        time(9, 30), time(17, 45)
    )


def test_time_range_rejects_reversed():
    with pytest.raises(ValidationError):
        validate_time_range("18-9", field="Часы")


def test_time_range_rejects_equal_bounds():
    with pytest.raises(ValidationError):
        validate_time_range("9-9", field="Часы")


def test_time_range_rejects_garbage():
    with pytest.raises(ValidationError):
        validate_time_range("абв", field="Часы")


def test_time_range_rejects_impossible_hour():
    with pytest.raises(ValidationError):
        validate_time_range("9-25", field="Часы")


def test_lunch_dash_clears_it():
    """«-» убирает обед — тот же жест, что и для цены."""
    assert validate_lunch("-") is None


def test_lunch_parses_range():
    assert validate_lunch("13-14") == (time(13), time(14))


def test_capacity_parses_number():
    assert validate_capacity("3") == 3


def test_capacity_rejects_zero():
    with pytest.raises(ValidationError):
        validate_capacity("0")


def test_capacity_rejects_above_limit():
    with pytest.raises(ValidationError):
        validate_capacity("21")


def test_horizon_parses_number():
    assert validate_horizon("14") == 14


def test_horizon_rejects_above_limit():
    with pytest.raises(ValidationError):
        validate_horizon("61")
```

- [ ] **Шаг 2: Запустить и убедиться, что тесты падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_validators.py -q`

Ожидается: `ImportError: cannot import name 'validate_time_range' from 'validators'`

- [ ] **Шаг 3: Реализовать валидаторы**

В `validators.py` добавьте импорт `from datetime import time` к остальным
импортам, затем после `validate_price`:

```python
_TIME_RANGE_RE = re.compile(
    r"^(\d{1,2})(?::(\d{2}))?\s*[-—–]\s*(\d{1,2})(?::(\d{2}))?$"
)


def _parse_time(hour: str, minute: str | None, *, field: str) -> time:
    value_h, value_m = int(hour), int(minute or 0)
    if value_h > 23 or value_m > 59:
        raise ValidationError(f"«{field}»: такого времени не бывает.")
    return time(value_h, value_m)


def validate_time_range(raw: object, *, field: str) -> tuple[time, time]:
    """
    Диапазон времени: «9-18» или «09:00-18:00».

    Короткая форма принимается намеренно: управляющий пишет часы работы так,
    как сказал бы их вслух, и отказ из-за пропущенных нулей выглядит
    придиркой, а не проверкой.
    """
    text = clean_text(raw, field=field, max_len=20)
    match = _TIME_RANGE_RE.match(text.replace(" ", ""))
    if not match:
        raise ValidationError(
            f"«{field}»: введите диапазон, например 9-18 или 09:00-18:00."
        )

    start = _parse_time(match.group(1), match.group(2), field=field)
    end = _parse_time(match.group(3), match.group(4), field=field)
    if start >= end:
        raise ValidationError(f"«{field}»: начало должно быть раньше конца.")
    return start, end


def validate_lunch(raw: object) -> tuple[time, time] | None:
    """Обед. None — обеда нет; «-» убирает его, как и у цены."""
    text = clean_text(raw, field="Обед", max_len=20)
    if text in {"-", "—", "–", ""}:
        return None
    return validate_time_range(text, field="Обед")


def _validate_int(raw: object, *, field: str, low: int, high: int, hint: str) -> int:
    text = clean_text(raw, field=field, max_len=10)
    compact = re.sub(r"\s", "", text)
    if not compact.isdecimal():
        raise ValidationError(hint)
    value = int(compact)
    if not low <= value <= high:
        raise ValidationError(hint)
    return value


def validate_capacity(raw: object) -> int:
    """Сколько машин сервис принимает в одно время."""
    return _validate_int(
        raw, field="Машин за раз", low=1, high=20,
        hint="Введите число от 1 до 20.",
    )


def validate_horizon(raw: object) -> int:
    """На сколько дней вперёд открыта запись."""
    return _validate_int(
        raw, field="Горизонт", low=1, high=60,
        hint="Введите число дней от 1 до 60.",
    )
```

- [ ] **Шаг 4: Запустить тесты — должны пройти**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_validators.py -q`

Ожидается: PASS, все тесты файла.

- [ ] **Шаг 5: Коммит**

```bash
git add validators.py tests/test_validators.py
git commit -m "Валидаторы расписания: часы, обед, вместимость, горизонт"
```

---

### Задача 2: Нарезка свободных окон

**Файлы:**
- Создать: `slots.py`
- Тест: `tests/test_slots.py`

**Интерфейсы:**
- Потребляет: ничего из проекта — модуль намеренно самостоятельный
- Производит: `slots.free_slots(schedule: Mapping, tz: str, now: datetime, taken: Mapping[datetime, int]) -> dict[date, list[time]]`
  - `schedule` — строка `service_schedule` или словарь с теми же ключами:
    `work_from`, `work_to`, `slot_minutes`, `lunch_from`, `lunch_to`,
    `weekdays`, `horizon_days`, `capacity`
  - `now` — момент «сейчас», обязательно с зоной
  - `taken` — сколько живых заявок на каждый момент
  - результат — только дни, где остались свободные окна

- [ ] **Шаг 1: Написать падающие тесты**

Создайте `tests/test_slots.py`:

```python
"""Тесты нарезки окон. Базы не требуют — функция чистая."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from slots import free_slots

MSK = ZoneInfo("Europe/Moscow")

# Пятница
NOW = datetime(2026, 8, 14, 8, 0, tzinfo=MSK)


def schedule(**overrides):
    base = {
        "work_from": time(9),
        "work_to": time(18),
        "slot_minutes": 60,
        "lunch_from": None,
        "lunch_to": None,
        "weekdays": [1, 2, 3, 4, 5],
        "horizon_days": 1,
        "capacity": 1,
    }
    base.update(overrides)
    return base


def test_slots_are_cut_by_step():
    result = free_slots(schedule(), "Europe/Moscow", NOW, {})
    assert result[date(2026, 8, 14)] == [
        time(9), time(10), time(11), time(12), time(13),
        time(14), time(15), time(16), time(17),
    ]


def test_slot_does_not_spill_past_closing():
    """Окно, не помещающееся целиком до конца дня, не предлагается."""
    result = free_slots(schedule(slot_minutes=120, work_to=time(14)), "Europe/Moscow", NOW, {})
    assert result[date(2026, 8, 14)] == [time(9), time(11)]


def test_lunch_removes_overlapping_slots():
    result = free_slots(
        schedule(lunch_from=time(13), lunch_to=time(14)), "Europe/Moscow", NOW, {}
    )
    assert time(13) not in result[date(2026, 8, 14)]
    assert time(12) in result[date(2026, 8, 14)]
    assert time(14) in result[date(2026, 8, 14)]


def test_non_working_days_are_skipped():
    """15 августа 2026 — суббота."""
    result = free_slots(schedule(horizon_days=3), "Europe/Moscow", NOW, {})
    assert date(2026, 8, 15) not in result
    assert date(2026, 8, 16) not in result


def test_past_slots_of_today_are_not_offered():
    now = datetime(2026, 8, 14, 11, 30, tzinfo=MSK)
    result = free_slots(schedule(), "Europe/Moscow", now, {})
    assert result[date(2026, 8, 14)][0] == time(12)


def test_horizon_limits_days():
    result = free_slots(schedule(horizon_days=5), "Europe/Moscow", NOW, {})
    assert max(result) == date(2026, 8, 18)


def test_taken_slot_disappears():
    taken = {datetime(2026, 8, 14, 9, 0, tzinfo=MSK): 1}
    result = free_slots(schedule(), "Europe/Moscow", NOW, taken)
    assert time(9) not in result[date(2026, 8, 14)]


def test_slot_lives_until_last_place_is_gone():
    taken = {datetime(2026, 8, 14, 9, 0, tzinfo=MSK): 2}
    result = free_slots(schedule(capacity=3), "Europe/Moscow", NOW, taken)
    assert time(9) in result[date(2026, 8, 14)]


def test_day_without_free_slots_is_absent():
    taken = {
        datetime(2026, 8, 14, hour, 0, tzinfo=MSK): 1 for hour in range(9, 18)
    }
    result = free_slots(schedule(), "Europe/Moscow", NOW, taken)
    assert date(2026, 8, 14) not in result


def test_day_is_computed_in_service_timezone():
    """Во Владивостоке уже 15-е, когда в Москве ещё 14-е."""
    now = datetime(2026, 8, 14, 20, 0, tzinfo=MSK)
    result = free_slots(schedule(horizon_days=1), "Asia/Vladivostok", now, {})
    assert list(result) == [date(2026, 8, 15)]
```

- [ ] **Шаг 2: Запустить и убедиться, что тесты падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_slots.py -q`

Ожидается: `ModuleNotFoundError: No module named 'slots'`

- [ ] **Шаг 3: Реализовать нарезку**

Создайте `slots.py`:

```python
"""
slots.py — нарезка свободных окон записи.

Функция чистая: ни базы, ни сети, ни системных часов. Всё, что влияет на
результат, приходит аргументами — поэтому поведение проверяется обычными
тестами, а не через поднятое приложение.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def _localize(day: date, moment: time, zone: ZoneInfo) -> datetime | None:
    """
    Локальное время в момент времени. None — такого времени не существует.

    При переходе на летнее время час пропадает целиком; предлагать запись на
    несуществующий час нельзя. Задвоенный час берём первый (fold=0).
    """
    naive = datetime.combine(day, moment)
    aware = naive.replace(tzinfo=zone)
    # Несуществующее время переживает round-trip через UTC с другим значением
    if aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
        return None
    return aware


def free_slots(
    schedule: Mapping,
    tz: str,
    now: datetime,
    taken: Mapping[datetime, int],
) -> dict[date, list[time]]:
    """
    Свободные окна записи по дням, в локальном времени сервиса.

    День попадает в результат, только если в нём осталось хотя бы одно окно:
    пустой список означал бы «день доступен», а он не доступен.
    """
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    workdays = set(schedule["weekdays"])
    step = timedelta(minutes=schedule["slot_minutes"])
    capacity = schedule["capacity"]
    lunch_from, lunch_to = schedule["lunch_from"], schedule["lunch_to"]

    result: dict[date, list[time]] = {}
    for offset in range(schedule["horizon_days"]):
        day = today + timedelta(days=offset)
        if day.isoweekday() not in workdays:
            continue

        opens = datetime.combine(day, schedule["work_from"])
        closes = datetime.combine(day, schedule["work_to"])
        free: list[time] = []

        cursor = opens
        while cursor + step <= closes:
            start, end = cursor.time(), (cursor + step).time()
            cursor += step

            # Окно, задевающее обед хотя бы краем, продавать нельзя
            if lunch_from and lunch_to and start < lunch_to and end > lunch_from:
                continue

            moment = _localize(day, start, zone)
            if moment is None or moment < now:
                continue
            if taken.get(moment, 0) >= capacity:
                continue
            free.append(start)

        if free:
            result[day] = free
    return result
```

- [ ] **Шаг 4: Запустить тесты — должны пройти**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_slots.py -q`

Ожидается: PASS, 10 тестов.

- [ ] **Шаг 5: Коммит**

```bash
git add slots.py tests/test_slots.py
git commit -m "Нарезка свободных окон записи из шаблона расписания"
```

---

### Задача 3: Схема — расписание сервиса и время заявки

**Файлы:**
- Изменить: `schema.sql`

**Интерфейсы:**
- Производит: таблицу `service_schedule`, колонку `requests.scheduled_at`,
  индекс `idx_requests_scheduled`

- [ ] **Шаг 1: Добавить таблицу расписания**

В `schema.sql`, после блока `service_catalog` и перед `CREATE TABLE ... requests`:

```sql
-- ── Расписание сервиса ───────────────────────────────────────────────────────
-- Слоты записи нигде не хранятся: они вычисляются из этого шаблона, а занятость
-- берётся из заявок. Так отмена заявки освобождает время сама собой, без
-- отдельной логики возврата, которой было бы что ломать.

CREATE TABLE IF NOT EXISTS service_schedule (
    idservice    uuid        PRIMARY KEY REFERENCES services(idservice) ON DELETE CASCADE,
    work_from    time        NOT NULL DEFAULT '09:00',
    work_to      time        NOT NULL DEFAULT '18:00',
    slot_minutes int         NOT NULL DEFAULT 60,
    lunch_from   time,
    lunch_to     time,
    weekdays     smallint[]  NOT NULL DEFAULT '{1,2,3,4,5}',  -- ISO: 1=пн … 7=вс
    horizon_days int         NOT NULL DEFAULT 14,
    capacity     int         NOT NULL DEFAULT 1,
    updatedate   timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE service_schedule DROP CONSTRAINT IF EXISTS chk_schedule_hours;
ALTER TABLE service_schedule ADD  CONSTRAINT chk_schedule_hours
    CHECK (work_from < work_to);

ALTER TABLE service_schedule DROP CONSTRAINT IF EXISTS chk_schedule_step;
ALTER TABLE service_schedule ADD  CONSTRAINT chk_schedule_step
    CHECK (slot_minutes IN (30, 60, 90, 120));

-- Обед либо не задан вовсе, либо задан целиком и лежит внутри рабочих часов:
-- половинчатое состояние сделало бы нарезку неоднозначной
ALTER TABLE service_schedule DROP CONSTRAINT IF EXISTS chk_schedule_lunch;
ALTER TABLE service_schedule ADD  CONSTRAINT chk_schedule_lunch
    CHECK ((lunch_from IS NULL AND lunch_to IS NULL)
        OR (lunch_from IS NOT NULL AND lunch_to IS NOT NULL
            AND lunch_from < lunch_to
            AND lunch_from >= work_from AND lunch_to <= work_to));

ALTER TABLE service_schedule DROP CONSTRAINT IF EXISTS chk_schedule_horizon;
ALTER TABLE service_schedule ADD  CONSTRAINT chk_schedule_horizon
    CHECK (horizon_days BETWEEN 1 AND 60);

ALTER TABLE service_schedule DROP CONSTRAINT IF EXISTS chk_schedule_capacity;
ALTER TABLE service_schedule ADD  CONSTRAINT chk_schedule_capacity
    CHECK (capacity BETWEEN 1 AND 20);

-- Хранятся рабочие дни, а не выходные: пустой список выходных двусмыслен
-- («работаем всегда» или «не заполнили»), а пустой список рабочих дней запрещён
ALTER TABLE service_schedule DROP CONSTRAINT IF EXISTS chk_schedule_weekdays;
ALTER TABLE service_schedule ADD  CONSTRAINT chk_schedule_weekdays
    CHECK (array_length(weekdays, 1) BETWEEN 1 AND 7
       AND weekdays <@ ARRAY[1,2,3,4,5,6,7]::smallint[]);
```

- [ ] **Шаг 2: Добавить время в заявку**

В `schema.sql`, в блок `ALTER TABLE requests ADD COLUMN IF NOT EXISTS ...`
добавьте строку:

```sql
    ADD COLUMN IF NOT EXISTS scheduled_at  timestamptz,
```

И рядом с остальными индексами заявок:

```sql
-- Занятость окна считается по этому индексу: (сервис, время) с отсечкой
-- заявок без времени — их всего три, они из эпохи «срочности»
CREATE INDEX IF NOT EXISTS idx_requests_scheduled
    ON requests (idservice, scheduled_at) WHERE scheduled_at IS NOT NULL;
```

- [ ] **Шаг 3: Добавить бэкфил расписания**

В конец `schema.sql`, в секцию бэкфилов:

```sql
-- Каждому существующему сервису — расписание по умолчанию. Альтернатива
-- («нет строки — значит не настроено») означала бы, что после выката все живые
-- сервисы перестают принимать заявки, пока управляющий не дойдёт до настроек.
INSERT INTO service_schedule (idservice)
SELECT idservice FROM services
ON CONFLICT (idservice) DO NOTHING;
```

- [ ] **Шаг 4: Применить схему к базе**

Запустить:

```bash
.venv/Scripts/python.exe -c "
import asyncio, asyncpg, config, pathlib
async def main():
    conn = await asyncpg.connect(config.DATABASE_URL)
    await conn.execute(pathlib.Path('schema.sql').read_text(encoding='utf-8'))
    await conn.close()
asyncio.run(main())
"
```

Ожидается: команда завершается без ошибок.

- [ ] **Шаг 5: Проверить результат**

Запустить:

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "
import asyncio, asyncpg, config
async def main():
    conn = await asyncpg.connect(config.DATABASE_URL)
    print('сервисов:', await conn.fetchval('SELECT count(*) FROM services'))
    print('расписаний:', await conn.fetchval('SELECT count(*) FROM service_schedule'))
    print('заявок без времени:', await conn.fetchval(
        'SELECT count(*) FROM requests WHERE scheduled_at IS NULL'))
    await conn.close()
asyncio.run(main())
"
```

Ожидается: число расписаний равно числу сервисов; заявки без времени — те, что
были до фичи.

- [ ] **Шаг 6: Прогнать схему повторно и сверить числа**

Запустите команды шагов 4 и 5 ещё раз. Числа не должны измениться — схема
идемпотентна, повторный прогон не плодит строк.

- [ ] **Шаг 7: Коммит**

```bash
git add schema.sql
git commit -m "Схема: расписание сервиса и время записи в заявке"
```

---

### Задача 4: Слой БД — чтение и правка расписания

**Файлы:**
- Изменить: `database.py` (методы рядом с `get_catalog`), `create_service`
- Тест: `tests/test_schedule.py` (создать)

**Интерфейсы:**
- Потребляет: `db.pool`, `_new_id` — есть в `database.py`
- Производит:
  - `db.get_schedule(idservice: str) -> asyncpg.Record | None`
  - `db.update_schedule(idservice: str, **fields) -> asyncpg.Record | None`
  - `db.get_taken_slots(idservice: str, since: datetime, until: datetime) -> dict[datetime, int]`
  - `db.create_service` дополнительно заводит строку расписания

- [ ] **Шаг 1: Написать падающие тесты**

Создайте `tests/test_schedule.py`:

```python
"""Тесты расписания в слое БД. Идут против настоящей базы."""

from datetime import datetime, time, timedelta, timezone

import asyncpg
import pytest

import config
from database import db

pytestmark = pytest.mark.asyncio


async def test_new_service_gets_default_schedule(service):
    """Сервис без расписания не должен существовать: иначе запись невозможна."""
    row = await db.get_schedule(service)
    assert row is not None
    assert row["work_from"] == time(9)
    assert row["work_to"] == time(18)
    assert row["slot_minutes"] == 60
    assert list(row["weekdays"]) == [1, 2, 3, 4, 5]
    assert row["capacity"] == 1


async def test_update_schedule_saves_fields(service):
    updated = await db.update_schedule(
        service, work_from=time(8), work_to=time(20), capacity=3
    )
    assert updated["work_from"] == time(8)
    assert updated["capacity"] == 3

    reread = await db.get_schedule(service)
    assert reread["work_to"] == time(20)


async def test_update_schedule_clears_lunch(service):
    await db.update_schedule(service, lunch_from=time(13), lunch_to=time(14))
    cleared = await db.update_schedule(service, lunch_from=None, lunch_to=None)
    assert cleared["lunch_from"] is None


async def test_update_schedule_rejects_reversed_hours(service):
    """Констрейнт — последняя линия обороны, даже если валидатор обойдут."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await db.update_schedule(service, work_from=time(20), work_to=time(8))


async def test_taken_slots_counts_live_requests(service, make_request):
    moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    await make_request(service, moment)
    await make_request(service, moment)

    taken = await db.get_taken_slots(
        service, moment - timedelta(hours=1), moment + timedelta(hours=1)
    )
    assert taken[moment] == 2


async def test_cancelled_request_frees_the_slot(service, make_request):
    moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    request = await make_request(service, moment)
    await db.update_request_status(
        str(request["idrequests"]), "cancelled",
        changed_by=None, allowed_from=config.STATUS_TRANSITIONS["cancelled"],
    )

    taken = await db.get_taken_slots(
        service, moment - timedelta(hours=1), moment + timedelta(hours=1)
    )
    assert taken.get(moment, 0) == 0


async def test_done_request_keeps_the_slot(service, make_request):
    """Выполненная заявка — история, это время больше не продаётся."""
    moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    request = await make_request(service, moment)
    # Прямо из «новой» в «выполнена» нельзя — так устроен STATUS_TRANSITIONS
    for status in ("accepted", "done"):
        await db.update_request_status(
            str(request["idrequests"]), status,
            changed_by=None, allowed_from=config.STATUS_TRANSITIONS[status],
        )

    taken = await db.get_taken_slots(
        service, moment - timedelta(hours=1), moment + timedelta(hours=1)
    )
    assert taken[moment] == 1
```

Добавьте в `tests/conftest.py` фикстуру, создающую заявку на время:

```python
@pytest_asyncio.fixture
async def make_request(db_ready):
    """Заявка на конкретное время. Услуга создаётся своя, чтобы не мешать тестам."""
    async def _make(idservice: str, moment, *, client_uid: str | None = None):
        item = await db.add_catalog_item(idservice, f"Работа {uuid.uuid4().hex[:6]}")
        request, _ = await db.create_request(
            idservice=idservice,
            client_tg_id=TEST_OWNER_ID,
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
            urgency="low",     # уходит в задаче 9 вместе с колонкой
            scheduled_at=moment,
            client_uid=client_uid or str(uuid.uuid4()),
        )
        return request
    return _make
```

> Фикстура вызывает `create_request` с `scheduled_at` — параметра ещё нет, он
> появится в задаче 5. До неё эти тесты падают на `TypeError`, и это нормально:
> шаг 4 ниже проверяет только те из них, что не создают заявок.

- [ ] **Шаг 2: Запустить и убедиться, что тесты падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_schedule.py -q`

Ожидается: FAIL, `AttributeError: 'Database' object has no attribute 'get_schedule'`

- [ ] **Шаг 3: Заводить расписание вместе с сервисом**

В `database.py`, в методе `create_service`, внутри той же транзакции, где
создаётся сервис, после вставки в `services`:

```python
            # Расписание по умолчанию — часть создания сервиса, а не отдельный
            # шаг: сервис без расписания не может принять ни одной заявки
            await conn.execute(
                "INSERT INTO service_schedule (idservice) VALUES ($1) "
                "ON CONFLICT (idservice) DO NOTHING",
                idservice,
            )
```

- [ ] **Шаг 4: Добавить методы расписания**

В `database.py`, после методов каталога:

```python
    # ── schedule ─────────────────────────────────────────────────────────────

    _SCHEDULE_FIELDS = (
        "work_from", "work_to", "slot_minutes",
        "lunch_from", "lunch_to", "weekdays", "horizon_days", "capacity",
    )

    async def get_schedule(self, idservice: str) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM service_schedule WHERE idservice=$1", idservice
            )

    async def update_schedule(self, idservice: str, **fields) -> asyncpg.Record | None:
        """
        Правка расписания. Имена полей сверяются с белым списком: они приходят
        из хендлеров, и подстановка их в SQL без проверки — дыра.
        """
        unknown = set(fields) - set(self._SCHEDULE_FIELDS)
        if unknown:
            raise ValueError(f"Неизвестные поля расписания: {sorted(unknown)}")
        if not fields:
            return await self.get_schedule(idservice)

        columns = list(fields)
        assignments = ", ".join(f"{name}=${i}" for i, name in enumerate(columns, 2))
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                f"UPDATE service_schedule SET {assignments}, updatedate=now() "
                "WHERE idservice=$1 RETURNING *",
                idservice, *(fields[name] for name in columns),
            )

    async def get_taken_slots(
        self, idservice: str, since: datetime, until: datetime
    ) -> dict[datetime, int]:
        """Сколько живых заявок на каждый момент времени в окне."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT scheduled_at, count(*) AS taken FROM requests
                 WHERE idservice=$1 AND idrecstatus=0
                   AND scheduled_at BETWEEN $2 AND $3
                   AND status = ANY($4::text[])
                 GROUP BY scheduled_at
                """,
                idservice, since, until, list(config.SLOT_HOLDING_STATUSES),
            )
        return {row["scheduled_at"]: row["taken"] for row in rows}
```

Добавьте `from datetime import datetime` в импорты `database.py`, если его там
ещё нет.

В `config.py`, рядом с `REQUEST_STATUS_LABELS`:

```python
# Статусы, при которых заявка держит своё окно записи. Отказ, отмена и закрытие
# сервиса окно освобождают; «выполнена» держит навсегда — это уже история.
SLOT_HOLDING_STATUSES: tuple[str, ...] = (
    "new", "accepted", "called", "in_progress", "done",
)
```

- [ ] **Шаг 5: Запустить тесты, не требующие заявок**

Запустить:

```bash
.venv/Scripts/python.exe -m pytest tests/test_schedule.py -q -k "default_schedule or update_schedule"
```

Ожидается: PASS, 4 теста. Остальные ждут задачи 5.

- [ ] **Шаг 6: Коммит**

```bash
git add database.py config.py tests/test_schedule.py tests/conftest.py
git commit -m "Слой БД: расписание сервиса и занятость окон"
```

---

### Задача 5: Заявка занимает окно

**Файлы:**
- Изменить: `database.py` (`create_request`), `handlers/requests.py`
- Тест: `tests/test_schedule.py`

**Интерфейсы:**
- Потребляет: `db.get_schedule`, `config.SLOT_HOLDING_STATUSES` (задача 4)
- Производит:
  - `database.SlotTaken` — исключение переполнения окна
  - `db.create_request(..., scheduled_at: datetime | None)` — новый параметр

- [ ] **Шаг 1: Написать падающий тест на гонку**

Добавьте в `tests/test_schedule.py`:

```python
import asyncio

from database import SlotTaken


async def test_slot_capacity_is_enforced(service, make_request):
    moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    await make_request(service, moment)
    with pytest.raises(SlotTaken):
        await make_request(service, moment)


async def test_parallel_booking_gives_one_request(service, make_request):
    """
    Двое жмут «Отправить» на одно окно одновременно.

    Без блокировки оба проходят проверку занятости до того, как любой вставит
    строку, и одно место продаётся дважды.
    """
    moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=2)
    results = await asyncio.gather(
        make_request(service, moment),
        make_request(service, moment),
        return_exceptions=True,
    )
    rejected = [r for r in results if isinstance(r, SlotTaken)]
    created = [r for r in results if not isinstance(r, Exception)]
    assert len(created) == 1
    assert len(rejected) == 1


async def test_second_place_is_free_when_capacity_allows(service, make_request):
    await db.update_schedule(service, capacity=2)
    moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=3)
    await make_request(service, moment)
    await make_request(service, moment)  # не бросает

    taken = await db.get_taken_slots(
        service, moment - timedelta(hours=1), moment + timedelta(hours=1)
    )
    assert taken[moment] == 2
```

- [ ] **Шаг 2: Запустить и убедиться, что тесты падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_schedule.py -q`

Ожидается: FAIL, `ImportError: cannot import name 'SlotTaken' from 'database'`

- [ ] **Шаг 3: Добавить исключение и параметр**

В `database.py`, рядом с `ForeignClientUid`:

```python
class SlotTaken(Exception):
    """Все места на выбранное время разобраны."""
```

В сигнатуру `create_request` добавьте параметр после `plate`:

```python
        scheduled_at: datetime | None = None,
```

`urgency` из сигнатуры **не трогайте**: он уходит целиком в задаче 9, а до тех
пор его убирание сломало бы все существующие вызовы.

- [ ] **Шаг 4: Занять окно в той же транзакции**

В `create_request`, внутри `async with self.pool.acquire() as conn,
conn.transaction():`, **перед** вставкой в `requests`:

```python
            # Блокировка строки расписания делает проверку и вставку неделимыми.
            # Без неё два одновременных клиента оба проходят проверку до того,
            # как любой из них вставит строку, и одно место уходит дважды.
            if scheduled_at is not None:
                schedule = await conn.fetchrow(
                    "SELECT capacity FROM service_schedule WHERE idservice=$1 FOR UPDATE",
                    idservice,
                )
                if schedule is None:
                    raise SlotTaken(scheduled_at)

                # Повторный тап не должен занимать второе место: заявка с этим
                # client_uid уже существует и своё место уже держит
                already = await conn.fetchval(
                    "SELECT count(*) FROM requests "
                    " WHERE idservice=$1 AND scheduled_at=$2 AND idrecstatus=0 "
                    "   AND status = ANY($3::text[]) "
                    "   AND ($4::text IS NULL OR client_uid IS DISTINCT FROM $4)",
                    idservice, scheduled_at,
                    list(config.SLOT_HOLDING_STATUSES), client_uid,
                )
                if already >= schedule["capacity"]:
                    raise SlotTaken(scheduled_at)
```

В `INSERT INTO requests` добавьте колонку `scheduled_at` и параметр `$12`:

```python
                """
                INSERT INTO requests
                    (idrequests, idservice, idclienttg, client_name, phone,
                     brand, model, plate, urgency, comment, client_uid,
                     scheduled_at, status, idrecstatus)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'new',0)
                ON CONFLICT (client_uid) WHERE client_uid IS NOT NULL DO NOTHING
                RETURNING *
                """,
                _new_id(), idservice, client_tg_id, client_name, phone,
                brand, model, plate, urgency, comment, client_uid,
                scheduled_at,
```

Добавьте `import config` в `database.py`, если его там нет.

- [ ] **Шаг 5: Превратить отказ в понятный текст**

В `handlers/requests.py`, в `create_request_flow`, расширьте существующий
`except ForeignClientUid`:

```python
    except SlotTaken:
        raise RequestRejected(
            "Это время только что заняли. Выберите другое — список обновится."
        ) from None
```

Импортируйте `SlotTaken` из `database` рядом с `ForeignClientUid`.

- [ ] **Шаг 6: Прогнать тесты расписания**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_schedule.py -q`

Ожидается: PASS, все тесты файла, включая гонку.

- [ ] **Шаг 7: Прогнать всю сюиту**

Запустить: `.venv/Scripts/python.exe -m pytest -q`

Ожидается: PASS. Существующие тесты заявок не передают `scheduled_at` — у
параметра значение по умолчанию `None`, они продолжают работать.

- [ ] **Шаг 8: Коммит**

```bash
git add database.py handlers/requests.py tests/test_schedule.py
git commit -m "Заявка занимает окно записи, гонка закрыта блокировкой расписания"
```

---

### Задача 6: Экран расписания у управляющего

**Файлы:**
- Создать: `handlers/schedule.py`
- Изменить: `keyboards.py`, `app.py` (регистрация роутера), `render.py`
- Тест: `tests/test_keyboards.py`, `tests/test_render.py`

**Интерфейсы:**
- Потребляет: `db.get_schedule`, `db.update_schedule` (задача 4),
  `slots.free_slots` (задача 2), валидаторы (задача 1)
- Производит:
  - `keyboards.BTN_SCHEDULE`, `kb.kb_schedule()`, `kb.kb_schedule_step()`,
    `kb.kb_schedule_days(selected: list[int])`
  - `render.schedule_card(svc, schedule, free_count) -> str`
  - `render.weekdays_label(weekdays) -> str`
  - роутер `handlers.schedule.router`

- [ ] **Шаг 1: Написать падающие тесты клавиатур и текста**

В `tests/test_keyboards.py`:

```python
def test_schedule_keyboard_has_all_fields(webapp_configured):
    actions = [b.callback_data for row in kb.kb_schedule().inline_keyboard for b in row]
    assert "schedhours" in actions
    assert "schedstep" in actions
    assert "schedlunch" in actions
    assert "scheddays" in actions
    assert "schedcap" in actions
    assert "schedhorizon" in actions


def test_schedule_days_marks_selected(webapp_configured):
    markup = kb.kb_schedule_days([1, 2])
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "✅ пн" in labels
    assert "вт" in " ".join(labels)
    assert "✅ сб" not in labels
```

В `tests/test_render.py`:

```python
from datetime import time

from render import weekdays_label


def test_weekdays_label_lists_days_in_order():
    assert weekdays_label([1, 2, 3, 4, 5]) == "пн, вт, ср, чт, пт"


def test_weekdays_label_sorts_input():
    assert weekdays_label([5, 1]) == "пн, пт"
```

- [ ] **Шаг 2: Запустить и убедиться, что тесты падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_keyboards.py tests/test_render.py -q`

Ожидается: FAIL, `AttributeError: module 'keyboards' has no attribute 'kb_schedule'`

- [ ] **Шаг 3: Добавить клавиатуры**

В `keyboards.py`, рядом с `BTN_SERVICES`:

```python
BTN_SCHEDULE       = "🗓 Расписание"
```

В `kb_owner_main`, в строку с услугами:

```python
        [KeyboardButton(text=BTN_SERVICES), KeyboardButton(text=BTN_SCHEDULE)],
```

В секцию inline-клавиатур:

Названия дней берутся из `render.WEEKDAY_NAMES` (шаг 4) — `keyboards.py` уже
импортирует `render`, и второй копии кортежа в проекте быть не должно.

```python
def kb_schedule() -> InlineKeyboardMarkup:
    """Карточка расписания: по кнопке на каждое поле шаблона."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕘 Часы", callback_data="schedhours"),
         InlineKeyboardButton(text="⏱ Шаг", callback_data="schedstep")],
        [InlineKeyboardButton(text="🍽 Обед", callback_data="schedlunch"),
         InlineKeyboardButton(text="📅 Дни", callback_data="scheddays")],
        [InlineKeyboardButton(text="🚗 Машин", callback_data="schedcap"),
         InlineKeyboardButton(text="📆 Горизонт", callback_data="schedhorizon")],
    ])


def kb_schedule_step() -> InlineKeyboardMarkup:
    """Шаг записи — выбор из конечного набора, печатать нечего."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{m} мин", callback_data=f"schedstep:{m}")
         for m in (30, 60)],
        [InlineKeyboardButton(text=f"{m} мин", callback_data=f"schedstep:{m}")
         for m in (90, 120)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="schedback")],
    ])


def kb_schedule_days(selected: list[int]) -> InlineKeyboardMarkup:
    """Семь переключателей: тап меняет день, «Готово» сохраняет."""
    chosen = set(selected)
    rows = []
    for start in (0, 4):
        rows.append([
            InlineKeyboardButton(
                text=("✅ " if day + 1 in chosen else "") + render.WEEKDAY_NAMES[day],
                callback_data=f"scheddaytoggle:{day + 1}",
            )
            for day in range(start, min(start + 4, 7))
        ])
    rows.append([InlineKeyboardButton(text="💾 Готово", callback_data="scheddaysdone")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
```

- [ ] **Шаг 4: Добавить сборку текста карточки**

В `render.py`, после `price_label`:

```python
WEEKDAY_NAMES = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def weekdays_label(weekdays) -> str:
    """[1,2,3,4,5] → «пн, вт, ср, чт, пт». Порядок всегда недельный."""
    return ", ".join(WEEKDAY_NAMES[day - 1] for day in sorted(weekdays))


def _hm(value) -> str:
    return value.strftime("%H:%M")


def schedule_card(svc, schedule, free_count: int) -> str:
    """
    Карточка расписания. Счётчик свободных окон считается тем же генератором,
    что и форма, — это единственный способ для управляющего сразу увидеть,
    что он поставил обед на весь день.
    """
    lunch = (
        f"{_hm(schedule['lunch_from'])} — {_hm(schedule['lunch_to'])}"
        if schedule["lunch_from"] else "нет"
    )
    return (
        f"🗓 <b>Расписание — {h(svc['service_name'])}</b>\n\n"
        f"🕘 Часы работы: {_hm(schedule['work_from'])} — {_hm(schedule['work_to'])}\n"
        f"⏱ Шаг записи: {schedule['slot_minutes']} минут\n"
        f"🍽 Обед: {lunch}\n"
        f"📅 Рабочие дни: {weekdays_label(schedule['weekdays'])}\n"
        f"🚗 Машин за раз: {schedule['capacity']}\n"
        f"📆 Открыто вперёд: {schedule['horizon_days']} дней\n\n"
        f"Свободных окон на этот срок: {free_count}"
    )
```

- [ ] **Шаг 5: Написать хендлеры**

Создайте `handlers/schedule.py` по образцу `handlers/catalog.py` — те же
`_owner_service`, `kb.kb_cancel()`, `show_main_menu`:

```python
"""
handlers/schedule.py — расписание сервиса.

Часы, обед, вместимость и горизонт правятся текстовым вводом, как цена услуги.
Шаг и рабочие дни — inline-кнопками: набор вариантов конечен, печатать и потом
разбирать опечатки незачем.
"""

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import keyboards as kb
import render
import slots
from database import db
from handlers.common import require_owner_service, show_main_menu
from validators import (
    ValidationError,
    validate_capacity,
    validate_horizon,
    validate_lunch,
    validate_time_range,
)

router = Router()


class ScheduleEdit(StatesGroup):
    hours = State()
    lunch = State()
    capacity = State()
    horizon = State()


async def _free_count(idservice: str, schedule, tz: str) -> int:
    """Сколько окон увидит клиент прямо сейчас."""
    now = datetime.now(timezone.utc)
    taken = await db.get_taken_slots(
        idservice, now, now + timedelta(days=schedule["horizon_days"] + 1)
    )
    free = slots.free_slots(schedule, tz, now, taken)
    return sum(len(times) for times in free.values())


async def _show_schedule(message: Message, svc, *, edit: bool = False) -> None:
    idservice = str(svc["idservice"])
    schedule = await db.get_schedule(idservice)
    text = render.schedule_card(
        svc, schedule, await _free_count(idservice, schedule, svc["timezone"])
    )
    if edit:
        await message.edit_text(text, reply_markup=kb.kb_schedule())
    else:
        await message.answer(text, reply_markup=kb.kb_schedule())


@router.message(F.text == kb.BTN_SCHEDULE)
async def schedule_open(message: Message, state: FSMContext) -> None:
    svc = await require_owner_service(message, state)
    if svc is None:
        return
    await _show_schedule(message, svc)


# ── Часы работы ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "schedhours")
async def hours_ask(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        return
    await state.set_state(ScheduleEdit.hours)
    await callback.message.answer(
        "Введите часы работы: <b>9-18</b> или <b>09:00-18:00</b>.",
        reply_markup=kb.kb_cancel(),
    )
    await callback.answer()


@router.message(ScheduleEdit.hours)
async def hours_finish(message: Message, state: FSMContext) -> None:
    svc = await require_owner_service(message, state)
    if svc is None:
        await state.set_state(None)
        await show_main_menu(message, state)
        return
    try:
        work_from, work_to = validate_time_range(message.text, field="Часы")
    except ValidationError as exc:
        await message.answer(f"❌ {exc}")
        return

    await db.update_schedule(str(svc["idservice"]), work_from=work_from, work_to=work_to)
    await state.set_state(None)
    await show_main_menu(message, state, greeting="✅ Часы работы обновлены.")
    await _show_schedule(message, svc)
```

Обед, вместимость и горизонт пишутся ровно по этому образцу — меняются только
состояние, валидатор, подсказка и поля в `update_schedule`:

| Кнопка | Состояние | Валидатор | Поля | Подсказка |
|---|---|---|---|---|
| `schedlunch` | `ScheduleEdit.lunch` | `validate_lunch` | `lunch_from`, `lunch_to` | «Введите обед: 13-14, или <b>-</b>, чтобы убрать» |
| `schedcap` | `ScheduleEdit.capacity` | `validate_capacity` | `capacity` | «Сколько машин принимаете в одно время? Число от 1 до 20» |
| `schedhorizon` | `ScheduleEdit.horizon` | `validate_horizon` | `horizon_days` | «На сколько дней вперёд открыта запись? Число от 1 до 60» |

Для обеда `validate_lunch` возвращает `None` или пару, поэтому:

```python
    lunch = validate_lunch(message.text)
    await db.update_schedule(
        str(svc["idservice"]),
        lunch_from=lunch[0] if lunch else None,
        lunch_to=lunch[1] if lunch else None,
    )
```

Шаг и дни правятся без ввода текста:

```python
# ── Шаг записи ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == "schedstep")
async def step_ask(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        return
    await callback.message.edit_reply_markup(reply_markup=kb.kb_schedule_step())
    await callback.answer()


@router.callback_query(F.data.startswith("schedstep:"))
async def step_set(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        return
    minutes = int(callback.data.split(":", 1)[1])
    await db.update_schedule(str(svc["idservice"]), slot_minutes=minutes)
    await _show_schedule(callback.message, svc, edit=True)
    await callback.answer(f"Шаг записи: {minutes} минут")


@router.callback_query(F.data == "schedback")
async def step_back(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        return
    await _show_schedule(callback.message, svc, edit=True)
    await callback.answer()


# ── Рабочие дни ──────────────────────────────────────────────────────────────
# Выбор копится в FSM, а не в базе: иначе каждый тап писал бы в неё, и снятый
# последний день на мгновение нарушал бы констрейнт непустого набора.

@router.callback_query(F.data == "scheddays")
async def days_ask(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        return
    schedule = await db.get_schedule(str(svc["idservice"]))
    chosen = list(schedule["weekdays"])
    await state.update_data(weekdays=chosen)
    await callback.message.edit_reply_markup(reply_markup=kb.kb_schedule_days(chosen))
    await callback.answer()


@router.callback_query(F.data.startswith("scheddaytoggle:"))
async def days_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    day = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    chosen = set(data.get("weekdays", []))
    chosen.symmetric_difference_update({day})
    await state.update_data(weekdays=sorted(chosen))
    await callback.message.edit_reply_markup(
        reply_markup=kb.kb_schedule_days(sorted(chosen))
    )
    await callback.answer()


@router.callback_query(F.data == "scheddaysdone")
async def days_save(callback: CallbackQuery, state: FSMContext) -> None:
    svc = await require_owner_service(callback.message, state, callback.from_user.id)
    if svc is None:
        return
    data = await state.get_data()
    chosen = sorted(data.get("weekdays", []))
    if not chosen:
        # Констрейнт это тоже поймает, но управляющему нужен текст, а не
        # ошибка драйвера
        await callback.answer("Оставьте хотя бы один рабочий день", show_alert=True)
        return

    await db.update_schedule(str(svc["idservice"]), weekdays=chosen)
    await state.update_data(weekdays=None)
    await _show_schedule(callback.message, svc, edit=True)
    await callback.answer("Рабочие дни сохранены")
```

- [ ] **Шаг 5б: Поднять `_owner_service` в общий модуль**

`handlers/schedule.py` нужна та же проверка «активный сервис и пользователь —
его управляющий», что уже есть приватной функцией `_owner_service` в
`handlers/catalog.py`. Импортировать приватное имя из соседнего модуля значит
закрепить случайную зависимость между двумя экранами.

Перенесите функцию в `handlers/common.py` под именем `require_owner_service`,
рядом с `require_active_service`, и обобщите текст отказа:

```python
async def require_owner_service(
    message: Message, state: FSMContext, user_id: int | None = None
):
    """Активный сервис, если пользователь — его управляющий. Иначе None."""
    user_id = user_id or message.from_user.id
    svc = await require_active_service(message, state, user_id)
    if svc is None:
        return None
    if svc["owner_id"] != user_id:
        await message.answer("❌ Это может только управляющий сервисом.")
        return None
    return svc
```

В `handlers/catalog.py` удалите `_owner_service`, импортируйте
`require_owner_service` из `handlers.common` и замените вызовы.

- [ ] **Шаг 6: Зарегистрировать роутер**

В `app.py`, в импорт и в кортеж роутеров:

```python
from handlers import admin_actions, admin_mgmt, catalog, register, requests, schedule, start
```

```python
    catalog.router,
    schedule.router,
```

- [ ] **Шаг 7: Прогнать тесты и проверку импорта**

Запустить:

```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -c "import app; print('роутеры собираются')"
```

Ожидается: PASS и «роутеры собираются».

- [ ] **Шаг 8: Коммит**

```bash
git add handlers/schedule.py keyboards.py render.py app.py tests/
git commit -m "Экран расписания: часы, шаг, обед, дни, вместимость, горизонт"
```

---

### Задача 7: API отдаёт свободные окна

**Файлы:**
- Изменить: `app.py` (`api_service`, `RequestPayload`), `validators.py`
- Тест: `tests/test_validators.py`

**Интерфейсы:**
- Потребляет: `slots.free_slots`, `db.get_schedule`, `db.get_taken_slots`
- Производит:
  - `/api/service/{id}` дополнительно отдаёт `timezone` и `slots`
  - `RequestPayload.scheduled_at: str = ""`, `urgency` становится необязательным
  - `validators.validate_scheduled_at(raw, *, free, tz) -> datetime`

- [ ] **Шаг 1: Написать падающий тест валидатора**

В `tests/test_validators.py`:

```python
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from validators import validate_scheduled_at

FREE = {date(2026, 8, 17): [time(9), time(10)]}


def test_scheduled_at_accepts_free_slot():
    got = validate_scheduled_at("2026-08-17 10:00", free=FREE, tz="Europe/Moscow")
    assert got == datetime(2026, 8, 17, 10, 0, tzinfo=ZoneInfo("Europe/Moscow"))


def test_scheduled_at_rejects_slot_outside_schedule():
    """Payload можно прислать в обход формы — форма не защита."""
    with pytest.raises(ValidationError):
        validate_scheduled_at("2026-08-17 03:00", free=FREE, tz="Europe/Moscow")


def test_scheduled_at_rejects_unknown_day():
    with pytest.raises(ValidationError):
        validate_scheduled_at("2026-08-18 09:00", free=FREE, tz="Europe/Moscow")


def test_scheduled_at_rejects_garbage():
    with pytest.raises(ValidationError):
        validate_scheduled_at("завтра", free=FREE, tz="Europe/Moscow")
```

- [ ] **Шаг 2: Запустить и убедиться, что тест падает**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_validators.py -q`

Ожидается: `ImportError: cannot import name 'validate_scheduled_at'`

- [ ] **Шаг 3: Реализовать валидатор**

В `validators.py`:

```python
def validate_scheduled_at(raw: object, *, free: dict, tz: str) -> datetime:
    """
    Момент записи из формы: «2026-08-17 10:00» в зоне сервиса.

    Проверяется не формат, а принадлежность реально свободному окну: payload
    можно отправить в обход формы, поэтому список окон — единственный источник
    истины, а не то, что прислал клиент.
    """
    text = clean_text(raw, field="Время записи", max_len=20)
    try:
        moment = datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValidationError("Выберите время записи.") from None

    if moment.time() not in free.get(moment.date(), []):
        raise ValidationError("Это время только что заняли. Выберите другое.")
    return moment.replace(tzinfo=ZoneInfo(tz))
```

Добавьте импорты `from datetime import datetime` и `from zoneinfo import ZoneInfo`.

- [ ] **Шаг 4: Отдавать окна в карточке сервиса**

В `app.py`, в `api_service`, внутри `async with _db_gate:` после `get_catalog`:

```python
        schedule = await db.get_schedule(service_id)
        now = datetime.now(timezone.utc)
        taken = await db.get_taken_slots(
            service_id, now, now + timedelta(days=schedule["horizon_days"] + 1)
        ) if schedule else {}
```

И в возвращаемый словарь:

```python
        "timezone": svc["timezone"],
        "slots": {
            day.isoformat(): [moment.strftime("%H:%M") for moment in times]
            for day, times in (
                slots.free_slots(schedule, svc["timezone"], now, taken)
                if schedule else {}
            ).items()
        },
```

- [ ] **Шаг 5: Принять время в payload**

В `RequestPayload` добавьте поле и ослабьте срочность:

```python
    scheduled_at: str = ""
    urgency: str = "low"   # уходит в задаче 9, пока терпим отсутствие в payload
```

В `validators.validate_request_fields` пока ничего не меняйте: время
проверяется отдельно, в `create_request_flow`, где доступны сервис и его зона.

В `handlers/requests.py`, в `create_request_flow`, после получения `service`:

```python
    schedule = await db.get_schedule(service_id)
    if schedule is None:
        raise RequestRejected("Сервис пока не открыл время для записи.")

    now = datetime.now(timezone.utc)
    taken = await db.get_taken_slots(
        service_id, now, now + timedelta(days=schedule["horizon_days"] + 1)
    )
    free = slots.free_slots(schedule, service["timezone"], now, taken)
    try:
        scheduled_at = validate_scheduled_at(
            payload.get("scheduled_at"), free=free, tz=service["timezone"]
        )
    except ValidationError as exc:
        raise RequestRejected(str(exc)) from None
```

и передайте `scheduled_at=scheduled_at` в `db.create_request`.

- [ ] **Шаг 6: Проверить API руками**

Поднимите приложение и запросите карточку сервиса:

```bash
curl -s "http://localhost:8080/api/service/<idservice>" | \
  .venv/Scripts/python.exe -c "import json,sys; d=json.load(sys.stdin); print(d['timezone']); print(list(d['slots'])[:3])"
```

Ожидается: зона сервиса и первые даты со свободными окнами.

- [ ] **Шаг 7: Прогнать сюиту**

Запустить: `.venv/Scripts/python.exe -m pytest -q`

Ожидается: PASS.

- [ ] **Шаг 8: Коммит**

```bash
git add app.py validators.py handlers/requests.py tests/test_validators.py
git commit -m "API: свободные окна в карточке сервиса и приём времени записи"
```

---

### Задача 8: Календарь в форме записи

**Файлы:**
- Изменить: `webapp/index.html`

**Интерфейсы:**
- Потребляет: `slots` и `timezone` из `/api/service/{id}` (задача 7)
- Производит: поле `scheduled_at` в теле `POST /api/requests`; `urgency` больше
  не отправляется

- [ ] **Шаг 1: Убрать срочность**

Удалите константу `URGENCY`, блок поля «Срочность» в `renderForm` и строку
`urgency: get("f_urgency")` из тела запроса в `doSubmit`.

- [ ] **Шаг 2: Добавить состояние выбора**

Рядом с остальными полями `state`:

```js
  slots:      {},      // { "2026-08-17": ["09:00", …] } из API
  month:      null,    // Date, первое число показываемого месяца
  pickedDay:  null,    // "2026-08-17"
  pickedTime: null,    // "10:00"
```

В `openService`, после получения карточки:

```js
    state.slots = state.selectedService.slots || {};
    const days = Object.keys(state.slots).sort();
    state.month = days.length ? firstOfMonth(days[0]) : null;
```

- [ ] **Шаг 3: Нарисовать календарь**

Добавьте функции и вставьте `renderCalendar()` на место блока «Срочность»:

```js
const MONTHS = ["января","февраля","марта","апреля","мая","июня",
                "июля","августа","сентября","октября","ноября","декабря"];
const MONTHS_NOM = ["Январь","Февраль","Март","Апрель","Май","Июнь",
                    "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"];

function firstOfMonth(iso) {
  const [y, m] = iso.split("-").map(Number);
  return new Date(y, m - 1, 1);
}

function isoDay(date) {
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${m}-${d}`;
}

// Сетка месяца начинается с понедельника: getDay() отдаёт воскресенье нулём
function mondayOffset(date) {
  return (date.getDay() + 6) % 7;
}

function renderCalendar() {
  const box = document.getElementById("calendar");
  if (!box || !state.month) return;

  const year = state.month.getFullYear();
  const month = state.month.getMonth();
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const lead = mondayOffset(new Date(year, month, 1));

  const cells = [];
  for (let i = 0; i < lead; i++) cells.push(`<span class="cal-cell"></span>`);
  for (let day = 1; day <= daysInMonth; day++) {
    const iso = isoDay(new Date(year, month, day));
    const free = (state.slots[iso] || []).length > 0;
    const picked = state.pickedDay === iso;
    cells.push(`
      <button type="button" class="cal-cell cal-day${free ? "" : " cal-off"}${picked ? " cal-picked" : ""}"
              ${free ? `data-day="${iso}"` : "disabled"}>${day}</button>`);
  }

  const all = Object.keys(state.slots).sort();
  const canPrev = all.some(d => firstOfMonth(d) < state.month);
  const canNext = all.some(d => firstOfMonth(d) > state.month);

  box.innerHTML = `
    <div class="cal-head">
      <button type="button" id="calPrev" ${canPrev ? "" : "disabled"}>‹</button>
      <span>${MONTHS_NOM[month]} ${year}</span>
      <button type="button" id="calNext" ${canNext ? "" : "disabled"}>›</button>
    </div>
    <div class="cal-grid">
      ${["пн","вт","ср","чт","пт","сб","вс"].map(d => `<span class="cal-dow">${d}</span>`).join("")}
      ${cells.join("")}
    </div>
    <div id="calTimes" class="cal-times"></div>`;

  box.querySelector("#calPrev").addEventListener("click", () => shiftMonth(-1));
  box.querySelector("#calNext").addEventListener("click", () => shiftMonth(1));
  box.querySelectorAll("[data-day]").forEach(el =>
    el.addEventListener("click", () => {
      state.pickedDay = el.dataset.day;
      state.pickedTime = null;
      renderCalendar();
    })
  );
  renderTimes();
}

function shiftMonth(delta) {
  state.month = new Date(state.month.getFullYear(), state.month.getMonth() + delta, 1);
  renderCalendar();
}

function renderTimes() {
  const box = document.getElementById("calTimes");
  if (!box) return;
  if (!state.pickedDay) { box.innerHTML = ""; return; }

  const [y, m, d] = state.pickedDay.split("-").map(Number);
  const times = state.slots[state.pickedDay] || [];
  box.innerHTML = `
    <div class="cal-daylabel">${d} ${MONTHS[m - 1]}</div>
    <div class="cal-chips">
      ${times.map(t => `
        <button type="button" class="cal-chip${state.pickedTime === t ? " cal-picked" : ""}"
                data-time="${t}">${t}</button>`).join("")}
    </div>
    <div class="cal-tz">время сервиса, ${esc(state.selectedService.timezone || "")}</div>`;

  box.querySelectorAll("[data-time]").forEach(el =>
    el.addEventListener("click", () => {
      state.pickedTime = el.dataset.time;
      renderTimes();
    })
  );
}
```

- [ ] **Шаг 4: Отправлять выбранное время**

В `doSubmit`, перед отправкой:

```js
  if (!state.pickedDay || !state.pickedTime) {
    el("formError").innerHTML = `<div class="error-banner">Выберите день и время записи.</div>`;
    return;
  }
```

и в тело запроса вместо `urgency`:

```js
        scheduled_at: `${state.pickedDay} ${state.pickedTime}`,
```

- [ ] **Шаг 5: Обновлять окна после отказа**

В `catch` блока отправки, после показа ошибки:

```js
    // Окно могли занять, пока клиент заполнял форму: перечитываем карточку,
    // иначе он будет тыкать в то же исчезнувшее время
    try {
      const fresh = await api(`/api/service/${encodeURIComponent(state.serviceId)}`);
      state.slots = fresh.slots || {};
      state.pickedTime = null;
      renderCalendar();
    } catch (ignored) {}
```

- [ ] **Шаг 6: Пустое расписание**

В `renderForm`, рядом с проверкой пустого каталога:

```js
  if (Object.keys(state.slots).length === 0) {
    render(`
      <div class="header"><div class="logo">${esc(svc.service_name || "Автосервис")}</div></div>
      <div class="card">
        <div class="error-banner">
          Сервис пока не открыл время для записи.<br>Попробуйте позже.
        </div>
      </div>
    `);
    return;
  }
```

- [ ] **Шаг 7: Добавить стили**

В `<style>`, рядом со стилями каталога:

```css
  .cal-head { display: flex; justify-content: space-between; align-items: center;
              font-weight: 600; padding: 4px 0 10px; }
  .cal-head button { background: none; border: none; color: var(--accent);
                     font-size: 22px; padding: 0 10px; cursor: pointer; }
  .cal-head button:disabled { color: var(--muted); opacity: .4; cursor: default; }
  .cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; }
  .cal-dow { text-align: center; font-size: 11px; color: var(--muted); padding-bottom: 4px; }
  .cal-cell { aspect-ratio: 1; display: flex; align-items: center; justify-content: center;
              border: none; background: none; color: var(--text);
              font: inherit; border-radius: var(--radius-sm); }
  .cal-day { cursor: pointer; background: rgba(255,255,255,0.05); }
  .cal-off { color: var(--muted); opacity: .35; cursor: default; }
  .cal-picked { background: linear-gradient(110deg, var(--accent), var(--accent2));
                color: #111; font-weight: 700; }
  .cal-times { margin-top: 14px; }
  .cal-daylabel { font-size: 13px; color: var(--muted); margin-bottom: 8px; }
  .cal-chips { display: grid; grid-template-columns: repeat(auto-fill, minmax(72px, 1fr)); gap: 8px; }
  .cal-chip { padding: 10px 6px; border: 1.5px solid var(--border); border-radius: var(--radius-sm);
              background: rgba(255,255,255,0.05); color: var(--text); font: inherit; cursor: pointer; }
  .cal-tz { margin-top: 10px; font-size: 11px; color: var(--muted); }
```

- [ ] **Шаг 8: Проверить, что срочности не осталось**

Запустить: `grep -n "urgency\|URGENCY\|Срочность" webapp/index.html`

Ожидается: пусто.

- [ ] **Шаг 9: Проверить синтаксис JavaScript**

Запустить:

```bash
sed -n '/^<script>$/,/^<\/script>$/p' webapp/index.html | sed '1d;$d' > /tmp/form.js
node --check /tmp/form.js && echo "JS syntax OK"
```

Ожидается: «JS syntax OK».

- [ ] **Шаг 10: Коммит**

```bash
git add webapp/index.html
git commit -m "Форма записи: календарь и выбор времени вместо срочности"
```

---

### Задача 9: Уборка срочности

**Файлы:**
- Изменить: `validators.py`, `render.py`, `database.py`, `app.py`,
  `handlers/requests.py`, `config.py`, `schema.sql`
- Тест: `tests/test_render.py`, `tests/test_catalog.py`

**Интерфейсы:**
- Производит: `render.request_card_for_staff` показывает время записи вместо
  срочности; `urgency` исчезает из кода

- [ ] **Шаг 1: Написать падающий тест карточки**

В `tests/test_render.py`:

```python
from datetime import datetime, timezone

from render import request_card_for_staff


def _req(**overrides):
    """Заявка как словарь: request_card_for_staff читает её по ключам."""
    base = {
        "seq": 42,
        "client_name": "Иван",
        "phone": "+79991234567",
        "idclienttg": 111,
        "brand": "Toyota",
        "model": "Camry",
        "plate": "А777АА777",
        "comment": "",
        "status": "new",
        "createdate": datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc),
        "scheduled_at": datetime(2026, 8, 17, 7, 0, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


SERVICES = [{"title": "Замена масла", "price_rub": 1200}]


def test_request_card_shows_scheduled_time():
    """Время записи заменило срочность."""
    text = request_card_for_staff(_req(), SERVICES, tz="Europe/Moscow")
    assert "Срочность" not in text
    assert "17.08.2026 10:00" in text


def test_request_card_without_time_omits_the_line():
    """Заявки из эпохи срочности времени не имеют — строки просто нет."""
    text = request_card_for_staff(_req(scheduled_at=None), SERVICES, tz="Europe/Moscow")
    assert "Запись" not in text
```

- [ ] **Шаг 2: Запустить и убедиться, что тест падает**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_render.py -q`

Ожидается: FAIL, в карточке всё ещё «Срочность».

- [ ] **Шаг 3: Заменить срочность на время в карточках**

В `render.py`, в `request_card_for_staff` вместо строки со срочностью:

```python
    if req["scheduled_at"]:
        text += f"🗓 <b>Запись:</b> {local_dt(req['scheduled_at'], tz)}\n"
```

В `request_line_for_staff` — так же, вместо `urgency_label`. Удалите функцию
`urgency_label`.

- [ ] **Шаг 4: Убрать срочность из остального кода**

- `validators.py`: удалить `validate_urgency` и строку `"urgency"` из
  `validate_request_fields`.
- `config.py`: удалить `URGENCY_LABELS`.
- `database.py`: убрать параметр `urgency` из `create_request` и колонку из
  `INSERT`.
- `app.py`: убрать поле `urgency` из `RequestPayload`.
- Тесты: убрать `urgency="low"` и `"urgency": "low"` из вызовов.

- [ ] **Шаг 5: Убедиться, что упоминаний не осталось**

Запустить:

```bash
grep -rn "urgency\|URGENCY" --include=*.py --include=*.html . | grep -v .venv | grep -v docs
```

Ожидается: только строка в `schema.sql` — колонка пока живёт как страховка.

- [ ] **Шаг 6: Прогнать сюиту и импорт**

Запустить:

```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -c "import app; print('ок')"
```

Ожидается: PASS.

- [ ] **Шаг 7: Коммит**

```bash
git add -A
git commit -m "Срочность убрана: в карточке заявки время записи"
```

---

### Задача 10: Сквозная проверка

**Файлы:** правки по итогам проверки

- [ ] **Шаг 1: Прогнать всю сюиту**

Запустить: `.venv/Scripts/python.exe -m pytest -q`

Ожидается: PASS, без пропусков кроме тех, что требуют `DATABASE_URL`.

- [ ] **Шаг 2: Поднять приложение**

Запустить `uvicorn app:app --port 8080` и убедиться, что в логе есть
«Webhook установлен».

- [ ] **Шаг 3: Пройти ручной чек-лист**

Управляющий:
- [ ] «🗓 Расписание» → карточка со всеми полями и счётчиком свободных окон
- [ ] «🕘 Часы» → `8-20` → часы изменились, счётчик окон вырос
- [ ] «🕘 Часы» → `20-8` → понятная ошибка, шаг не потерян
- [ ] «⏱ Шаг» → 30 минут → окон стало вдвое больше
- [ ] «🍽 Обед» → `13-14` → счётчик уменьшился; `-` → обед убран
- [ ] «📅 Дни» → снять сб и вс → в календаре клиента этих дней нет
- [ ] «📅 Дни» → снять все → «Оставьте хотя бы один рабочий день»
- [ ] «🚗 Машин» → `3` → одно окно принимает три заявки
- [ ] «📆 Горизонт» → `3` → в календаре видно только три дня

Клиент:
- [ ] Форма → календарь на месяц, выходные и прошедшие дни приглушены
- [ ] Тап по дню → плитки времени, обеденное время отсутствует
- [ ] Подпись «время сервиса» показывает зону сервиса
- [ ] Стрелка «назад» неактивна в текущем месяце
- [ ] Отправка без выбранного времени → «Выберите день и время записи»
- [ ] Отправка заявки → в карточке администратору строка «🗓 Запись»
- [ ] Занять последнее место в окне → окно исчезло из формы у второго клиента
- [ ] Отклонить заявку → время вернулось в список
- [ ] Завершить заявку («выполнена») → время осталось занятым
- [ ] Старые заявки без времени → строка «Запись» не выводится

- [ ] **Шаг 4: Финальный коммит, если правки были**

```bash
git add -A
git commit -m "Правки по итогам сквозной проверки записи на время"
```

---

## Что осталось за рамками

- Точечное закрытие отдельного окна и переопределения на конкретную дату.
- Перенос записи клиентом или администратором.
- Напоминания за день до визита.
- Удаление колонки `urgency` из базы — отдельным коммитом после того, как
  выкат отстоится, по образцу `service_type`.
