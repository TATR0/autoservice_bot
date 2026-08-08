# AutoService Bot — проработка логики (Supabase + Render)

Документ разбирает текущее состояние репозитория `TATR0/autoservice_bot`, расхождения с ТЗ,
критичные архитектурные риски и целевую схему. Порядок разделов = порядок внедрения.

---

## 1. Что уже есть и чего не хватает по ТЗ

Реализовано в демке: регистрация сервиса (FSM), deep-link `?start=SVC_<uuid>`, WebApp-форма,
добавление/удаление админов, смена статусов заявок, «Мои заявки», мягкое удаление через `idrecstatus`.

Из ТЗ **не реализовано**:

| Пункт ТЗ | Роль | Статус |
|---|---|---|
| Статистика заявок | Управляющий, Администратор | нет ни метода в `database.py`, ни кнопки |
| Запись в собственный сервис | Управляющий, Администратор | нет |
| Удалиться из админов (самому) | Администратор | нет, удаляет только управляющий |
| Итоговое сообщение после регистрации со всеми полями | Управляющий | частично (нужно свести данные + ссылку в один блок) |
| Выбор «активного» сервиса | Управляющий у нескольких сервисов | нет, логика подразумевает один |

Расхождения именования (не проблема, но зафиксировать): ТЗ говорит `service` / `admin` / `request`,
в схеме — `services` / `admins` / `requests`. Оставляем множественное число, оно уже в коде.

---

## 2. Семь рисков, которые ломают прод

### 2.1. Render не поднимет polling-бота на бесплатном плане
Background Worker — платный тип сервиса. Web Service на free-плане засыпает после ~15 минут
без трафика. Текущая схема «два сервиса: worker + web» на free просто не запустится.

**Решение:** один процесс, webhook вместо polling. Раздел 3.

### 2.2. Прямое подключение к Supabase не резолвится с Render
`db.<project>.supabase.co` отдаёт только IPv6-адрес. Исходящий трафик Render — IPv4.
Подключение будет висеть и падать по таймауту.

**Решение:** строка подключения через pooler. Раздел 11.

### 2.3. `tg.sendData()` работает не везде
Метод доступен **только** для WebApp, открытых через кнопку reply-клавиатуры (`KeyboardButton(web_app=...)`).
Если WebApp открывается из inline-кнопки, из кнопки меню или по deep-link — `sendData` молча ничего не делает,
заявка теряется. Плюс лимит 4096 байт.

**Решение:** приём заявки через `POST /api/requests` с валидацией `initData`. Раздел 7.

### 2.4. Таблицы в Supabase без RLS открыты наружу
Supabase поднимает PostgREST на публичном URL. Таблицы в схеме `public` без включённого RLS
читаются с `anon`-ключом. В `requests` лежат ФИО, телефоны и госномера клиентов.

**Решение:** `ENABLE ROW LEVEL SECURITY` без политик. Раздел 11.

### 2.5. Админу нельзя написать первым
Управляющий вводит tg id администратора. Если тот никогда не открывал бота, любая отправка
падает с `403 Forbidden: bot can't initiate conversation with a user`. Плюс обычный человек
не знает свой числовой tg id — придётся объяснять про @userinfobot.

**Решение:** инвайт-ссылки вместо ручного ввода id. Раздел 5.3.

### 2.6. `MemoryStorage` теряет FSM при каждом деплое
Render перезапускает контейнер при пуше, при смене env, при OOM. Управляющий на 4-м шаге
регистрации получает «молчание бота».

**Решение:** Redis (Upstash free) или хранилище FSM в Postgres. Раздел 12.

### 2.7. `_ssl_ctx()` объявлен, но не передаётся в `create_pool`
Мёртвый код. Функция отключает проверку сертификата — если её всё-таки подключить как есть,
это MITM-риск. Либо удалить, либо использовать `ssl='require'` строкой.

---

## 3. Целевая архитектура: один процесс

```
┌──────────── Render Web Service (один) ─────────────┐
│  FastAPI                                           │
│   ├── POST /webhook/<secret>   → aiogram Dispatcher │
│   ├── GET  /api/services?city=                     │
│   ├── GET  /api/service/{id}                       │
│   ├── POST /api/requests        (initData)         │
│   ├── GET  /healthz                                │
│   └── GET  /app  → статика webapp/index.html       │
└────────────────────────────────────────────────────┘
              │                    │
       Supabase (pooler)     Telegram Bot API
```

