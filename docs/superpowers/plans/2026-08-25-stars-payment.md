# Оплата подписки за Telegram Stars — план реализации

> **Для агентов:** ОБЯЗАТЕЛЬНЫЙ САБ-СКИЛЛ: используйте
> superpowers:subagent-driven-development (рекомендуется) или
> superpowers:executing-plans, чтобы выполнять план задача за задачей.
> Шаги отмечаются чекбоксами (`- [ ]`).

**Цель:** управляющий покупает подписку сервиса за Telegram Stars прямо в боте,
владелец бота может вернуть звёзды и отобрать выданные дни.

**Архитектура:** платёж — ещё один источник дней в уже собранном механизме
подписки. Чистый расчёт срока живёт в `subscription.py`, транзакции — в
`database.py`, разговор с Telegram — в новом `handlers/payment.py`. Ключ
идемпотентности `telegram_payment_charge_id` ложится в
`subscription_payments.external_id`, под который частичный уникальный индекс
заведён заранее.

**Стек:** Python 3.12+, aiogram 3.27 (Stars: `send_invoice(currency="XTR")`,
`answer_pre_checkout_query`, `refund_star_payment`), asyncpg, PostgreSQL,
pytest + pytest-asyncio.

**Спека:** `docs/superpowers/specs/2026-08-25-stars-payment-design.md` — план
не спорит со спекой, спека едет вместе с ним, исполнители читают обе.

---

## Глобальные ограничения

- Жёсткий `DELETE` в `database.py` запрещён: удаление мягкое, через
  `idrecstatus = -1`. Исключение — только чистка в тестовых фикстурах.
- Всё пользовательское, что уходит в HTML-сообщение Telegram, экранируется
  через `h()` из `validators.py`. Это правило уже ловило дефекты дважды.
- **Клиент автосервиса никогда не слышит слов «подписка» и «оплата».** Все
  тексты этой фичи видит только управляющий или владелец бота.
- Пустое значение показывается пустотой. Заглушек вроде «не указано» не пишем,
  строку скрываем целиком.
- Все тексты — по-русски, с ёфикацией как в остальном коде.
- Время в базе — `timestamptz`. «Сейчас» в чистых функциях приходит аргументом,
  а не берётся из системных часов.
- Хендлер не должен падать необработанным исключением на кривом вводе.
- `SUBSCRIPTION_ENFORCED` этой фичей **не включается** и остаётся `false`.
- **Тесты идут при включённой подписке**: в `tests/conftest.py` есть autouse-
  фикстура `subscription_enforced`, которая ставит `config.SUBSCRIPTION_ENFORCED
  = True` на каждый тест. Отдельно выключать её в новых тестах не нужно.
- Запуск тестов — только `.venv/Scripts/python.exe -m pytest`. Голый
  `python -m pytest` в проекте не работает.
- Полная сюита идёт ~11 минут против удалённой базы. Во время работы гоняйте
  свой файл точечно, полный прогон — в последней задаче. `TimeoutError`
  перепроверяйте точечным запуском, прежде чем считать падением.
- DDL против общей базы применяет контролёр, а не исполнитель задачи.

---

## Карта файлов

| Файл | Ответственность | Задачи |
|---|---|---|
| `subscription.py` | чистый расчёт срока, знает про знак дней | T1 |
| `config.py` | тарифы и цены из окружения | T2 |
| `schema.sql` | `refunded_at`, констрейнт знака дней | T3 |
| `database.py` | транзакции: продление, восстановление, возврат | T4, T5, T6 |
| `render.py` | тексты экрана тарифов, подтверждений | T7 |
| `keyboards.py` | кнопка меню, inline-клавиатуры тарифов | T8 |
| `handlers/payment.py` | **новый** — экран, счёт, pre-checkout, зачисление | T9, T10, T11 |
| `handlers/subscription.py` | `/extend` со знаком, `/refund` | T12 |
| `notifications.py`, `handlers/common.py` | кнопка оплаты в письмах и меню | T13 |
| `app.py`, `README.md`, `.env.example` | подключение и документация | T13 |

---

### Задача 1: Дни со знаком в чистом модуле

**Файлы:**
- Изменить: `subscription.py`
- Тест: `tests/test_subscription.py`

**Интерфейсы:**
- Потребляет: ничего
- Производит: `subscription.apply_days(paid_until: datetime | None, now: datetime,
  days: int) -> datetime`. `subscription.extend(...)` сохраняется как есть.

- [ ] **Шаг 1: Написать падающие тесты**

Допишите в конец `tests/test_subscription.py`:

```python
# ── Дни со знаком ────────────────────────────────────────────────────────────


def test_positive_days_do_not_burn_the_paid_remainder():
    """Заплатил заранее — дни ложатся на остаток, а не вместо него."""
    assert subscription.apply_days(NOW + timedelta(days=10), NOW, 30) == (
        NOW + timedelta(days=40)
    )


def test_positive_days_after_expiry_count_from_today():
    """Иначе деньги ушли бы в оплату уже прошедшего простоя."""
    assert subscription.apply_days(NOW - timedelta(days=10), NOW, 30) == (
        NOW + timedelta(days=30)
    )


def test_negative_days_are_taken_from_the_bought_term():
    """
    Возврат отбирает купленный срок, а не считает от «сейчас».

    У просроченного сервиса «сейчас» уже позже срока, и вычитание из него
    увело бы дату непредсказуемо далеко в прошлое.
    """
    assert subscription.apply_days(NOW - timedelta(days=10), NOW, -30) == (
        NOW - timedelta(days=40)
    )


def test_negative_days_from_an_active_term():
    assert subscription.apply_days(NOW + timedelta(days=60), NOW, -30) == (
        NOW + timedelta(days=30)
    )


def test_negative_days_without_a_term_do_nothing_strange():
    """Срока не было — отбирать нечего, отсчёт от «сейчас»."""
    assert subscription.apply_days(None, NOW, -30) == NOW - timedelta(days=30)


def test_extend_is_apply_days_with_a_positive_sign():
    """Старое имя обязано остаться синонимом: его вызовы уже проверены."""
    assert subscription.extend(NOW, NOW, 30) == subscription.apply_days(NOW, NOW, 30)
```

- [ ] **Шаг 2: Прогнать, убедиться что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription.py -q`
Ожидается: FAIL, `AttributeError: module 'subscription' has no attribute 'apply_days'`

- [ ] **Шаг 3: Реализовать**

В `subscription.py` замените функцию `extend` на пару:

```python
def apply_days(paid_until: datetime | None, now: datetime, days: int) -> datetime:
    """
    Новый срок после начисления или изъятия days дней.

    Положительные дни считаются от большего из «сейчас» и текущего срока:
    заплатил заранее — дни прибавляются к остатку и не сгорают; заплатил после
    просрочки — отсчёт с сегодня, иначе деньги уходят в оплату простоя.

    Отрицательные вычитаются из самого срока, а не из «сейчас». У просроченного
    сервиса «сейчас» уже позже срока, и вычитание из него увело бы дату дальше в
    прошлое, чем отбирали. Возврат обязан отобрать ровно то, что выдал.
    """
    if days < 0:
        return (paid_until or now) + timedelta(days=days)
    base = max(now, paid_until) if paid_until else now
    return base + timedelta(days=days)


def extend(paid_until: datetime | None, now: datetime, days: int) -> datetime:
    """Начисление дней. Синоним apply_days: вызовы с ним уже проверены."""
    return apply_days(paid_until, now, days)
```

- [ ] **Шаг 4: Прогнать, убедиться что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription.py -q`
Ожидается: PASS

- [ ] **Шаг 5: Коммит**

```bash
git add subscription.py tests/test_subscription.py
git commit -m "Срок подписки умеет отбирать дни, а не только начислять"
```

---

### Задача 2: Тарифы в настройках

**Файлы:**
- Изменить: `config.py`, `.env.example`
- Тест: `tests/test_config.py`

**Интерфейсы:**
- Потребляет: ничего
- Производит: `config.StarPlan` (NamedTuple с полями `days: int`, `stars: int`,
  `label: str`), `config.STAR_PLANS: tuple[StarPlan, ...]`,
  `config.plan_by_days(days: int) -> StarPlan | None`

