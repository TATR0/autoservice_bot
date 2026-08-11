"""
Тесты защиты HTTP-слоя: лимиты частоты и заголовки безопасности.

TestClient создаётся без контекстного менеджера — тогда lifespan не
запускается и подключение к базе не требуется. Всё, что проверяется здесь,
срабатывает до обращения к базе.
"""

import pytest
from starlette.testclient import TestClient

import app as app_module


@pytest.fixture
def client(monkeypatch):
    # База здесь не нужна: проверяется слой HTTP, который отрабатывает раньше.
    async def _no_services(_city):
        return []

    monkeypatch.setattr(app_module.db, "get_services_by_city", _no_services)
    return TestClient(app_module.app)


@pytest.fixture(autouse=True)
def reset_limiters():
    """Лимиты живут в памяти процесса — чистим, чтобы тесты не влияли друг на друга."""
    for limiter in (
        app_module._lookup_limiter,
        app_module._profile_limiter,
        app_module._submit_limiter,
    ):
        limiter._hits.clear()
    yield


def test_security_headers_present(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_csp_allows_telegram_sdk(client):
    """Форма грузит telegram-web-app.js — политика не должна его резать."""
    csp = client.get("/healthz").headers["Content-Security-Policy"]
    assert "https://telegram.org" in csp


def test_lookup_endpoint_rate_limited(client):
    limit = app_module._lookup_limiter.limit
    codes = [
        client.get("/api/services", params={"city": "Москва"}).status_code
        for _ in range(limit + 5)
    ]
    assert 429 in codes, "поиск по городу должен ограничиваться по частоте"
    assert codes[-1] == 429


def test_rate_limited_response_has_retry_after(client):
    limit = app_module._lookup_limiter.limit
    response = None
    for _ in range(limit + 2):
        response = client.get("/api/services", params={"city": "Москва"})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1


def test_submit_endpoint_rate_limited(client):
    """Отправка заявки ограничена жёстче, чем чтение."""
    assert app_module._submit_limiter.limit < app_module._lookup_limiter.limit
    codes = []
    for _ in range(app_module._submit_limiter.limit + 3):
        response = client.post("/api/requests", json={"init_data": "x", "service_id": "y"})
        codes.append(response.status_code)
    assert codes[-1] == 429


def test_limits_are_per_client(client):
    """Исчерпанный лимит одного адреса не блокирует остальных."""
    limit = app_module._lookup_limiter.limit
    for _ in range(limit + 2):
        client.get(
            "/api/services",
            params={"city": "Москва"},
            headers={"X-Forwarded-For": "10.0.0.1"},
        )
    other = client.get(
        "/api/services",
        params={"city": "Москва"},
        headers={"X-Forwarded-For": "10.0.0.2"},
    )
    assert other.status_code != 429


def test_docs_are_disabled(client):
    """Схема API наружу не публикуется."""
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404


def test_webhook_rejects_wrong_secret(client):
    response = client.post("/webhook/не-тот-секрет", json={"update_id": 1})
    assert response.status_code == 403
