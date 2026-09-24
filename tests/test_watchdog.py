"""
Сторож: правила, по которым он решает будить владельца.

Проверяется чистая часть — сами решения. Ошибка здесь стоит либо письма
среди ночи из-за моргнувшей сети, либо молчания в тот единственный раз,
когда бот действительно лёг.
"""

import watchdog
from watchdog import Alarm, Problem, certificate_problem, disk_problem, webhook_problem

BASE = "https://bot.myservice.ru:8443"
GOOD_INFO = {
    "url": f"{BASE}/webhook/секрет",
    "pending_update_count": 0,
    "has_custom_certificate": False,
}


# ── Вебхук ──────────────────────────────────────────────────────────────────

def test_working_webhook_is_not_a_problem():
    assert webhook_problem(GOOD_INFO, BASE, 50) is None


def test_missing_webhook_is_caught():
    """Самый частый молчаливый отказ: бот жив, а апдейтов ему никто не шлёт."""
    problem = webhook_problem({"url": "", "pending_update_count": 0}, BASE, 50)
    assert problem and "не установлен" in problem.text


def test_webhook_pointing_elsewhere_is_caught():
    """
    Второй бот на том же токене переставит вебхук на себя, и заявки уедут
    к нему. Снаружи это выглядит как «наш бот молчит».
    """
    other = {"url": "https://other.example.com/webhook/x", "pending_update_count": 0}
    problem = webhook_problem(other, BASE, 50)
    assert problem and "чужой адрес" in problem.text


def test_secret_never_leaves_the_watchdog():
    """Адрес вебхука содержит секрет — в письме владельцу ему не место."""
    problem = webhook_problem(
        {"url": "https://other.example.com/webhook/оченьсекретно",
         "pending_update_count": 0},
        BASE, 50,
    )
    assert problem and "оченьсекретно" not in problem.text


def test_delivery_error_with_a_queue_is_caught():
    info = {**GOOD_INFO, "last_error_message": "Connection refused",
            "pending_update_count": 7}
    problem = webhook_problem(info, BASE, 50)
    assert problem and "Connection refused" in problem.text


def test_old_error_without_a_queue_is_left_alone():
    """
    Telegram помнит последнюю ошибку и после того, как доставка наладилась.
    Пустая очередь означает, что всё уже доставлено, — будить незачем.
    """
    info = {**GOOD_INFO, "last_error_message": "Connection refused",
            "pending_update_count": 0}
    assert webhook_problem(info, BASE, 50) is None


def test_long_queue_is_caught_even_without_an_error():
    info = {**GOOD_INFO, "pending_update_count": 200}
    problem = webhook_problem(info, BASE, 50)
    assert problem and "200" in problem.text


def test_a_burst_of_updates_is_not_a_queue():
    assert webhook_problem({**GOOD_INFO, "pending_update_count": 5}, BASE, 50) is None


# ── Диск и сертификат ───────────────────────────────────────────────────────

def test_full_disk_is_caught():
    problem = disk_problem(free_bytes=2 * 1024 ** 3, total_bytes=50 * 1024 ** 3,
                           min_free_pct=10)
    assert problem and "перестанет писать" in problem.text


def test_roomy_disk_is_left_alone():
    assert disk_problem(20 * 1024 ** 3, 50 * 1024 ** 3, 10) is None


def test_certificate_that_should_have_renewed_is_caught():
    problem = certificate_problem(days_left=3, min_days=7)
    assert problem and "3 дн" in problem.text


def test_expired_certificate_says_what_it_breaks():
    problem = certificate_problem(days_left=-1, min_days=7)
    assert problem and "форма записи" in problem.text


def test_fresh_certificate_is_left_alone():
    assert certificate_problem(days_left=60, min_days=7) is None


# ── Когда писать ────────────────────────────────────────────────────────────

def test_a_single_blip_does_not_wake_the_owner():
    """Один неудачный круг — моргнувшая сеть. Письмо за это читать перестанут."""
    alarm = Alarm(patience=2)
    assert alarm.update([Problem("db", "база не отвечает")]) is None


def test_the_same_trouble_twice_does():
    alarm = Alarm(patience=2)
    alarm.update([Problem("db", "база не отвечает")])
    message = alarm.update([Problem("db", "база не отвечает")])
    assert message and "база не отвечает" in message


def test_the_same_trouble_is_reported_once():
    alarm = Alarm(patience=2)
    for _ in range(2):
        alarm.update([Problem("db", "база не отвечает")])
    assert alarm.update([Problem("db", "база не отвечает")]) is None


def test_a_new_trouble_is_reported_even_if_another_is_known():
    alarm = Alarm(patience=1)
    alarm.update([Problem("db", "база не отвечает")])
    message = alarm.update([
        Problem("db", "база не отвечает"), Problem("disk", "места нет"),
    ])
    assert message and "места нет" in message
    assert "база не отвечает" in message, "в письме — всё, что сейчас сломано"


def test_recovery_is_announced_once():
    alarm = Alarm(patience=1)
    alarm.update([Problem("db", "база не отвечает")])
    assert alarm.healed([]), "было о чём писать — надо сказать, что починилось"
    alarm.update([])
    assert not alarm.healed([]), "и сказать один раз"


def test_quiet_watchdog_says_nothing_about_recovery():
    """Ничего не ломалось — не о чем и отчитываться."""
    alarm = Alarm(patience=1)
    alarm.update([])
    assert not alarm.healed([])


def test_trouble_that_comes_and_goes_starts_the_count_over():
    """
    Беда должна повториться подряд, а не «когда-нибудь дважды»: иначе редкие
    сетевые обрывы за неделю сложатся в письмо на ровном месте.
    """
    alarm = Alarm(patience=2)
    alarm.update([Problem("db", "база не отвечает")])
    alarm.update([])
    assert alarm.update([Problem("db", "база не отвечает")]) is None


# ── Проверки с вводом-выводом ───────────────────────────────────────────────

def test_unreachable_telegram_does_not_raise_a_false_alarm(monkeypatch):
    """
    Недоступный Telegram — беда не наша и чинится сама. Молчание Telegram не
    должно выглядеть как сломанный вебхук.
    """
    def _boom(_url, timeout=10.0):
        raise OSError("сеть недоступна")

    monkeypatch.setattr(watchdog, "_get_json", _boom)
    import asyncio
    assert asyncio.run(watchdog.check_webhook("123:ABC", BASE, 50)) is None


def test_health_check_reports_a_silent_bot(monkeypatch):
    import asyncio

    def _boom(url, timeout=10):
        raise OSError("connection refused")

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", _boom)
    problem = asyncio.run(watchdog.check_health("http://app:8080/healthz"))
    assert problem and "не отвечает" in problem.text
