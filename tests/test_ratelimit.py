"""Тесты ограничителя запросов. Базы не требуют."""

import asyncio

import pytest
from fastapi import HTTPException

from ratelimit import DatabaseGate, RateLimiter, client_key


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    def __init__(self, headers: dict[str, str] | None = None, host: str | None = "1.2.3.4") -> None:
        self.headers = headers or {}
        self.client = _FakeClient(host) if host else None


def test_allows_up_to_limit():
    limiter = RateLimiter(limit=3, window_seconds=60)
    assert [limiter.allow("ip", now=100.0) for _ in range(3)] == [True, True, True]


def test_blocks_after_limit():
    limiter = RateLimiter(limit=2, window_seconds=60)
    for _ in range(2):
        limiter.allow("ip", now=100.0)
    assert limiter.allow("ip", now=100.0) is False


def test_window_resets():
    limiter = RateLimiter(limit=1, window_seconds=60)
    assert limiter.allow("ip", now=100.0) is True
    assert limiter.allow("ip", now=120.0) is False
    assert limiter.allow("ip", now=161.0) is True


def test_keys_are_independent():
    limiter = RateLimiter(limit=1, window_seconds=60)
    assert limiter.allow("first", now=100.0) is True
    assert limiter.allow("second", now=100.0) is True


def test_memory_is_bounded():
    """Поток запросов с разных адресов не должен раздувать память без предела."""
    limiter = RateLimiter(limit=1, window_seconds=60)
    for i in range(10_050):
        limiter.allow(f"ip-{i}", now=100.0)
    assert len(limiter._hits) <= 10_000


def test_retry_after_is_positive():
    limiter = RateLimiter(limit=1, window_seconds=60)
    limiter.allow("ip", now=100.0)
    assert limiter.retry_after("ip", now=130.0) >= 1


def test_client_key_prefers_forwarded_header():
    request = _FakeRequest({"x-forwarded-for": "9.9.9.9, 10.0.0.1"}, host="127.0.0.1")
    assert client_key(request) == "9.9.9.9"


def test_client_key_falls_back_to_peer():
    assert client_key(_FakeRequest(host="5.6.7.8")) == "5.6.7.8"


def test_client_key_survives_missing_client():
    assert client_key(_FakeRequest(host=None)) == "unknown"


async def test_gate_allows_within_capacity():
    gate = DatabaseGate(max_concurrent=2)
    async with gate:
        async with gate:
            pass


async def test_gate_rejects_when_saturated():
    """Переполнение отдаёт 503, а не копит очередь."""
    gate = DatabaseGate(max_concurrent=1, wait_timeout=0.05)

    async with gate:
        with pytest.raises(HTTPException) as exc:
            async with gate:
                pass
    assert exc.value.status_code == 503


async def test_gate_releases_slot_after_use():
    gate = DatabaseGate(max_concurrent=1, wait_timeout=0.05)
    async with gate:
        pass
    async with gate:
        pass


async def test_gate_releases_slot_on_error():
    """Упавший обработчик не должен навсегда занимать место в пуле."""
    gate = DatabaseGate(max_concurrent=1, wait_timeout=0.05)
    with pytest.raises(RuntimeError):
        async with gate:
            raise RuntimeError("обработчик упал")
    async with gate:
        pass


async def test_gate_serialises_concurrent_users():
    gate = DatabaseGate(max_concurrent=2, wait_timeout=1.0)
    active = 0
    peak = 0

    async def worker():
        nonlocal active, peak
        async with gate:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(6)))
    assert peak <= 2
