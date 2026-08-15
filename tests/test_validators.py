"""Тесты валидаторов справочника услуг."""

import pytest

from validators import ValidationError, validate_catalog_ids, validate_price, validate_service_title, validate_uuid


def test_title_strips_and_collapses_spaces():
    assert validate_service_title("  Полировка   кузова  ") == "Полировка кузова"


def test_title_too_short_rejected():
    with pytest.raises(ValidationError):
        validate_service_title("П")


def test_title_truncated_to_40():
    # clean_text не бросает ошибку на длинном вводе, а обрезает — здесь так же
    assert len(validate_service_title("П" * 50)) == 40


def test_title_drops_control_chars():
    assert validate_service_title("Поли\x00ровка") == "Полировка"


def test_uuid_accepted():
    value = "3f8b6c1e-9d2a-4b7c-8e1f-0a5b6c7d8e9f"
    assert validate_uuid(value, field="Услуга") == value


def test_uuid_rejects_garbage():
    with pytest.raises(ValidationError):
        validate_uuid("'; DROP TABLE requests--", field="Услуга")


def test_uuid_rejects_empty():
    with pytest.raises(ValidationError):
        validate_uuid(None, field="Услуга")


# ── Цена услуги ──────────────────────────────────────────────────────────────

def test_price_accepts_plain_number():
    assert validate_price("3000") == 3000


def test_price_accepts_spaces_inside():
    """«3 000» — обычный способ записи, придираться к нему нельзя."""
    assert validate_price("3 000") == 3000


def test_price_accepts_currency_suffix():
    assert validate_price("3000 ₽") == 3000
    assert validate_price("3000 руб") == 3000


def test_price_dash_means_not_set():
    assert validate_price("-") is None
    assert validate_price("—") is None


def test_price_accepts_en_dash_as_not_set():
    """Код принимает три вида тире — проверяем все."""
    assert validate_price("–") is None


def test_price_rejects_text():
    with pytest.raises(ValidationError):
        validate_price("дорого")


def test_price_rejects_negative():
    with pytest.raises(ValidationError):
        validate_price("-500")


def test_price_rejects_absurd_value():
    with pytest.raises(ValidationError):
        validate_price("10000001")


def test_price_allows_upper_boundary():
    assert validate_price("10000000") == 10_000_000


def test_price_allows_zero():
    """Ноль — это «бесплатно», осмысленное значение для акции."""
    assert validate_price("0") == 0


def test_price_rejects_unicode_digit_lookalikes():
    """isdigit() пропускает надстрочные цифры, а int() их не понимает."""
    with pytest.raises(ValidationError):
        validate_price("²")


# ── Список выбранных услуг ───────────────────────────────────────────────────

def test_catalog_ids_keeps_order():
    first = "3f8b6c1e-9d2a-4b7c-8e1f-0a5b6c7d8e9f"
    second = "11111111-2222-3333-4444-555555555555"
    assert validate_catalog_ids([first, second]) == [first, second]


def test_catalog_ids_drops_duplicates():
    value = "3f8b6c1e-9d2a-4b7c-8e1f-0a5b6c7d8e9f"
    assert validate_catalog_ids([value, value]) == [value]


def test_catalog_ids_rejects_empty():
    with pytest.raises(ValidationError):
        validate_catalog_ids([])


def test_catalog_ids_rejects_not_a_list():
    with pytest.raises(ValidationError):
        validate_catalog_ids("3f8b6c1e-9d2a-4b7c-8e1f-0a5b6c7d8e9f")


def test_catalog_ids_rejects_garbage_inside():
    with pytest.raises(ValidationError):
        validate_catalog_ids(["3f8b6c1e-9d2a-4b7c-8e1f-0a5b6c7d8e9f", "'; DROP TABLE--"])


# ── Расписание ───────────────────────────────────────────────────────────────

from datetime import time

from validators import (
    validate_capacity,
    validate_horizon,
    validate_lunch,
    validate_time_range,
)


def test_time_range_accepts_short_form():
    """«9-18» — то, как человек пишет часы работы в переписке."""
    assert validate_time_range("9-18", field="Часы") == (time(9), time(18))


def test_time_range_accepts_full_form():
    assert validate_time_range("09:00-18:00", field="Часы") == (time(9), time(18))


def test_time_range_tolerates_spaces_and_dashes():
    assert validate_time_range(" 9:30 — 17:45 ", field="Часы") == (
        time(9, 30), time(17, 45)
    )


def test_time_range_rejects_reversed():
    with pytest.raises(ValidationError):
        validate_time_range("18-9", field="Часы")


def test_time_range_rejects_equal_bounds():
    with pytest.raises(ValidationError):
        validate_time_range("9-9", field="Часы")


def test_time_range_rejects_garbage():
    with pytest.raises(ValidationError):
        validate_time_range("абв", field="Часы")


def test_time_range_rejects_impossible_hour():
    with pytest.raises(ValidationError):
        validate_time_range("9-25", field="Часы")


def test_lunch_dash_clears_it():
    """«-» убирает обед — тот же жест, что и для цены."""
    assert validate_lunch("-") is None


def test_lunch_parses_range():
    assert validate_lunch("13-14") == (time(13), time(14))


def test_capacity_parses_number():
    assert validate_capacity("3") == 3


def test_capacity_rejects_zero():
    with pytest.raises(ValidationError):
        validate_capacity("0")


def test_capacity_rejects_above_limit():
    with pytest.raises(ValidationError):
        validate_capacity("21")


def test_horizon_parses_number():
    assert validate_horizon("14") == 14


def test_horizon_rejects_above_limit():
    with pytest.raises(ValidationError):
        validate_horizon("61")