- [ ] **Шаг 1: Написать падающие тесты**

Допишите в конец `tests/test_config.py`:

```python
# ── Тарифы подписки ──────────────────────────────────────────────────────────


def test_three_plans_are_configured():
    assert [p.days for p in config.STAR_PLANS] == [30, 90, 365]


def test_a_year_is_a_year():
    """365, а не 360: через год управляющий пересчитает и будет прав."""
    assert config.STAR_PLANS[-1].days == 365


def test_prices_are_whole_stars():
    """Звёзды не дробятся: дробная цена — это счёт, который Telegram не примет."""
    for plan in config.STAR_PLANS:
        assert isinstance(plan.stars, int) and plan.stars > 0


def test_every_plan_has_a_human_label():
    """«365 дней» на кнопке читается хуже, чем «12 месяцев»."""
    for plan in config.STAR_PLANS:
        assert plan.label.strip()


def test_plan_is_found_by_days():
    assert config.plan_by_days(90).stars == config.STAR_PLANS[1].stars


def test_unknown_plan_is_not_invented():
    """Счёт на неизвестный тариф выставлять нельзя — цену взять неоткуда."""
    assert config.plan_by_days(7) is None
    assert config.plan_by_days(0) is None
    assert config.plan_by_days(-30) is None
```

- [ ] **Шаг 2: Прогнать, убедиться что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_config.py -q`
Ожидается: FAIL, `AttributeError: module 'config' has no attribute 'STAR_PLANS'`

- [ ] **Шаг 3: Реализовать**

В `config.py` сразу после блока `TRIAL_DAYS` (рядом с остальными настройками
подписки) добавьте:

```python
class StarPlan(NamedTuple):
    """Тариф подписки: сколько дней, сколько звёзд и как назвать на кнопке."""
    days: int
    stars: int
    label: str


# Подпись лежит рядом с числами, а не собирается из дней: «12 месяцев» читается
# лучше, чем «365 дней», а делить дни на тридцать ради подписи — врать в мелочах.
# Цены целые: звёзды не дробятся. Добавить четвёртый тариф — дописать строку
STAR_PLANS: tuple[StarPlan, ...] = (
    StarPlan(30, int(os.getenv("STARS_PRICE_1M") or 150), "1 месяц"),
    StarPlan(90, int(os.getenv("STARS_PRICE_3M") or 400), "3 месяца"),
    StarPlan(365, int(os.getenv("STARS_PRICE_12M") or 1350), "12 месяцев"),
)


def plan_by_days(days: int) -> StarPlan | None:
    """Тариф по числу дней. None — такого тарифа нет, цену взять неоткуда."""
    return next((plan for plan in STAR_PLANS if plan.days == days), None)
```

Добавьте `NamedTuple` в импорт типов вверху файла: `from typing import NamedTuple`.

В `.env.example`, в блок `── Подписка ──`, допишите:

```
STARS_PRICE_1M=150               # цена месяца в звёздах Telegram
STARS_PRICE_3M=400               # три месяца
STARS_PRICE_12M=1350             # год
```

- [ ] **Шаг 4: Прогнать, убедиться что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_config.py -q`
Ожидается: PASS

- [ ] **Шаг 5: Коммит**

```bash
git add config.py .env.example tests/test_config.py
git commit -m "Три тарифа подписки в настройках"
```

---

### Задача 3: Журнал помнит возвраты

**Файлы:**
- Изменить: `schema.sql`
- Тест: `tests/test_subscription_db.py`

**Интерфейсы:**
- Потребляет: ничего
- Производит: колонка `subscription_payments.refunded_at timestamptz`,
  констрейнт `chk_subscription_payments_days` теперь `CHECK (days <> 0)`

**Внимание:** миграцию к общей базе применяет контролёр. Исполнитель правит
`schema.sql` и пишет тесты; если тесты падают из-за неприменённой миграции —
напишите об этом в отчёте и остановитесь, не трогая базу сами.

- [ ] **Шаг 1: Написать падающие тесты**

Допишите в `tests/test_subscription_db.py` после блока продлений:

```python
async def test_journal_remembers_a_refund(service):
    """Возврат помечает ту строку, которую отменяет, а не заводит новую."""
    await db.extend_subscription(service, days=30)
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT idpayment, refunded_at FROM subscription_payments"
            " WHERE idservice=$1 AND source='manual'",
            service,
        )
        assert row["refunded_at"] is None, "свежий платёж возвращённым не бывает"
        await conn.execute(
            "UPDATE subscription_payments SET refunded_at=now() WHERE idpayment=$1",
            row["idpayment"],
        )
        stamped = await conn.fetchval(
            "SELECT refunded_at FROM subscription_payments WHERE idpayment=$1",
            row["idpayment"],
        )
    assert stamped is not None


async def test_journal_accepts_taken_away_days(service):
    """
    Ручное укорачивание — самостоятельное событие, и в журнале оно видно.

    Констрейнт до этой задачи требовал days > 0 и такую строку не пропускал.
    """
    svc = await db.get_service(service)
    async with db.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO subscription_payments (idservice, days, paid_until, source)"
            " VALUES ($1,-30,$2,'manual')",
            service, svc["paid_until"],
        )
        taken = await conn.fetchval(
            "SELECT days FROM subscription_payments"
            " WHERE idservice=$1 AND days < 0",
            service,
        )
    assert taken == -30


async def test_journal_still_rejects_zero_days(service):
    """Начисление на ноль дней ничего не значит — это опечатка, а не операция."""
    svc = await db.get_service(service)
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                "INSERT INTO subscription_payments (idservice, days, paid_until, source)"
                " VALUES ($1,0,$2,'manual')",
                service, svc["paid_until"],
            )
```

- [ ] **Шаг 2: Прогнать, убедиться что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -q -k "journal"`
Ожидается: FAIL — колонки `refunded_at` нет, отрицательные дни отвергает констрейнт.

- [ ] **Шаг 3: Поправить схему**

В `schema.sql`, в блоке «Подписка сервиса», замените констрейнт дней и добавьте
колонку:

```sql
ALTER TABLE subscription_payments
    ADD COLUMN IF NOT EXISTS refunded_at timestamptz;

-- Журнал помнит и отобранные дни: возврат звёзд и ручное укорачивание. Ноль
-- по-прежнему отвергается — начисление на ноль дней это опечатка, а не операция
ALTER TABLE subscription_payments DROP CONSTRAINT IF EXISTS chk_subscription_payments_days;
ALTER TABLE subscription_payments ADD  CONSTRAINT chk_subscription_payments_days
    CHECK (days <> 0);
```

- [ ] **Шаг 4: Сообщить контролёру и дождаться миграции**

Напишите в отчёте, что схема готова к применению. Контролёр накатит её на базу.
После этого прогоните тесты снова.

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -q -k "journal"`
Ожидается: PASS

- [ ] **Шаг 5: Коммит**

```bash
git add schema.sql tests/test_subscription_db.py
git commit -m "Журнал подписки помнит возвраты и отобранные дни"
```

---

### Задача 4: Продление не начисляет один платёж дважды

**Файлы:**
- Изменить: `database.py` (метод `extend_subscription`)
- Тест: `tests/test_subscription_db.py`

**Интерфейсы:**
- Потребляет: `subscription.apply_days` (T1), колонки из T3
- Производит: `db.extend_subscription(idservice, *, days, source='manual',
  external_id=None, granted_by=None) -> datetime | None` — сигнатура прежняя,
  поведение новое: отрицательные дни разрешены, повторный `external_id`
  не начисляет ничего и возвращает текущий срок.

- [ ] **Шаг 1: Написать падающие тесты**

