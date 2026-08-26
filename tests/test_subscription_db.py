"""
Схема подписки. Идут против настоящей базы.

Проверяются те свойства схемы, на которые опирается код выше: тип колонки
срока, однократность напоминания и запрет продления «на ноль дней».
"""

import asyncio
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


async def test_extension_reads_the_deadline_after_the_lock_not_before(service):
    """
    Два продления внахлёст — так ведут себя вебхуки платёжного провайдера.

    Повтор доставки, ретрай на таймауте, две попытки оплаты подряд: дубли
    приходят одновременно, а не друг за другом. Без FOR UPDATE обе транзакции
    читают один и тот же paid_until, считают от него и пишут одинаковый срок —
    заплачено дважды, дни начислены одни. Виноватых не найти: в журнале две
    честные строки по тридцать дней.

    Гонку здесь не разыгрывают, а ставят: строка держится заблокированной, срок
    под ней меняется. Продление, читающее с FOR UPDATE, дождётся коммита и
    увидит новое значение. Читающее без блокировки — уже прочло старое и
    посчитает от него, сколько бы потом ни ждало своей очереди на запись.
    """
    before = (await db.get_service(service))["paid_until"]
    bumped = before + timedelta(days=100)

    # Держим строку отдельным соединением, мимо пула: иначе конкурент мог бы
    # ждать не блокировку, а свободное соединение, и тест доказывал бы не то
    holder = await asyncpg.connect(config.DATABASE_URL)
    try:
        tx = holder.transaction()
        await tx.start()
        await holder.fetchval(
            "SELECT paid_until FROM services WHERE idservice=$1 FOR UPDATE", service
        )

        rival = asyncio.create_task(db.extend_subscription(service, days=30))
        # Дать конкуренту дойти до строки и упереться. Раз он не завершился,
        # пока строка заперта, — значит, до неё он уже добрался
        await asyncio.sleep(1)
        assert not rival.done(), "продление обязано ждать, а не писать поверх"

        await holder.execute(
            "UPDATE services SET paid_until=$2 WHERE idservice=$1", service, bumped
        )
        await tx.commit()
        result = await asyncio.wait_for(rival, 10)
    finally:
        await holder.close()

    assert result == bumped + timedelta(days=30), "срок прочитан до блокировки"
    assert (await db.get_service(service))["paid_until"] == result


async def test_extension_of_a_missing_service_changes_nothing(service):
    assert await db.extend_subscription(str(uuid.uuid4()), days=30) is None


async def test_the_same_charge_is_credited_once(service):
    """
    Главное свойство приёма денег: Telegram доставляет апдейт повторно.

    Без занятия права до начисления второй раз либо начислит дни заново, либо
    уронит транзакцию нарушением уникальности — и то и другое видно клиенту.
    """
    first = await db.extend_subscription(
        service, days=30, source="stars", external_id="charge_1", granted_by=1
    )
    second = await db.extend_subscription(
        service, days=30, source="stars", external_id="charge_1", granted_by=1
    )
    assert second == first, "повтор обязан вернуть тот же срок, а не новый"

    async with db.pool.acquire() as conn:
        rows = await conn.fetchval(
            "SELECT count(*) FROM subscription_payments"
            " WHERE idservice=$1 AND source='stars'",
            service,
        )
    assert rows == 1, "в журнале должен остаться один платёж"


async def test_different_charges_are_credited_separately(service):
    first = await db.extend_subscription(
        service, days=30, source="stars", external_id="charge_a", granted_by=1
    )
    second = await db.extend_subscription(
        service, days=30, source="stars", external_id="charge_b", granted_by=1
    )
    assert second == first + timedelta(days=30)


async def test_negative_days_shorten_the_term(service):
    before = (await db.get_service(service))["paid_until"]
    after = await db.extend_subscription(service, days=-10)
    assert after == before - timedelta(days=10)


