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


# ── Правила пользования записью ─────────────────────────────────────────────
# Вторая галочка под формой. Документ про то, кто перед клиентом отвечает:
# бот передаёт заявку, а ремонтирует и обещает сроки сервис.


def test_terms_name_who_does_the_work():
    page = policy.render_terms(SERVICE)
    assert "Гараж на Мира" in page
    assert "+7 (999) 123-45-67" in page
    assert "Не оказывает ремонт" in page


def test_terms_escape_the_service_name():
    page = policy.render_terms({**SERVICE, "service_name": "<script>alert(1)</script>"})
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_terms_without_a_service_still_explain_the_rules():
    page = policy.render_terms(None)
    assert "автосервис, в который вы записываетесь" in page
    assert "Отменить" in page or "отменить" in page


def test_terms_say_how_to_cancel():
    """Правило «приезжайте вовремя» без способа отмены — ловушка."""
    page = policy.render_terms(SERVICE)
    assert "Мои заявки" in page


def test_terms_promise_a_reminder_only_when_it_is_sent(monkeypatch):
    monkeypatch.setattr(policy.config, "APPOINTMENT_REMINDER_HOURS", 3)
    assert "За 3 часа" in policy.render_terms(SERVICE)

    monkeypatch.setattr(policy.config, "APPOINTMENT_REMINDER_HOURS", 1)
    assert "За 1 час " in policy.render_terms(SERVICE)

    # Напоминания выключены — обещать их нельзя
    monkeypatch.setattr(policy.config, "APPOINTMENT_REMINDER_HOURS", 0)
    assert "напомнит" not in policy.render_terms(SERVICE)


def test_terms_page_opens(client):
    response = client.get(f"/terms?service={SERVICE['idservice']}")
    assert response.status_code == 200
    assert "Гараж на Мира" in response.text
    assert client.get("/terms").status_code == 200
    assert client.get("/terms?service=не-uuid").status_code == 200


# ── Оферта ──────────────────────────────────────────────────────────────────
# Договор владельца бота с автосервисом, а не с клиентом. Открывается, только
# когда названы все реквизиты: принять предложение можно лишь у кого-то.

REQUISITES = {
    "OFFER_PROVIDER": "Самозанятый Иванов Иван Иванович",
    "OFFER_INN": "123456789012",
    "OFFER_CONTACT": "owner@example.com",
}


@pytest.fixture
def published(monkeypatch):
    for name, value in REQUISITES.items():
        monkeypatch.setattr(policy.config, name, value)
    return REQUISITES


def test_offer_names_the_party_and_where_to_complain(published):
    page = policy.render_offer()
    for value in published.values():
        assert value in page


def test_offer_escapes_the_party(monkeypatch):
    monkeypatch.setattr(policy.config, "OFFER_PROVIDER", "ИП <script>alert(1)</script>")
    monkeypatch.setattr(policy.config, "OFFER_INN", "1")
    monkeypatch.setattr(policy.config, "OFFER_CONTACT", "a@b.c")
    page = policy.render_offer()
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_offer_prices_are_the_real_ones(published, monkeypatch):
    """
    Цена в договоре и цена на кнопке — одно число. Разойдутся — платить будут
    по кнопке, а спорить по договору.
    """
    monkeypatch.setattr(policy.config, "PAYMENT_METHODS",
                        (policy.config.PAYMENT_STARS,))
    page = policy.render_offer()
    for plan in policy.config.PLANS:
        assert plan.label in page
        assert f"{plan.stars} ⭐" in page

    monkeypatch.setattr(policy.config, "PAYMENT_METHODS",
                        (policy.config.PAYMENT_YOOMONEY,))
    page = policy.render_offer()
    assert f"{policy.config.PLANS[0].rubles} ₽" in page
    assert "ЮMoney" in page


def test_offer_shows_both_prices_when_both_are_taken(published, monkeypatch):
    """
    Способов два — цен тоже две, и в договоре стоят обе. Одна цена на две
    кнопки означала бы, что заплативший звёздами платил не по договору.
    """
    monkeypatch.setattr(policy.config, "PAYMENT_METHODS",
                        (policy.config.PAYMENT_YOOMONEY, policy.config.PAYMENT_STARS))
    page = policy.render_offer()
    plan = policy.config.PLANS[0]
    assert f"{plan.rubles} ₽" in page
    assert f"{plan.stars} ⭐" in page
    assert "Переводом" in page and "Звёздами" in page
    # Разница в цене объяснена: она достаётся магазинам, а не исполнителю
    assert "комиссию" in page


def test_offer_mentions_the_trial_only_when_there_is_one(published, monkeypatch):
    monkeypatch.setattr(policy.config, "TRIAL_DAYS", 5)
    assert "Первые 5 дней" in policy.render_offer()

    monkeypatch.setattr(policy.config, "TRIAL_DAYS", 0)
    assert "бесплатно, чтобы посмотреть" not in policy.render_offer()


def test_offer_does_not_threaten_a_shutdown_that_is_switched_off(published, monkeypatch):
    """
    Пока отключение за неоплату выключено, обещать его в договоре — врать в
    первом же абзаце, который заказчик проверит на себе.
    """
    monkeypatch.setattr(policy.config, "SUBSCRIPTION_ENFORCED", False)
    page = policy.render_offer()
    assert "пока не введено" in page

    monkeypatch.setattr(policy.config, "SUBSCRIPTION_ENFORCED", True)
    page = policy.render_offer()
    assert "пропадает из поиска" in page


def test_offer_puts_the_data_in_the_right_hands(published):
    """
    Оператор данных клиента — автосервис, владелец бота обрабатывает их по
    поручению. Ровно это обещает клиенту страница /privacy, и оферта —
    единственное место, где поручение оформлено.
    """
    page = policy.render_offer()
    assert "по поручению заказчика" in page
    assert "не продаёт" in page


def test_offer_page_is_closed_until_the_party_is_named(client, monkeypatch):
    for name in REQUISITES:
        monkeypatch.setattr(app_module.config, name, "")
    assert client.get("/offer").status_code == 404


def test_offer_page_opens_when_the_party_is_named(client, monkeypatch):
    for name, value in REQUISITES.items():
        monkeypatch.setattr(app_module.config, name, value)
    response = client.get("/offer")
    assert response.status_code == 200
    assert REQUISITES["OFFER_PROVIDER"] in response.text
