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


# ── Время записи ─────────────────────────────────────────────────────────────

from datetime import date, datetime
from zoneinfo import ZoneInfo

from validators import validate_scheduled_at

FREE = {date(2026, 8, 17): [time(9), time(10)]}


def test_scheduled_at_accepts_free_slot():
    got = validate_scheduled_at("2026-08-17 10:00", free=FREE, tz="Europe/Moscow")
    assert got == datetime(2026, 8, 17, 10, 0, tzinfo=ZoneInfo("Europe/Moscow"))


def test_scheduled_at_rejects_slot_outside_schedule():
    """Payload можно прислать в обход формы — форма не защита."""
    with pytest.raises(ValidationError):
        validate_scheduled_at("2026-08-17 03:00", free=FREE, tz="Europe/Moscow")


def test_scheduled_at_rejects_unknown_day():
    with pytest.raises(ValidationError):
        validate_scheduled_at("2026-08-18 09:00", free=FREE, tz="Europe/Moscow")


def test_scheduled_at_rejects_garbage():
    with pytest.raises(ValidationError):
        validate_scheduled_at("завтра", free=FREE, tz="Europe/Moscow")


# ── Обед должен ложиться на сетку записи ─────────────────────────────────────

from validators import validate_lunch_on_grid


def test_lunch_on_grid_accepts_whole_slot():
    """13-14 при часовом шаге — ровно одно окно."""
    lunch = (time(13), time(14))
    assert validate_lunch_on_grid(lunch, work_from=time(9), work_to=time(18), slot_minutes=60) == lunch


def test_lunch_on_grid_accepts_several_whole_slots():
    lunch = (time(13), time(15))
    assert validate_lunch_on_grid(lunch, work_from=time(9), work_to=time(18), slot_minutes=60) == lunch


def test_lunch_on_grid_accepts_none():
    assert validate_lunch_on_grid(None, work_from=time(9), work_to=time(18), slot_minutes=60) is None


def test_lunch_on_grid_rejects_partial_slot():
    """45 минут при часовом шаге всё равно съедали бы целый час."""
    with pytest.raises(ValidationError):
        validate_lunch_on_grid((time(13), time(13, 45)), work_from=time(9), work_to=time(18), slot_minutes=60)


def test_lunch_on_grid_rejects_offset_start():
    """Начало обеда обязано совпасть с началом окна, а не резать его."""
    with pytest.raises(ValidationError):
        validate_lunch_on_grid((time(13, 30), time(14, 30)), work_from=time(9), work_to=time(18), slot_minutes=60)


def test_lunch_on_grid_error_suggests_nearest_range():
    """Ошибка должна считать за управляющего, а не заставлять его считать."""
    with pytest.raises(ValidationError) as exc:
        validate_lunch_on_grid((time(13), time(13, 45)), work_from=time(9), work_to=time(18), slot_minutes=60)
    assert "13:00" in str(exc.value) and "14:00" in str(exc.value)


def test_lunch_on_grid_half_hour_step_allows_half_hour_lunch():
    """При шаге 30 обед в полчаса — законное целое окно."""
    lunch = (time(13), time(13, 30))
    assert validate_lunch_on_grid(lunch, work_from=time(9), work_to=time(18), slot_minutes=30) == lunch


def test_lunch_on_grid_counts_from_work_start():
    """Сетка начинается в 9:20, значит окна 9:20, 10:20 — обед обязан лечь на них."""
    lunch = (time(10, 20), time(11, 20))
    assert validate_lunch_on_grid(lunch, work_from=time(9, 20), work_to=time(18), slot_minutes=60) == lunch
    with pytest.raises(ValidationError):
        validate_lunch_on_grid((time(10), time(11)), work_from=time(9, 20), work_to=time(18), slot_minutes=60)


def test_snap_lunch_widens_to_whole_slots():
    """Раздвигаем наружу: обед короче задуманного отправил бы клиента на обед."""
    from validators import snap_lunch_to_grid
    assert snap_lunch_to_grid(
        (time(13), time(13, 45)), work_from=time(9), work_to=time(18), slot_minutes=60
    ) == (time(13), time(14))


def test_snap_lunch_widens_both_ends():
    from validators import snap_lunch_to_grid
    assert snap_lunch_to_grid(
        (time(13, 20), time(13, 40)), work_from=time(9), work_to=time(18), slot_minutes=60
    ) == (time(13), time(14))


def test_snap_lunch_leaves_aligned_alone():
    from validators import snap_lunch_to_grid
    lunch = (time(13), time(14))
    assert snap_lunch_to_grid(lunch, work_from=time(9), work_to=time(18), slot_minutes=60) == lunch


# ── Обед не должен вылезать за рабочий день ──────────────────────────────────
# Констрейнт chk_schedule_lunch держит обед внутри часов. Раздвигая обед до
# целого окна, легко перешагнуть закрытие — и тогда владелец увидит ошибку
# драйвера вместо текста.

def test_snap_lunch_stops_at_closing():
    """Раздвигать наружу некуда, если наружу — уже нерабочее время."""
    from validators import snap_lunch_to_grid
    assert snap_lunch_to_grid(
        (time(17, 30), time(18)), work_from=time(9, 30), work_to=time(18), slot_minutes=60
    ) == (time(17, 30), time(18))


def test_snap_lunch_near_midnight_does_not_overflow_the_day():
    """Без упора здесь получалось бы time(24, 0) — ValueError, а не отказ."""
    from validators import snap_lunch_to_grid
    assert snap_lunch_to_grid(
        (time(23, 30), time(23, 59)), work_from=time(23), work_to=time(23, 59),
        slot_minutes=60,
    ) == (time(23), time(23, 59))


def test_lunch_on_grid_rejects_lunch_outside_hours():
    """20-21 при рабочем дне до 18:00 ляжет на сетку, но нарушит констрейнт."""
    with pytest.raises(ValidationError) as exc:
        validate_lunch_on_grid(
            (time(20), time(21)), work_from=time(9), work_to=time(18), slot_minutes=60
        )
    assert "18:00" in str(exc.value)


def test_lunch_on_grid_rejects_lunch_before_opening():
    with pytest.raises(ValidationError):
        validate_lunch_on_grid(
            (time(8), time(9)), work_from=time(9), work_to=time(18), slot_minutes=60
        )


def test_lunch_on_grid_accepts_last_partial_slot_at_closing():
    """
    Обед 17:30-18:00 при шаге 60 и начале в 9:30 не делится на окна, но и не
    задевает ни одного: окна кончаются в 16:30. Требовать выравнивания здесь —
    требовать невозможного.
    """
    lunch = (time(17, 30), time(18))
    assert validate_lunch_on_grid(
        lunch, work_from=time(9, 30), work_to=time(18), slot_minutes=60
    ) == lunch


def test_lunch_fits_hours():
    from validators import lunch_fits_hours
    assert lunch_fits_hours((time(13), time(14)), work_from=time(9), work_to=time(18))
    assert lunch_fits_hours(None, work_from=time(9), work_to=time(18))
    assert not lunch_fits_hours((time(13), time(14)), work_from=time(9), work_to=time(12))
    assert not lunch_fits_hours((time(8), time(9)), work_from=time(9), work_to=time(18))
