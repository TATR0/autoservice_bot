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


# ── Тарифы подписки ──────────────────────────────────────────────────────────


def test_three_plans_are_configured():
    assert [p.days for p in config.PLANS] == [30, 90, 365]


def test_a_year_is_a_year():
    """365, а не 360: через год управляющий пересчитает и будет прав."""
    assert config.PLANS[-1].days == 365


def test_prices_are_whole_stars():
    """Звёзды не дробятся: дробная цена — это счёт, который Telegram не примет."""
    for plan in config.PLANS:
        assert isinstance(plan.stars, int) and plan.stars > 0


def test_every_plan_has_a_human_label():
    """«365 дней» на кнопке читается хуже, чем «12 месяцев»."""
    for plan in config.PLANS:
        assert plan.label.strip()


def test_stars_cost_the_same_money_as_rubles(monkeypatch):
    """
    Платящий звёздами и платящий переводом приносят одинаково: звёздная цена —
    рублёвая плюс комиссия магазинов, которую они же и заберут.
    """
    monkeypatch.setattr(config, "STARS_FEE_PCT", 30)
    monkeypatch.setattr(config, "STAR_RATE_RUB", 1)
    assert config.stars_for(590) == 767
    assert config.stars_for(1490) == 1937


def test_stars_round_up_not_down(monkeypatch):
    """Округление вниз — это комиссия из своего кармана, каждый раз."""
    monkeypatch.setattr(config, "STARS_FEE_PCT", 30)
    monkeypatch.setattr(config, "STAR_RATE_RUB", 1)
    assert config.stars_for(1) == 2


def test_star_price_follows_the_ruble_price(monkeypatch):
    """Цена одна: правят рубли — звёзды идут следом, без второй правки."""
    monkeypatch.setattr(config, "STARS_FEE_PCT", 0)
    monkeypatch.setattr(config, "STAR_RATE_RUB", 1)
    assert config.PLANS[0].stars == config.PLANS[0].rubles


def test_both_methods_can_be_on_at_once(monkeypatch):
    """У иностранца нет карты, у соседнего сервиса — звёзд. Нужны оба."""
    assert config.pays_with(config.PAYMENT_METHODS[0])
    assert set(config.PAYMENT_METHODS) <= set(config.KNOWN_PAYMENT_METHODS)


def test_price_of_a_method_is_named_in_its_own_units():
    plan = config.PLANS[0]
    assert config.method_price(plan, config.PAYMENT_YOOMONEY) == f"{plan.rubles} ₽"
    assert config.method_price(plan, config.PAYMENT_STARS) == f"{plan.stars} ⭐"


def test_plan_is_found_by_days():
    assert config.plan_by_days(90).stars == config.PLANS[1].stars


def test_unknown_plan_is_not_invented():
    """Счёт на неизвестный тариф выставлять нельзя — цену взять неоткуда."""
    assert config.plan_by_days(7) is None
    assert config.plan_by_days(0) is None
    assert config.plan_by_days(-30) is None


# ── Куда подключается пул ────────────────────────────────────────────────────

async def test_connect_uses_the_dsn_it_was_given(monkeypatch):
    """
    Тесты ходят в свою базу, а не в боевую: они чистят за собой настоящим
    DELETE. Проверяется сам механизм — что аргумент доходит до asyncpg.
    """
    import asyncpg

    import database as database_module

    seen = {}

    async def _fake_pool(**kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(asyncpg, "create_pool", _fake_pool)
    db = database_module.Database()
    await db.connect("postgresql://postgres:postgres@localhost:5432/autoservice_test")

    assert seen["dsn"].endswith("/autoservice_test")
    assert "ssl" not in seen, "локальной базе TLS не навязываем"


async def test_connect_without_a_dsn_takes_the_live_one(monkeypatch):
    import asyncpg

    import config
    import database as database_module

    seen = {}

    async def _fake_pool(**kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(asyncpg, "create_pool", _fake_pool)
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://user:pw@db.example.com:5432/postgres")
    db = database_module.Database()
    await db.connect()

    assert seen["dsn"].endswith("@db.example.com:5432/postgres")
    assert seen["ssl"] == "require", "удалённой базе TLS обязателен"