Почему так:
- один порт, один сервис → влезает в free-план;
- webhook не требует постоянного соединения, сервис просыпается на входящем апдейте;
- статика WebApp с того же домена → CORS не нужен вообще, `API_BASE` можно оставить пустым;
- нет риска двух параллельных `getUpdates` при пересборке.

Скелет входной точки:

```python
# app.py — заменяет bot.py + api.py
import os
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from config import BOT_TOKEN, BASE_URL, WEBHOOK_SECRET
from database import db
from handlers import admin_actions, admin_mgmt, register, requests, start, stats

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=...)  # см. раздел 12
dp.include_routers(requests.router, start.router, register.router,
                   admin_mgmt.router, stats.router, admin_actions.router)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    await bot.set_webhook(
        f"{BASE_URL}/webhook/{WEBHOOK_SECRET}",
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types(),
    )
    yield
    await bot.delete_webhook()
    await db.close()
    await bot.session.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/app", StaticFiles(directory="webapp", html=True), name="webapp")


@app.post("/webhook/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if secret != WEBHOOK_SECRET or x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(403)
    await dp.feed_update(bot, Update.model_validate(await request.json(), context={"bot": bot}))
    return {"ok": True}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
```

Проверка `X-Telegram-Bot-Api-Secret-Token` обязательна: без неё любой, кто угадает URL,
сможет слать боту поддельные апдейты от чужого имени.

---

## 4. Модель данных v2

Текущая схема рабочая, но не покрывает статистику, аудит и уведомления. Ниже — миграция,
её можно выполнить поверх существующей базы.

### 4.1. Новая таблица `users`

Нужна, чтобы показывать администраторов по имени, а не голым числом, чтобы знать кто заблокировал
бота, и чтобы вести статистику по клиентам.

```sql
CREATE TABLE IF NOT EXISTS users (
    idusertg    bigint PRIMARY KEY,
    username    text,
    first_name  text,
    last_name   text,
    phone       text,                       -- последний введённый, для автоподстановки в форму
    is_blocked  boolean NOT NULL DEFAULT false,
    createdate  timestamptz NOT NULL DEFAULT now(),
    updatedate  timestamptz NOT NULL DEFAULT now()
);
```

Апсерт на каждом апдейте — через middleware, не в каждом хендлере:

```sql
INSERT INTO users (idusertg, username, first_name, last_name)
VALUES ($1,$2,$3,$4)
ON CONFLICT (idusertg) DO UPDATE
SET username = EXCLUDED.username,
    first_name = EXCLUDED.first_name,
    last_name  = EXCLUDED.last_name,
    is_blocked = false,
    updatedate = now();
```

### 4.2. Доработка `services`

```sql
ALTER TABLE services
    ADD COLUMN IF NOT EXISTS timezone   text NOT NULL DEFAULT 'Europe/Moscow',
    ADD COLUMN IF NOT EXISTS plan       text NOT NULL DEFAULT 'free',
    ADD COLUMN IF NOT EXISTS paid_until date,
    ADD COLUMN IF NOT EXISTS updatedate timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS deletedate timestamptz;

-- один и тот же сервис не регистрируем дважды
CREATE UNIQUE INDEX IF NOT EXISTS idx_services_unique_active
    ON services (owner_id, lower(trim(service_name)), lower(trim(city)))
    WHERE idrecstatus = 0;
```

`timezone` нужен, чтобы «заявок за сегодня» считалось по времени сервиса, а не по UTC.
`plan` / `paid_until` — задел под платную регистрацию из ТЗ.

### 4.3. Доработка `requests`

```sql
ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS seq        bigint GENERATED BY DEFAULT AS IDENTITY,
    ADD COLUMN IF NOT EXISTS client_uid text,
    ADD COLUMN IF NOT EXISTS handled_by bigint,
    ADD COLUMN IF NOT EXISTS updatedate timestamptz NOT NULL DEFAULT now();

-- человекочитаемый номер заявки: клиенту показываем #000142, а не uuid
CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_seq ON requests (seq);

-- идемпотентность: повторный тап «Отправить» не создаст дубль
CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_client_uid
    ON requests (client_uid) WHERE client_uid IS NOT NULL;

ALTER TABLE requests
    DROP CONSTRAINT IF EXISTS chk_requests_status,
    ADD  CONSTRAINT chk_requests_status CHECK (status IN
        ('new','accepted','called','in_progress','done','rejected','cancelled','service_closed'));

ALTER TABLE requests
    DROP CONSTRAINT IF EXISTS chk_requests_urgency,
    ADD  CONSTRAINT chk_requests_urgency CHECK (urgency IN ('low','medium','high','urgent'));
```

