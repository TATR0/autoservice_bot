"""
Политика обработки данных: та самая страница, к которой отсылает галочка
согласия в форме записи.

Проверяется главное — что документ не врёт (срок хранения берётся настоящий)
и что название сервиса из базы не становится дырой: его вписывает управляющий,
а читает чужой браузер.
"""

import policy

SERVICE = {
    "idservice": "11111111-1111-1111-1111-111111111111",
    "service_name": "Гараж на Мира",
    "service_number": "+79991234567",
    "city": "Казань",
    "location_service": "ул. Мира, 15",
}


def test_operator_is_the_service_by_name():
    page = policy.render(SERVICE, 365)
    assert "Гараж на Мира" in page
    assert "+7 (999) 123-45-67" in page, "телефон показываем человеческим"
    assert "Казань" in page and "ул. Мира, 15" in page


def test_service_name_cannot_carry_a_script():
    """
    Название сервиса вводит управляющий, а страница открыта всем. Без
    экранирования чужая разметка приехала бы в браузер клиента.
    """
    page = policy.render({**SERVICE, "service_name": "<script>alert(1)</script>"}, 365)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_retention_period_is_the_real_one():
    assert "365 дней" in policy.render(SERVICE, 365)
    assert "30 дней" in policy.render(SERVICE, 30)
    assert "21 день" in policy.render(SERVICE, 21)
    assert "92 дня" in policy.render(SERVICE, 92)
    assert "11 дней" in policy.render(SERVICE, 11), "одиннадцать — не «один»"


def test_no_retention_is_stated_plainly():
    """
    Чистка выключена — и документ говорит об этом, а не обещает удаление.
    Обещание, которого никто не выполняет, хуже прямого умолчания.
    """
    page = policy.render(SERVICE, 0)
    assert "пока вы не попросите их удалить" in page
    assert "дней" not in page.split("Сколько хранится")[1].split("Как отозвать")[0]


def test_unknown_service_still_gets_a_page():
    """
    Сервис удалён или id не назвали — человек всё равно должен прочесть, что
    с его данными происходит. Без имени оператора, но текст на месте.
    """
    page = policy.render(None, 365)
    assert "автосервис, в который вы записываетесь" in page
    assert "/forget_me" in page


def test_the_way_out_is_named():
    """Право удалить данные бесполезно, если в документе не сказано как."""
    page = policy.render(SERVICE, 365)
    assert "/forget_me" in page
    assert "незакрытая заявка" in page, "про исключение с активной заявкой — тоже"


def test_collected_fields_match_what_is_erased():
    """
    Список в политике и поля, которые стирает обезличивание, обязаны
    совпадать: иначе документ обещает удалить не то, что удаляется.
    """
    from database import Database

    erased = Database._ANONYMIZE_SET
    for field in ("client_name", "phone", "brand", "model", "plate", "comment"):
        assert f"{field}=" in erased
    for word in ("имя", "телефон", "марка", "модель", "госномер", "комментарий"):
        assert word in policy.COLLECTED


# ── Страница ────────────────────────────────────────────────────────────────
# TestClient без контекстного менеджера: lifespan не поднимается, база не нужна.

import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    async def _service(_id):
        return SERVICE

    monkeypatch.setattr(app_module.db, "get_service", _service)
    app_module._lookup_limiter._hits.clear()
    return TestClient(app_module.app)


def test_page_opens_and_names_the_operator(client):
    response = client.get(f"/privacy?service={SERVICE['idservice']}")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Гараж на Мира" in response.text


def test_page_opens_without_a_service(client):
    assert client.get("/privacy").status_code == 200


def test_garbage_id_does_not_break_the_page(client):
    """
    Мусорный id — не повод показать ошибку вместо документа: читать его
    приходят по ссылке из формы, и сломанная ссылка не должна лишать текста.
    """
    response = client.get("/privacy?service=не-uuid")
    assert response.status_code == 200
    assert "автосервис, в который вы записываетесь" in response.text


def test_silent_database_does_not_break_the_page(client, monkeypatch):
    async def _boom(_id):
        raise RuntimeError("база молчит")

    monkeypatch.setattr(app_module.db, "get_service", _boom)
    response = client.get(f"/privacy?service={SERVICE['idservice']}")
    assert response.status_code == 200
    assert "/forget_me" in response.text


def test_expired_service_still_shows_its_policy(client, monkeypatch):
    """
    Просрочка подписки закрывает запись, но не право клиента прочесть, что
    с уже сданными данными происходит.
    """
    async def _expired(_id):
        return {**SERVICE, "paid_until": None}

    monkeypatch.setattr(app_module.db, "get_service", _expired)
    response = client.get(f"/privacy?service={SERVICE['idservice']}")
    assert response.status_code == 200
    assert "Гараж на Мира" in response.text
