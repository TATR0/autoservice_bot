#!/usr/bin/env python3
"""
Проверка перед выкатом: доедет ли этот .env до работающего бота.

Половина неудачных выкатов — не код, а забытая строка в .env: плейсхолдер
секрета, http вместо https, домен, не совпавший с BASE_URL. Всё это бот
обнаруживает уже на старте, а видно это только в логах контейнера, если
догадаться туда посмотреть. Здесь то же самое сказано до выката и словами.

    python scripts/preflight_deploy.py

Код возврата 1 — выкатывать нельзя. scripts/deploy.sh на этом останавливается.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote, urlsplit

STOP = "стоп"
WARN = "!"

# Строки из .env.example: их забывают заменить чаще всего
PLACEHOLDERS = frozenset({
    "замените-на-случайную-строку",
    "your_bot_username",
    "1234567890:AAABBBCCC...",
    "https://your-service.onrender.com",
})

# Имена из RFC 2606: их нельзя ни зарегистрировать, ни подтвердить. Let's
# Encrypt отказывает и домену, и почте с таким адресом — а Caddy будет
# ломиться раз за разом, и это единственное, что помешает выкату
EXAMPLE_HOSTS = ("example.com", "example.org", "example.net")
EXAMPLE_TLDS = (".example", ".invalid", ".test", ".localhost")

# Пусто — допустимо (сработает умолчание), мусор — нет: config читает их
# через int() прямо при импорте, и контейнер уходит в перезапуск
NUMERIC = (
    "DB_POOL_MIN", "DB_POOL_MAX", "MASTER_CHAT_ID", "TRIAL_DAYS",
    "REMINDER_TICK_SECONDS", "APPOINTMENT_REMINDER_HOURS", "PII_RETENTION_DAYS",
    "STARS_PRICE_1M", "STARS_PRICE_3M", "STARS_PRICE_12M",
    "FREE_PLAN_SERVICE_LIMIT", "REQUEST_COOLDOWN_SECONDS", "MAX_ACTIVE_REQUESTS",
    "INIT_DATA_MAX_AGE", "INVITE_TTL_DAYS", "BACKUP_INTERVAL_HOURS", "BACKUP_KEEP",
    "HTTPS_PORT",
)

# Что должно быть в базе после schema.sql. Не вся схема — те места, где
# «миграцию не применили» проявляется молчанием, а не ошибкой при старте
REQUIRED_SCHEMA = {
    "services": ("paid_until",),
    "requests": ("scheduled_at", "reminder_sent_at", "anonymized_at"),
    "subscription_payments": ("external_id", "refunded_at"),
    "subscription_reminders": ("stage",),
    "fsm_storage": ("state",),
}

# Куда Telegram соглашается доставлять вебхук. Любой другой порт он примет
# в set_webhook и молча не станет использовать: бот останется без апдейтов,
# и виноватым будет выглядеть код
WEBHOOK_PORTS = (443, 80, 88, 8443)

# Имя сервиса базы в docker-compose.yml. Такой хост в строке подключения
# означает: база своя, в соседнем контейнере, и про неё есть что проверить
INTERNAL_DB_HOST = "db"

TRUE_WORDS = ("1", "true", "yes", "on", "да")
QUOTES = '"' + chr(39)


class Problem(NamedTuple):
    level: str
    text: str


def check_env(env: Mapping[str, str]) -> list[Problem]:
    """Что не так с переменными окружения. Пустой список — всё в порядке."""
    problems: list[Problem] = []

    def stop(text: str) -> None:
        problems.append(Problem(STOP, text))

    def warn(text: str) -> None:
        problems.append(Problem(WARN, text))

    def value(name: str) -> str:
        return (env.get(name) or "").strip()

    token = value("BOT_TOKEN")
    if not token or token in PLACEHOLDERS:
        stop("BOT_TOKEN не задан")
    elif not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", token):
        stop("BOT_TOKEN не похож на токен: ожидается 123456:AA... от @BotFather")

    dsn = value("DATABASE_URL")
    if not dsn:
        stop("DATABASE_URL не задан")
    elif not dsn.startswith("postgres"):
        stop("DATABASE_URL должен начинаться с postgresql://")
    else:
        problems += check_internal_database(dsn, env)

    base = value("BASE_URL")
    domain = value("DOMAIN")
    if not base or base in PLACEHOLDERS:
        stop("BASE_URL не задан — вебхук не встанет, и бот не получит ни одного апдейта")
    elif not base.startswith("https://"):
        stop("BASE_URL должен быть https: Telegram ставит вебхук только на него")
    else:
        problems += check_base_url(base, domain, env)

    if not domain:
        stop("DOMAIN не задан — Caddy не поймёт, на какое имя выпускать сертификат")
    elif "/" in domain or domain.startswith("http"):
        stop(f"DOMAIN — только имя, без схемы и пути: было «{domain}»")
    elif is_example_host(domain):
        stop(f"DOMAIN={domain} — это пример из .env.example, а не ваш домен: "
             "на такое имя сертификат не выпишут")

    secret = value("WEBHOOK_SECRET")
    if not secret or secret in PLACEHOLDERS:
        stop(
            "WEBHOOK_SECRET не заменён. Сгенерировать: "
            "python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    elif len(secret) < 16:
        stop(f"WEBHOOK_SECRET короткий ({len(secret)} символов) — нужно хотя бы 16")
    elif not re.fullmatch(r"[A-Za-z0-9_-]+", secret):
        stop("WEBHOOK_SECRET уходит в URL вебхука: только латиница, цифры, дефис и _")

    if domain and value("TRUST_PROXY").lower() not in TRUE_WORDS:
        stop(
            "TRUST_PROXY=true обязателен за Caddy: иначе все запросы придут с "
            "адреса прокси, попадут в один счётчик, и первый же активный клиент "
            "исчерпает лимит частоты для всех остальных"
        )

    for name in NUMERIC:
        raw = value(name)
        if raw and not re.fullmatch(r"-?\d+", raw):
            stop(f"{name}={raw} — ожидается целое число, иначе бот не запустится вовсе")

    if not value("BOT_OWNER_IDS"):
        warn("BOT_OWNER_IDS пуст: /extend, /refund и /revoke не ответят никому")
    if not value("BOT_USERNAME") or value("BOT_USERNAME") in PLACEHOLDERS:
        warn("BOT_USERNAME не задан: ссылки t.me/<bot>?start=... будут битыми")
    email = value("ACME_EMAIL")
    if not email:
        warn("ACME_EMAIL пуст: центр сертификации не предупредит письмом, если что-то сломается")
    elif "@" not in email or is_example_host(email.rsplit("@", 1)[1]):
        stop(f"ACME_EMAIL={email} — Let's Encrypt такую почту не принимает "
             "(«contact email has forbidden domain»), и сертификата не будет")

    return problems


def is_example_host(host: str) -> bool:
    """Имя из документации, которое не станет настоящим адресом."""
    host = host.strip().lower().rstrip(".")
    return (
        host in EXAMPLE_HOSTS
        or any(host.endswith("." + known) for known in EXAMPLE_HOSTS)
        or host.endswith(EXAMPLE_TLDS)
    )


def check_base_url(base: str, domain: str, env: Mapping[str, str]) -> list[Problem]:
    """
    Тот ли адрес у бота: имя, порт и согласие порта с тем, что слушает Caddy.

    Порт в BASE_URL появляется, когда 443 на сервере уже занят чем-то другим
    (VPN, чужой веб-сервер). Тогда важны обе половины: Telegram доставляет
    вебхук не на любой порт, а Caddy должен слушать ровно тот, что в адресе.
    """
    problems: list[Problem] = []

    def stop(text: str) -> None:
        problems.append(Problem(STOP, text))

    try:
        parsed = urlsplit(base.rstrip("/"))
        port = parsed.port or 443
    except ValueError:
        return [Problem(STOP, f"BASE_URL не разбирается как адрес: {base}")]

    if domain and parsed.hostname != domain:
        stop(
            f"BASE_URL ({base}) не совпадает с DOMAIN ({domain}): сертификат "
            "выпишется на один адрес, а вебхук встанет на другой"
        )

    if port not in WEBHOOK_PORTS:
        stop(
            f"порт {port} в BASE_URL: Telegram доставляет вебхук только на "
            f"{', '.join(str(known) for known in WEBHOOK_PORTS)}"
        )

    listening = (env.get("HTTPS_PORT") or "").strip()
    if listening.isdigit() and int(listening) != port:
        stop(
            f"HTTPS_PORT={listening}, а в BASE_URL порт {port}: Telegram "
            "постучится туда, где никто не слушает"
        )

    return problems


def check_internal_database(dsn: str, env: Mapping[str, str]) -> list[Problem]:
    """
    Сходится ли строка подключения с тем, как поднимется контейнер db.

    База в контейнере создаётся один раз, по POSTGRES_*, и потом эти
    переменные ни на что не влияют. Если DATABASE_URL разошёлся с ними хоть
    в одном символе, бот молча не подключится — а выглядеть это будет как
    «бот не работает», без единого намёка на пароль.
    """
    parsed = urlsplit(dsn)
    if parsed.hostname != INTERNAL_DB_HOST:
        # Внешняя база — про неё здесь ничего не известно, и это нормально
        return []

    problems: list[Problem] = []

    def stop(text: str) -> None:
        problems.append(Problem(STOP, text))

    def value(name: str, default: str) -> str:
        return (env.get(name) or "").strip() or default

    password = unquote(parsed.password or "")
    expected_password = (env.get("POSTGRES_PASSWORD") or "").strip()
    if not expected_password or expected_password in PLACEHOLDERS:
        stop("POSTGRES_PASSWORD не задан — сгенерировать: ./scripts/init_env.sh")
    elif password != expected_password:
        stop(
            "пароль в DATABASE_URL не совпадает с POSTGRES_PASSWORD: база "
            "поднимется с одним паролем, а бот придёт с другим"
        )

    expected_user = value("POSTGRES_USER", "autoservice")
    user = parsed.username or "—"
    if user != expected_user:
        stop(
            f"пользователь в DATABASE_URL ({user}) не тот, что создаст "
            f"контейнер (POSTGRES_USER={expected_user})"
        )

    expected_name = value("POSTGRES_DB", "autoservice")
    name = parsed.path.lstrip("/") or "—"
    if name != expected_name:
        stop(
            f"база в DATABASE_URL ({name}) не та, что создаст контейнер "
            f"(POSTGRES_DB={expected_name})"
        )

    if "sslmode=disable" not in parsed.query:
        stop(
            "к базе в контейнере нужен ?sslmode=disable: без него драйвер "
            "потребует TLS, которого у соседнего контейнера нет"
        )

    return problems


async def check_schema(dsn: str) -> list[Problem]:
    """Применён ли schema.sql. Без asyncpg проверять нечем — так и скажем."""
    try:
        import asyncpg
    except ImportError:
        return [Problem(WARN, "asyncpg не установлен — схему не проверял")]

    kwargs = {}
    if "sslmode" not in dsn and "localhost" not in dsn and "127.0.0.1" not in dsn:
        kwargs["ssl"] = "require"
    try:
        conn = await asyncpg.connect(dsn=dsn, timeout=15, **kwargs)
    except Exception as exc:
        return [Problem(STOP, f"база недоступна: {type(exc).__name__} {exc}")]

    try:
        rows = await conn.fetch(
            """
            SELECT table_name, column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ANY($1::text[])
            """,
            list(REQUIRED_SCHEMA),
        )
    finally:
        await conn.close()

    have: dict[str, set[str]] = {}
    for row in rows:
        have.setdefault(row["table_name"], set()).add(row["column_name"])

    problems: list[Problem] = []
    for table, columns in REQUIRED_SCHEMA.items():
        if table not in have:
            problems.append(Problem(STOP, f"нет таблицы {table} — schema.sql не применён"))
            continue
        missing = [column for column in columns if column not in have[table]]
        if missing:
            problems.append(Problem(
                STOP,
                f"в таблице {table} нет колонок: {', '.join(missing)} — "
                "примените schema.sql, он идемпотентный",
            ))
    return problems


def read_env_file(path: Path) -> dict[str, str]:
    """Разбор .env: ровно то, что читает из него docker compose."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, raw = line.partition("=")
        values[name.strip()] = raw.split("#")[0].strip().strip(QUOTES)
    return values


async def main() -> int:
    root = Path(__file__).resolve().parent.parent
    # Переменные окружения важнее файла: в контейнере .env уже развёрнут в них
    env = {**read_env_file(root / ".env"), **os.environ}

    print("Проверка окружения перед выкатом")
    problems = check_env(env)

    dsn = (env.get("DATABASE_URL") or "").strip()
    if dsn.startswith("postgres"):
        problems += await check_schema(dsn)

    for level, text in problems:
        print(f"[{level:^4}] {text}")

    blockers = sum(1 for problem in problems if problem.level == STOP)
    print()
    if blockers:
        print(f"Выкатывать рано: {blockers} шт. с пометкой [стоп].")
        return 1
    print("Окружение в порядке, можно выкатывать.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
