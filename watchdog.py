"""
watchdog.py — присмотр за живым стендом.

Бот умеет падать молча. Процесс жив, /healthz отвечает 200 — а вебхук слетел,
и Telegram складывает апдейты в очередь; или кончилось место, и база перестала
писать; или сертификат не продлился, и через несколько дней всё встанет разом.
Узнать об этом сейчас можно одним способом: от клиента, который не дождался
ответа.

Сторож ходит по тем же местам, куда пошёл бы человек, и пишет владельцу в
Telegram. Правила те же, что у вывоза копий: одно письмо на событие, а не
каждый круг, и второе — когда починилось.

Чего он не может: сообщить, что машина умерла, — он умрёт вместе с ней.
Для этого есть WATCHDOG_PING_URL: внешняя «кнопка живости», которую сторож
нажимает каждый удачный круг. Перестали нажимать — сторонний сервис напишет
сам. Молчание и есть сигнал.

Запускается из docker compose (сервис watchdog). Разовая проверка:
    docker compose run --rm watchdog python watchdog.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Problem:
    """key — чтобы узнать беду в следующем круге, text — чтобы прочёл человек."""

    key: str
    text: str


# ── Решения без ввода-вывода ─────────────────────────────────────────────────
# Отдельно от проверок: сеть в тестах поднимать незачем, а ошибиться здесь
# дороже — именно эти правила решают, будить владельца или нет.

def webhook_problem(info: dict, base_url: str, max_pending: int) -> Problem | None:
    """
    Что не так с доставкой апдейтов, по ответу getWebhookInfo.

    Молчащий бот чаще всего именно здесь: процесс работает, а Telegram либо
    не знает, куда слать, либо получает ошибку и копит очередь.
    """
    url = str(info.get("url") or "")
    if not url:
        return Problem("webhook", "вебхук не установлен — Telegram не шлёт боту апдейты")

    # Сравниваем только начало: дальше в адресе секрет, и ему не место
    # ни в сообщении владельцу, ни в журнале
    if base_url and not url.startswith(base_url):
        return Problem(
            "webhook",
            "вебхук ведёт на чужой адрес — апдейты уходят не этому боту",
        )

    error = str(info.get("last_error_message") or "")
    pending = int(info.get("pending_update_count") or 0)
    if error and pending > 0:
        # Ошибка без очереди — прошлая беда, уже пережитая: Telegram хранит
        # последнюю ошибку и после того, как доставка наладилась
        return Problem("webhook", f"Telegram не может доставить апдейты: {error}")
    if pending > max_pending:
        return Problem(
            "webhook",
            f"в очереди Telegram {pending} необработанных апдейтов",
        )
    return None


def disk_problem(free_bytes: int, total_bytes: int, min_free_pct: int) -> Problem | None:
    """
    Кончившееся место — не про неудобство: PostgreSQL перестаёт писать, и
    заявки начинают теряться на каждой попытке.
    """
    if total_bytes <= 0:
        return None
    percent = free_bytes * 100 / total_bytes
    if percent >= min_free_pct:
        return None
    gib = free_bytes / 1024 ** 3
    return Problem(
        "disk",
        f"на диске осталось {percent:.0f}% ({gib:.1f} ГБ) — база перестанет писать",
    )


def certificate_problem(days_left: float, min_days: int) -> Problem | None:
    """
    Caddy продлевает сертификат сам, поэтому важен не срок, а то, что
    продление не состоялось: за неделю до конца это ещё можно починить.
    """
    if days_left > min_days:
        return None
    if days_left <= 0:
        return Problem("cert", "сертификат просрочен — форма записи не открывается")
    return Problem(
        "cert",
        f"сертификату осталось {days_left:.0f} дн., а продлиться он должен был раньше",
    )


class Alarm:
    """
    Кому и когда писать.

    Терпение (patience) — сколько кругов подряд беда должна повториться,
    прежде чем звать человека: одиночный обрыв сети чинится сам, а письмо о
    нём приучает читать эти письма по диагонали.
    """

    def __init__(self, patience: int = 2) -> None:
        self.patience = patience
        self._seen: dict[str, int] = {}
        self._told: set[str] = set()

    def update(self, problems: list[Problem]) -> str | None:
        """Текст письма владельцу или None, если писать не о чем."""
        current = {p.key: p.text for p in problems}

        for key in list(self._seen):
            if key not in current:
                del self._seen[key]
        for key in current:
            self._seen[key] = self._seen.get(key, 0) + 1

        ripe = {k: t for k, t in current.items() if self._seen[k] >= self.patience}
        fresh = set(ripe) - self._told
        self._told = set(ripe)

        if fresh:
            lines = "\n".join(f"• {text}" for text in ripe.values())
            return (
                f"⚠️ С ботом что-то не так.\n\n{lines}\n\n"
                "Логи: docker logs autoservice_watchdog"
            )
        # Беда та же — второго письма не будет. Починилось — про это скажет
        # healed(), и сказать он должен до update(), пока память не очищена
        return None

    def healed(self, problems: list[Problem]) -> bool:
        """Было о чём писать, а теперь не о чем."""
        return not problems and bool(self._told)


# ── Проверки ─────────────────────────────────────────────────────────────────

def _get_json(url: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


async def check_health(url: str) -> Problem | None:
    def _call() -> Problem | None:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                if response.status != 200:
                    return Problem("health", f"бот отвечает {response.status}, а не 200")
        except Exception as exc:
            return Problem("health", f"бот не отвечает на {url}: {exc}")
        return None

    return await asyncio.to_thread(_call)


async def check_database(dsn: str) -> Problem | None:
    if not dsn:
        return None
    import asyncpg

    try:
        conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=15)
    except Exception as exc:
        return Problem("db", f"база не отвечает: {exc}")
    try:
        await conn.fetchval("SELECT 1")
    except Exception as exc:
        return Problem("db", f"база отвечает, но не читается: {exc}")
    finally:
        await conn.close()
    return None


async def check_webhook(token: str, base_url: str, max_pending: int) -> Problem | None:
    if not token:
        return None

    def _call() -> Problem | None:
        try:
            payload = _get_json(f"https://api.telegram.org/bot{token}/getWebhookInfo")
        except Exception as exc:
            # Недоступный Telegram — беда не наша и чинится сама; сказать о
            # ней в журнал стоит, будить владельца — нет
            logger.warning("Не удалось спросить Telegram о вебхуке: %s", exc)
            return None
        return webhook_problem(payload.get("result") or {}, base_url, max_pending)

    return await asyncio.to_thread(_call)


async def check_disk(path: str, min_free_pct: int) -> Problem | None:
    def _call() -> Problem | None:
        try:
            usage = shutil.disk_usage(path)
        except OSError as exc:
            logger.warning("Не удалось посмотреть место на %s: %s", path, exc)
            return None
        return disk_problem(usage.free, usage.total, min_free_pct)

    return await asyncio.to_thread(_call)


async def check_certificate(base_url: str, min_days: int) -> Problem | None:
    host = urlparse(base_url).hostname
    port = urlparse(base_url).port or 443
    if not host:
        return None

    def _call() -> Problem | None:
        try:
            context = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=10) as raw:
                with context.wrap_socket(raw, server_hostname=host) as tls:
                    cert = tls.getpeercert()
        except Exception as exc:
            # Свой публичный адрес изнутри машины доступен не всегда (NAT,
            # фильтры провайдера). Не достучались — значит не проверили, а не
            # «сертификат плох»: ложная тревога дороже пропущенной проверки
            logger.warning("Сертификат %s:%s не проверить: %s", host, port, exc)
            return None
        raw_until = cert.get("notAfter") if cert else None
        if not raw_until:
            return None
        until = datetime.strptime(raw_until, "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=timezone.utc
        )
        days_left = (until - datetime.now(timezone.utc)).total_seconds() / 86400
        return certificate_problem(days_left, min_days)

    return await asyncio.to_thread(_call)


async def collect() -> list[Problem]:
    """Один обход всех проверок. Порядок — от самого частого к самому редкому."""
    found = await asyncio.gather(
        check_health(config.WATCHDOG_HEALTH_URL),
        check_database(config.DATABASE_URL),
        check_webhook(config.BOT_TOKEN, config.BASE_URL, config.WATCHDOG_MAX_PENDING),
        check_disk(config.WATCHDOG_DISK_PATH, config.WATCHDOG_DISK_MIN_FREE_PCT),
        check_certificate(config.BASE_URL, config.WATCHDOG_CERT_MIN_DAYS),
    )
    return [problem for problem in found if problem]


# ── Связь с внешним миром ────────────────────────────────────────────────────

def notify(text: str) -> None:
    """Письмо владельцу. Не через aiogram: сторож не должен зависеть от бота."""
    if not config.BOT_TOKEN or not config.BOT_OWNER_IDS:
        logger.warning("Некому написать (нет BOT_TOKEN или BOT_OWNER_IDS): %s", text)
        return
    for chat_id in config.BOT_OWNER_IDS:
        body = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(request, timeout=15).close()
        except Exception as exc:
            logger.warning("Не удалось написать владельцу %s: %s", chat_id, exc)


def ping(healthy: bool) -> None:
    """
    Нажать внешнюю кнопку живости. Пусто — слоя нет, и предполётная проверка
    об этом предупреждала: пропавшую машину изнутри заметить нельзя.
    """
    url = config.WATCHDOG_PING_URL
    if not url:
        return
    if not healthy:
        url = url.rstrip("/") + "/fail"
    try:
        urllib.request.urlopen(url, timeout=10).close()
    except Exception as exc:
        logger.warning("Внешний сторож недоступен: %s", exc)


async def watch_forever(alarm: Alarm | None = None) -> None:
    alarm = alarm or Alarm()
    logger.info(
        "Сторож запущен: круг раз в %d с, терпение %d круга",
        config.WATCHDOG_TICK_SECONDS, alarm.patience,
    )
    while True:
        try:
            problems = await collect()
            healed = alarm.healed(problems)
            message = alarm.update(problems)
            if message:
                notify(message)
            elif healed:
                notify("✅ С ботом снова всё в порядке.")
            if problems:
                logger.warning(
                    "Замечено: %s", "; ".join(problem.text for problem in problems)
                )
            else:
                logger.info("Всё в порядке")
            ping(not problems)
        except Exception:
            # Свалившийся сторож — худший из отказов: он ещё и молчит об этом
            logger.exception("Круг сторожа не удался")
        await asyncio.sleep(config.WATCHDOG_TICK_SECONDS)


async def once() -> int:
    problems = await collect()
    for problem in problems:
        print(f"[!] {problem.text}")
    if not problems:
        print("Всё в порядке")
    return 1 if problems else 0


if __name__ == "__main__":
    import sys

    import os

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s  %(levelname)-8s  watchdog — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if "--loop" in sys.argv:
        asyncio.run(watch_forever())
    else:
        sys.exit(asyncio.run(once()))
