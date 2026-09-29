"""
slots.py — нарезка свободных окон записи.

Функции чистые: ни базы, ни сети, ни системных часов. Всё, что влияет на
результат, приходит аргументами — поэтому поведение проверяется обычными
тестами, а не через поднятое приложение.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from math import ceil
from zoneinfo import ZoneInfo


def _localize(day: date, moment: time, zone: ZoneInfo) -> datetime | None:
    """
    Локальное время в момент времени. None — такого времени не существует.

    При переходе на летнее время час пропадает целиком; предлагать запись на
    несуществующий час нельзя. Задвоенный час берём первый (fold=0).
    """
    naive = datetime.combine(day, moment)
    aware = naive.replace(tzinfo=zone)
    # Несуществующее время переживает round-trip через UTC с другим значением
    if aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
        return None
    return aware


def windows_needed(minutes: int | None, slot_minutes: int) -> int:
    """
    Сколько окон подряд займёт работа. Длительность не указана — одно окно,
    как до того, как длительность появилась.
    """
    if not minutes or minutes <= 0:
        return 1
    return max(1, ceil(minutes / slot_minutes))


def _working_windows(schedule: Mapping, day: date) -> list[tuple[datetime, datetime]]:
    """
    Рабочие окна дня по порядку, в наивном локальном времени.

    Окно, задевающее обед хотя бы краем, не рабочее. Работа, идущая через
    обед, перескакивает его: окна до и после обеда для неё соседние.
    """
    step = timedelta(minutes=schedule["slot_minutes"])
    lunch_from, lunch_to = schedule["lunch_from"], schedule["lunch_to"]
    closes = datetime.combine(day, schedule["work_to"])

    windows = []
    cursor = datetime.combine(day, schedule["work_from"])
    while cursor + step <= closes:
        start, end = cursor, cursor + step
        cursor = end
        if lunch_from and lunch_to and start.time() < lunch_to and end.time() > lunch_from:
            continue
        windows.append((start, end))
    return windows


def windows_per_day(schedule: Mapping) -> int:
    """
    Сколько рабочих окон в дне. Часы и обед у всех рабочих дней одни, поэтому
    число не зависит от даты; дата ниже — любая.
    """
    return len(_working_windows(schedule, date(2000, 1, 3)))


def too_long_for_day(schedule: Mapping | None, minutes: int | None) -> bool:
    """Работа не влезает ни в один рабочий день — онлайн её не записать."""
    if schedule is None:
        return False
    return windows_needed(minutes, schedule["slot_minutes"]) > windows_per_day(schedule)


def peak_load(
    busy: Sequence[tuple[datetime, datetime]], start: datetime, end: datetime
) -> int:
    """
    Сколько работ идёт одновременно в самый загруженный момент отрезка.

    Не просто «сколько задевают отрезок»: две работы 9–11 и 11–13 задевают
    отрезок 9–13 обе, но бокс им нужен один. Пик достигается в начале отрезка
    или в начале одной из работ, поэтому проверять хватает этих точек.
    """
    inside = [item for item in busy if item[0] < end and item[1] > start]
    points = [start] + [taken_from for taken_from, _ in inside if taken_from > start]
    return max(
        (sum(1 for taken_from, taken_to in inside if taken_from <= point < taken_to)
         for point in points),
        default=0,
    )


def free_slots(
    schedule: Mapping,
    tz: str,
    now: datetime,
    busy: Sequence[tuple[datetime, datetime]],
    windows: int = 1,
) -> dict[date, list[time]]:
    """
    Свободные начала записи по дням, в локальном времени сервиса.

    busy — занятость живых заявок: с какого момента и до какого. Начало
    свободно, если каждое из windows окон подряд, начиная с него, занято
    меньше, чем вместимость сервиса.

    День попадает в результат, только если в нём осталось хотя бы одно окно:
    пустой список означал бы «день доступен», а он не доступен.
    """
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    workdays = set(schedule["weekdays"])
    capacity = schedule["capacity"]

    result: dict[date, list[time]] = {}
    for offset in range(schedule["horizon_days"]):
        day = today + timedelta(days=offset)
        if day.isoweekday() not in workdays:
            continue

        local = [
            (_localize(day, start.time(), zone), _localize(day, end.time(), zone))
            for start, end in _working_windows(schedule, day)
        ]
        free: list[time] = []
        for first in range(len(local) - windows + 1):
            chunk = local[first:first + windows]
            # Несуществующий при переходе на летнее время час продавать нельзя
            if any(start is None or end is None for start, end in chunk):
                continue
            if chunk[0][0] < now:
                continue
            if any(peak_load(busy, start, end) >= capacity for start, end in chunk):
                continue
            free.append(chunk[0][0].astimezone(zone).time())

        if free:
            result[day] = free
    return result


def booking_end(
    schedule: Mapping, tz: str, start: datetime, windows: int
) -> datetime | None:
    """
    Когда кончится работа, начатая в start. None — не помещается в день.

    Конец считается по окнам, а не прибавлением часов: обед работу
    приостанавливает, и четыре часа с 11:00 при обеде 13–14 кончаются в 16:00.
    """
    zone = ZoneInfo(tz)
    local_start = start.astimezone(zone)
    day = local_start.date()
    days_windows = _working_windows(schedule, day)
    starts = [begin.time() for begin, _ in days_windows]
    moment = local_start.time().replace(tzinfo=None)
    if moment not in starts:
        return None
    first = starts.index(moment)
    chunk = days_windows[first:first + windows]
    if len(chunk) < windows:
        return None
    return _localize(day, chunk[-1][1].time(), zone)