```python
async def test_the_same_charge_is_credited_once(service):
    """
    Главное свойство приёма денег: Telegram доставляет апдейт повторно.

    Без занятия права до начисления второй раз либо начислит дни заново, либо
    уронит транзакцию нарушением уникальности — и то и другое видно клиенту.
    """
    first = await db.extend_subscription(
        service, days=30, source="stars", external_id="charge_1", granted_by=1
    )
    second = await db.extend_subscription(
        service, days=30, source="stars", external_id="charge_1", granted_by=1
    )
    assert second == first, "повтор обязан вернуть тот же срок, а не новый"

    async with db.pool.acquire() as conn:
        rows = await conn.fetchval(
            "SELECT count(*) FROM subscription_payments"
            " WHERE idservice=$1 AND source='stars'",
            service,
        )
    assert rows == 1, "в журнале должен остаться один платёж"


async def test_different_charges_are_credited_separately(service):
    first = await db.extend_subscription(
        service, days=30, source="stars", external_id="charge_a", granted_by=1
    )
    second = await db.extend_subscription(
        service, days=30, source="stars", external_id="charge_b", granted_by=1
    )
    assert second == first + timedelta(days=30)


async def test_negative_days_shorten_the_term(service):
    before = (await db.get_service(service))["paid_until"]
    after = await db.extend_subscription(service, days=-10)
    assert after == before - timedelta(days=10)


async def test_shortening_is_written_to_the_journal(service):
    await db.extend_subscription(service, days=-10)
    async with db.pool.acquire() as conn:
        days = await conn.fetchval(
            "SELECT days FROM subscription_payments"
            " WHERE idservice=$1 AND days < 0",
            service,
        )
    assert days == -10
```

- [ ] **Шаг 2: Прогнать, убедиться что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -q -k "charge or negative_days or shortening"`
Ожидается: FAIL — повторный `external_id` даёт `UniqueViolationError`.

- [ ] **Шаг 3: Реализовать**

В `database.py` замените тело `extend_subscription` (сохранив сигнатуру):

```python
        now = datetime.now(UTC)
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT paid_until FROM services"
                " WHERE idservice=$1 AND idrecstatus=0 FOR UPDATE",
                idservice,
            )
            if row is None:
                return None

            paid_until = subscription.apply_days(row["paid_until"], now, days)

            # Право на начисление занимается до самого начисления — тем же
            # приёмом, что и право на письмо в claim_reminder. Telegram
            # доставляет успешный платёж повторно, и второй раз начислять
            # нечего: деньги те же
            claimed = await conn.fetchrow(
                """
                INSERT INTO subscription_payments
                    (idservice, days, paid_until, source, external_id, granted_by)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT DO NOTHING
                RETURNING idpayment
                """,
                idservice, days, paid_until, source, external_id, granted_by,
            )
            if claimed is None:
                # Этот платёж уже зачтён. Возвращаем срок, который есть, —
                # плательщик увидит ту же дату и не заметит, что повторилось
                return row["paid_until"]

            await conn.execute(
                "UPDATE services SET paid_until=$2 WHERE idservice=$1",
                idservice, paid_until,
            )
        return paid_until
```

- [ ] **Шаг 4: Прогнать, убедиться что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -q`
Ожидается: PASS, включая уже существующие тесты продления и гонки.

- [ ] **Шаг 5: Коммит**

```bash
git add database.py tests/test_subscription_db.py
git commit -m "Один платёж начисляется один раз, дни умеют уходить обратно"
```

---

### Задача 5: Оплата воскрешает удалённый сервис

**Файлы:**
- Изменить: `database.py`
- Тест: `tests/test_subscription_db.py`

**Интерфейсы:**
- Потребляет: `db.extend_subscription` (T4)
- Производит: `db.apply_stars_payment(idservice: str, *, days: int,
  charge_id: str, payer_id: int) -> PaymentApplied | None`, где
  `PaymentApplied` — NamedTuple с полями `paid_until: datetime`,
  `restored: bool`. `None` — сервиса нет вовсе (не удалён, а не существует).

- [ ] **Шаг 1: Написать падающие тесты**

```python
async def test_payment_brings_a_deleted_service_back(service):
    """
    Взять деньги и не дать товар — худшее, что бот может сделать.

    Между pre-checkout и списанием сервис могли удалить. Деньги уже у нас.
    """
    await db.close_service(service)
    assert (await db.get_service(service)) is None

    applied = await db.apply_stars_payment(
        service, days=30, charge_id="charge_revive", payer_id=999_000_001
    )
    assert applied.restored is True

    svc = await db.get_service(service)
    assert svc is not None, "сервис обязан вернуться в строй"
    assert svc["paid_until"] == applied.paid_until


async def test_payment_for_a_live_service_restores_nothing(service):
    applied = await db.apply_stars_payment(
        service, days=30, charge_id="charge_live", payer_id=999_000_001
    )
    assert applied.restored is False


async def test_payment_for_a_service_that_never_existed(service):
    assert await db.apply_stars_payment(
        str(uuid.uuid4()), days=30, charge_id="charge_ghost", payer_id=1
    ) is None
```

- [ ] **Шаг 2: Прогнать, убедиться что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -q -k "payment_"`
Ожидается: FAIL, `AttributeError: 'Database' object has no attribute 'apply_stars_payment'`

- [ ] **Шаг 3: Реализовать**

Добавьте в `database.py` рядом с `extend_subscription`:

```python
class PaymentApplied(NamedTuple):
    """Что сделал платёж: до какого числа продлил и поднимал ли сервис."""
    paid_until: datetime
    restored: bool
