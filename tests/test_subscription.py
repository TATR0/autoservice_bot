"""Тесты подписки. Базы не требуют — модуль чистый."""

from datetime import datetime, timedelta, timezone

import subscription

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def test_future_date_is_active():
    assert subscription.is_active(NOW + timedelta(seconds=1), NOW)


def test_past_date_is_not_active():
    assert not subscription.is_active(NOW - timedelta(seconds=1), NOW)


def test_expiring_exactly_now_is_not_active():
    """Граница закрыта в пользу отключения: срок «до» — значит до."""
    assert not subscription.is_active(NOW, NOW)


def test_never_paid_is_not_active():
    assert not subscription.is_active(None, NOW)


def test_extend_before_expiry_adds_to_remainder():
    """Заплатил заранее — оплаченные дни не сгорают."""
    paid_until = NOW + timedelta(days=3)
    assert subscription.extend(paid_until, NOW, 30) == paid_until + timedelta(days=30)


def test_extend_after_expiry_counts_from_now():
    """Иначе оплата уходит в оплату уже прошедшего простоя."""
    paid_until = NOW - timedelta(days=10)
    assert subscription.extend(paid_until, NOW, 30) == NOW + timedelta(days=30)


def test_extend_from_scratch_counts_from_now():
    assert subscription.extend(None, NOW, 5) == NOW + timedelta(days=5)


def test_no_reminder_while_expiry_is_far():
    assert subscription.due_stage(NOW + timedelta(days=6), NOW) is None


def test_five_days_stage_inside_the_lead():
    assert subscription.due_stage(NOW + timedelta(days=4), NOW) == subscription.STAGE_5D


def test_last_day_gives_the_24h_stage():
    assert subscription.due_stage(NOW + timedelta(hours=23), NOW) == subscription.STAGE_24H


def test_expired_stage_right_after_the_deadline():
    assert subscription.due_stage(NOW - timedelta(seconds=1), NOW) == subscription.STAGE_EXPIRED


def test_long_silent_cron_does_not_send_stale_stages():
    """
    Крон молчал двое суток, срок за это время прошёл.

    Досылать «осталось 24 часа» вдогонку нельзя: это неправда в момент
    получения. Отдаётся только самая поздняя подошедшая стадия.
    """
    assert subscription.due_stage(NOW - timedelta(days=2), NOW) == subscription.STAGE_EXPIRED


def test_never_paid_gets_no_reminder():
    """Напоминать не о чем: срока никогда не было."""
    assert subscription.due_stage(None, NOW) is None