Статусы расширены относительно демки (`new/accepted/called/rejected`):
`in_progress` — машина в работе, `done` — закрыта успешно, `cancelled` — отменил клиент,
`service_closed` — сервис удалён. Без `done` невозможно посчитать конверсию.

### 4.4. История статусов

Без неё нельзя ответить «сколько в среднем ждём реакции админа» и «кто отклонил заявку».

```sql
CREATE TABLE IF NOT EXISTS request_status_history (
    id          bigserial PRIMARY KEY,
    idrequests  uuid NOT NULL REFERENCES requests(idrequests) ON DELETE CASCADE,
    status_from text,
    status_to   text NOT NULL,
    changed_by  bigint,
    note        text,
    changed_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rsh_request ON request_status_history (idrequests, changed_at);
```

### 4.5. Инвайты администраторов

```sql
CREATE TABLE IF NOT EXISTS admin_invites (
    token      text PRIMARY KEY,
    idservice  uuid NOT NULL REFERENCES services(idservice) ON DELETE CASCADE,
    created_by bigint NOT NULL,
    used_by    bigint,
    used_at    timestamptz,
    expires_at timestamptz NOT NULL DEFAULT now() + interval '7 days',
    createdate timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_invites_service ON admin_invites (idservice) WHERE used_at IS NULL;
```

### 4.6. Представление ролей

Роли остаются динамическими (отдельной таблицы ролей нет — это правильно, роль всегда выводится
из фактов), но запрос стоит вынести в одно место:

```sql
CREATE OR REPLACE VIEW v_user_roles AS
SELECT s.owner_id AS idusertg, s.idservice, 'owner'::text AS role
  FROM services s WHERE s.idrecstatus = 0
UNION ALL
SELECT a.idusertg, a.idservice, 'admin'::text
  FROM admins a JOIN services s USING (idservice)
 WHERE a.idrecstatus = 0 AND s.idrecstatus = 0;
```

Роль пользователя = максимальная по `owner > admin > user`. Один запрос на `/start`.

### 4.7. Триггер `updatedate`

```sql
CREATE OR REPLACE FUNCTION touch_updatedate() RETURNS trigger AS $$
BEGIN NEW.updatedate = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_requests_touch BEFORE UPDATE ON requests
    FOR EACH ROW EXECUTE FUNCTION touch_updatedate();
CREATE TRIGGER trg_services_touch BEFORE UPDATE ON services
    FOR EACH ROW EXECUTE FUNCTION touch_updatedate();
```

---

## 5. Роли и сценарии

### 5.1. Определение роли на `/start`

```
запрос к v_user_roles по tg_id
  ├── есть строка role='owner'  → меню Управляющего
  ├── есть строка role='admin'  → меню Администратора
  └── строк нет                 → меню Пользователя
```

Если строк несколько (управляющий двух сервисов, или владелец одного и админ другого) —
показываем экран выбора сервиса и держим выбранный `idservice` в FSM-данных как `active_service`.
Все кнопки меню работают в контексте активного сервиса, в шапке сообщения — его название.
Это единственный способ не сломать логику при росте.

### 5.2. Регистрация сервиса

FSM из 5 шагов, к каждому шагу — кнопка «Отмена» и валидация:

| Шаг | Валидация | При ошибке |
|---|---|---|
| Название | 2–80 символов, обрезать пробелы | повтор шага с пояснением |
| Телефон | нормализация к `+7XXXXXXXXXX`, регексп | повтор |
| Город | 2–60 символов, нормализация регистра; подсказать из существующих | повтор |
| Улица | 2–120 символов | повтор |
| tg id администратора | **убрать этот шаг** — см. 5.3 | — |

Итоговое сообщение (по ТЗ) — карточка со всеми полями, `idservice` и ссылка
`https://t.me/<bot>?start=SVC_<uuid>`. Отдельной кнопкой — «Пригласить администратора».

Важно: перед регистрацией проверять лимит `plan` (сейчас free = 1 сервис). Иначе один человек
нарегистрирует сотню и засорит поиск по городу.

### 5.3. Приглашение администратора вместо ввода id

Управляющий жмёт «Добавить админа» → бот генерирует токен и отдаёт ссылку:

```
https://t.me/<bot>?start=ADM_<token>
```

