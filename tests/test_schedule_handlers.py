"""
Отказ в правах у кнопок расписания.

Каждый inline-обработчик начинается одинаково: нет прав — молча выйти. Но
выйти нужно, ответив Telegram: без callback.answer() спиннер на кнопке крутится
до таймаута, и отказ выглядит зависанием. Проверка дешёвая, а забыть её в
одном обработчике из десяти легко — поэтому перебираем все.
"""

import pytest

import handlers.schedule as schedule

pytestmark = pytest.mark.asyncio


class FakeMessage:
    """Сообщение, которое обработчик после отказа трогать не должен."""

    def __init__(self):
        self.touched = []

    async def answer(self, *args, **kwargs):
        self.touched.append("answer")

    async def edit_text(self, *args, **kwargs):
        self.touched.append("edit_text")

    async def edit_reply_markup(self, *args, **kwargs):
        self.touched.append("edit_reply_markup")


class FakeCallback:
    def __init__(self, data: str):
        self.data = data
        self.message = FakeMessage()
        self.from_user = type("User", (), {"id": 999_000_002})()
        self.answers = []

    async def answer(self, text: str | None = None, **kwargs):
        self.answers.append(text)


class FakeState:
    """FSM трогать тоже незачем: отказ ничего не запоминает."""

    def __init__(self):
        self.touched = []

    async def get_data(self):
        self.touched.append("get_data")
        return {}

    async def update_data(self, **kwargs):
        self.touched.append("update_data")

    async def set_state(self, *args):
        self.touched.append("set_state")


HANDLERS = [
    (schedule.hours_ask, "schedhours"),
    (schedule.lunch_ask, "schedlunch"),
    (schedule.capacity_ask, "schedcap"),
    (schedule.horizon_ask, "schedhorizon"),
    (schedule.step_ask, "schedstep"),
    (schedule.step_set, "schedstep:30"),
    (schedule.step_back, "schedback"),
    (schedule.days_ask, "scheddays"),
    (schedule.days_toggle, "scheddaytoggle:3"),
    (schedule.days_save, "scheddaysdone"),
]


@pytest.fixture
def no_rights(monkeypatch):
    """Пользователь не управляющий: require_owner_service уже написал ему отказ."""
    async def _denied(message, state, user_id=None):
        return None

    monkeypatch.setattr(schedule, "require_owner_service", _denied)


@pytest.mark.parametrize("handler,data", HANDLERS, ids=[d for _, d in HANDLERS])
async def test_callback_without_rights_answers_and_stops(handler, data, no_rights):
    callback = FakeCallback(data)
    state = FakeState()

    await handler(callback, state)

    assert callback.answers == [None], "кнопке нужен ответ, иначе спиннер зависнет"
    assert callback.message.touched == [], "отказ ничего не переписывает в чате"
    assert state.touched == [], "отказ ничего не сохраняет в FSM"
