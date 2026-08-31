"""
Срок хранения персональных данных. Базы не требует — чистка подменена.

Проверяются свойства круга: он идёт сразу, переживает падение и глушится
вместе с приложением. Сам SQL проверяет test_privacy_db.
"""

import asyncio

import pytest

import config
import retention
from database import db

pytestmark = pytest.mark.asyncio

PATIENCE = 5


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_first_purge_runs_without_waiting(monkeypatch):
    """Перезапуск не должен откладывать чистку на сутки."""
    ran = asyncio.Event()

    async def _purge(days):
        ran.set()
        return 0

    monkeypatch.setattr(retention.db, "anonymize_old_requests", _purge)
    task = asyncio.create_task(retention.purge_forever(3600))
    try:
        await asyncio.wait_for(ran.wait(), PATIENCE)
    finally:
        await _stop(task)


async def test_a_failed_purge_does_not_kill_the_loop(monkeypatch):
    """Обрыв базы стоит попробовать снова завтра, а не молчать до перезапуска."""
    calls = []
    second = asyncio.Event()

    async def _purge(days):
        calls.append(days)
        if len(calls) == 1:
            raise RuntimeError("база отвалилась")
        second.set()
        return 0

    monkeypatch.setattr(retention.db, "anonymize_old_requests", _purge)
    task = asyncio.create_task(retention.purge_forever(0))
    try:
        await asyncio.wait_for(second.wait(), PATIENCE)
    finally:
        await _stop(task)
    assert len(calls) >= 2


async def test_cancel_reaches_the_loop_mid_round(monkeypatch):
    """Отмену круг пропускает наверх: иначе остановка приложения ждала бы его вечно."""
    entered = asyncio.Event()

    async def _purge(days):
        entered.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(retention.db, "anonymize_old_requests", _purge)
    task = asyncio.create_task(retention.purge_forever(0))
    await asyncio.wait_for(entered.wait(), PATIENCE)
    await _stop(task)


async def test_the_setting_is_read_every_round(monkeypatch):
    """
    Срок хранения меняют в .env и перезапускают процесс, но круг обязан
    читать настройку, а не запоминать её при импорте: иначе выключенная
    чистка осталась бы выключенной и после включения.
    """
    seen = []
    ran = asyncio.Event()

    async def _purge(days):
        seen.append(days)
        ran.set()
        return 0

    monkeypatch.setattr(config, "PII_RETENTION_DAYS", 400)
    monkeypatch.setattr(retention.db, "anonymize_old_requests", _purge)
    task = asyncio.create_task(retention.purge_forever(3600))
    try:
        await asyncio.wait_for(ran.wait(), PATIENCE)
    finally:
        await _stop(task)
    assert seen[0] == 400


async def test_unset_retention_does_not_touch_the_database():
    """
    Ноль — это «срок не назначен», а не «стереть всё сегодняшнее». Чистка
    обязана выйти раньше, чем возьмёт соединение: пула в этом тесте и нет.
    """
    assert await db.anonymize_old_requests(0) == 0