```

и метод:

```python
    async def apply_stars_payment(
        self, idservice: str, *, days: int, charge_id: str, payer_id: int
    ) -> PaymentApplied | None:
        """
        Зачесть платёж звёздами. None — сервиса с таким id не существует.

        Удалённый сервис поднимается: деньги уже списаны, и оставить человека
        без товара нельзя. Поднимается сам сервис — отменённые при удалении
        заявки не воскресают (клиентам уже сказали, что сервис закрыт), админы
        не возвращаются (их снял управляющий своим решением).

        Восстановление и начисление идут двумя транзакциями, и иначе нельзя:
        extend_subscription ищет сервис условием idrecstatus=0, то есть
        удалённого не находит — подъём обязан закоммититься раньше. Щель между
        ними самозалечивается: Telegram доставит платёж повторно, подъём станет
        пустой операцией, а начисление отработает. Ради этого и заведена
        идемпотентность по external_id.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT idrecstatus FROM services WHERE idservice=$1 FOR UPDATE",
                idservice,
            )
            if row is None:
                return None
            restored = row["idrecstatus"] != 0
            if restored:
                await conn.execute(
                    "UPDATE services SET idrecstatus=0, deletedate=NULL"
                    " WHERE idservice=$1",
                    idservice,
                )

        paid_until = await self.extend_subscription(
            idservice,
            days=days,
            source="stars",
            external_id=charge_id,
            granted_by=payer_id,
        )
        if paid_until is None:
            return None
        return PaymentApplied(paid_until=paid_until, restored=restored)
```

Добавьте `NamedTuple` в импорты `database.py`: `from typing import NamedTuple`
(если модуль `typing` там ещё не импортирован — добавьте строку рядом с
остальными импортами стандартной библиотеки).

- [ ] **Шаг 4: Прогнать, убедиться что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -q`
Ожидается: PASS

- [ ] **Шаг 5: Коммит**

```bash
git add database.py tests/test_subscription_db.py
git commit -m "Оплата поднимает удалённый сервис вместо потери денег"
```

---

### Задача 6: Возврат звёзд отбирает выданные дни

**Файлы:**
- Изменить: `database.py`
- Тест: `tests/test_subscription_db.py`

**Интерфейсы:**
- Потребляет: T3, T4
- Производит: `db.get_stars_payment(charge_id: str) -> asyncpg.Record | None`
  (поля `idpayment`, `idservice`, `days`, `granted_by`, `refunded_at`),
  `db.revoke_payment(idpayment: str) -> datetime | None` — новый срок сервиса,
  `None` если платёж уже был возвращён.

- [ ] **Шаг 1: Написать падающие тесты**

```python
async def test_refund_takes_back_exactly_what_it_gave(service):
    before = (await db.get_service(service))["paid_until"]
    await db.extend_subscription(
        service, days=30, source="stars", external_id="charge_r1", granted_by=42
    )
    payment = await db.get_stars_payment("charge_r1")
    assert payment["granted_by"] == 42, "без плательщика возврат некому сделать"

    after = await db.revoke_payment(str(payment["idpayment"]))
    assert after == before, "срок обязан вернуться ровно туда, где был"
    assert (await db.get_service(service))["paid_until"] == after


async def test_a_refunded_payment_is_not_refunded_twice(service):
    await db.extend_subscription(
        service, days=30, source="stars", external_id="charge_r2", granted_by=42
    )
    payment = await db.get_stars_payment("charge_r2")
    assert await db.revoke_payment(str(payment["idpayment"])) is not None
    assert await db.revoke_payment(str(payment["idpayment"])) is None


async def test_refund_can_push_the_term_into_the_past(service):
    """
    Не заплатил — не пользуешься.

    Если после спорного платежа были другие продления, вычитание может сделать
    сервис просроченным прямо сейчас. Смягчать это значило бы дарить месяц
    каждому, кто попросит возврат.
    """
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE services SET paid_until=now() + interval '5 days'"
            " WHERE idservice=$1",
            service,
        )
    await db.extend_subscription(
        service, days=30, source="stars", external_id="charge_r3", granted_by=42
    )
    payment = await db.get_stars_payment("charge_r3")
    after = await db.revoke_payment(str(payment["idpayment"]))
    assert after < datetime.now(timezone.utc) + timedelta(days=6)


async def test_unknown_charge_is_not_found(service):
    assert await db.get_stars_payment("charge_that_never_was") is None
```

- [ ] **Шаг 2: Прогнать, убедиться что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -q -k "refund or unknown_charge"`
Ожидается: FAIL, методов нет.

- [ ] **Шаг 3: Реализовать**

```python
    async def get_stars_payment(self, charge_id: str) -> asyncpg.Record | None:
        """Платёж звёздами по идентификатору списания. None — такого нет."""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM subscription_payments"
                " WHERE source='stars' AND external_id=$1",
                charge_id,
            )

    async def revoke_payment(self, idpayment: str) -> datetime | None:
        """
        Отобрать выданные платежом дни и пометить его возвращённым.

        None — платёж уже был возвращён. Отметка ставится условием в самом
        UPDATE, а не проверкой перед ним: два одновременных возврата иначе
        вычли бы дни дважды.

        Срок может уйти в прошлое, и это правильно: не заплатил — не
        пользуешься.
        """
        now = datetime.now(UTC)
        async with self.pool.acquire() as conn, conn.transaction():
            payment = await conn.fetchrow(
                "UPDATE subscription_payments SET refunded_at=$2"
                " WHERE idpayment=$1 AND refunded_at IS NULL"
                " RETURNING idservice, days",
                idpayment, now,
            )
            if payment is None:
                return None
            row = await conn.fetchrow(
                "SELECT paid_until FROM services WHERE idservice=$1 FOR UPDATE",
                payment["idservice"],
            )
            if row is None:
                return None
            paid_until = subscription.apply_days(
                row["paid_until"], now, -payment["days"]
            )
            await conn.execute(
                "UPDATE services SET paid_until=$2 WHERE idservice=$1",
                payment["idservice"], paid_until,
            )
        return paid_until
```

- [ ] **Шаг 4: Прогнать, убедиться что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -q`
Ожидается: PASS

- [ ] **Шаг 5: Коммит**

```bash
git add database.py tests/test_subscription_db.py
git commit -m "Возврат отбирает ровно те дни, которые выдал"
```

---

### Задача 7: Тексты оплаты

**Файлы:**
- Изменить: `render.py`
- Тест: `tests/test_render.py`

**Интерфейсы:**
- Потребляет: `config.STAR_PLANS` (T2)
- Производит: `render.tariff_screen(svc) -> str`,
  `render.payment_done(svc, *, days: int, restored: bool) -> str`,
  `render.refund_done(svc, paid_until) -> str`,
  `render.invoice_title(svc) -> str`, `render.invoice_description(plan) -> str`

- [ ] **Шаг 1: Написать падающие тесты**

```python
def test_tariff_screen_names_the_current_term():
    """Человек должен видеть, что продлевает, а не покупать вслепую."""
    text = render.tariff_screen(_svc(datetime.now(timezone.utc) + timedelta(days=3)))
    assert "оплачена до" in text


def test_tariff_screen_does_not_promise_consequences_that_are_switched_off(monkeypatch):
    """
    «Оплачена до», а не «действует до»: при выключенной подписке is_active
    истинен всегда, и просроченный сервис получил бы дату в прошлом со словом
    «действует». «Оплачена до <прошлое>» — правда в обоих мирах.
    """
    monkeypatch.setattr(render.config, "SUBSCRIPTION_ENFORCED", False)
    text = render.tariff_screen(_svc(datetime.now(timezone.utc) - timedelta(days=10)))
    assert "оплачена до" in text
    assert "скрыт из поиска" not in text, "отключения нет — обещать его нельзя"


def test_tariff_screen_of_an_expired_service_says_so():
    text = render.tariff_screen(_svc(datetime.now(timezone.utc) - timedelta(days=1)))
    assert "истекла" in text


def test_tariff_screen_has_no_totals():
    """Пустое место лучше подписей вроде «Итого» и «выгода 25%»."""
    text = render.tariff_screen(_svc(datetime.now(timezone.utc) + timedelta(days=3)))
    assert "Итого" not in text and "выгода" not in text


def test_payment_confirmation_names_the_new_date():
    svc = _svc(datetime.now(timezone.utc) + timedelta(days=33))
    text = render.payment_done(svc, days=30, restored=False)
    assert render.local_dt(svc["paid_until"], svc["timezone"]) in text


def test_restored_service_is_told_about_out_loud():
    """
    Молчаливое воскрешение выглядит как отказ бота выполнять удаление.

    И про то, что вернулось не всё, человек должен узнать от бота, а не через
    неделю обнаружить сам.
    """
    svc = _svc(datetime.now(timezone.utc) + timedelta(days=30))
    text = render.payment_done(svc, days=30, restored=True)
    assert "восстановлен" in text
    assert "заявки" in text.lower() and "админ" in text.lower()


def test_ordinary_payment_says_nothing_about_restoring():
    svc = _svc(datetime.now(timezone.utc) + timedelta(days=30))
    assert "восстановлен" not in render.payment_done(svc, days=30, restored=False)


def test_service_name_is_escaped_in_the_invoice_title():
    """Правило проекта: всё пользовательское экранируется на выходе."""
    svc = _svc(datetime.now(timezone.utc)) | {"service_name": "Аста & <Сервис>"}
    assert "&amp;" in render.invoice_title(svc) or "&" not in render.invoice_title(svc)
```

- [ ] **Шаг 2: Прогнать, убедиться что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_render.py -q`
Ожидается: FAIL, функций нет.

- [ ] **Шаг 3: Реализовать**

В `render.py`, в блок «Подписка», добавьте:

```python
def tariff_screen(svc) -> str:
    """Шапка экрана тарифов. Кнопки с ценами рисует keyboards.kb_tariffs."""
    now = datetime.now(timezone.utc)
    paid_until = svc["paid_until"]
    # «Оплачена до», а не «действует до»: при выключенной подписке is_active
    # истинен всегда, и просроченный сервис получил бы дату в прошлом со словом
    # «действует». «Оплачена до» — правда и когда срок идёт, и когда он прошёл,
    # но отключение ещё не введено в действие
    if subscription.is_active(paid_until, now):
        head = f"💳 Подписка оплачена до {local_dt(paid_until, svc['timezone'])}."
    else:
        head = (
            "⛔️ <b>Подписка истекла.</b> Сервис скрыт из поиска, "
            "новые записи не принимаются."
        )
    return f"{head}\n\nВыберите срок продления:"


def invoice_title(svc) -> str:
    """Заголовок счёта. Telegram показывает его без разметки — но чужого HTML
    в нём быть не должно всё равно: заголовок попадает и в наши сообщения."""
    return f"Подписка «{h(svc['service_name'])}»"


def invoice_description(plan) -> str:
    return f"Продление подписки сервиса на {plan.label}."


def payment_done(svc, *, days: int, restored: bool) -> str:
    """Подтверждение оплаты управляющему."""
    lines = [
        "✅ <b>Оплата прошла.</b>",
        f"Подписка действует до {local_dt(svc['paid_until'], svc['timezone'])}.",
    ]
    if restored:
        lines.append(
            "\n🔄 <b>Сервис восстановлен</b> — он снова в поиске и принимает "
            "записи.\nЗаявки, отменённые при удалении, и администраторы не "
            "вернулись: клиентам уже ушло уведомление о закрытии, а админов "
            "нужно пригласить заново."
        )
    return "\n".join(lines)


def refund_done(svc, paid_until) -> str:
    """Уведомление плательщику. Молча отбирать оплаченное нельзя."""
    return (
        "↩️ <b>Звёзды возвращены.</b>\n"
        f"Подписка сервиса «{h(svc['service_name'])}» действует "
        f"до {local_dt(paid_until, svc['timezone'])}."
    )
```

- [ ] **Шаг 4: Прогнать, убедиться что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_render.py -q`
Ожидается: PASS

- [ ] **Шаг 5: Коммит**

```bash
git add render.py tests/test_render.py
git commit -m "Тексты экрана тарифов, оплаты и возврата"
```

---

### Задача 8: Кнопки

**Файлы:**
- Изменить: `keyboards.py`
- Тест: `tests/test_keyboards.py`

**Интерфейсы:**
- Потребляет: `config.STAR_PLANS` (T2)
- Производит: `kb.BTN_SUBSCRIPTION`, `kb.kb_tariffs() -> InlineKeyboardMarkup`,
  `kb.kb_pay() -> InlineKeyboardMarkup`. Callback-данные: `subscr:open` —
  открыть экран тарифов, `subscr:buy:<дней>` — выставить счёт.

- [ ] **Шаг 1: Написать падающие тесты**

```python
def test_owner_menu_has_a_subscription_button():
    kb_markup = kb.kb_owner_main("11111111-1111-1111-1111-111111111111",
                                 many_services=False)
    texts = [b.text for row in kb_markup.keyboard for b in row]
    assert kb.BTN_SUBSCRIPTION in texts


def test_admin_menu_has_no_subscription_button():
    """Подписка сервиса — не дело администратора, как и остальные хозяйские экраны."""
    kb_markup = kb.kb_admin_main("11111111-1111-1111-1111-111111111111",
                                 many_services=False)
    texts = [b.text for row in kb_markup.keyboard for b in row]
    assert kb.BTN_SUBSCRIPTION not in texts


def test_every_plan_gets_a_button():
    rows = kb.kb_tariffs().inline_keyboard
    assert len([b for row in rows for b in row]) == len(config.STAR_PLANS)


def test_tariff_button_carries_days_not_price():
    """
    Цену берём из конфига по дням, а не из callback_data.

    Иначе устаревшая кнопка из прошлогоднего письма выставила бы счёт по
    прошлогодней цене.
    """
    first = kb.kb_tariffs().inline_keyboard[0][0]
    assert first.callback_data == f"subscr:buy:{config.STAR_PLANS[0].days}"


def test_tariff_button_shows_the_price():
    first = kb.kb_tariffs().inline_keyboard[0][0]
    assert str(config.STAR_PLANS[0].stars) in first.text
    assert config.STAR_PLANS[0].label in first.text


def test_pay_button_opens_the_tariff_screen():
    button = kb.kb_pay().inline_keyboard[0][0]
    assert button.callback_data == "subscr:open"
```

- [ ] **Шаг 2: Прогнать, убедиться что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_keyboards.py -q`
Ожидается: FAIL

- [ ] **Шаг 3: Реализовать**

В `keyboards.py` рядом с остальными константами кнопок:

```python
BTN_SUBSCRIPTION = "💳 Подписка"
```

В `kb_owner_main` добавьте кнопку в ряд с расписанием и услугами — она
хозяйская, и место ей среди хозяйских:

```python
        [KeyboardButton(text=BTN_SERVICES), KeyboardButton(text=BTN_SCHEDULE)],
        [KeyboardButton(text=BTN_SUBSCRIPTION)],
        [KeyboardButton(text=BTN_BOOK_OWN)],
```

`kb_admin_main` не трогайте.

В блок inline-клавиатур:

```python
def kb_tariffs() -> InlineKeyboardMarkup:
    """
    Кнопки тарифов. В callback_data едут дни, а не цена: цену берём из конфига
    в момент нажатия, иначе кнопка из прошлогоднего письма выставит счёт по
    прошлогодней цене.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{plan.label} — {plan.stars} ⭐",
            callback_data=f"subscr:buy:{plan.days}",
        )]
        for plan in config.STAR_PLANS
    ])


