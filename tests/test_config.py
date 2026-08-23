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
