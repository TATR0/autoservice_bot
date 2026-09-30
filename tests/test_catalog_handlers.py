"""
Добавление услуг в «🔧 Услуги». Базы не требует — слой БД и меню подменены.

Услуги вводятся так же, как при регистрации: строками одним сообщением,
затем проверка списка. Разбор строк проверяется в test_validators.
"""

import pytest

import handlers.catalog as catalog
import keyboards as kb

pytestmark = pytest.mark.asyncio

SERVICE_ID = "11111111-1111-1111-1111-111111111111"


class FakeMessage:
    def __init__(self, text: str):
        self.text = text
        self.from_user = type("User", (), {"id": 1})()
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


class FakeState:
    def __init__(self, data: dict):
        self._data = data
        self.state = None

    async def get_data(self):
        return dict(self._data)

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **kwargs):
        self._data.update(kwargs)


@pytest.fixture
def shop(monkeypatch):
    """Сервис с одной услугой в каталоге. Возвращает список добавленных в базу."""
    catalog_rows = [{"title": "Дубликат ключа"}]
    added = []

    async def _owner(*args, **kwargs):
        return {"idservice": SERVICE_ID, "service_name": "Гараж №1"}

    async def _get_catalog(_id):
        return catalog_rows

    async def _add(_id, title, price, duration):
        if title == "Занято параллельно":
            return None
        added.append((title, price, duration))
        return {"title": title}

    async def _noop(*args, **kwargs):
        pass

    monkeypatch.setattr(catalog, "require_owner_service", _owner)
    monkeypatch.setattr(catalog.db, "get_catalog", _get_catalog)
    monkeypatch.setattr(catalog.db, "add_catalog_item", _add)
    monkeypatch.setattr(catalog, "show_main_menu", _noop)
    monkeypatch.setattr(catalog, "_show_catalog", _noop)
    return added


async def test_services_come_in_one_message_then_check(shop):
    state = FakeState({"new_items": []})
    message = FakeMessage("Полировка, от 2000р, 2-4 ч\nХимчистка")
    await catalog.add_lines(message, state)

    assert state.state == catalog.ServiceCatalog.confirm
    assert state._data["new_items"] == [
        ["Полировка", 2000, 120, 240],
        ["Химчистка", None, None, None],
    ]
    assert "💰 от 2 000 ₽" in message.answers[-1]
    assert "⏱ 2–4 ч" in message.answers[-1]
    assert shop == []  # в базу — только после «Всё верно»


async def test_confirm_saves_all_services(shop):
    state = FakeState({"new_items": [
        ["Полировка", 2000, 120, 240],
        ["Химчистка", None, None, None],
    ]})
    await catalog.add_save(FakeMessage(kb.BTN_SERVICES_OK), state)

    assert shop == [("Полировка", 2000, (120, 240)), ("Химчистка", None, None)]
    assert state.state is None


async def test_service_already_in_catalog_is_refused(shop):
    state = FakeState({"new_items": []})
    message = FakeMessage("Полировка\nдубликат ключа")
    await catalog.add_lines(message, state)

    assert state._data["new_items"] == []
    assert message.answers[-1].startswith("❌")


async def test_line_without_separator_is_refused(shop):
    state = FakeState({"new_items": []})
    message = FakeMessage("полировка 5000 2")
    await catalog.add_lines(message, state)

    assert state._data["new_items"] == []
    assert "запятыми" in message.answers[-1]


async def test_catalog_limit_counts_existing_services(shop, monkeypatch):
    rows = [{"title": f"Услуга {i}"} for i in range(catalog.MAX_CATALOG_ITEMS - 1)]

    async def _full(_id):
        return rows

    monkeypatch.setattr(catalog.db, "get_catalog", _full)
    state = FakeState({"new_items": []})
    message = FakeMessage("Полировка\nХимчистка")
    await catalog.add_lines(message, state)

    assert state._data["new_items"] == []
    assert message.answers[-1].startswith("❌")


async def test_reset_empties_the_list(shop):
    state = FakeState({"new_items": [["Полировка", None, None, None]]})
    await catalog.add_reset(FakeMessage(kb.BTN_SERVICES_RESET), state)

    assert state._data["new_items"] == []
    assert state.state == catalog.ServiceCatalog.lines


async def test_service_taken_meanwhile_is_reported(shop, monkeypatch):
    """Дубликат, заведённый со второго устройства, не теряется молча."""
    greetings = []

    async def _menu(message, state, *, greeting=None, **kwargs):
        greetings.append(greeting)

    monkeypatch.setattr(catalog, "show_main_menu", _menu)
    state = FakeState({"new_items": [
        ["Полировка", None, None, None],
        ["Занято параллельно", None, None, None],
    ]})
    await catalog.add_save(FakeMessage(kb.BTN_SERVICES_OK), state)

    assert shop == [("Полировка", None, None)]
    assert "Добавлено услуг: 1" in greetings[-1]
    assert "«Занято параллельно»" in greetings[-1]