async def test_shortening_is_written_to_the_journal(service):
    await db.extend_subscription(service, days=-10)
    async with db.pool.acquire() as conn:
        days = await conn.fetchval(
            "SELECT days FROM subscription_payments"
            " WHERE idservice=$1 AND days < 0",
            service,
        )
    assert days == -10


# ── Журнал платежей ──────────────────────────────────────────────────────────


async def test_journal_remembers_a_refund(service):
    """Возврат помечает ту строку, которую отменяет, а не заводит новую."""
    await db.extend_subscription(service, days=30)
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT idpayment, refunded_at FROM subscription_payments"
            " WHERE idservice=$1 AND source='manual'",
            service,
        )
        assert row["refunded_at"] is None, "свежий платёж возвращённым не бывает"
        await conn.execute(
            "UPDATE subscription_payments SET refunded_at=now() WHERE idpayment=$1",
            row["idpayment"],
        )
        stamped = await conn.fetchval(
            "SELECT refunded_at FROM subscription_payments WHERE idpayment=$1",
            row["idpayment"],
        )
    assert stamped is not None


async def test_journal_accepts_taken_away_days(service):
    """
    Ручное укорачивание — самостоятельное событие, и в журнале оно видно.

    Констрейнт до этой задачи требовал days > 0 и такую строку не пропускал.
    """
    svc = await db.get_service(service)
    async with db.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO subscription_payments (idservice, days, paid_until, source)"
            " VALUES ($1,-30,$2,'manual')",
            service, svc["paid_until"],
        )
        taken = await conn.fetchval(
            "SELECT days FROM subscription_payments"
            " WHERE idservice=$1 AND days < 0",
            service,
        )
    assert taken == -30


async def test_journal_still_rejects_zero_days(service):
    """Начисление на ноль дней ничего не значит — это опечатка, а не операция."""
    svc = await db.get_service(service)
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                "INSERT INTO subscription_payments (idservice, days, paid_until, source)"
                " VALUES ($1,0,$2,'manual')",
                service, svc["paid_until"],
            )


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


async def test_a_longer_trial_keeps_its_five_day_warning(db_ready, monkeypatch):
    """
    Заглушка выше держится на равенстве TRIAL_DAYS == REMIND_LEAD_DAYS.

    Удлини триал — и она погасила бы настоящее предупреждение: управляющий
    досидел бы пробный период и узнал о конце только письмом «истекла», уже
    после отключения. Константы живут в разных модулях и совпадают случайно.
    """
    monkeypatch.setattr(config, "TRIAL_DAYS", subscription.REMIND_LEAD_DAYS + 9)
    idservice = await db.create_service(
        name=f"Тест {_uuid.uuid4().hex[:8]}",
        phone="+79990000000",
        city="Тестоград",
        address="ул. Тестовая, 1",
        owner_tg_id=999_000_101,
    )
    try:
        svc = await db.get_service(idservice)
        claimed = await db.claim_reminder(
            idservice, svc["paid_until"], subscription.STAGE_5D
        )
        assert claimed is True, "предзанятая отметка съела бы настоящее письмо"
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM services WHERE idservice=$1", idservice)


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


# ── Гейт приёма заявки ───────────────────────────────────────────────────────

import uuid as _uuid

from handlers.requests import RequestRejected, create_request_flow

CLIENT_ID = 999_000_008


def _payload(service_id: str, item, **extra) -> dict:
    """Свой, а не импортированный из test_schedule: тестовые файлы друг друга
    не импортируют — в tests нет пакета, и порядок сборки sys.path не наш."""
    return {
        "service_id": service_id,
        "idcatalogs": [str(item["idcatalog"])],
        "client_name": "Иван Тестов",
        "phone": "+79990000008",
        "brand": "Toyota",
        "model": "Camry",
        "plate": "А777АА777",
        "comment": "",
        "consent": True,
        "client_uid": str(_uuid.uuid4()),
        **extra,
    }


