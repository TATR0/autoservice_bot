"""
slots.py — нарезка свободных окон записи.

Функция чистая: ни базы, ни сети, ни системных часов. Всё, что влияет на
результат, приходит аргументами — поэтому поведение проверяется обычными
тестами, а не через поднятое приложение.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
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


def free_slots(
    schedule: Mapping,
    tz: str,
    now: datetime,
    taken: Mapping[datetime, int],
) -> dict[date, list[time]]:
    """
    Свободные окна записи по дням, в локальном времени сервиса.

    День попадает в результат, только если в нём осталось хотя бы одно окно:
    пустой список означал бы «день доступен», а он не доступен.
    """
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    workdays = set(schedule["weekdays"])
    step = timedelta(minutes=schedule["slot_minutes"])
    capacity = schedule["capacity"]
    lunch_from, lunch_to = schedule["lunch_from"], schedule["lunch_to"]

    result: dict[date, list[time]] = {}
    for offset in range(schedule["horizon_days"]):
        day = today + timedelta(days=offset)
        if offset > 0 and day.isoweekday() not in workdays:
            continue

        opens = datetime.combine(day, schedule["work_from"])
        closes = datetime.combine(day, schedule["work_to"])
        free: list[time] = []

        cursor = opens
        while cursor + step <= closes:
            start, end = cursor.time(), (cursor + step).time()
            cursor += step

            # Окно, задевающее обед хотя бы краем, продавать нельзя
            if lunch_from and lunch_to and start < lunch_to and end > lunch_from:
                continue

            moment = _localize(day, start, zone)
            if moment is None or moment < now:
                continue
            if taken.get(moment, 0) >= capacity:
                continue
            free.append(start)

        if free:
            result[day] = free
    return result
