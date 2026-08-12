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


def test_price_rejects_text():
    with pytest.raises(ValidationError):
        validate_price("дорого")


def test_price_rejects_negative():
    with pytest.raises(ValidationError):
        validate_price("-500")


def test_price_rejects_absurd_value():
    with pytest.raises(ValidationError):
        validate_price("10000001")


def test_price_allows_zero():
    """Ноль — это «бесплатно», осмысленное значение для акции."""
    assert validate_price("0") == 0


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