async def test_expired_service_does_not_take_requests(service):
    """Форму можно отправить в обход интерфейса — решает сервер."""
    item = (await db.get_catalog(service))[0]
    svc = await db.get_service(service)
    free = await db.free_slots(svc)
    day = sorted(free)[0]
    moment = free[day][0]
    await _expire(service)

    with pytest.raises(RequestRejected) as exc:
        await create_request_flow(
            None,
            client_tg_id=CLIENT_ID,
            payload=_payload(service, item, scheduled_at=f"{day} {moment:%H:%M}"),
        )
    text = str(exc.value).lower()
    assert "подписк" not in text and "оплат" not in text, "клиенту про оплату не говорим"
    # Тот же номер и в том же виде, что показывает бот по ссылке на сервис
    assert "+7 (999) 000-00-00" in str(exc.value)


# ── Путь подписки целиком ────────────────────────────────────────────────────


async def test_expiry_and_renewal_round_trip(service, make_request):
    """
    Полный круг: истёк → пропал из поиска и не принимает записи → продлён →
    вернулся. И записанный клиент всё это время остаётся записанным.
    """
    svc = await db.get_service(service)
    moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    booked = await make_request(service, moment)

    await _expire(service)
    assert all(
        str(row["idservice"]) != service
        for row in await db.get_services_by_city(svc["city"])
    )
    # Заявка на месте: просрочка отключает продажу нового времени, а не сервис
    taken = await db.get_taken_slots(
        service, moment - timedelta(hours=1), moment + timedelta(hours=1)
    )
    assert taken[moment] == 1

    await db.extend_subscription(service, days=30)
    assert any(
        str(row["idservice"]) == service
        for row in await db.get_services_by_city(svc["city"])
    )
    assert booked["idrequests"]


async def test_two_ticks_send_one_letter(service, monkeypatch):
    """Главное свойство конструкции, проверенное на настоящей базе."""
    import notifications

    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE services SET paid_until=now() + interval '20 hours'"
            " WHERE idservice=$1",
            service,
        )

    sent = []

    async def _fake_send(bot, chat_id, text, **kwargs):
        sent.append(chat_id)
        return True

    monkeypatch.setattr(notifications, "safe_send", _fake_send)
    await notifications.send_subscription_reminders(None)
    await notifications.send_subscription_reminders(None)
    assert sent.count(999_000_001) == 1


# ── Пока подписка не введена в действие ──────────────────────────────────────
# Всё выше проверяет механизм включённым, каким он поедет в бой. Здесь — что он
# делает, пока платить нечем: не мешает. Это состояние бота на стадии тестирования.


async def test_expired_service_is_found_while_not_enforced(service, monkeypatch):
    """Гейт поиска живёт в SQL отдельно от остальных — проверяем его отдельно."""
    svc = await db.get_service(service)
    await _expire(service)

    monkeypatch.setattr(config, "SUBSCRIPTION_ENFORCED", False)
    found = await db.get_services_by_city(svc["city"])
    assert any(str(row["idservice"]) == service for row in found)


async def test_expired_service_still_takes_requests_while_not_enforced(
    service, monkeypatch
):
    """
    Главное свойство выключенного состояния: клиент записывается как обычно.

    Без этого пробный период через пять дней превратил бы тестовый стенд в
    мёртвый бот, а вернуть его можно было бы только командой /extend.
    """
    item = (await db.get_catalog(service))[0]
    svc = await db.get_service(service)
    free = await db.free_slots(svc)
    day = sorted(free)[0]
    moment = free[day][0]
    await _expire(service)

    monkeypatch.setattr(config, "SUBSCRIPTION_ENFORCED", False)
    summary, is_duplicate = await create_request_flow(
        None,
        client_tg_id=CLIENT_ID,
        payload=_payload(service, item, scheduled_at=f"{day} {moment:%H:%M}"),
    )
    assert summary["request_id"], "заявка должна быть создана, а не отклонена"
    assert not is_duplicate