def kb_pay() -> InlineKeyboardMarkup:
    """Кнопка под письмом о подписке. Ведёт на тот же экран, что и меню."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Продлить подписку", callback_data="subscr:open")]
    ])
```

Убедитесь, что `keyboards.py` импортирует `config` — если нет, добавьте.

- [ ] **Шаг 4: Прогнать, убедиться что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_keyboards.py -q`
Ожидается: PASS

- [ ] **Шаг 5: Коммит**

```bash
git add keyboards.py tests/test_keyboards.py
git commit -m "Кнопка подписки в меню и клавиатура тарифов"
```

---

### Задача 9: Экран тарифов и счёт

**Файлы:**
- Создать: `handlers/payment.py`
- Тест: `tests/test_payment_handlers.py` (создать)

**Интерфейсы:**
- Потребляет: T2, T7, T8, `handlers.common.require_active_service`
- Производит: `payment.router`, `payment.PAYLOAD_PREFIX = "sub"`,
  `payment.make_payload(idservice: str, days: int) -> str`,
  `payment.parse_payload(raw: str) -> tuple[str, int] | None`,
  хендлеры `payment.subscription_screen`, `payment.open_screen`,
  `payment.buy_plan`

- [ ] **Шаг 1: Написать падающие тесты**

Создайте `tests/test_payment_handlers.py`:

```python
"""Оплата звёздами. Базы не требует — слой БД и Telegram подменены."""

import pytest

import config
import handlers.payment as payment

pytestmark = pytest.mark.asyncio

SERVICE_ID = "11111111-1111-1111-1111-111111111111"


def test_payload_round_trip():
    assert payment.parse_payload(payment.make_payload(SERVICE_ID, 30)) == (
        SERVICE_ID, 30
    )


def test_broken_payload_is_not_guessed():
    """Мусор в payload — это не повод угадывать, за что человек заплатил."""
    for raw in ("", "sub", "sub:x", f"sub:{SERVICE_ID}", f"sub:{SERVICE_ID}:x",
                f"sub:{SERVICE_ID}:30:extra", f"other:{SERVICE_ID}:30"):
        assert payment.parse_payload(raw) is None, raw


def test_non_ascii_digits_do_not_crash_the_parser():
    """isdigit() истинно для «²», int() его не парсит — уже ловили в /extend."""
    assert payment.parse_payload(f"sub:{SERVICE_ID}:²") is None


class FakeScreenMessage:
    def __init__(self):
        self.answers = []
        self.markups = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)
        self.markups.append(kwargs.get("reply_markup"))


async def test_expired_owner_can_still_reach_the_tariffs(monkeypatch):
    """
    Гейты подписки этот экран не закрывают.

    Иначе просрочка становится ловушкой: заплатить можно только изнутри,
    а внутрь не пускают, пока не заплатишь.
    """
    from datetime import datetime, timedelta, timezone

    async def _expired(_message, _state, user_id=None):
        return {
            "idservice": SERVICE_ID,
            "service_name": "Тест",
            "timezone": "Europe/Moscow",
            "paid_until": datetime.now(timezone.utc) - timedelta(days=10),
        }

    monkeypatch.setattr(payment, "require_active_service", _expired)
    message = FakeScreenMessage()
    await payment.subscription_screen(message, None)

    assert message.answers, "просроченному управляющему экран обязан открыться"
    assert message.markups[0] is not None, "тарифы без кнопок бесполезны"
```

- [ ] **Шаг 2: Прогнать, убедиться что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_payment_handlers.py -q`
Ожидается: FAIL, модуля нет.

- [ ] **Шаг 3: Реализовать**

Создайте `handlers/payment.py`:

```python
"""
handlers/payment.py — оплата подписки звёздами Telegram.

Экран тарифов открывается двумя путями (кнопкой меню и кнопкой из письма) и
ведёт в одно место. Счёт формируется в момент нажатия, поэтому кнопка из
старого письма не устаревает.

Гейты подписки этот экран не закрывают: просроченный управляющий обязан иметь
возможность заплатить, иначе просрочка становится ловушкой без выхода.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import default_state
from aiogram.types import CallbackQuery, LabeledPrice, Message

