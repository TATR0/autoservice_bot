"""keyboards.py — все клавиатуры и подписи кнопок бота."""

from __future__ import annotations

from urllib.parse import urlencode

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)

import config

# ── Подписи кнопок (импортируются хендлерами, чтобы не расходились) ──────────
BTN_BOOK           = "🚗 Записаться в автосервис"
BTN_BOOK_OWN       = "🚗 Записаться в свой сервис"
BTN_MY_REQUESTS    = "📋 Мои заявки"
BTN_REGISTER       = "📝 Зарегистрировать сервис"
BTN_SERVICE_REQS   = "📋 Заявки сервиса"
BTN_STATS          = "📊 Статистика"
BTN_ADMINS         = "👥 Администраторы"
BTN_INVITE         = "➕ Пригласить админа"
BTN_REMOVE_ADMIN   = "➖ Удалить админа"
BTN_SERVICES       = "🔧 Услуги"
BTN_ABOUT          = "ℹ️ О сервисе"
BTN_SWITCH         = "🔄 Сменить сервис"
BTN_LEAVE          = "🚪 Уйти из администраторов"
BTN_DELETE_SERVICE = "🗑 Удалить сервис"
BTN_CANCEL         = "❌ Отмена"


def webapp_url(service_id: str | None = None) -> str | None:
    """URL формы записи. None — если BASE_URL не задан (локальная разработка)."""
    if not config.WEBAPP_URL:
        return None
    if service_id:
        return f"{config.WEBAPP_URL}?{urlencode({'service_id': service_id})}"
    return config.WEBAPP_URL


def kb_open_webapp(service_id: str | None = None) -> InlineKeyboardMarkup | None:
    """
    Inline-кнопка, открывающая форму записи. None — если BASE_URL не задан.

    Форму нельзя открывать кнопкой reply-клавиатуры: такие мини-приложения
    задуманы под старый механизм sendData и подписанный initData от Telegram
    не получают (в хеше приходят только версия, платформа и тема). Сервер
    заявку без initData не принимает — иначе её можно было бы оформить от
    чужого имени. initData дают только inline-кнопки, кнопка меню, вложения
    и прямые ссылки на мини-приложение.
    """
    url = webapp_url(service_id)
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚗 Открыть форму записи", web_app=WebAppInfo(url=url))
    ]])


# ── Reply-клавиатуры ─────────────────────────────────────────────────────────

def kb_client_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_BOOK)],
            [KeyboardButton(text=BTN_MY_REQUESTS), KeyboardButton(text=BTN_REGISTER)],
        ],
        resize_keyboard=True,
    )


def kb_client_service(idservice: str) -> ReplyKeyboardMarkup:
    """Кнопка записи в конкретный сервис (переход по deep-link)."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_BOOK)],
            [KeyboardButton(text=BTN_MY_REQUESTS)],
        ],
        resize_keyboard=True,
    )


def kb_owner_main(idservice: str, *, many_services: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_SERVICE_REQS), KeyboardButton(text=BTN_STATS)],
        [KeyboardButton(text=BTN_INVITE), KeyboardButton(text=BTN_REMOVE_ADMIN)],
        [KeyboardButton(text=BTN_ADMINS), KeyboardButton(text=BTN_ABOUT)],
        [KeyboardButton(text=BTN_SERVICES)],
        [KeyboardButton(text=BTN_BOOK_OWN)],
    ]
    if many_services:
        rows.append([KeyboardButton(text=BTN_SWITCH)])
    rows.append([KeyboardButton(text=BTN_MY_REQUESTS), KeyboardButton(text=BTN_REGISTER)])
    rows.append([KeyboardButton(text=BTN_DELETE_SERVICE)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def kb_admin_main(idservice: str, *, many_services: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_SERVICE_REQS), KeyboardButton(text=BTN_STATS)],
        [KeyboardButton(text=BTN_ADMINS), KeyboardButton(text=BTN_ABOUT)],
        [KeyboardButton(text=BTN_BOOK_OWN)],
    ]
    if many_services:
        rows.append([KeyboardButton(text=BTN_SWITCH)])
    rows.append([KeyboardButton(text=BTN_MY_REQUESTS), KeyboardButton(text=BTN_LEAVE)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def kb_cancel() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
    )


def kb_remove() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


# ── Inline-клавиатуры ────────────────────────────────────────────────────────

def kb_request_actions(request_id: str, status: str) -> InlineKeyboardMarkup | None:
    """
    Кнопки под карточкой заявки. Показываются только переходы,
    допустимые из текущего статуса.
    """
    buttons = [
        ("✅ Принять",   "accepted"),
        ("📞 Позвонили", "called"),
        ("🔧 В работу",  "in_progress"),
        ("🏁 Готово",    "done"),
        ("❌ Отказать",  "rejected"),
    ]
    available = [
        InlineKeyboardButton(text=text, callback_data=f"req:{target}:{request_id}")
        for text, target in buttons
        if status in config.STATUS_TRANSITIONS.get(target, ())
    ]
    if not available:
        return None

    rows = [available[i:i + 2] for i in range(0, len(available), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_client_request_actions(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚫 Отменить заявку", callback_data=f"cancelreq:{request_id}")
    ]])


def kb_select_service(services: list, action: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=svc["service_name"],
            callback_data=f"{action}:{svc['idservice']}",
        )]
        for svc in services
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_select_admin(admins: list, owner_id: int, titles: dict[int, str]) -> InlineKeyboardMarkup:
    """Список админов на удаление. Владельца снять нельзя — его в списке нет."""
    rows = [
        [InlineKeyboardButton(
            text=titles.get(adm["idusertg"], f"ID {adm['idusertg']}"),
            callback_data=f"rmadmin:{adm['idusertg']}",
        )]
        for adm in admins if adm["idusertg"] != owner_id
    ]
    rows.append([InlineKeyboardButton(text=BTN_CANCEL, callback_data="cancel_action")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_catalog(items: list) -> InlineKeyboardMarkup:
    """Список услуг: тап по услуге ведёт к её удалению."""
    rows = [
        [InlineKeyboardButton(
            text=f"❌ {item['title']}",
            callback_data=f"svcdel:{item['idcatalog']}",
        )]
        for item in items
    ]
    rows.append([InlineKeyboardButton(
        text="➕ Добавить услугу", callback_data="svcadd"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_confirm(action: str, payload: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data=f"{action}:{payload}"),
        InlineKeyboardButton(text="❌ Нет", callback_data="cancel_action"),
    ]])


def kb_accept_invite(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Стать администратором", callback_data=f"invite_accept:{token}"),
        InlineKeyboardButton(text="❌ Отказаться", callback_data="cancel_action"),
    ]])


def kb_share_invite(link: str, service_name: str) -> InlineKeyboardMarkup:
    """Кнопка «переслать приглашение» — открывает выбор чата в Telegram."""
    share = f"https://t.me/share/url?{urlencode({'url': link, 'text': f'Приглашение администратора сервиса «{service_name}»'})}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📤 Отправить приглашение", url=share)
    ]])
