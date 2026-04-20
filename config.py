import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ─────────────────────────────────────────────────────────────────
BOT_TOKEN: str   = os.getenv("BOT_TOKEN", "")
BOT_USERNAME: str = os.getenv("BOT_USERNAME", "")      # без @
WEBAPP_URL: str  = os.getenv("WEBAPP_URL", "")         # https://... (GitHub Pages / Vercel)

# ── Supabase / PostgreSQL ─────────────────────────────────────────────────────
DATABASE_URL: str = os.getenv("DATABASE_URL", "")      # postgresql://user:pass@host:port/db

# ── Misc ──────────────────────────────────────────────────────────────────────
MASTER_CHAT_ID: int = int(os.getenv("MASTER_CHAT_ID", "0"))   # куда слать «потерянные» заявки

# ── Справочники ───────────────────────────────────────────────────────────────
SERVICE_TYPES: dict[str, str] = {
    "diagnostic":   "Диагностика",
    "oil-change":   "Замена масла",
    "tires":        "Шины и диски",
    "brake":        "Тормозная система",
    "engine":       "Ремонт двигателя",
    "transmission": "Коробка передач",
    "suspension":   "Подвеска",
    "body":         "Кузовные работы",
    "other":        "Другое",
}

URGENCY_LABELS: dict[str, str] = {
    "low":    "Обычный (7+ дней)",
    "medium": "Средний (3–5 дней)",
    "high":   "Срочный (1–2 дня)",
    "urgent": "Очень срочный (сегодня)",
}

REQUEST_STATUS_LABELS: dict[str, str] = {
    "new":      "🆕 Новая",
    "accepted": "✅ Принята",
    "called":   "📞 Связались",
    "rejected": "❌ Отказ",
}

# Уведомления клиенту при смене статуса
CLIENT_NOTIFICATIONS: dict[str, str] = {
    "accepted": (
        "✅ <b>Ваша заявка принята!</b>\n"
        "Администратор сервиса подтвердил запись. Ожидайте звонка."
    ),
    "called": (
        "📞 <b>С вами пытаются связаться!</b>\n"
        "Проверьте телефон — сервис звонит для уточнения деталей."
    ),
    "rejected": (
        "❌ <b>Ваша заявка отклонена.</b>\n"
        "К сожалению, сервис не может принять заявку. "
        "Попробуйте обратиться позже или выбрать другой сервис."
    ),
}
