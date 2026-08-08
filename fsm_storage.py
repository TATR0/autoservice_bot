"""
fsm_storage.py — хранилище FSM, переживающее рестарт контейнера.

MemoryStorage терял состояние при каждом деплое Render: управляющий на
4-м шаге регистрации получал «молчание бота».

По умолчанию состояние живёт в PostgreSQL (таблица fsm_storage) — новых
зависимостей не требуется. Если задан REDIS_URL и установлен пакет redis,
используется он.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

import config
from database import db

logger = logging.getLogger(__name__)


class PostgresStorage(BaseStorage):
    """FSM-хранилище поверх таблицы fsm_storage."""

    @staticmethod
    def _key(key: StorageKey) -> str:
        parts = [
            str(key.bot_id),
            str(key.chat_id),
            str(key.user_id),
            str(getattr(key, "thread_id", None) or 0),
            str(getattr(key, "business_connection_id", None) or 0),
            str(key.destiny),
        ]
        return ":".join(parts)

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        async with db.pool.acquire() as conn:
            if value is None:
                # Сбрасываем состояние, но данные оставляем: в них лежит active_service
                await conn.execute(
                    "UPDATE fsm_storage SET state=NULL, updatedate=now() WHERE key=$1",
                    self._key(key),
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO fsm_storage (key, state) VALUES ($1,$2)
                    ON CONFLICT (key) DO UPDATE
                    SET state = EXCLUDED.state, updatedate = now()
                    """,
                    self._key(key), value,
                )

    async def get_state(self, key: StorageKey) -> str | None:
        async with db.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT state FROM fsm_storage WHERE key=$1", self._key(key)
            )

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        async with db.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO fsm_storage (key, data) VALUES ($1, $2::jsonb)
                ON CONFLICT (key) DO UPDATE
                SET data = EXCLUDED.data, updatedate = now()
                """,
                self._key(key), json.dumps(data or {}, ensure_ascii=False),
            )

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with db.pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT data FROM fsm_storage WHERE key=$1", self._key(key)
            )
        if not raw:
            return {}
        return json.loads(raw) if isinstance(raw, str) else dict(raw)

    async def close(self) -> None:
        # Пул закрывается в lifespan приложения
        return None


def build_storage() -> BaseStorage:
    if config.REDIS_URL:
        try:
            from aiogram.fsm.storage.redis import RedisStorage

            logger.info("FSM: Redis")
            return RedisStorage.from_url(config.REDIS_URL)
        except ImportError:
            logger.warning(
                "REDIS_URL задан, но пакет redis не установлен "
                "(pip install redis) — использую PostgreSQL."
            )
    logger.info("FSM: PostgreSQL")
    return PostgresStorage()
