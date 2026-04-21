"""keyboards.py — все клавиатуры бота."""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from config import WEBAPP_URL


# ─────────────────────────────────────────────────────────────────────────────
# Reply keyboards
# ─────────────────────────────────────────────────────────────────────────────

def kb_client_main() -> ReplyKeyboardMarkup:
    """Главное меню клиента."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="🚗 Записаться в автосервис",
                    web_app=WebAppInfo(url=WEBAPP_URL) if WEBAPP_URL else None,
                )
            ],
            [
                KeyboardButton(text="📋 Мои заявки"),
                KeyboardButton(text="📝 Зарегистрировать сервис"),
            ],
        ],
        resize_keyboard=True,
    )


def kb_client_webservice(idservice: str) -> ReplyKeyboardMarkup:
    """Кнопка открытия формы для конкретного сервиса (по deep-link)."""
    url = f"{WEBAPP_URL}?service_id={idservice}"
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(
                text="🚗 Записаться онлайн",
                web_app=WebAppInfo(url=url),
            )
        ]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def kb_admin_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Заявки сервиса")],
            [KeyboardButton(text="👥 Администраторы"), KeyboardButton(text="ℹ️ О сервисе")],
        ],
        resize_keyboard=True,
    )


def kb_owner_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Заявки сервиса")],
            [KeyboardButton(text="👥 Администраторы"), KeyboardButton(text="ℹ️ О сервисе")],
            [KeyboardButton(text="➕ Добавить админа"), KeyboardButton(text="➖ Удалить админа")],
        ],
        resize_keyboard=True,
    )


def kb_cancel() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Inline keyboards
# ─────────────────────────────────────────────────────────────────────────────

def kb_request_actions(request_id: str) -> InlineKeyboardMarkup:
    """Кнопки управления заявкой для администратора."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять",   callback_data=f"req:accepted:{request_id}"),
            InlineKeyboardButton(text="📞 Позвонили", callback_data=f"req:called:{request_id}"),
        ],
        [
            InlineKeyboardButton(text="❌ Отказать",  callback_data=f"req:rejected:{request_id}"),
        ],
    ])


def kb_select_service(services: list, action: str) -> InlineKeyboardMarkup:
    """Выбор сервиса из списка. action — префикс callback_data."""
    rows = []
    for svc in services:
        rows.append([
            InlineKeyboardButton(
                text=svc["service_name"],
                callback_data=f"{action}:{svc['idservice']}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_select_admin(admins: list) -> InlineKeyboardMarkup:
    rows = []
    for adm in admins:
        rows.append([
            InlineKeyboardButton(
                text=f"🆔 {adm['idusertg']}",
                callback_data=f"rmadmin:{adm['idusertg']}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
