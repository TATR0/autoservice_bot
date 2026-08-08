"""
config.py — переменные окружения и справочники.

Все настройки читаются один раз при импорте. Обязательные переменные
проверяются в `validate()` — вызывается из app.py при старте.
"""

import logging
import os
import secrets

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ── Telegram ─────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME: str = os.getenv("BOT_USERNAME", "").strip().lstrip("@")

# Публичный URL сервиса на Render: https://xxx.onrender.com (без слэша в конце)
BASE_URL: str = os.getenv("BASE_URL", "").strip().rstrip("/")

# Секрет вебхука. Одновременно кусок URL и значение X-Telegram-Bot-Api-Secret-Token.
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "").strip()
if not WEBHOOK_SECRET:
    WEBHOOK_SECRET = secrets.token_urlsafe(32)
    logger.warning(
        "WEBHOOK_SECRET не задан — сгенерирован временный. "
        "Задайте его в переменных окружения, иначе при каждом перезапуске меняется URL вебхука."
    )

# Статика WebApp отдаётся этим же сервисом → CORS не нужен, API_BASE пустой.
WEBAPP_PATH: str = "/app/"
WEBAPP_URL: str = f"{BASE_URL}{WEBAPP_PATH}" if BASE_URL else ""

# ── Supabase / PostgreSQL ────────────────────────────────────────────────────
# ВАЖНО: строка Session Pooler'а, а не db.<ref>.supabase.co (тот IPv6-only).
DATABASE_URL: str = os.getenv("DATABASE_URL", "").strip()
DB_POOL_MIN: int = int(os.getenv("DB_POOL_MIN") or 1)
DB_POOL_MAX: int = int(os.getenv("DB_POOL_MAX") or 5)

# ── FSM ──────────────────────────────────────────────────────────────────────
# Пусто → хранилище состояний в PostgreSQL (таблица fsm_storage).
REDIS_URL: str = os.getenv("REDIS_URL", "").strip()

# ── Misc ─────────────────────────────────────────────────────────────────────
MASTER_CHAT_ID: int = int(os.getenv("MASTER_CHAT_ID") or 0)
DEFAULT_TIMEZONE: str = os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow")

# ── Лимиты и антиспам ────────────────────────────────────────────────────────
FREE_PLAN_SERVICE_LIMIT: int = int(os.getenv("FREE_PLAN_SERVICE_LIMIT") or 1)
REQUEST_COOLDOWN_SECONDS: int = int(os.getenv("REQUEST_COOLDOWN_SECONDS") or 60)
MAX_ACTIVE_REQUESTS_PER_CLIENT: int = int(os.getenv("MAX_ACTIVE_REQUESTS") or 3)
INIT_DATA_MAX_AGE: int = int(os.getenv("INIT_DATA_MAX_AGE") or 3600)
INVITE_TTL_DAYS: int = int(os.getenv("INVITE_TTL_DAYS") or 7)

# ── Справочники ──────────────────────────────────────────────────────────────

# Шаблонный набор услуг: копируется сервису при регистрации, дальше
# управляющий правит список сам (handlers/catalog.py).
DEFAULT_SERVICE_TITLES: tuple[str, ...] = (
    "Диагностика",
    "Замена масла",
    "Шины и диски",
    "Тормозная система",
    "Ремонт двигателя",
    "Коробка передач",
    "Подвеска",
    "Кузовные работы",
    "Другое",
)

URGENCY_LABELS: dict[str, str] = {
    "low":    "Обычный (7+ дней)",
    "medium": "Средний (3–5 дней)",
    "high":   "Срочный (1–2 дня)",
    "urgent": "Очень срочный (сегодня)",
}

# Полный набор статусов заявки — синхронизирован с CHECK-констрейнтом в schema.sql
REQUEST_STATUSES: tuple[str, ...] = (
    "new", "accepted", "called", "in_progress",
    "done", "rejected", "cancelled", "service_closed",
)

# Заявка «живая»: её ещё можно обрабатывать, она учитывается в лимитах клиента
ACTIVE_STATUSES: tuple[str, ...] = ("new", "accepted", "called", "in_progress")

REQUEST_STATUS_LABELS: dict[str, str] = {
    "new":            "🆕 Новая",
    "accepted":       "✅ Принята",
    "called":         "📞 Связались",
    "in_progress":    "🔧 В работе",
    "done":           "🏁 Выполнена",
    "rejected":       "❌ Отказ",
    "cancelled":      "🚫 Отменена клиентом",
    "service_closed": "🚪 Сервис закрыт",
}

# Из каких статусов разрешён переход в целевой.
# Используется в условном UPDATE — защищает от гонки двух администраторов.
STATUS_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "accepted":    ("new",),
    "called":      ("new", "accepted"),
    "in_progress": ("new", "accepted", "called"),
    "done":        ("accepted", "called", "in_progress"),
    "rejected":    ("new", "accepted", "called"),
    "cancelled":   ("new", "accepted", "called", "in_progress"),
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
    "in_progress": (
        "🔧 <b>Автомобиль принят в работу.</b>\n"
        "Мы сообщим, когда работы будут завершены."
    ),
    "done": (
        "🏁 <b>Работы завершены!</b>\n"
        "Спасибо, что выбрали наш сервис."
    ),
    "rejected": (
        "❌ <b>Ваша заявка отклонена.</b>\n"
        "К сожалению, сервис не может принять заявку. "
        "Попробуйте обратиться позже или выбрать другой сервис."
    ),
    "service_closed": (
        "🚪 <b>Сервис прекратил работу.</b>\n"
        "Ваша заявка закрыта. Выберите, пожалуйста, другой автосервис."
    ),
}


def validate() -> None:
    """Проверить обязательные переменные. Бросает RuntimeError при пропуске."""
    missing = [name for name in ("BOT_TOKEN", "DATABASE_URL") if not globals()[name]]
    if missing:
        raise RuntimeError(
            "Не заданы обязательные переменные окружения: " + ", ".join(missing)
        )
    if not BASE_URL:
        logger.warning(
            "BASE_URL не задан — вебхук не будет установлен, кнопки WebApp скрыты. "
            "Для продакшена задайте BASE_URL=https://<ваш-сервис>.onrender.com"
        )
    if not BOT_USERNAME:
        logger.warning("BOT_USERNAME не задан — ссылки вида t.me/<bot>?start=... будут битыми.")
