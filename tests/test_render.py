"""Тесты сборки текстов. Базы не требуют."""

from render import price_label


def test_price_label_shows_from_prefix():
    """Цена всегда ориентировочная: точную сервис назовёт после осмотра."""
    assert price_label(3000) == "от 3 000 ₽"


def test_price_label_groups_thousands():
    assert price_label(1234567) == "от 1 234 567 ₽"


def test_price_label_without_price():
    assert price_label(None) == "цена по запросу"


def test_price_label_zero_is_a_price():
    assert price_label(0) == "от 0 ₽"
