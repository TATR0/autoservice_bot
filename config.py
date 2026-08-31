"""
config.py — переменные окружения и справочники.

Все настройки читаются один раз при импорте. Обязательные переменные
проверяются в `validate()` — вызывается из app.py при старте.
"""

import logging
import os
import secrets

from typing import NamedTuple
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

# База для тестов. Сюита чистит за собой настоящим DELETE, и делать это в
# боевой базе — вопрос времени, а не осторожности. Пусто — тесты идут в
# DATABASE_URL и предупреждают об этом на каждом прогоне
TEST_DATABASE_URL: str = os.getenv("TEST_DATABASE_URL", "").strip()
DB_POOL_MIN: int = int(os.getenv("DB_POOL_MIN") or 1)
DB_POOL_MAX: int = int(os.getenv("DB_POOL_MAX") or 5)

# ── FSM ──────────────────────────────────────────────────────────────────────
# Пусто → хранилище состояний в PostgreSQL (таблица fsm_storage).
REDIS_URL: str = os.getenv("REDIS_URL", "").strip()

# ── Misc ─────────────────────────────────────────────────────────────────────
MASTER_CHAT_ID: int = int(os.getenv("MASTER_CHAT_ID") or 0)
DEFAULT_TIMEZONE: str = os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow")


def _int_list(raw: str) -> tuple[int, ...]:
    """«1, 2 ,» → (1, 2). Пустые куски пропускаются: .env правят руками."""
    return tuple(int(part) for part in raw.split(",") if part.strip())


def _flag(name: str, *, default: bool) -> bool:
    """Да/нет из .env. Незнакомое значение — это опечатка, берём умолчание."""
    raw = os.getenv(name, "").strip().lower()
    if raw in ("1", "true", "yes", "on", "да"):
        return True
    if raw in ("0", "false", "no", "off", "нет"):
        return False
    return default


# Кому доступна команда /extend. Не MASTER_CHAT_ID: тот — чат для
# недоставленных уведомлений и вполне может оказаться группой, а у группы id
# отрицательный и с user id не совпадёт никогда
BOT_OWNER_IDS: tuple[int, ...] = _int_list(os.getenv("BOT_OWNER_IDS", ""))

# Верить ли заголовку X-Forwarded-For. Включать, только если перед ботом
# действительно стоит обратный прокси (Caddy, nginx): иначе адрес клиента
# в лимитах частоты назначает сам клиент, и лимит обходится одной строкой.
# Без прокси все запросы придут с одного адреса — это и есть правда о них
TRUST_PROXY: bool = _flag("TRUST_PROXY", default=False)

# Секрет эндпоинта напоминаний. Пусто — эндпоинт закрыт совсем
TICK_SECRET: str = os.getenv("TICK_SECRET", "").strip()

# Куда управляющему писать за продлением. Пусто — строку не показываем
SUPPORT_CONTACT: str = os.getenv("SUPPORT_CONTACT", "").strip()

# Пробный период нового сервиса, дней
TRIAL_DAYS: int = int(os.getenv("TRIAL_DAYS") or 5)

# Введена ли подписка в действие. Пока платить нечем — нечего и отключать:
# срок считается, журнал пишется, но ни один гейт не закрывается и ни одно
# письмо не уходит. Ставится в true в тот день, когда появится приём денег,
# и с этого момента механизм работает целиком — он собран и проверен заранее
SUBSCRIPTION_ENFORCED: bool = _flag("SUBSCRIPTION_ENFORCED", default=False)

# Как часто бот сам будит рассылку напоминаний. Раз в час: точности до часа
# хватает и суточному письму. Внешний крон не нужен — его пришлось бы
# настраивать на каждом новом сервере и он молча не работал бы, если забыли
REMINDER_TICK_SECONDS: int = int(os.getenv("REMINDER_TICK_SECONDS") or 3600)

# За сколько часов до записи напомнить клиенту. Три — поздно, чтобы забыть, и
# рано, чтобы успеть отменить и освободить окно сервису. Ноль выключает
# напоминания совсем: рассылка тогда не ходит в базу вовсе
APPOINTMENT_REMINDER_HOURS: int = int(os.getenv("APPOINTMENT_REMINDER_HOURS") or 3)

# Сколько дней хранить персональные данные в закрытых заявках. Ноль — хранить
# бессрочно: срок назначает управляющий сервисом, а не мы за него. Когда срок
# задан, старые заявки обезличиваются сами — строки остаются, имени, телефона,
# машины и комментария в них больше нет
PII_RETENTION_DAYS: int = int(os.getenv("PII_RETENTION_DAYS") or 0)


class StarPlan(NamedTuple):
    """Тариф подписки: сколько дней, сколько звёзд и как назвать на кнопке."""
    days: int
    stars: int
    label: str


# Подпись лежит рядом с числами, а не собирается из дней: «12 месяцев» читается
# лучше, чем «365 дней», а делить дни на тридцать ради подписи — врать в мелочах.
# Цены целые: звёзды не дробятся. Добавить четвёртый тариф — дописать строку
STAR_PLANS: tuple[StarPlan, ...] = (
    StarPlan(30, int(os.getenv("STARS_PRICE_1M") or 150), "1 месяц"),
    StarPlan(90, int(os.getenv("STARS_PRICE_3M") or 400), "3 месяца"),
    StarPlan(365, int(os.getenv("STARS_PRICE_12M") or 1350), "12 месяцев"),
)


def plan_by_days(days: int) -> StarPlan | None:
    """Тариф по числу дней. None — такого тарифа нет, цену взять неоткуда."""
    return next((plan for plan in STAR_PLANS if plan.days == days), None)

# ── Лимиты и антиспам ────────────────────────────────────────────────────────
FREE_PLAN_SERVICE_LIMIT: int = int(os.getenv("FREE_PLAN_SERVICE_LIMIT") or 1)
REQUEST_COOLDOWN_SECONDS: int = int(os.getenv("REQUEST_COOLDOWN_SECONDS") or 60)
MAX_ACTIVE_REQUESTS_PER_CLIENT: int = int(os.getenv("MAX_ACTIVE_REQUESTS") or 3)
# Срок годности подписи Telegram WebApp. Чем он короче, тем меньше окно, в
# котором перехваченный initData можно переиграть. Форму заполняют за минуты,
# поэтому час был неоправданно щедрым.
INIT_DATA_MAX_AGE: int = int(os.getenv("INIT_DATA_MAX_AGE") or 600)
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

# Полный набор статусов заявки — синхронизирован с CHECK-констрейнтом в schema.sql
REQUEST_STATUSES: tuple[str, ...] = (
    "new", "accepted", "called", "in_progress",
    "done", "rejected", "cancelled", "service_closed",
)

# Заявка «живая»: её ещё можно обрабатывать, она учитывается в лимитах клиента
ACTIVE_STATUSES: tuple[str, ...] = ("new", "accepted", "called", "in_progress")

# Статусы, при которых заявка держит своё окно записи. Отказ, отмена и закрытие
# сервиса окно освобождают; «выполнена» держит навсегда — это уже история.
SLOT_HOLDING_STATUSES: tuple[str, ...] = (
    "new", "accepted", "called", "in_progress", "done",
)

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

# Клиент не должен знать, что у сервиса с оплатой: это не его дело и для
# сервиса унизительно. Телефон подставляется на месте
CLOSED_FOR_BOOKING = (
    "Сервис сейчас не принимает онлайн-запись. "
    "Позвоните, пожалуйста, по телефону: {phone}"
)

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
