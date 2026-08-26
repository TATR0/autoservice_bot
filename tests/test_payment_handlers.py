"""Оплата звёздами. Базы не требует — слой БД и Telegram подменены."""

import pytest

import config
import handlers.payment as payment

pytestmark = pytest.mark.asyncio

SERVICE_ID = "11111111-1111-1111-1111-111111111111"


def test_payload_round_trip():
    assert payment.parse_payload(payment.make_payload(SERVICE_ID, 30)) == (
        SERVICE_ID, 30
    )


def test_broken_payload_is_not_guessed():
    """Мусор в payload — это не повод угадывать, за что человек заплатил."""
    for raw in ("", "sub", "sub:x", f"sub:{SERVICE_ID}", f"sub:{SERVICE_ID}:x",
                f"sub:{SERVICE_ID}:30:extra", f"other:{SERVICE_ID}:30"):
        assert payment.parse_payload(raw) is None, raw


def test_non_ascii_digits_do_not_crash_the_parser():
    """isdigit() истинно для «²», int() его не парсит — уже ловили в /extend."""
    assert payment.parse_payload(f"sub:{SERVICE_ID}:²") is None


class FakeScreenMessage:
    def __init__(self):
        self.answers = []
        self.markups = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)
        self.markups.append(kwargs.get("reply_markup"))


async def test_expired_owner_can_still_reach_the_tariffs(monkeypatch):
    """
    Гейты подписки этот экран не закрывают.

    Иначе просрочка становится ловушкой: заплатить можно только изнутри,
    а внутрь не пускают, пока не заплатишь.
    """
    from datetime import datetime, timedelta, timezone

    async def _expired(_message, _state, user_id=None):
        return {
            "idservice": SERVICE_ID,
            "service_name": "Тест",
            "timezone": "Europe/Moscow",
            "paid_until": datetime.now(timezone.utc) - timedelta(days=10),
        }

    monkeypatch.setattr(payment, "require_owner_service", _expired)
    message = FakeScreenMessage()
    await payment.subscription_screen(message, None)

    assert message.answers, "просроченному управляющему экран обязан открыться"
    assert message.markups[0] is not None, "тарифы без кнопок бесполезны"


async def test_admin_is_refused_the_tariff_screen(monkeypatch):
    """
    Подписка сервиса — не дело администратора.

    Кнопку ему не показывают, но текст можно набрать руками, а callback —
    нажать в пересланном письме. Решает сервер, а не клавиатура.
    """
    refused = []

    async def _not_owner(message, _state, user_id=None):
        refused.append(user_id)
        return None

    monkeypatch.setattr(payment, "require_owner_service", _not_owner)
    message = FakeScreenMessage()
    await payment.subscription_screen(message, None)

    assert refused, "проверка владельца обязана быть вызвана"
    assert message.answers == [], "чужому экран не рисуем"