import config
import keyboards as kb
import render
from database import db
from handlers.common import require_active_service

logger = logging.getLogger(__name__)
router = Router()

PAYLOAD_PREFIX = "sub"


def make_payload(idservice: str, days: int) -> str:
    """Что именно оплачено. Telegram вернёт эту строку в неизменном виде."""
    return f"{PAYLOAD_PREFIX}:{idservice}:{days}"


def parse_payload(raw: str) -> tuple[str, int] | None:
    """
    Разобрать payload счёта. None — строка не наша или испорчена.

    isdecimal, а не isdigit: isdigit истинно для не-ASCII цифр вроде «²»,
    которые int() не парсит, и хендлер падал бы необработанным исключением.
    """
    parts = (raw or "").split(":")
    if len(parts) != 3 or parts[0] != PAYLOAD_PREFIX:
        return None
    idservice, days = parts[1], parts[2]
    if not idservice or not days.isdecimal():
        return None
    return idservice, int(days)


async def _show_tariffs(message: Message, svc) -> None:
    await message.answer(render.tariff_screen(svc), reply_markup=kb.kb_tariffs())


@router.message(F.text == kb.BTN_SUBSCRIPTION, StateFilter(default_state))
async def subscription_screen(message: Message, state: FSMContext) -> None:
    svc = await require_active_service(message, state)
    if svc is None:
        return
    await _show_tariffs(message, svc)


@router.callback_query(F.data == "subscr:open")
async def open_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    # user_id обязателен: у callback.message автор — бот, а не человек, и без
    # этого аргумента сервис искался бы по id бота и не находился никогда
    svc = await require_active_service(
        callback.message, state, user_id=callback.from_user.id
    )
    if svc is None:
        return
    await _show_tariffs(callback.message, svc)


