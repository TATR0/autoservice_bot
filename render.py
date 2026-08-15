"""render.py — сборка текстов сообщений. Всё пользовательское экранируется."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
from validators import format_phone, h

logger = logging.getLogger(__name__)


def request_number(seq: int | None) -> str:
    return f"#{seq:06d}" if seq else "#——"


def local_dt(value: datetime | None, tz: str | None = None) -> str:
    """
    Дата в часовом поясе сервиса.

    Фолбэк — datetime.timezone.utc, а не ZoneInfo("UTC"): в окружении без
    базы часовых поясов (slim-образы, Windows) ZoneInfo бросает исключение
    для ЛЮБОГО ключа, включая UTC, и вместе с ним падает вся карточка заявки.
    """
    if not value:
        return "—"
    try:
        zone = ZoneInfo(tz or config.DEFAULT_TIMEZONE)
    except Exception:
        logger.warning(
            "Часовой пояс %r недоступен, показываю время в UTC. "
            "Установите пакет tzdata.", tz or config.DEFAULT_TIMEZONE,
        )
        zone = timezone.utc
    return value.astimezone(zone).strftime("%d.%m.%Y %H:%M")


def status_label(status: str) -> str:
    return config.REQUEST_STATUS_LABELS.get(status, status)


def price_label(price_rub: int | None) -> str:
    """Цена для показа. Всегда «от»: точную сервис назовёт после осмотра.

    Цены нет — пустая строка: пока управляющий её не задал, показывать нечего.
    """
    if price_rub is None:
        return ""
    return "от " + f"{price_rub:,}".replace(",", " ") + " ₽"


WEEKDAY_NAMES = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def weekdays_label(weekdays) -> str:
    """[1,2,3,4,5] → «пн, вт, ср, чт, пт». Порядок всегда недельный."""
    return ", ".join(WEEKDAY_NAMES[day - 1] for day in sorted(weekdays))


def _hm(value) -> str:
    return value.strftime("%H:%M")


def schedule_card(svc, schedule, free_count: int) -> str:
    """
    Карточка расписания. Счётчик свободных окон считается тем же генератором,
    что и форма, — это единственный способ для управляющего сразу увидеть,
    что он поставил обед на весь день.
    """
    lunch = (
        f"{_hm(schedule['lunch_from'])} — {_hm(schedule['lunch_to'])}"
        if schedule["lunch_from"] else "нет"
    )
    return (
        f"🗓 <b>Расписание — {h(svc['service_name'])}</b>\n\n"
        f"🕘 Часы работы: {_hm(schedule['work_from'])} — {_hm(schedule['work_to'])}\n"
        f"⏱ Шаг записи: {schedule['slot_minutes']} минут\n"
        f"🍽 Обед: {lunch}\n"
        f"📅 Рабочие дни: {weekdays_label(schedule['weekdays'])}\n"
        f"🚗 Машин за раз: {schedule['capacity']}\n"
        f"📆 Открыто вперёд: {schedule['horizon_days']} дней\n\n"
        f"Свободных окон на этот срок: {free_count}"
    )


def titled_price(title: str, price_rub: int | None) -> str:
    """«Название — от 3 000 ₽» либо просто «Название», без висящего тире.

    Название приходит уже подготовленным для своего места вывода
    (экранированным для HTML или сырым для текста кнопки).
    """
    label = price_label(price_rub)
    return f"{title} — {label}" if label else title


def urgency_label(key: str) -> str:
    return config.URGENCY_LABELS.get(key, key)


# ── Карточки заявок ──────────────────────────────────────────────────────────

def request_card_for_staff(
    req, services, *, tz: str | None = None, title: str = "🚗 <b>НОВАЯ ЗАЯВКА</b>"
) -> str:
    text = (
        f"{title} {request_number(req['seq'])}\n"
        "─────────────────────\n"
        f"👤 <b>Клиент:</b> {h(req['client_name'])}\n"
        f"📞 <b>Телефон:</b> <code>{h(format_phone(req['phone']))}</code>\n"
        f"💬 <b>Telegram ID:</b> <code>{req['idclienttg']}</code>\n\n"
        f"🚙 <b>Автомобиль:</b> {h(req['brand'])} {h(req['model'])}\n"
        f"🔢 <b>Гос. номер:</b> <code>{h(req['plate'])}</code>\n\n"
    )

    priced = [item for item in services if item["price_rub"] is not None]
    lines = "".join(
        f"  • {titled_price(h(item['title']), item['price_rub'])}\n"
        for item in services
    )
    text += f"🔧 <b>Услуги:</b>\n{lines}"

    if priced:
        total = sum(item["price_rub"] for item in priced)
        # Оговорка обязательна: без неё сумма выглядит полной, хотя часть
        # работ в неё не вошла
        note = "" if len(priced) == len(services) else " <i>(часть работ — по запросу)</i>"
        text += "💰 <b>Итого:</b> от " + f"{total:,}".replace(",", " ") + f" ₽{note}\n"

    text += f"⚡ <b>Срочность:</b> {h(urgency_label(req['urgency']))}\n"

    if req["comment"]:
        text += f"\n💬 <b>Комментарий:</b>\n<i>{h(req['comment'])}</i>\n"
    text += (
        f"\n⏰ {local_dt(req['createdate'], tz)}\n"
        f"📌 <b>Статус:</b> {status_label(req['status'])}"
    )
    return text


def request_line_for_staff(req, tz: str | None = None) -> str:
    """Компактная строка для списка заявок сервиса."""
    overdue = ""
    if req["status"] == "new" and req["createdate"]:
        age = datetime.now(req["createdate"].tzinfo) - req["createdate"]
        if age.total_seconds() > 24 * 3600:
            overdue = " ⏳<i>висит &gt;24ч</i>"
    return (
        f"\n• {request_number(req['seq'])} <b>{h(req['client_name'])}</b> | "
        f"{status_label(req['status'])}{overdue}\n"
        f"  📞 <code>{h(format_phone(req['phone']))}</code> | "
        f"🚗 {h(req['brand'])} {h(req['model'])} ({h(req['plate'])})\n"
        f"  🔧 {h(req['services_summary'] or '—')} | "
        f"⚡ {h(urgency_label(req['urgency']))}\n"
        f"  🕒 {local_dt(req['createdate'], tz)}\n"
    )


def request_line_for_client(req) -> str:
    return (
        f"{request_number(req['seq'])} 🚗 <b>{h(req['brand'])} {h(req['model'])}</b>\n"
        f"   Сервис: {h(req['service_name'] or '—')}\n"
        f"   Услуги: {h(req['services_summary'] or '—')}\n"
        f"   Статус: {status_label(req['status'])}\n"
        f"   Дата: {local_dt(req['createdate'])}\n\n"
    )


# ── Карточка сервиса ─────────────────────────────────────────────────────────

def service_card(svc, *, link: str, role: str, admins_text: str) -> str:
    role_label = "👑 Управляющий" if role == "owner" else "🔧 Администратор"
    return (
        f"<b>ℹ️ {h(svc['service_name'])}</b>\n\n"
        f"📞 Телефон: {h(format_phone(svc['service_number']))}\n"
        f"🏙 Город: {h(svc['city'])}\n"
        f"📍 Адрес: {h(svc['location_service'])}\n"
        f"🕐 Часовой пояс: {h(svc['timezone'])}\n"
        f"👥 Администраторы: {admins_text}\n"
        f"🔑 Ваша роль: {role_label}\n\n"
        f"🆔 <code>{svc['idservice']}</code>\n\n"
        f"🔗 <b>Ссылка для клиентов:</b>\n{h(link)}"
    )


def registration_summary(svc, link: str) -> str:
    return (
        "✅ <b>Сервис зарегистрирован!</b>\n\n"
        f"<b>Название:</b> {h(svc['service_name'])}\n"
        f"<b>Телефон:</b> {h(format_phone(svc['service_number']))}\n"
        f"<b>Город:</b> {h(svc['city'])}\n"
        f"<b>Адрес:</b> {h(svc['location_service'])}\n"
        f"<b>Управляющий:</b> вы (и первый администратор)\n\n"
        f"<b>ID сервиса:</b> <code>{svc['idservice']}</code>\n\n"
        "🔗 <b>Ссылка для клиентов</b> — разместите её там, где вас найдут:\n"
        f"{h(link)}\n\n"
        "Чтобы подключить сотрудников, нажмите «➕ Пригласить админа»."
    )


# ── Статистика ───────────────────────────────────────────────────────────────

def _humanize_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {minutes} мин"
    days, hours = divmod(hours, 24)
    return f"{days} д {hours} ч"


def stats_card(svc, stats, breakdown, avg_reaction_seconds: float | None) -> str:
    total = stats["total"] or 0
    done = stats["st_done"] or 0
    conversion = f"{done / total * 100:.0f}%" if total else "—"

    text = (
        f"📊 <b>Статистика — {h(svc['service_name'])}</b>\n"
        "─────────────────────\n"
        f"<b>Всего заявок:</b> {total}\n"
        f"• сегодня: {stats['today']}\n"
        f"• за 7 дней: {stats['week']}\n"
        f"• за 30 дней: {stats['month']}\n\n"
        "<b>По статусам:</b>\n"
        f"🆕 новые: {stats['st_new']}\n"
        f"🔧 в работе: {stats['st_work']}\n"
        f"🏁 выполнены: {stats['st_done']}\n"
        f"❌ отказы: {stats['st_rejected']}\n"
        f"🚫 отмены клиентом: {stats['st_cancelled']}\n\n"
        f"<b>Конверсия:</b> {conversion}\n"
        f"<b>Среднее время реакции (30 дней):</b> {_humanize_seconds(avg_reaction_seconds)}\n"
    )
    if stats["overdue"]:
        text += f"\n⚠️ <b>Без реакции больше 24 часов:</b> {stats['overdue']}\n"

    if breakdown:
        text += "\n<b>Популярные работы:</b>\n"
        for row in breakdown[:5]:
            text += f"• {h(row['title'])} — {row['cnt']}\n"
    return text


# ── Утилита разбиения длинных сообщений ──────────────────────────────────────

def split_text(text: str, limit: int = 3800) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit and current:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    return chunks or [text]
