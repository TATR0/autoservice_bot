# 🚗 AutoService Bot

Телеграм-бот для онлайн-записи в автосервис.  
Стек: **Python + aiogram 3 + asyncpg + FastAPI + Supabase + Render**

---

## Архитектура

```
autoservice_bot/
├── bot.py              # точка входа (polling)
├── api.py              # FastAPI REST (для WebApp)
├── config.py           # все переменные окружения
├── database.py         # asyncpg-слой, все SQL-запросы
├── keyboards.py        # все клавиатуры бота
├── schema.sql          # SQL-схема для Supabase
├── requirements.txt
├── .env.example
├── handlers/
│   ├── start.py        # /start, deep-link SVC_
│   ├── register.py     # регистрация сервиса (FSM)
│   ├── admin_mgmt.py   # добавить / удалить администратора
│   ├── requests.py     # WebApp-заявки + «Мои заявки»
│   └── admin_actions.py# статусы, просмотр, fallback
└── webapp/
    └── index.html      # Telegram WebApp (фронтенд)
```

---

## Роли

| Роль | Как получить | Возможности |
|---|---|---|
| **Пользователь** | Любой, кто открыл бота | Записаться в сервис, смотреть свои заявки |
| **Администратор** | Назначается управляющим | Видеть заявки сервиса, менять статусы |
| **Управляющий** | Зарегистрировал сервис | Всё вышеперечисленное + добавлять/удалять администраторов |

Роли определяются **динамически при каждом /start** — отдельной таблицы ролей нет.  
Управляющий = `services.owner_id == tg_id`.  
Администратор = активная запись в `admins`.

---

## Быстрый старт

### 1. Создать бота в Telegram

1. Открыть [@BotFather](https://t.me/BotFather)
2. `/newbot` → задать имя и username
3. Скопировать токен
4. `/setprivacy` → выбрать бота → `Disable` (чтобы бот видел все сообщения)

---

### 2. Создать базу данных в Supabase

1. Зарегистрироваться на [supabase.com](https://supabase.com)
2. Создать новый проект
3. Открыть **SQL Editor** и выполнить содержимое `schema.sql`
4. Открыть **Project Settings → Database → Connection string (URI)**
5. Скопировать строку вида:
   ```
   postgresql://postgres:<password>@db.<project>.supabase.co:5432/postgres
   ```

---

### 3. Разместить WebApp-фронтенд

Файл `webapp/index.html` — это фронтенд для записи клиентов.  
Telegram требует **HTTPS**. Самый простой вариант — **GitHub Pages**.

1. Создать репозиторий (можно приватный с GitHub Pro, или публичный)
2. Положить `webapp/index.html` в корень (или в папку `docs/`)
3. **Settings → Pages → Source: Deploy from a branch → main / root**
4. Получить URL: `https://your-name.github.io/repo-name`
5. В `index.html` заменить значение `window.API_BASE`:
   ```js
   const API_BASE = "https://your-api.onrender.com";
   ```

---

### 4. Деплой на Render

#### Вариант A — два сервиса (рекомендуется)

**Сервис 1 — бот** (Background Worker)
- **Build command:** `pip install -r requirements.txt`
- **Start command:** `python bot.py`

**Сервис 2 — API** (Web Service)
- **Build command:** `pip install -r requirements.txt`
- **Start command:** `uvicorn api:app --host 0.0.0.0 --port $PORT`

#### Вариант B — один сервис (через Procfile)

Создать файл `Procfile` в корне:
```
web: uvicorn api:app --host 0.0.0.0 --port $PORT
worker: python bot.py
```
Render поддерживает `Procfile` — оба процесса запустятся из одного репо.

---

### 5. Переменные окружения (Environment Variables на Render)

| Переменная | Описание | Пример |
|---|---|---|
| `BOT_TOKEN` | Токен от BotFather | `1234567890:AAA...` |
| `BOT_USERNAME` | Username бота без @ | `my_autoservice_bot` |
| `DATABASE_URL` | Строка подключения Supabase | `postgresql://postgres:...` |
| `WEBAPP_URL` | URL GitHub Pages | `https://you.github.io/repo` |
| `WEBAPP_ORIGIN` | Для CORS в api.py | `https://you.github.io` |
| `MASTER_CHAT_ID` | Чат для «потерянных» заявок | `123456789` (опционально) |

---

## Как это работает

### Клиент
1. Открывает бота → видит кнопку **«Записаться в автосервис»**
2. В WebApp выбирает город → видит список сервисов → выбирает сервис
3. Заполняет форму (имя, телефон, авто, проблема, срочность)
4. Нажимает «Отправить» → данные через `tg.sendData()` попадают в бот
5. Получает подтверждение с номером заявки

### Администратор
- Получает уведомление о новой заявке с кнопками: **Принять / Позвонили / Отказать**
- При смене статуса клиент получает уведомление

### Управляющий
- Регистрирует сервис через `/register_service` (5-шаговый FSM)
- Получает **ссылку** (`https://t.me/bot?start=SVC_<uuid>`) для размещения
- Управляет администраторами через **«➕ Добавить»** / **«➖ Удалить»**

### Deep-link
Клиент открывает бота по ссылке `?start=SVC_<uuid>` → сразу видит форму записи конкретного сервиса (WebApp открывается с `?service_id=<uuid>`).

---

## Мягкое удаление

Ни одна запись физически не удаляется. Используется поле `idrecstatus`:

| Значение | Смысл |
|---|---|
| `0` | Активная запись |
| `-1` | Удалена / неактивна |

Для восстановления достаточно выполнить:
```sql
UPDATE admins SET idrecstatus = 0 WHERE idusertg = 123456789;
```

---

## Локальный запуск

```bash
# 1. Клонировать / скопировать файлы
cd autoservice_bot

# 2. Создать виртуальное окружение
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 3. Установить зависимости
pip install -r requirements.txt

# 4. Создать .env из примера
cp .env.example .env
# Заполнить .env своими значениями

# 5. Применить схему в Supabase (SQL Editor) из schema.sql

# 6. Запустить бота
python bot.py

# 7. Запустить API (в отдельном терминале, если нужен WebApp)
python api.py
```

---

## Команды бота

| Команда / Кнопка | Кто видит | Действие |
|---|---|---|
| `/start` | Все | Главное меню по роли |
| `/register_service` | Все | Регистрация нового сервиса |
| `🚗 Записаться в автосервис` | Клиент | Открыть WebApp |
| `📋 Мои заявки` | Клиент / Админ | Список заявок |
| `📋 Заявки сервиса` | Админ / Управляющий | Заявки с деталями |
| `ℹ️ О сервисе` | Админ / Управляющий | Информация + ссылка |
| `👥 Администраторы` | Админ / Управляющий | Список администраторов |
| `➕ Добавить админа` | Управляющий | FSM добавления |
| `➖ Удалить админа` | Управляющий | FSM удаления |
