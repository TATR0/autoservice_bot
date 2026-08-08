"""Тесты валидаторов справочника услуг."""

import pytest

from validators import ValidationError, validate_service_title, validate_uuid


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
