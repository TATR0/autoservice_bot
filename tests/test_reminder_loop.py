"""
Свой будильник вместо внешнего крона. Базы не требует — тик подменён.

Проверяется не рассылка (она в test_subscription_db), а свойства самого цикла:
он стартует сразу, переживает падение круга и глушится вместе с приложением.

Ждём событий, а не секунд: цикл живёт на том же event loop, что и остальная
сюита, и под нагрузкой полного прогона любой sleep-таймаут — это ложное падение.
"""

import asyncio

import pytest

import notifications

pytestmark = pytest.mark.asyncio

# Столько ждём события, прежде чем признать цикл сломанным. Заведомо больше
# любой разумной задержки планировщика и заведомо меньше терпения человека
PATIENCE = 5


@pytest.fixture(autouse=True)
def quiet_appointments(monkeypatch):
    """
    Круг напоминаний о записи в этих тестах молчит.

    Здесь проверяются свойства самого будильника, а не рассылки; настоящий
    круг без базы просто писал бы в лог исключение на каждом обороте.
    """
    async def _none(_bot):
        return 0

    monkeypatch.setattr(notifications, "send_appointment_reminders", _none)


async def _stop(task: asyncio.Task) -> None:
    """Погасить цикл так же, как это делает lifespan приложения."""
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_first_round_runs_without_waiting(monkeypatch):
    """Перезапуск не должен стоить письма: круг идёт сразу, а не через час."""
    calls = []
    rang = asyncio.Event()

    async def _tick(_bot):
        calls.append(1)
        rang.set()
        return 0

    monkeypatch.setattr(notifications, "send_subscription_reminders", _tick)
    task = asyncio.create_task(notifications.send_reminders_forever(None, 3600))
    try:
        await asyncio.wait_for(rang.wait(), PATIENCE)
    finally:
        await _stop(task)
    assert calls == [1], "цикл обязан сходить в базу до первого сна"


async def test_a_failed_round_does_not_kill_the_alarm(monkeypatch):
    """
    Обрыв базы или сети через час стоит попробовать снова.

    Умри цикл на первом исключении — снаружи это молчащий бот: гейты стоят,
    сроки идут, а писем нет и никто об этом не узнает.
    """
    calls = []
    second_round = asyncio.Event()

    async def _tick(_bot):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("база отвалилась")
        second_round.set()
        return 0

    monkeypatch.setattr(notifications, "send_subscription_reminders", _tick)
    task = asyncio.create_task(notifications.send_reminders_forever(None, 0))
    try:
        await asyncio.wait_for(second_round.wait(), PATIENCE)
    finally:
        await _stop(task)
    assert len(calls) >= 2, "после падения круга будильник должен продолжить"


async def test_cancel_reaches_the_loop_mid_round(monkeypatch):
    """
    Отмену цикл пропускает наверх, а не глотает своим except.

    Иначе остановка приложения ждала бы его вечно, а пул базы закрылся бы
    из-под работающего круга.
    """
    entered = asyncio.Event()

    async def _tick(_bot):
        entered.set()
        await asyncio.sleep(3600)
        return 0

    monkeypatch.setattr(notifications, "send_subscription_reminders", _tick)
    task = asyncio.create_task(notifications.send_reminders_forever(None, 0))
    await asyncio.wait_for(entered.wait(), PATIENCE)
    await _stop(task)


async def test_reminder_carries_a_pay_button(monkeypatch):
    """
    Момент, когда человек готов платить, — момент письма.

    Заставить его идти искать кнопку в меню значит потерять часть платежей.
    """
    import keyboards as kb

    sent = []

    async def _fake_send(bot, chat_id, text, **kwargs):
        sent.append(kwargs.get("reply_markup"))
        return True

    async def _one_service():
        from datetime import datetime, timedelta, timezone
        return [{
            "idservice": "11111111-1111-1111-1111-111111111111",
            "service_name": "Тест",
            "owner_id": 1,
            "timezone": "Europe/Moscow",
            "paid_until": datetime.now(timezone.utc) + timedelta(hours=20),
        }]

    async def _claim(*args, **kwargs):
        return True

    monkeypatch.setattr(notifications, "safe_send", _fake_send)
    monkeypatch.setattr(notifications.db, "services_for_reminders", _one_service)
    monkeypatch.setattr(notifications.db, "claim_reminder", _claim)

    await notifications.send_subscription_reminders(None)
    assert sent and sent[0] == kb.kb_pay()


async def test_a_failed_round_does_not_cost_the_other_one(monkeypatch):
    """
    Круги независимы.

    Подписка и записи ходят в базу по-разному и падают по-разному, а клиент,
    записанный на сегодня, не должен остаться без напоминания из-за того, что
    у нас не сложилось с чьим-то сроком оплаты.
    """
    reminded = asyncio.Event()

    async def _broken(_bot):
        raise RuntimeError("база отвалилась")

    async def _appointments(_bot):
        reminded.set()
        return 1

    monkeypatch.setattr(notifications, "send_subscription_reminders", _broken)
    monkeypatch.setattr(notifications, "send_appointment_reminders", _appointments)

    task = asyncio.create_task(notifications.send_reminders_forever(None, 3600))
    try:
        await asyncio.wait_for(reminded.wait(), PATIENCE)
    finally:
        await _stop(task)
