"""
ratelimit.py — защита публичных эндпоинтов от наплыва запросов.

`/api/services` и `/api/service/{id}` открыты без аутентификации, и каждый
запрос идёт в базу. Пул соединений маленький (DB_POOL_MAX по умолчанию 5),
а бот живёт в том же процессе и том же пуле — значит скрипт, долбящий поиск
по городу, кладёт заодно и обработку апдейтов Telegram.

Две линии обороны, потому что одной мало:

1. Счётчик на ключ (IP). Ловит обычное злоупотребление, но ключ берётся из
   заголовка X-Forwarded-For, который подделывается тривиально.
2. Ограничение одновременных обращений к базе. Работает независимо от того,
   что клиент написал в заголовках, и гарантирует, что публичному API
   никогда не достанется весь пул целиком.

Всё в памяти процесса: воркер один (см. Dockerfile), внешних зависимостей
не добавляем.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict

from fastapi import HTTPException, Request

# Больше ключей в памяти не держим: при переполнении вытесняем самые старые.
# 10 000 записей — это сотни килобайт, а рост ограничен сверху.
_MAX_TRACKED_KEYS = 10_000


class RateLimiter:
    """
    Счётчик запросов в фиксированном окне.

    Фиксированное окно, а не скользящее: на границе окон клиент может выдать
    двойной лимит, но нам важно отсечь поток в тысячи запросов, а не считать
    точно. Зато памяти — два числа на ключ.
    """

    def __init__(self, *, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: OrderedDict[str, tuple[float, int]] = OrderedDict()

    def _evict_if_needed(self) -> None:
        while len(self._hits) > _MAX_TRACKED_KEYS:
            self._hits.popitem(last=False)

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """True — запрос пропускаем, False — лимит исчерпан."""
        now = time.monotonic() if now is None else now
        window_start, count = self._hits.get(key, (now, 0))

        if now - window_start >= self.window:
            window_start, count = now, 0

        count += 1
        self._hits[key] = (window_start, count)
        self._hits.move_to_end(key)
        self._evict_if_needed()
        return count <= self.limit

    def retry_after(self, key: str, *, now: float | None = None) -> int:
        """Через сколько секунд окно клиента обнулится."""
        now = time.monotonic() if now is None else now
        window_start, _ = self._hits.get(key, (now, 0))
        return max(1, int(self.window - (now - window_start)) + 1)


def client_key(request: Request) -> str:
    """
    Ключ клиента. За обратным прокси реальный адрес приходит в
    X-Forwarded-For; подделать его может кто угодно, поэтому счётчик по нему
    считается лишь первой линией — вторая не зависит от заголовков.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class DatabaseGate:
    """
    Ограничитель одновременных обращений публичного API к базе.

    Держит для публичных запросов меньше соединений, чем есть в пуле, —
    остаток гарантированно остаётся боту. Если свободного места нет дольше
    таймаута, клиент получает 503 вместо того, чтобы копиться в очереди.
    """

    def __init__(self, *, max_concurrent: int, wait_timeout: float = 2.0) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._wait_timeout = wait_timeout

    async def __aenter__(self) -> "DatabaseGate":
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._wait_timeout)
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=503,
                detail="Сервис перегружен, попробуйте через минуту",
            ) from None
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._semaphore.release()


def enforce(limiter: RateLimiter, request: Request) -> None:
    """Проверить лимит и бросить 429, если он исчерпан."""
    key = client_key(request)
    if not limiter.allow(key):
        raise HTTPException(
            status_code=429,
            detail="Слишком много запросов. Попробуйте позже.",
            headers={"Retry-After": str(limiter.retry_after(key))},
        )
