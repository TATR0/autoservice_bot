"""Тесты нарезки окон. Базы не требуют — функция чистая."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from slots import booking_end, free_slots, peak_load, windows_needed

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


def _busy(hour, hours=1, day=14):
    """Занятость живой заявки: с какого момента и до какого."""
    start = datetime(2026, 8, day, hour, 0, tzinfo=MSK)
    return start, start + timedelta(hours=hours)


def test_slots_are_cut_by_step():
    result = free_slots(schedule(), "Europe/Moscow", NOW, [])
    assert result[date(2026, 8, 14)] == [
        time(9), time(10), time(11), time(12), time(13),
        time(14), time(15), time(16), time(17),
    ]


def test_slot_does_not_spill_past_closing():
    """Окно, не помещающееся целиком до конца дня, не предлагается."""
    result = free_slots(schedule(slot_minutes=120, work_to=time(14)), "Europe/Moscow", NOW, [])
    assert result[date(2026, 8, 14)] == [time(9), time(11)]


def test_lunch_removes_overlapping_slots():
    result = free_slots(
        schedule(lunch_from=time(13), lunch_to=time(14)), "Europe/Moscow", NOW, []
    )
    assert time(13) not in result[date(2026, 8, 14)]
    assert time(12) in result[date(2026, 8, 14)]
    assert time(14) in result[date(2026, 8, 14)]


def test_non_working_days_are_skipped():
    """15 августа 2026 — суббота."""
    result = free_slots(schedule(horizon_days=3), "Europe/Moscow", NOW, [])
    assert date(2026, 8, 15) not in result
    assert date(2026, 8, 16) not in result


def test_past_slots_of_today_are_not_offered():
    now = datetime(2026, 8, 14, 11, 30, tzinfo=MSK)
    result = free_slots(schedule(), "Europe/Moscow", now, [])
    assert result[date(2026, 8, 14)][0] == time(12)


def test_horizon_limits_days():
    result = free_slots(schedule(horizon_days=5), "Europe/Moscow", NOW, [])
    assert max(result) == date(2026, 8, 18)


def test_taken_slot_disappears():
    taken = [_busy(9)]
    result = free_slots(schedule(), "Europe/Moscow", NOW, taken)
    assert time(9) not in result[date(2026, 8, 14)]


def test_slot_lives_until_last_place_is_gone():
    taken = [_busy(9), _busy(9)]
    result = free_slots(schedule(capacity=3), "Europe/Moscow", NOW, taken)
    assert time(9) in result[date(2026, 8, 14)]


def test_day_without_free_slots_is_absent():
    taken = [_busy(hour) for hour in range(9, 18)]
    result = free_slots(schedule(), "Europe/Moscow", NOW, taken)
    assert date(2026, 8, 14) not in result


def test_day_is_computed_in_service_timezone():
    """В 20:00 по Москве во Владивостоке уже следующий день."""
    now = datetime(2026, 8, 13, 20, 0, tzinfo=MSK)
    # В Москве ещё 13-е, но все слоты (9-17) уже прошли
    assert list(free_slots(schedule(horizon_days=1), "Europe/Moscow", now, [])) == []
    # Во Владивостоке уже 14-е (сдвиг на 7 часов), и слоты (9-17) ещё впереди
    assert list(free_slots(schedule(horizon_days=1), "Asia/Vladivostok", now, [])) == [
        date(2026, 8, 14)
    ]


def test_today_is_skipped_when_it_is_not_a_working_day():
    """Суббота закрыта и сегодня: закрытый день не должен выглядеть доступным."""
    saturday = datetime(2026, 8, 15, 8, 0, tzinfo=MSK)
    assert free_slots(schedule(), "Europe/Moscow", saturday, []) == {}


# ── Длительность работы ──────────────────────────────────────────────────────
# Запись длиннее окна занимает несколько окон подряд. Обед работу
# приостанавливает, а не запрещает: иначе шестичасовую керамику при обеде
# посреди девятичасового дня нельзя было бы поставить вообще.

FRIDAY = date(2026, 8, 14)


def test_windows_needed_rounds_up_to_whole_windows():
    assert windows_needed(None, 60) == 1
    assert windows_needed(0, 60) == 1
    assert windows_needed(60, 60) == 1
    assert windows_needed(90, 60) == 2
    assert windows_needed(240, 60) == 4


def test_long_job_is_offered_only_where_it_fits_before_closing():
    result = free_slots(schedule(), "Europe/Moscow", NOW, [], windows=4)
    assert result[FRIDAY][0] == time(9)
    assert result[FRIDAY][-1] == time(14), "с 15:00 четыре часа до 18:00 не помещаются"


def test_long_job_runs_through_lunch():
    """Шесть часов при обеде 13–14 и дне 9–18: начало в 9, 10 или 11."""
    result = free_slots(
        schedule(lunch_from=time(13), lunch_to=time(14)), "Europe/Moscow", NOW, [], windows=6
    )
    assert result[FRIDAY] == [time(9), time(10), time(11)]


def test_long_job_needs_every_window_free():
    """Бокс занят в 12:00 — четырёхчасовую работу нельзя начать с 9 до 12."""
    result = free_slots(schedule(), "Europe/Moscow", NOW, [_busy(12)], windows=4)
    assert result[FRIDAY] == [time(13), time(14)]


def test_long_booking_blocks_the_following_windows():
    """Ради этого всё и делалось: запись 10:00–14:00 держит бокс до 14:00."""
    result = free_slots(schedule(), "Europe/Moscow", NOW, [_busy(10, hours=4)])
    assert result[FRIDAY] == [time(9), time(14), time(15), time(16), time(17)]


def test_second_box_takes_an_overlapping_long_job():
    result = free_slots(
        schedule(capacity=2), "Europe/Moscow", NOW, [_busy(10, hours=4)], windows=4
    )
    assert time(10) in result[FRIDAY]


def test_job_longer_than_a_day_is_never_offered():
    assert free_slots(schedule(), "Europe/Moscow", NOW, [], windows=10) == {}


def test_booking_end_skips_lunch():
    sched = schedule(lunch_from=time(13), lunch_to=time(14))
    start = datetime(2026, 8, 14, 11, 0, tzinfo=MSK)
    assert booking_end(sched, "Europe/Moscow", start, 4) == datetime(
        2026, 8, 14, 16, 0, tzinfo=MSK
    )


def test_booking_end_is_none_when_job_does_not_fit():
    start = datetime(2026, 8, 14, 16, 0, tzinfo=MSK)
    assert booking_end(schedule(), "Europe/Moscow", start, 4) is None


def test_peak_load_counts_simultaneous_jobs_only():
    busy = [_busy(9, hours=2), _busy(10, hours=3), _busy(14)]
    start = datetime(2026, 8, 14, 9, 0, tzinfo=MSK)
    assert peak_load(busy, start, start + timedelta(hours=4)) == 2
    assert peak_load(busy, start + timedelta(hours=4), start + timedelta(hours=5)) == 0


def test_peak_load_touching_ends_do_not_overlap():
    """Работа, кончившаяся в 13:00, не мешает начатой в 13:00."""
    start = datetime(2026, 8, 14, 13, 0, tzinfo=MSK)
    assert peak_load([_busy(9, hours=4)], start, start + timedelta(hours=1)) == 0


def test_windows_per_day_leaves_out_lunch():
    from slots import windows_per_day

    assert windows_per_day(schedule()) == 9
    assert windows_per_day(schedule(lunch_from=time(13), lunch_to=time(14))) == 8