Управляющий пересылает её будущему админу любым способом. Тот открывает — бот:
1. проверяет токен: существует, `used_at IS NULL`, `expires_at > now()`;
2. показывает подтверждение «Стать администратором сервиса «X»?»;
3. при согласии — `add_admin(idservice, tg_id)`, помечает токен использованным,
   уведомляет управляющего.

Это решает сразу две проблемы: пользователь не должен знать свой id, и бот гарантированно
может писать новому админу (тот уже нажал `/start`).

Payload `/start` ограничен 64 символами и алфавитом `A-Za-z0-9_-` — токен из
`secrets.token_urlsafe(16)` (22 символа) подходит.

### 5.4. Заявка клиента

```
«Записаться в автосервис»
  → WebApp открывается кнопкой reply-клавиатуры
  → форма поиска по городу → GET /api/services?city=
  → список сервисов → выбор
  → форма (имя, телефон, марка, модель, госномер, работы, срочность, комментарий)
  → POST /api/requests с initData
  → бот шлёт клиенту подтверждение с номером #seq
  → бот шлёт всем активным админам + владельцу карточку с кнопками
```

При входе по deep-link `SVC_<uuid>` шаг поиска города пропускается, WebApp открывается сразу
с `?service_id=<uuid>`.

Все поля обязательны кроме комментария. Плюс чекбокс согласия на обработку персональных данных —
собираются ФИО, телефон и госномер, для РФ это 152-ФЗ.

### 5.5. Самоудаление администратора

Кнопка «Удалиться из админов» → подтверждение → `remove_admin` → уведомление управляющему.
Управляющий не может удалить сам себя из владельцев (иначе сервис остаётся без хозяина) —
для него отдельный сценарий «Удалить сервис».

### 5.6. Удаление сервиса (каскад, которого сейчас нет)

`soft_delete_service` меняет только `services.idrecstatus`. Админы остаются активными,
заявки висят в статусе `new`. Правильно — в одной транзакции:

```sql
UPDATE services SET idrecstatus = -1, deletedate = now() WHERE idservice = $1;
UPDATE admins   SET idrecstatus = -1 WHERE idservice = $1 AND idrecstatus = 0;
UPDATE requests SET status = 'service_closed'
 WHERE idservice = $1 AND status IN ('new','accepted','called','in_progress');
```

И разослать клиентам этих заявок уведомление, что сервис закрылся.

---

## 6. Приём заявки: `sendData` → HTTP API

Заменяем `tg.sendData()` на обычный POST. Фронт:

```js
const uid = crypto.randomUUID();          // идемпотентность
const res = await fetch(`${API_BASE}/api/requests`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    init_data: Telegram.WebApp.initData,   // подпись Telegram
    client_uid: uid,
    service_id: serviceId,
    client_name: form.name.value,
    phone: form.phone.value,
    brand: form.brand.value,
    model: form.model.value,
    plate: form.plate.value,
    service_type: form.serviceType.value,
    urgency: form.urgency.value,
    comment: form.comment.value,
  }),
});
```

Бэкенд обязан проверить подпись — без этого любой сможет создавать заявки от чужого имени:

```python
import hashlib, hmac, json, time
from urllib.parse import parse_qsl


def verify_init_data(init_data: str, bot_token: str, max_age: int = 3600) -> dict:
    """Возвращает объект user или бросает ValueError."""
    pairs = dict(parse_qsl(init_data, strict_parsing=True))
    received = pairs.pop("hash", "")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise ValueError("bad signature")
    if time.time() - int(pairs.get("auth_date", 0)) > max_age:
        raise ValueError("init_data expired")
    return json.loads(pairs["user"])
```

`idclienttg` берём **только** из проверенного `initData`, никогда из тела запроса.

При отдаче статики с того же домена (раздел 3) CORS-мидлварь можно удалить целиком.
Если WebApp всё-таки остаётся на GitHub Pages — `allow_origins` сузить до конкретного origin,
`allow_methods` расширить до `["GET","POST","OPTIONS"]`.

---

## 7. Уведомления

### 7.1. Кому

Всем активным администраторам сервиса **плюс** владельцу, с дедупликацией по tg id
(владелец может числиться и админом).

### 7.2. Гонка двух админов

Два админа одновременно жмут «Принять» — оба апдейта проходят, история пишется дважды.
Лечится условным апдейтом:

```sql
UPDATE requests
   SET status = $2, handled_by = $3
 WHERE idrequests = $1 AND status = $4      -- $4 = ожидаемый текущий статус
RETURNING *;
```

Вернулся `None` → заявку уже обработали, отвечаем на callback «Заявку уже взял другой сотрудник».

