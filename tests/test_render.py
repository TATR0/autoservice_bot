"""Тесты сборки текстов. Базы не требуют."""

from render import price_label, titled_price


def test_price_label_shows_from_prefix():
    """Цена всегда ориентировочная: точную сервис назовёт после осмотра."""
    assert price_label(3000) == "от 3 000 ₽"


def test_price_label_groups_thousands():
    assert price_label(1234567) == "от 1 234 567 ₽"


def test_price_label_without_price():
    """Цены нет — показывать нечего, заглушку не выдумываем."""
    assert price_label(None) == ""


def test_price_label_zero_is_a_price():
    assert price_label(0) == "от 0 ₽"


def test_titled_price_appends_price():
    assert titled_price("Полировка", 3000) == "Полировка — от 3 000 ₽"


def test_titled_price_without_price_is_just_title():
    """Без цены не остаётся висящего тире."""
    assert titled_price("Полировка", None) == "Полировка"
