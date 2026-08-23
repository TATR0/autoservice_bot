"""
Схема подписки. Идут против настоящей базы.

Проверяются те свойства схемы, на которые опирается код выше: тип колонки
срока, однократность напоминания и запрет продления «на ноль дней».
"""

from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from database import db

pytestmark = pytest.mark.asyncio

LATER = datetime(2027, 1, 1, tzinfo=timezone.utc)


async def test_paid_until_keeps_the_time_of_day(service):
    """
    Колонка обязана быть timestamptz.

    С date «за 24 часа» превратилось бы в «в полночь накануне по серверному
    времени», а у сервисов свои зоны.
    """
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE services SET paid_until=$2 WHERE idservice=$1", service, LATER
        )
        stored = await conn.fetchval(
            "SELECT paid_until FROM services WHERE idservice=$1", service
        )
    assert stored == LATER


async def test_reminder_is_claimed_once(service):
    """Ключ отметки — сервис, срок и стадия. Второй такой же не пройдёт."""
    async with db.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO subscription_reminders (idservice, paid_until, stage)"
            " VALUES ($1,$2,'24h')",
            service, LATER,
        )
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await conn.execute(
                "INSERT INTO subscription_reminders (idservice, paid_until, stage)"
                " VALUES ($1,$2,'24h')",
                service, LATER,
            )


async def test_new_deadline_gets_its_own_reminders(service):
    """После продления срок другой — значит, напоминания по нему свои."""
    async with db.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO subscription_reminders (idservice, paid_until, stage)"
            " VALUES ($1,$2,'24h')",
            service, LATER,
        )
        await conn.execute(  # не бросает
            "INSERT INTO subscription_reminders (idservice, paid_until, stage)"
            " VALUES ($1,$2,'24h')",
            service, LATER + timedelta(days=30),
        )


async def test_payment_of_zero_days_is_rejected(service):
    """Продление, ничего не продлевающее, — это ошибка вызывающего."""
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                "INSERT INTO subscription_payments (idservice, days, paid_until)"
                " VALUES ($1, 0, $2)",
                service, LATER,
            )


async def test_same_external_payment_lands_once(service):
    """
    Ради этого журнал заводится сейчас, а не вместе с провайдером: повторный
    вебхук того же платежа не должен продлить подписку дважды.
    """
    async with db.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO subscription_payments"
            " (idservice, days, paid_until, source, external_id)"
            " VALUES ($1, 30, $2, 'yookassa', 'pay-1')",
            service, LATER,
        )
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await conn.execute(
                "INSERT INTO subscription_payments"
                " (idservice, days, paid_until, source, external_id)"
                " VALUES ($1, 30, $2, 'yookassa', 'pay-1')",
                service, LATER,
            )


async def test_manual_payments_do_not_collide(service):
    """У ручных продлений external_id пуст — индекс частичный и их не трогает."""
    async with db.pool.acquire() as conn:
        for _ in range(2):
            await conn.execute(
                "INSERT INTO subscription_payments (idservice, days, paid_until)"
                " VALUES ($1, 30, $2)",
                service, LATER,
            )
