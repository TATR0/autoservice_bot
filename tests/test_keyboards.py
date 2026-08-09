"""
Тесты клавиатур.

Главное здесь — регрессия на способ открытия формы записи. Мини-приложение,
открытое кнопкой reply-клавиатуры, не получает от Telegram подписанный
initData: такие кнопки задуманы под старый механизм sendData. Сервер заявку
без initData не принимает, поэтому запись через reply-кнопку не работает
вообще. Форму открываем только inline-кнопкой.
"""

import pytest

import config
import keyboards as kb

SERVICE_ID = "554a7d40-9483-4417-8c83-2cf995107bf8"


@pytest.fixture
def webapp_configured(monkeypatch):
    """BASE_URL задан — клавиатуры строятся как в продакшене."""
    monkeypatch.setattr(config, "WEBAPP_URL", "https://example.test/app/")


def _all_reply_keyboards():
    return {
        "kb_client_main": kb.kb_client_main(),
        "kb_client_service": kb.kb_client_service(SERVICE_ID),
        "kb_owner_main": kb.kb_owner_main(SERVICE_ID, many_services=False),
        "kb_admin_main": kb.kb_admin_main(SERVICE_ID, many_services=False),
        "kb_cancel": kb.kb_cancel(),
    }


def test_reply_keyboards_have_no_webapp_buttons(webapp_configured):
    """Ни одна reply-клавиатура не открывает мини-приложение напрямую."""
    offenders = [
        f"{name}: {button.text}"
        for name, keyboard in _all_reply_keyboards().items()
        for row in keyboard.keyboard
        for button in row
        if button.web_app is not None
    ]
    assert offenders == [], (
        "WebApp открыт кнопкой reply-клавиатуры — Telegram не передаст initData: "
        + ", ".join(offenders)
    )


def test_open_webapp_keyboard_carries_web_app_button(webapp_configured):
    markup = kb.kb_open_webapp()
    button = markup.inline_keyboard[0][0]
    assert button.web_app is not None
    assert button.web_app.url == "https://example.test/app/"


def test_open_webapp_keyboard_passes_service_id(webapp_configured):
    markup = kb.kb_open_webapp(SERVICE_ID)
    assert markup.inline_keyboard[0][0].web_app.url.endswith(f"service_id={SERVICE_ID}")


def test_open_webapp_keyboard_is_none_without_base_url(monkeypatch):
    """Локальная разработка без BASE_URL: кнопку показывать нечем."""
    monkeypatch.setattr(config, "WEBAPP_URL", "")
    assert kb.kb_open_webapp() is None
