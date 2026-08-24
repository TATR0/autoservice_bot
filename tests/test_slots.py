"""Тесты нарезки окон. Базы не требуют — функция чистая."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from slots import free_slots

MSK = ZoneInfo("Europe/Moscow")

# Пятница
NOW = datetime(2026, 8, 14, 8, 0, tzinfo=MSK)


def schedule(**overrides):
    base = {
        "work_from": time(9),
        "work_to": time(18),
        "slot_minutes": 60,
        "lunch_from": None,
        "lunch_to": None,
        "weekdays": [1, 2, 3, 4, 5],
        "horizon_days": 1,
        "capacity": 1,
    }
    base.update(overrides)
    return base


def test_slots_are_cut_by_step():
    result = free_slots(schedule(), "Europe/Moscow", NOW, {})
    assert result[date(2026, 8, 14)] == [
        time(9), time(10), time(11), time(12), time(13),
        time(14), time(15), time(16), time(17),
    ]


def test_slot_does_not_spill_past_closing():
    """Окно, не помещающееся целиком до конца дня, не предлагается."""
    result = free_slots(schedule(slot_minutes=120, work_to=time(14)), "Europe/Moscow", NOW, {})
    assert result[date(2026, 8, 14)] == [time(9), time(11)]


def test_lunch_removes_overlapping_slots():
    result = free_slots(
        schedule(lunch_from=time(13), lunch_to=time(14)), "Europe/Moscow", NOW, {}
    )
    assert time(13) not in result[date(2026, 8, 14)]
    assert time(12) in result[date(2026, 8, 14)]
    assert time(14) in result[date(2026, 8, 14)]


def test_non_working_days_are_skipped():
    """15 августа 2026 — суббота."""
    result = free_slots(schedule(horizon_days=3), "Europe/Moscow", NOW, {})
    assert date(2026, 8, 15) not in result
    assert date(2026, 8, 16) not in result


def test_past_slots_of_today_are_not_offered():
    now = datetime(2026, 8, 14, 11, 30, tzinfo=MSK)
    result = free_slots(schedule(), "Europe/Moscow", now, {})
    assert result[date(2026, 8, 14)][0] == time(12)


def test_horizon_limits_days():
    result = free_slots(schedule(horizon_days=5), "Europe/Moscow", NOW, {})
    assert max(result) == date(2026, 8, 18)


def test_taken_slot_disappears():
    taken = {datetime(2026, 8, 14, 9, 0, tzinfo=MSK): 1}
    result = free_slots(schedule(), "Europe/Moscow", NOW, taken)
    assert time(9) not in result[date(2026, 8, 14)]


def test_slot_lives_until_last_place_is_gone():
    taken = {datetime(2026, 8, 14, 9, 0, tzinfo=MSK): 2}
    result = free_slots(schedule(capacity=3), "Europe/Moscow", NOW, taken)
    assert time(9) in result[date(2026, 8, 14)]


def test_day_without_free_slots_is_absent():
    taken = {
        datetime(2026, 8, 14, hour, 0, tzinfo=MSK): 1 for hour in range(9, 18)
    }
    result = free_slots(schedule(), "Europe/Moscow", NOW, taken)
    assert date(2026, 8, 14) not in result


def test_day_is_computed_in_service_timezone():
    """В 20:00 по Москве во Владивостоке уже следующий день."""
    now = datetime(2026, 8, 13, 20, 0, tzinfo=MSK)
    # В Москве ещё 13-е, но все слоты (9-17) уже прошли
    assert list(free_slots(schedule(horizon_days=1), "Europe/Moscow", now, {})) == []
    # Во Владивостоке уже 14-е (сдвиг на 7 часов), и слоты (9-17) ещё впереди
    assert list(free_slots(schedule(horizon_days=1), "Asia/Vladivostok", now, {})) == [
        date(2026, 8, 14)
    ]


def test_today_is_skipped_when_it_is_not_a_working_day():
    """Суббота закрыта и сегодня: закрытый день не должен выглядеть доступным."""
    saturday = datetime(2026, 8, 15, 8, 0, tzinfo=MSK)
    assert free_slots(schedule(), "Europe/Moscow", saturday, {}) == {}