@router.callback_query(F.data.startswith("subscr:buy:"))
async def buy_plan(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    days = callback.data.rsplit(":", 1)[-1]
    plan = config.plan_by_days(int(days)) if days.isdecimal() else None
    if plan is None:
        # Тариф убрали из конфига, пока письмо лежало в чате
        await callback.message.answer("Этот тариф больше не действует.")
        return

    svc = await require_active_service(
        callback.message, state, user_id=callback.from_user.id
    )
    if svc is None:
        return

    await callback.message.answer_invoice(
        title=render.invoice_title(svc),
        description=render.invoice_description(plan),
        payload=make_payload(str(svc["idservice"]), plan.days),
        currency="XTR",
        prices=[LabeledPrice(label=plan.label, amount=plan.stars)],
    )
```

- [ ] **Шаг 4: Прогнать, убедиться что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_payment_handlers.py -q`
Ожидается: PASS

- [ ] **Шаг 5: Коммит**

```bash
git add handlers/payment.py tests/test_payment_handlers.py
git commit -m "Экран тарифов и счёт в звёздах"
```

---

### Задача 10: Проверка перед списанием

**Файлы:**
- Изменить: `handlers/payment.py`
- Тест: `tests/test_payment_handlers.py`

**Интерфейсы:**
- Потребляет: `payment.parse_payload` (T9), `config.plan_by_days` (T2)
- Производит: хендлер `payment.pre_checkout`

- [ ] **Шаг 1: Написать падающие тесты**

```python
class FakePreCheckout:
    def __init__(self, payload: str, total_amount: int = 150):
        self.invoice_payload = payload
        self.total_amount = total_amount
        self.answers = []

    async def answer(self, ok: bool, error_message: str | None = None):
        self.answers.append((ok, error_message))


@pytest.fixture
def live_service(monkeypatch):
    async def _service(_id):
        return {"idservice": SERVICE_ID, "service_name": "Тест"}

    monkeypatch.setattr(payment.db, "get_service", _service)


async def test_good_invoice_is_let_through(live_service):
    query = FakePreCheckout(payment.make_payload(SERVICE_ID, 30))
    await payment.pre_checkout(query)
    assert query.answers == [(True, None)]


async def test_broken_payload_is_refused_with_words(live_service):
    query = FakePreCheckout("мусор")
    await payment.pre_checkout(query)
    ok, message = query.answers[0]
    assert ok is False
    assert message, "отказ без текста человек не поймёт"


async def test_unknown_plan_is_refused(live_service):
    query = FakePreCheckout(payment.make_payload(SERVICE_ID, 7))
    await payment.pre_checkout(query)
    assert query.answers[0][0] is False


async def test_deleted_service_is_refused_before_the_money(monkeypatch):
    """
    Не взять денег лучше, чем взять и чинить последствия.

    Восстановление сервиса закрывает только щель между этой проверкой и
    списанием, а не заменяет её.
    """
    async def _gone(_id):
        return None

    monkeypatch.setattr(payment.db, "get_service", _gone)
    query = FakePreCheckout(payment.make_payload(SERVICE_ID, 30))
    await payment.pre_checkout(query)
    assert query.answers[0][0] is False
```

- [ ] **Шаг 2: Прогнать, убедиться что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_payment_handlers.py -q`
Ожидается: FAIL, `AttributeError: module has no attribute 'pre_checkout'`

- [ ] **Шаг 3: Реализовать**

Допишите в `handlers/payment.py`:

```python
from aiogram.types import PreCheckoutQuery  # добавьте к остальным импортам types


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    """
    Последняя проверка перед списанием. Telegram ждёт ответа десять секунд.

    Поэтому здесь только разбор payload и одно чтение сервиса — ничего
    тяжёлого. Отказ на этом шаге не стоит человеку ни звезды.
    """
    parsed = parse_payload(query.invoice_payload)
    if parsed is None:
        await query.answer(False, error_message="Счёт испорчен. Откройте оплату заново.")
        return

    idservice, days = parsed
    if config.plan_by_days(days) is None:
        await query.answer(False, error_message="Этот тариф больше не действует.")
        return

    if await db.get_service(idservice) is None:
        await query.answer(
            False, error_message="Сервис недоступен. Оплата отменена."
        )
        return

    await query.answer(True)
```

- [ ] **Шаг 4: Прогнать, убедиться что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_payment_handlers.py -q`
Ожидается: PASS

- [ ] **Шаг 5: Коммит**

```bash
git add handlers/payment.py tests/test_payment_handlers.py
git commit -m "Pre-checkout отказывает до списания, а не после"
```

---

### Задача 11: Зачисление платежа

**Файлы:**
- Изменить: `handlers/payment.py`
- Тест: `tests/test_payment_handlers.py`

**Интерфейсы:**
- Потребляет: `db.apply_stars_payment` (T5), `render.payment_done` (T7)
- Производит: хендлер `payment.paid`

- [ ] **Шаг 1: Написать падающие тесты**

```python
class FakePayment:
    def __init__(self, payload: str, charge_id: str = "ch_1", amount: int = 150):
        self.invoice_payload = payload
        self.telegram_payment_charge_id = charge_id
        self.total_amount = amount
        self.currency = "XTR"


class FakePaidMessage:
    def __init__(self, payload: str, user_id: int = 999_000_001, **kwargs):
        self.successful_payment = FakePayment(payload, **kwargs)
        self.from_user = type("User", (), {"id": user_id})()
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


@pytest.fixture
def applied(monkeypatch):
    calls = []

    async def _apply(idservice, *, days, charge_id, payer_id):
        calls.append((idservice, days, charge_id, payer_id))
        # PaymentApplied — класс уровня модуля database, а не атрибут db:
        # db это экземпляр Database, и через него класс не достать
        return PaymentApplied(
            paid_until=datetime.now(timezone.utc) + timedelta(days=days),
            restored=False,
        )

    async def _service(_id):
        return {
            "idservice": SERVICE_ID,
            "service_name": "Тест",
            "timezone": "Europe/Moscow",
            "paid_until": datetime.now(timezone.utc) + timedelta(days=30),
        }

    monkeypatch.setattr(payment.db, "apply_stars_payment", _apply)
    monkeypatch.setattr(payment.db, "get_service", _service)
    return calls


async def test_payment_credits_the_service(applied):
    message = FakePaidMessage(payment.make_payload(SERVICE_ID, 30))
    await payment.paid(message)
    assert applied == [(SERVICE_ID, 30, "ch_1", 999_000_001)]
    assert message.answers, "человек обязан увидеть подтверждение"


async def test_broken_payload_after_payment_does_not_crash(applied):
    """
    Деньги уже списаны. Падение здесь означало бы платёж без товара и без следа.
    """
    message = FakePaidMessage("мусор")
    await payment.paid(message)
    assert applied == []
    assert message.answers, "молчать после списания нельзя"
```

Добавьте в начало файла импорты:
`from datetime import datetime, timedelta, timezone` и
`from database import PaymentApplied`.

- [ ] **Шаг 2: Прогнать, убедиться что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_payment_handlers.py -q`
Ожидается: FAIL

- [ ] **Шаг 3: Реализовать**

```python
@router.message(F.successful_payment)
async def paid(message: Message) -> None:
    """
    Деньги уже списаны — отсюда нельзя уйти молча ни при какой ошибке.

    Повторную доставку того же платежа отсекает db.extend_subscription: право
    на начисление занимается уникальным индексом по (source, external_id).
    """
    payment_info = message.successful_payment
    parsed = parse_payload(payment_info.invoice_payload)
    if parsed is None:
        logger.error(
            "Платёж %s с неразбираемым payload %r",
            payment_info.telegram_payment_charge_id, payment_info.invoice_payload,
        )
        await message.answer(
            "✅ Оплата прошла, но счёт не удалось распознать. "
            "Напишите нам — разберёмся вручную."
        )
        return

    idservice, days = parsed
    plan = config.plan_by_days(days)
    if plan is not None and plan.stars != payment_info.total_amount:
        # Цену поменяли, пока счёт лежал в чате. Дни начисляем — деньги уже
        # у нас, — но расхождение должно быть видно в логе
        logger.warning(
            "Платёж %s: заплачено %d звёзд, тариф стоит %d",
            payment_info.telegram_payment_charge_id,
            payment_info.total_amount, plan.stars,
        )

    applied = await db.apply_stars_payment(
        idservice,
        days=days,
        charge_id=payment_info.telegram_payment_charge_id,
        payer_id=message.from_user.id,
    )
    if applied is None:
        logger.error(
            "Платёж %s за несуществующий сервис %s",
            payment_info.telegram_payment_charge_id, idservice,
        )
        await message.answer(
            "✅ Оплата прошла, но сервис не найден. "
            "Напишите нам — вернём звёзды."
        )
        return

    svc = await db.get_service(idservice)
    await message.answer(
        render.payment_done(svc, days=days, restored=applied.restored)
    )
```

- [ ] **Шаг 4: Прогнать, убедиться что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_payment_handlers.py -q`
Ожидается: PASS

- [ ] **Шаг 5: Коммит**

```bash
git add handlers/payment.py tests/test_payment_handlers.py
git commit -m "Успешный платёж начисляет дни и не молчит при ошибке"
```

---

### Задача 12: Команды владельца бота

**Файлы:**
- Изменить: `handlers/subscription.py`
- Тест: `tests/test_subscription_handlers.py`

**Интерфейсы:**
- Потребляет: `db.get_stars_payment`, `db.revoke_payment` (T6),
  `render.refund_done` (T7)
- Производит: хендлер `handler.refund_command`, изменённый
  `handler.extend_command` (`MAX_DAYS = 36500`, отрицательные дни)

- [ ] **Шаг 1: Написать падающие тесты**

```python
async def test_owner_can_grant_a_century(extended):
    """Бессрочная подписка — это очень длинный срок, а не отдельное состояние."""
    message = FakeMessage(f"/extend {SERVICE_ID} 36500", OWNER_ID)
    await handler.extend_command(message)
    assert extended == [(SERVICE_ID, 36500, OWNER_ID)]


async def test_owner_can_take_days_back(extended):
    """Необратимая операция без отмены рано или поздно случается."""
    message = FakeMessage(f"/extend {SERVICE_ID} -30", OWNER_ID)
    await handler.extend_command(message)
    assert extended == [(SERVICE_ID, -30, OWNER_ID)]


async def test_zero_is_still_rejected(extended):
    message = FakeMessage(f"/extend {SERVICE_ID} 0", OWNER_ID)
    await handler.extend_command(message)
    assert extended == []


async def test_minus_zero_is_rejected_too(extended):
    message = FakeMessage(f"/extend {SERVICE_ID} -0", OWNER_ID)
    await handler.extend_command(message)
    assert extended == []


async def test_absurd_number_is_rejected(extended):
    message = FakeMessage(f"/extend {SERVICE_ID} 999999", OWNER_ID)
    await handler.extend_command(message)
    assert extended == []


async def test_stranger_cannot_refund():
    """Знать о существовании команды постороннему незачем."""
    message = FakeMessage("/refund ch_1", STRANGER_ID)
    await handler.refund_command(message, None)
    assert message.answers == []
```

- [ ] **Шаг 2: Прогнать, убедиться что падают**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_handlers.py -q`
Ожидается: FAIL

- [ ] **Шаг 3: Реализовать**

В `handlers/subscription.py`:

```python
MAX_DAYS = 36500  # сто лет: так выражается «бессрочная подписка»
```

Замените проверку аргумента (была `parts[2].isdecimal() and 1 <= … <= MAX_DAYS`):

```python
    raw = parts[2]
    negative = raw.startswith("-")
    digits = raw[1:] if negative else raw
    if not digits.isdecimal() or not 1 <= int(digits) <= MAX_DAYS:
        await message.answer(USAGE)
        return
    days = -int(digits) if negative else int(digits)
```

и передайте `days` в `db.extend_subscription`. Обновите `USAGE`:

```python
USAGE = (
    "Продление подписки:\n"
    "<code>/extend &lt;idservice&gt; &lt;дней&gt;</code>\n"
    f"Дней — от 1 до {MAX_DAYS}. Со знаком минус — отобрать дни.\n\n"
    "Возврат звёзд:\n"
    "<code>/refund &lt;id списания&gt;</code>"
)
```

Добавьте команду возврата:

```python
@router.message(Command("refund"))
async def refund_command(message: Message, bot: Bot) -> None:
    """
    Вернуть звёзды и отобрать выданные дни.

    Порядок «сначала деньги, потом срок» выбран сознательно: обратный при
    отказе Telegram оставил бы сервис укороченным без возврата денег — то есть
    отобрал бы и товар, и оплату.
    """
    if message.from_user.id not in config.BOT_OWNER_IDS:
        return

    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer(USAGE)
        return

    payment = await db.get_stars_payment(parts[1])
    if payment is None:
        await message.answer("❌ Платёж не найден.")
        return
    if payment["refunded_at"] is not None:
        await message.answer(
            f"Этот платёж уже возвращён {render.local_dt(payment['refunded_at'])}."
        )
        return

    try:
        await bot.refund_star_payment(
            user_id=payment["granted_by"],
            telegram_payment_charge_id=parts[1],
        )
    except Exception as exc:
        logger.exception("Telegram отказал в возврате %s", parts[1])
        await message.answer(f"❌ Telegram отказал: {h(str(exc))}")
        return

    paid_until = await db.revoke_payment(str(payment["idpayment"]))
    if paid_until is None:
        await message.answer("Звёзды возвращены, но платёж уже был отменён раньше.")
        return

    svc = await db.get_service(str(payment["idservice"]))
    await message.answer(
        f"↩️ Возвращено. Срок сервиса «{h(svc['service_name'])}» — "
        f"{render.local_dt(paid_until, svc['timezone'])}."
    )
    await safe_send(bot, payment["granted_by"], render.refund_done(svc, paid_until))
```

Добавьте недостающие импорты в `handlers/subscription.py`: `from aiogram import Bot`,
`import render`, `from notifications import safe_send`, `import logging` и
`logger = logging.getLogger(__name__)`.

- [ ] **Шаг 4: Прогнать, убедиться что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_handlers.py -q`
Ожидается: PASS

- [ ] **Шаг 5: Коммит**

```bash
git add handlers/subscription.py tests/test_subscription_handlers.py
git commit -m "Владелец бота выдаёт век, отбирает дни и возвращает звёзды"
```

---

### Задача 13: Кнопка оплаты в письмах и подключение

**Файлы:**
- Изменить: `notifications.py`, `handlers/common.py`, `app.py`, `README.md`
- Тест: `tests/test_reminder_loop.py`, `tests/test_render.py`

**Интерфейсы:**
- Потребляет: `kb.kb_pay()` (T8), `payment.router` (T9)
- Производит: подключённый роутер и кнопка под письмами

- [ ] **Шаг 1: Написать падающие тесты**

Допишите в `tests/test_reminder_loop.py`:

```python
async def test_reminder_carries_a_pay_button(monkeypatch):
    """
    Момент, когда человек готов платить, — момент письма.

    Заставить его идти искать кнопку в меню значит потерять часть платежей.
    """
    import keyboards as kb

    sent = []

    async def _fake_send(bot, chat_id, text, **kwargs):
        sent.append(kwargs.get("reply_markup"))
        return True

    async def _one_service():
        from datetime import datetime, timedelta, timezone
        return [{
            "idservice": "11111111-1111-1111-1111-111111111111",
            "service_name": "Тест",
            "owner_id": 1,
            "timezone": "Europe/Moscow",
            "paid_until": datetime.now(timezone.utc) + timedelta(hours=20),
        }]

    async def _claim(*args, **kwargs):
        return True

    monkeypatch.setattr(notifications, "safe_send", _fake_send)
    monkeypatch.setattr(notifications.db, "services_for_reminders", _one_service)
    monkeypatch.setattr(notifications.db, "claim_reminder", _claim)

    await notifications.send_subscription_reminders(None)
    assert sent and sent[0] == kb.kb_pay()
```

- [ ] **Шаг 2: Прогнать, убедиться что падает**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_reminder_loop.py -q`
Ожидается: FAIL — письмо уходит без клавиатуры.

- [ ] **Шаг 3: Реализовать**

В `notifications.py`, в `send_subscription_reminders`, передайте клавиатуру:

```python
        if await safe_send(
            bot,
            svc["owner_id"],
            render.subscription_reminder(stage, svc),
            reply_markup=kb.kb_pay(),
        ):
```

и добавьте импорт `import keyboards as kb`.

В `handlers/common.py` строка о подписке сейчас вклеивается в текст меню
(строки 145-150) и уходит вместе с reply-клавиатурой. Inline-кнопку к
reply-клавиатуре приложить нельзя, поэтому строку нужно вынести в отдельное
сообщение — **после** меню, чтобы порядок в чате остался прежним.

Замените блок:

```python
    # Только к обычному приветствию: подшивать предупреждение о подписке к
    # «✅ Часы работы обновлены» значит показывать его на каждый чих
    if greeting is None and role == "owner":
        note = render.subscription_line(svc)
        if note:
            text = f"{text}\n\n{note}"

    await message.answer(
        text,
        reply_markup=main_menu_markup(svc, role, len(services) > 1),
```

на:

```python
    await message.answer(
        text,
        reply_markup=main_menu_markup(svc, role, len(services) > 1),
```

(остальные аргументы вызова оставьте как есть), а сразу после этого вызова
добавьте:

```python
    # Отдельным сообщением, а не строкой в меню: к reply-клавиатуре inline-кнопку
    # не приложить, а строка о подписке без способа продлить её бесполезна.
    # Только к обычному приветствию: подшивать предупреждение к «✅ Часы работы
    # обновлены» значит показывать его на каждый чих
    if greeting is None and role == "owner":
        note = render.subscription_line(svc)
        if note:
            await message.answer(note, reply_markup=kb.kb_pay())
```

В `app.py` подключите роутер — **до** `subscription_handlers.router`, чтобы
кнопка меню разбиралась раньше общих обработчиков:

```python
from handlers import payment
...
dp.include_routers(
    requests.router,
    start.router,
    register.router,
    catalog.router,
    schedule.router,
    payment.router,
    subscription_handlers.router,
    admin_mgmt.router,
    admin_actions.router,
)
```

В `README.md`, в раздел «Подписка сервиса», допишите:

```markdown
### Оплата звёздами

Управляющий платит кнопкой «💳 Подписка» в меню или кнопкой под письмом о
подписке. Тарифы и цены — переменные `STARS_PRICE_1M`, `STARS_PRICE_3M`,
`STARS_PRICE_12M`, цена задаётся целым числом звёзд.

Ни договора, ни `provider_token` звёзды не требуют — бот принимает их сразу.

Возврат делает владелец бота: `/refund <id списания>`. Звёзды возвращаются
плательщику, выданные дни отбираются. Идентификатор списания виден в журнале
`subscription_payments.external_id`.

Обе команды, `/extend` и `/refund`, доступны только тем, чьи Telegram id
перечислены в `BOT_OWNER_IDS`. **Пустое значение не пропускает никого** —
заполните переменную первым делом, иначе команды молчат и на владельца бота.
```

- [ ] **Шаг 4: Прогнать, убедиться что проходят**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_reminder_loop.py tests/test_render.py -q`
Ожидается: PASS

Проверьте, что приложение поднимается:
`.venv/Scripts/python.exe -c "import app; print('ok')"`

- [ ] **Шаг 5: Коммит**

```bash
git add notifications.py handlers/common.py app.py README.md tests/
git commit -m "Кнопка оплаты в письмах и в меню, роутер подключён"
```

---

### Задача 14: Сквозная проверка

**Файлы:**
- Тест: `tests/test_subscription_db.py`

**Интерфейсы:**
- Потребляет: всё выше
- Производит: ничего

- [ ] **Шаг 1: Написать сквозной тест**

```python
async def test_payment_round_trip(service):
    """
    Полный круг: истёк → оплачен → вернулся в поиск → возврат → снова истёк.
    """
    svc = await db.get_service(service)
    await _expire(service)
    assert all(
        str(row["idservice"]) != service
        for row in await db.get_services_by_city(svc["city"])
    )

    applied = await db.apply_stars_payment(
        service, days=30, charge_id="ch_round", payer_id=999_000_001
    )
    assert applied.restored is False
    assert any(
        str(row["idservice"]) == service
        for row in await db.get_services_by_city(svc["city"])
    )

    payment = await db.get_stars_payment("ch_round")
    await db.revoke_payment(str(payment["idpayment"]))
    assert all(
        str(row["idservice"]) != service
        for row in await db.get_services_by_city(svc["city"])
    )


async def test_a_redelivered_payment_does_not_pay_twice(service):
    """Свойство, ради которого заводился уникальный индекс, — на живой базе."""
    first = await db.apply_stars_payment(
        service, days=30, charge_id="ch_twice", payer_id=999_000_001
    )
    second = await db.apply_stars_payment(
        service, days=30, charge_id="ch_twice", payer_id=999_000_001
    )
    assert second.paid_until == first.paid_until
```

- [ ] **Шаг 2: Прогнать точечно**

Запустить: `.venv/Scripts/python.exe -m pytest tests/test_subscription_db.py -q`
Ожидается: PASS

- [ ] **Шаг 3: Полный прогон**

Запустить: `.venv/Scripts/python.exe -m pytest -q`
Ожидается: PASS. Прогон ~11 минут — база удалённая. `TimeoutError` в одиночном
файле перепроверяйте точечным запуском, прежде чем считать падением.

- [ ] **Шаг 4: Коммит**

```bash
git add tests/test_subscription_db.py
git commit -m "Сквозная проверка: оплата, возврат в поиск, возврат звёзд"
```

- [ ] **Шаг 5: Ручная проверка** (выполняет управляющий, не подагент)

Требует поднятого приложения и настоящих звёзд.

- [ ] `BOT_OWNER_IDS` заполнен, `/extend` отвечает
- [ ] «💳 Подписка» в меню → экран с тремя тарифами и текущим сроком
- [ ] Нажатие тарифа → счёт в звёздах на нужную сумму
- [ ] Оплата → подтверждение с новой датой, `paid_until` сдвинулся
- [ ] `/refund <id списания>` → звёзды вернулись, срок укоротился, пришло
      уведомление плательщику
- [ ] Повторный `/refund` того же списания → сказано, что уже возвращён
- [ ] Письмо о подписке приходит с кнопкой «💳 Продлить подписку»
- [ ] Кнопка из письма открывает тот же экран тарифов
- [ ] У администратора (не управляющего) кнопки «💳 Подписка» нет
- [ ] `/refund` и `/extend` от постороннего аккаунта — бот молчит
