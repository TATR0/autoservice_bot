"""
show_main_menu — второе сообщение о подписке.

Строка о подписке отдельным сообщением с inline-кнопкой ушла из handlers/common.py
в этой задаче. Ни один тест раньше не проверял её отправку — этот файл закрывает
дыру. Базы не требует — слой БД подменён.
"""

from datetime import datetime, timedelta, timezone

import pytest

import config
import handlers.common as common

pytestmark = pytest.mark.asyncio

OWNER_ID = 999_000_400
SERVICE_ID = "33333333-3333-3333-3333-333333333333"


class FakeMessage:
    def __init__(self, user_id: int):
        self.from_user = type("User", (), {"id": user_id})()
        self.answers = []
        self.markups = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)
        self.markups.append(kwargs.get("reply_markup"))


class FakeState:
    """FSM не трогаем: единственный сервис выбирается сам, без сохранённого выбора."""

    async def get_data(self):
        return {}

    async def update_data(self, **kwargs):
        pass


def _owner_service(paid_until):
    return {
        "idservice": SERVICE_ID,
        "service_name": "Тест",
        "timezone": "Europe/Moscow",
        "role": "owner",
        "paid_until": paid_until,
    }


@pytest.fixture
def one_service(monkeypatch):
    """Слой БД подменён: единственный сервис пользователя."""
    box = {}

    async def _services(_tg_id):
        return [box["svc"]]

    monkeypatch.setattr(common.db, "get_user_services", _services)
    monkeypatch.setattr(config, "SUBSCRIPTION_ENFORCED", True)
    return box


async def test_second_message_with_pay_button_when_due_soon(one_service):
    """Близкий срок — второе сообщение с kb.kb_pay() уходит."""
    one_service["svc"] = _owner_service(
        datetime.now(timezone.utc) + timedelta(days=2)
    )
    message = FakeMessage(OWNER_ID)
    await common.show_main_menu(message, FakeState())

    assert len(message.answers) == 2, "должно уйти меню и отдельное письмо о подписке"
    markup = message.markups[1]
    assert markup is not None
    assert markup.inline_keyboard[0][0].callback_data == "subscr:open", (
        "второе сообщение обязано нести кнопку оплаты"
    )


async def test_no_second_message_when_subscription_line_is_empty(one_service):
    """Срок далеко — render.subscription_line пуст, второго сообщения нет вовсе."""
    one_service["svc"] = _owner_service(
        datetime.now(timezone.utc) + timedelta(days=60)
    )
    message = FakeMessage(OWNER_ID)
    await common.show_main_menu(message, FakeState())

    assert len(message.answers) == 1, (
        "пустой пузырь с кнопкой недопустим: пустое значение — это пустота"
    )


async def test_no_second_message_when_greeting_is_given(one_service):
    """
    Явный greeting — это не первый заход в меню, а реакция на действие
    вроде «✅ Часы работы обновлены». Предупреждение о подписке не должно
    всплывать на каждый чих, даже если срок и правда близок.
    """
    one_service["svc"] = _owner_service(
        datetime.now(timezone.utc) + timedelta(days=2)
    )
    message = FakeMessage(OWNER_ID)
    await common.show_main_menu(
        message, FakeState(), greeting="✅ Часы работы обновлены"
    )

    assert len(message.answers) == 1, (
        "greeting подшивать предупреждением о подписке нельзя"
    )