### 7.3. Лимиты Telegram

Ограничение — примерно 30 сообщений в секунду суммарно и не чаще 1 в секунду в один чат.
При десятке админов на заявку это некритично, но рассылки (например, «сервис закрылся»)
надо гнать через очередь с задержкой и ловить `TelegramRetryAfter`.

`TelegramForbiddenError` при отправке → ставим `users.is_blocked = true` и больше не пишем.

### 7.4. Экранирование

Клиент вводит имя `<b>Вася`. При `parse_mode=HTML` сообщение не отправится вообще
(`can't parse entities`) — заявка создана, но админ о ней не узнает.
Оборачивать **каждое** пользовательское поле в `html.escape()`.

---

## 8. Валидация и антиспам

| Поле | Правило |
|---|---|
| Имя | 2–60 символов, вырезать управляющие символы |
| Телефон | нормализация к `+7XXXXXXXXXX`, отклонять короткие |
| Марка / модель | 1–40 символов |
| Госномер | uppercase, убрать пробелы и дефисы, 5–12 символов |
| Тип работ | строго из словаря 9 значений |
| Срочность | строго из словаря 4 значений |
| Комментарий | до 500 символов, обрезать |

Антиспам, три уровня:
1. **Идемпотентность** — `client_uid` UNIQUE ловит двойной тап.
2. **Кулдаун** — не чаще одной заявки в 60 секунд от одного tg id.
3. **Потолок** — не более 3 активных заявок (`status IN ('new','accepted','called','in_progress')`)
   на одного клиента в одном сервисе.

```sql
SELECT count(*) FROM requests
 WHERE idclienttg = $1 AND idservice = $2
   AND idrecstatus = 0
   AND status IN ('new','accepted','called','in_progress');
```

---

## 9. Статистика заявок

Пункт ТЗ, которого нет в коде. Метрики считаются по времени сервиса, не по UTC:

```sql
-- сводка по сервису
SELECT
  count(*)                                                              AS total,
  count(*) FILTER (WHERE createdate >= date_trunc('day',  now() AT TIME ZONE tz)) AS today,
  count(*) FILTER (WHERE createdate >= now() - interval '7 days')       AS week,
  count(*) FILTER (WHERE createdate >= now() - interval '30 days')      AS month,
  count(*) FILTER (WHERE status = 'new')                                AS st_new,
  count(*) FILTER (WHERE status IN ('accepted','called','in_progress')) AS st_work,
  count(*) FILTER (WHERE status = 'done')                               AS st_done,
  count(*) FILTER (WHERE status = 'rejected')                           AS st_rejected
FROM requests r, LATERAL (SELECT timezone FROM services WHERE idservice = r.idservice) s(tz)
WHERE r.idservice = $1 AND r.idrecstatus = 0;
```

```sql
-- разрез по видам работ
SELECT service_type, count(*) AS cnt
FROM requests WHERE idservice = $1 AND idrecstatus = 0
GROUP BY service_type ORDER BY cnt DESC;
```

```sql
-- среднее время до первой реакции админа
SELECT avg(h.first_change - r.createdate) AS avg_reaction
FROM requests r
JOIN LATERAL (
    SELECT min(changed_at) AS first_change
    FROM request_status_history
    WHERE idrequests = r.idrequests AND status_to <> 'new'
) h ON true
WHERE r.idservice = $1 AND r.createdate >= now() - interval '30 days';
```

Конверсия = `st_done / total`. Доля просроченных = заявки в `new` старше 24 часов —
их же стоит подсвечивать в списке и слать напоминание админам раз в сутки.

---

## 10. Supabase

### 10.1. Строка подключения

Использовать **Session Pooler**, не прямое подключение:

```
postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
```

Прямой хост `db.<ref>.supabase.co` — IPv6-only, с Render не резолвится.

Если берёте Transaction Pooler (порт 6543), asyncpg нужно отключить кэш подготовленных выражений,
иначе будут ошибки `prepared statement "__asyncpg_stmt_1__" already exists`:

```python
self._pool = await asyncpg.create_pool(
    dsn=DATABASE_URL,
    min_size=1,
    max_size=5,               # free-план Supabase скуп на коннекты
    statement_cache_size=0,   # обязательно для transaction pooler
    command_timeout=10,
)
```

`max_size=10` при двух сервисах Render — это уже 20 коннектов, на free-плане легко упереться.
После объединения в один процесс достаточно 5.

### 10.2. RLS

