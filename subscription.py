"""
subscription.py — срок подписки сервиса.

Модуль чистый: ни базы, ни сети, ни системных часов — «сейчас» приходит
аргументом, как в slots.py. Состояние подписки нигде не хранится отдельным
полем: активность выводится из paid_until. Переключать нечего, значит нечему
и разъехаться.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# За сколько предупреждаем о конце срока. Не то же самое, что TRIAL_DAYS:
# сегодня оба равны пяти, но означают разное — длину пробного периода и
# заблаговременность напоминания
REMIND_LEAD_DAYS = 5

STAGE_5D = "5d"
STAGE_24H = "24h"
STAGE_EXPIRED = "expired"


def is_active(paid_until: datetime | None, now: datetime) -> bool:
    """Продаёт ли сервис новое время прямо сейчас."""
    return paid_until is not None and paid_until > now


def extend(paid_until: datetime | None, now: datetime, days: int) -> datetime:
    """
    Новый срок после продления на days дней.

    Отсчёт от большего из «сейчас» и текущего срока: заплатил заранее — дни
    прибавляются к остатку и не сгорают; заплатил после просрочки — отсчёт с
    сегодня, иначе деньги уходят в оплату уже прошедшего простоя.
    """
    base = max(now, paid_until) if paid_until else now
    return base + timedelta(days=days)


def due_stage(paid_until: datetime | None, now: datetime) -> str | None:
    """
    Какое напоминание причитается прямо сейчас. None — ещё рано или не о чем.

    Отдаётся самая поздняя подошедшая стадия, пропущенные не досылаются: если
    крон молчал сутки и срок успел кончиться, «осталось 24 часа» — уже неправда.
    """
    if paid_until is None:
        return None
    left = paid_until - now
    if left <= timedelta(0):
        return STAGE_EXPIRED
    if left <= timedelta(hours=24):
        return STAGE_24H
    if left <= timedelta(days=REMIND_LEAD_DAYS):
        return STAGE_5D
    return None
