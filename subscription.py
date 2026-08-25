"""
subscription.py — срок подписки сервиса.

Модуль чистый: ни базы, ни сети, ни системных часов — «сейчас» приходит
аргументом, как в slots.py. Состояние подписки нигде не хранится отдельным
полем: активность выводится из paid_until. Переключать нечего, значит нечему
и разъехаться.

Единственное, что читается снаружи, — config.SUBSCRIPTION_ENFORCED: введена ли
подписка в действие вообще. Проверка стоит здесь, а не у каждого гейта, именно
чтобы её нельзя было забыть в новом месте.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import config

# За сколько предупреждаем о конце срока. Не то же самое, что TRIAL_DAYS:
# сегодня оба равны пяти, но означают разное — длину пробного периода и
# заблаговременность напоминания
REMIND_LEAD_DAYS = 5

STAGE_5D = "5d"
STAGE_24H = "24h"
STAGE_EXPIRED = "expired"


def is_active(paid_until: datetime | None, now: datetime) -> bool:
    """
    Продаёт ли сервис новое время прямо сейчас.

    Пока подписка не введена в действие, активны все: отключать сервис за
    неоплату, когда оплатить его нечем, — это ломать работающий бот, а не
    зарабатывать. Срок при этом считается и пишется, так что в день включения
    у каждого сервиса уже есть честная дата.
    """
    if not config.SUBSCRIPTION_ENFORCED:
        return True
    return paid_until is not None and paid_until > now


def apply_days(paid_until: datetime | None, now: datetime, days: int) -> datetime:
    """
    Новый срок после начисления или изъятия days дней.

    Положительные дни считаются от большего из «сейчас» и текущего срока:
    заплатил заранее — дни прибавляются к остатку и не сгорают; заплатил после
    просрочки — отсчёт с сегодня, иначе деньги уходят в оплату простоя.

    Отрицательные вычитаются из самого срока, а не из «сейчас». У просроченного
    сервиса «сейчас» уже позже срока, и вычитание из него увело бы дату дальше в
    прошлое, чем отбирали. Возврат обязан отобрать ровно то, что выдал.
    """
    if days < 0:
        return (paid_until or now) + timedelta(days=days)
    base = max(now, paid_until) if paid_until else now
    return base + timedelta(days=days)


def extend(paid_until: datetime | None, now: datetime, days: int) -> datetime:
    """Начисление дней. Синоним apply_days: вызовы с ним уже проверены."""
    return apply_days(paid_until, now, days)


def due_stage(paid_until: datetime | None, now: datetime) -> str | None:
    """
    Какое напоминание причитается прямо сейчас. None — ещё рано или не о чем.

    Отдаётся самая поздняя подошедшая стадия, пропущенные не досылаются: если
    крон молчал сутки и срок успел кончиться, «осталось 24 часа» — уже неправда.

    Пока подписка не введена в действие, не причитается ничего: письмо «срок
    заканчивается» от бота, который ничем не заканчивается, — обман.
    """
    if not config.SUBSCRIPTION_ENFORCED:
        return None
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