```sql
ALTER TABLE services              ENABLE ROW LEVEL SECURITY;
ALTER TABLE admins                ENABLE ROW LEVEL SECURITY;
ALTER TABLE requests              ENABLE ROW LEVEL SECURITY;
ALTER TABLE users                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE admin_invites         ENABLE ROW LEVEL SECURITY;
ALTER TABLE request_status_history ENABLE ROW LEVEL SECURITY;
```

Политики создавать не нужно: бот ходит напрямую по `DATABASE_URL` под владельцем таблиц,
RLS его не ограничивает, а `anon` и `authenticated` через PostgREST не получат ничего.

Проверить после включения: открыть `https://<ref>.supabase.co/rest/v1/requests?apikey=<anon>` —
должен вернуться пустой массив, а не данные клиентов.

### 10.3. Бэкапы

Free-план Supabase бэкапит ежедневно, но хранит недолго и проекты усыпляет после недели простоя.
Для боевого запуска — либо платный план, либо свой `pg_dump` по расписанию в S3/R2.

---

## 11. Render

### 11.1. Сервис

Один Web Service:
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Health Check Path: `/healthz`

### 11.2. Переменные окружения

| Переменная | Назначение |
|---|---|
| `BOT_TOKEN` | токен BotFather |
| `BOT_USERNAME` | username без `@`, для сборки deep-link |
| `DATABASE_URL` | строка pooler'а Supabase |
| `BASE_URL` | публичный URL сервиса, `https://xxx.onrender.com` |
| `WEBHOOK_SECRET` | случайная строка, `secrets.token_urlsafe(32)` |
| `REDIS_URL` | хранилище FSM (опционально, см. 12) |
| `MASTER_CHAT_ID` | чат для «потерянных» заявок |

`WEBAPP_URL` и `WEBAPP_ORIGIN` уходят, если статика отдаётся из того же сервиса.

### 11.3. Засыпание free-плана

Сервис засыпает после 15 минут без запросов, холодный старт — до 50 секунд.
Telegram не гарантирует повтор доставки апдейта, так что первая заявка после простоя может
потеряться. Внешний пинг `/healthz` каждые 10 минут (cron-job.org, UptimeRobot) решает это
на free-плане; для боевого запуска — платный instance.

---

## 12. FSM-хранилище

Вариант A (проще): Upstash Redis, бесплатный тариф.

```python
from aiogram.fsm.storage.redis import RedisStorage
dp = Dispatcher(storage=RedisStorage.from_url(REDIS_URL))
```

Вариант B (без новых зависимостей): таблица в Postgres.

```sql
CREATE TABLE IF NOT EXISTS fsm_storage (
    key        text PRIMARY KEY,
    state      text,
    data       jsonb NOT NULL DEFAULT '{}'::jsonb,
    updatedate timestamptz NOT NULL DEFAULT now()
);
```
и свой класс на `BaseStorage` (4 метода: `set_state`, `get_state`, `set_data`, `get_data`).

Вариант C (костыль, но работает для регистрации): собирать все 4 поля сервиса в одной
WebApp-форме вместо пошагового FSM — тогда состояние вообще не нужно.

---

## 13. План внедрения

**Этап 1 — не упасть на проде**
1. Строка подключения через pooler + `statement_cache_size=0`, `max_size=5`.
2. Включить RLS на всех таблицах.
3. Объединить `bot.py` и `api.py` в `app.py` на webhook, проверять secret token.
4. Удалить неиспользуемый `_ssl_ctx()`.
5. `html.escape()` на все пользовательские поля.

**Этап 2 — закрыть дыры логики**
6. Миграция схемы: `users`, `request_status_history`, `admin_invites`, `seq`, `client_uid`, CHECK-констрейнты, `v_user_roles`.
7. Приём заявки через `POST /api/requests` с валидацией `initData`.
8. Инвайт-ссылки вместо ввода tg id админа.
9. Условный UPDATE при смене статуса + запись в историю.
10. Каскад при удалении сервиса.

**Этап 3 — допилить ТЗ**
11. Статистика заявок (обе роли).
12. Запись в собственный сервис.
13. Самоудаление администратора.
14. Выбор активного сервиса при нескольких.
15. Валидация полей и антиспам.

**Этап 4 — эксплуатация**
16. FSM в Redis или Postgres.
17. Sentry, keep-alive пинг, напоминания по «зависшим» заявкам.
18. Согласие на обработку ПДн, кнопка «удалить мои данные».
19. Заготовка под платную регистрацию: таблица `payments`, проверка `paid_until`.
