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
