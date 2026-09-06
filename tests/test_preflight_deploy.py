"""
Проверка окружения перед выкатом на VPS.

Проверяется именно чистая часть — разбор переменных: она отвечает за то,
пустят ли выкат вообще, а ошибка здесь либо остановит рабочий выкат зря,
либо пропустит нерабочий.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from preflight_deploy import STOP, check_env  # noqa: E402

GOOD = {
    # Не «похожий на настоящий»: сканер секретов GitHub ловит форму
    # <цифры>:AA<34 символа> и поднимает тревогу на образец из документации
    "BOT_TOKEN": "1234567890:TEST-token-for-tests-only-000000",
    "BOT_USERNAME": "autoservice_bot",
    "DATABASE_URL": "postgresql://autoservice:s3cret@db:5432/autoservice?sslmode=disable",
    "POSTGRES_USER": "autoservice",
    "POSTGRES_DB": "autoservice",
    "POSTGRES_PASSWORD": "s3cret",
    "BASE_URL": "https://bot.myservice.ru",
    "DOMAIN": "bot.myservice.ru",
    "WEBHOOK_SECRET": "sJ9_lQeQ2mX7nT4vB8kZpR1yUcW0aHgE",
    "ACME_EMAIL": "owner@myservice.ru",
    "TRUST_PROXY": "true",
    "BOT_OWNER_IDS": "12345",
}


def blockers(env):
    return [p.text for p in check_env(env) if p.level == STOP]


def test_a_filled_env_passes():
    assert check_env(GOOD) == []


def test_missing_token_blocks():
    assert blockers({**GOOD, "BOT_TOKEN": ""})


def test_a_token_that_is_not_a_token_blocks():
    """Скопированный не из того места токен ловится здесь, а не в логах."""
    assert blockers({**GOOD, "BOT_TOKEN": "TEST-token-for-tests-only-000000"})


def test_placeholder_secret_blocks():
    """
    Строка из .env.example — самое частое, что забывают заменить, и самое
    дорогое: секрет вебхука публично известен, апдейты боту шлёт кто угодно.
    """
    assert blockers({**GOOD, "WEBHOOK_SECRET": "замените-на-случайную-строку"})


def test_short_secret_blocks():
    assert blockers({**GOOD, "WEBHOOK_SECRET": "abc123"})


def test_secret_with_odd_characters_blocks():
    """Секрет уходит в URL вебхука: пробел или слэш ломают сам адрес."""
    assert blockers({**GOOD, "WEBHOOK_SECRET": "секрет с пробелом и /слэшем"})


def test_http_base_url_blocks():
    """Telegram ставит вебхук только на https."""
    assert blockers({**GOOD, "BASE_URL": "http://bot.myservice.ru"})


def test_base_url_must_match_the_domain():
    """
    Иначе Caddy получит сертификат на один адрес, а вебхук встанет на
    другой — бот поднимется и будет молчать.
    """
    assert blockers({**GOOD, "BASE_URL": "https://old.myservice.ru"})


def test_proxy_without_trust_blocks():
    """За Caddy все запросы придут с одного адреса и попадут в один счётчик."""
    assert blockers({**GOOD, "TRUST_PROXY": "false"})


def test_a_number_that_is_not_a_number_blocks():
    """
    Иначе контейнер падает при импорте config с ValueError и уходит в
    перезапуск — по логам это выглядит как что угодно, кроме опечатки.
    """
    assert blockers({**GOOD, "BACKUP_KEEP": "четырнадцать"})


def test_empty_numbers_are_fine():
    assert check_env({**GOOD, "BACKUP_KEEP": ""}) == []


def test_empty_owner_ids_warn_but_do_not_block():
    problems = check_env({**GOOD, "BOT_OWNER_IDS": ""})
    assert problems, "про пустой BOT_OWNER_IDS надо сказать"
    assert not blockers({**GOOD, "BOT_OWNER_IDS": ""}), "но выкат это не останавливает"


def test_password_that_does_not_match_the_container_blocks():
    """
    Контейнер создаёт базу один раз, по POSTGRES_PASSWORD. Разошлись —
    бот не подключится, и в логах будет только «пароль не подошёл».
    """
    assert blockers({**GOOD, "POSTGRES_PASSWORD": "другой"})


def test_placeholder_database_password_blocks():
    assert blockers({**GOOD, "POSTGRES_PASSWORD": "замените-на-случайную-строку"})


def test_wrong_user_or_database_name_blocks():
    """Такой роли и такой базы в контейнере просто не появится."""
    assert blockers({**GOOD, "POSTGRES_USER": "postgres"})
    assert blockers({**GOOD, "POSTGRES_DB": "postgres"})


def test_container_database_needs_sslmode_disable():
    """
    У соседнего контейнера нет TLS, а драйвер по умолчанию его требует.
    Соединение не состоится, и виновата будет «сеть».
    """
    assert blockers({
        **GOOD,
        "DATABASE_URL": "postgresql://autoservice:s3cret@db:5432/autoservice",
    })


def test_external_database_is_left_alone():
    """
    Строку до чужой базы проверять нечем: POSTGRES_* к ней отношения не
    имеют, и требовать их совпадения было бы неправдой.
    """
    external = {
        **GOOD,
        "DATABASE_URL": "postgresql://postgres:pw@aws-0-eu.pooler.supabase.com:5432/postgres",
        "POSTGRES_PASSWORD": "",
    }
    assert check_env(external) == []


def test_a_port_telegram_does_not_use_blocks():
    """
    Вебхук на 9443 Telegram примет в set_webhook и не станет доставлять на
    него ни одного апдейта. Молча — виноватым будет выглядеть код бота.
    """
    assert blockers({**GOOD, "BASE_URL": "https://bot.myservice.ru:9443"})


def test_a_port_telegram_uses_is_fine():
    """8443 — запасной порт для случая, когда 443 на сервере уже занят."""
    assert check_env({
        **GOOD,
        "BASE_URL": "https://bot.myservice.ru:8443",
        "HTTPS_PORT": "8443",
    }) == []


def test_https_port_must_match_the_base_url():
    """Caddy слушал бы один порт, а Telegram стучался в другой."""
    assert blockers({
        **GOOD,
        "BASE_URL": "https://bot.myservice.ru:8443",
        "HTTPS_PORT": "443",
    })


def test_port_does_not_break_the_domain_comparison():
    """Порт в адресе — не повод считать, что имя разошлось с DOMAIN."""
    assert blockers({
        **GOOD,
        "BASE_URL": "https://other.duckdns.org:8443",
        "HTTPS_PORT": "8443",
    })


def test_domain_from_the_example_file_blocks():
    """
    bot.example.com — имя из документации: сертификат на него не выпустят.

    Без этой проверки выкат проходит, а Caddy потом сутками ломится в
    Let's Encrypt, и понять почему можно только из его логов.
    """
    assert blockers({**GOOD, "DOMAIN": "bot.example.com",
                     "BASE_URL": "https://bot.example.com"})


def test_acme_email_from_the_example_file_blocks():
    assert blockers({**GOOD, "ACME_EMAIL": "owner@example.com"})


def test_acme_email_without_at_blocks():
    assert blockers({**GOOD, "ACME_EMAIL": "owner"})


def test_ordinary_acme_email_is_fine():
    assert blockers({**GOOD, "ACME_EMAIL": "k@duck.com"}) == []
