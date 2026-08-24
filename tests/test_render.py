"""Тесты сборки текстов. Базы не требуют."""

from render import price_label, titled_price, weekdays_label


def test_price_label_shows_from_prefix():
    """Цена всегда ориентировочная: точную сервис назовёт после осмотра."""
    assert price_label(3000) == "от 3 000 ₽"


def test_price_label_groups_thousands():
    assert price_label(1234567) == "от 1 234 567 ₽"


def test_price_label_without_price():
    """Цены нет — показывать нечего, заглушку не выдумываем."""
    assert price_label(None) == ""


def test_price_label_zero_is_a_price():
    assert price_label(0) == "от 0 ₽"


def test_titled_price_appends_price():
    assert titled_price("Полировка", 3000) == "Полировка — от 3 000 ₽"


def test_titled_price_without_price_is_just_title():
    """Без цены не остаётся висящего тире."""
    assert titled_price("Полировка", None) == "Полировка"


def test_weekdays_label_lists_days_in_order():
    assert weekdays_label([1, 2, 3, 4, 5]) == "пн, вт, ср, чт, пт"


def test_weekdays_label_sorts_input():
    assert weekdays_label([5, 1]) == "пн, пт"


# ── Время записи в карточке заявки ───────────────────────────────────────────

from datetime import datetime, timezone

from render import request_card_for_staff


def _req(**overrides):
    """Заявка словарём: request_card_for_staff читает её по ключам."""
    base = {
        "seq": 42,
        "client_name": "Иван",
        "phone": "+79991234567",
        "idclienttg": 111,
        "brand": "Toyota",
        "model": "Camry",
        "plate": "А777АА777",
        "comment": "",
        "status": "new",
        "createdate": datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc),
        "scheduled_at": datetime(2026, 8, 17, 7, 0, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


SERVICES = [{"title": "Замена масла", "price_rub": 1200}]


def test_request_card_shows_scheduled_time():
    """Время записи заменило срочность."""
    text = request_card_for_staff(_req(), SERVICES, tz="Europe/Moscow")
    assert "Срочность" not in text
    assert "17.08.2026 10:00" in text


def test_request_card_without_time_omits_the_line():
    """Заявки из эпохи срочности времени не имеют — строки просто нет."""
    text = request_card_for_staff(_req(scheduled_at=None), SERVICES, tz="Europe/Moscow")
    assert "Запись" not in text


# ── Подписка ─────────────────────────────────────────────────────────────────

from datetime import datetime, timedelta, timezone

import render
import subscription


def _svc(paid_until):
    return {
        "service_name": "Тест",
        "service_number": "+79990000000",
        "timezone": "Europe/Moscow",
        "paid_until": paid_until,
    }


def test_no_subscription_line_while_the_deadline_is_far():
    """Лишняя строка в меню тратит внимание на то, что не требует действий."""
    assert render.subscription_line(_svc(datetime.now(timezone.utc) + timedelta(days=90))) == ""


def test_subscription_line_warns_before_the_deadline():
    """Строка обязана назвать дату: «что-то с подпиской» не повод к действию."""
    deadline = datetime.now(timezone.utc) + timedelta(days=2)
    line = render.subscription_line(_svc(deadline))
    assert "действует до" in line
    assert render.local_dt(deadline, "Europe/Moscow") in line


def test_expired_line_says_what_stopped_and_what_did_not():
    """
    Без второй половины управляющий решит, что теряет всю запись на неделю
    вперёд, — и будет спасать несуществующую беду.
    """
    line = render.subscription_line(_svc(datetime.now(timezone.utc) - timedelta(days=1)))
    assert "скрыт из поиска" in line
    assert "уже записанные" in line.lower()


def test_reminder_mentions_the_deadline_and_the_survivors():
    svc = _svc(datetime.now(timezone.utc) + timedelta(hours=20))
    text = render.subscription_reminder(subscription.STAGE_24H, svc)
    assert "уже записанные" in text.lower()
    assert svc["service_name"] in text


def test_support_contact_is_omitted_when_unset(monkeypatch):
    """Пустое поле — пустое место, а не «обратитесь в поддержку: »."""
    monkeypatch.setattr(render.config, "SUPPORT_CONTACT", "")
    line = render.subscription_line(_svc(datetime.now(timezone.utc) - timedelta(days=1)))
    assert "Продлить" not in line
