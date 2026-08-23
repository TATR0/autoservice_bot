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


# ── Продление ────────────────────────────────────────────────────────────────

import uuid


async def test_extension_sets_the_deadline_and_logs_it(service):
    """Срок и журнал меняются вместе — иначе на «почему» отвечать нечем."""
    paid_until = await db.extend_subscription(service, days=30, granted_by=42)
    assert paid_until is not None

    async with db.pool.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT paid_until FROM services WHERE idservice=$1", service
        )
        logged = await conn.fetchrow(
            "SELECT * FROM subscription_payments WHERE idservice=$1"
            " ORDER BY createdate DESC LIMIT 1",
            service,
        )
    assert stored == paid_until
    assert logged["days"] == 30
    assert logged["paid_until"] == paid_until
    assert logged["source"] == "manual"
    assert logged["granted_by"] == 42


async def test_second_extension_counts_from_the_first(service):
    """Два продления подряд не должны считать от одного и того же остатка."""
    first = await db.extend_subscription(service, days=30)
    second = await db.extend_subscription(service, days=30)
    assert second == first + timedelta(days=30)


async def test_extension_of_a_missing_service_changes_nothing(service):
    assert await db.extend_subscription(str(uuid.uuid4()), days=30) is None


# ── Пробный период ───────────────────────────────────────────────────────────

import config
import subscription


async def test_new_service_starts_with_a_trial(service):
    """Управляющий должен увидеть, как это работает, прежде чем платить."""
    svc = await db.get_service(service)
    now = datetime.now(timezone.utc)
    assert subscription.is_active(svc["paid_until"], now)
    left = svc["paid_until"] - now
    assert timedelta(days=config.TRIAL_DAYS - 1) < left <= timedelta(days=config.TRIAL_DAYS)


async def test_trial_is_written_to_the_journal(service):
    """Журнал отвечает на «почему у сервиса такой срок» — в том числе про триал."""
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM subscription_payments WHERE idservice=$1", service
        )
    assert row["source"] == "trial"
    assert row["days"] == config.TRIAL_DAYS


async def test_trial_does_not_trigger_the_five_day_reminder(service):
    """
    Триал длится ровно столько, за сколько мы предупреждаем.

    Без предзанятой отметки управляющий получил бы «осталось 5 дней» в секунду
    регистрации, сразу после приветствия, где эта дата уже названа.
    """
    svc = await db.get_service(service)
    claimed = await db.claim_reminder(service, svc["paid_until"], subscription.STAGE_5D)
    assert claimed is False


# ── Поиск ────────────────────────────────────────────────────────────────────


async def _expire(idservice: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE services SET paid_until=now() - interval '1 day' WHERE idservice=$1",
            idservice,
        )


async def test_paid_service_is_found_by_city(service):
    svc = await db.get_service(service)
    found = await db.get_services_by_city(svc["city"])
    assert any(str(row["idservice"]) == service for row in found)


async def test_expired_service_disappears_from_search(service):
    svc = await db.get_service(service)
    await _expire(service)
    found = await db.get_services_by_city(svc["city"])
    assert all(str(row["idservice"]) != service for row in found)


async def test_expired_service_is_still_available_to_its_owner(service):
    """
    Гейт стоит на продаже времени, а не на существовании сервиса.

    Спрячь сервис от get_service — и управляющий потеряет кабинет вместе с
    возможностью заплатить. Ровно та ошибка, из-за которой отвергнут вариант
    с idrecstatus = -1.
    """
    await _expire(service)
    assert await db.get_service(service) is not None
    owned = await db.get_owned_services(999_000_001)
    assert any(str(row["idservice"]) == service for row in owned)


# ── Напоминания ──────────────────────────────────────────────────────────────


async def test_service_close_to_expiry_becomes_a_candidate(service):
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE services SET paid_until=now() + interval '2 days' WHERE idservice=$1",
            service,
        )
    candidates = await db.services_for_reminders()
    assert any(str(row["idservice"]) == service for row in candidates)


async def test_service_with_a_distant_deadline_is_not_a_candidate(service):
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE services SET paid_until=now() + interval '90 days' WHERE idservice=$1",
            service,
        )
    candidates = await db.services_for_reminders()
    assert all(str(row["idservice"]) != service for row in candidates)


async def test_long_expired_service_is_not_a_candidate(service):
    """Своё он уже получил, перебирать его каждый час незачем."""
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE services SET paid_until=now() - interval '60 days' WHERE idservice=$1",
            service,
        )
    candidates = await db.services_for_reminders()
    assert all(str(row["idservice"]) != service for row in candidates)


async def test_reminder_is_claimed_by_the_first_caller_only(service):
    """
    Главное свойство всей конструкции: повторный тик, два тика внахлёст и
    перезапуск посреди рассылки дают одно письмо.
    """
    moment = datetime.now(timezone.utc) + timedelta(days=1)
    assert await db.claim_reminder(service, moment, "24h") is True
    assert await db.claim_reminder(service, moment, "24h") is False


async def test_new_deadline_can_be_claimed_again(service):
    """После продления срок другой — напоминания по нему свои."""
    moment = datetime.now(timezone.utc) + timedelta(days=1)
    await db.claim_reminder(service, moment, "24h")
    assert await db.claim_reminder(service, moment + timedelta(days=30), "24h") is True
