#!/usr/bin/env python3
"""
Что случится, если включить SUBSCRIPTION_ENFORCED.

Гейты включаются одной строкой в .env, а последствия наступают в ту же
секунду: сервис без оплаченного срока пропадает из поиска, его форма не
открывается, заявки не принимаются. Отчёт показывает, кто именно погаснет,
до того как это увидят клиенты.

    python scripts/preflight_subscription.py

Код возврата 1 — включать рано, сначала разобраться со списком.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from database import db  # noqa: E402

OK = "[ ок ]"
WARN = "[ !  ]"
STOP = "[стоп]"


async def report() -> int:
    problems = 0
    print("Проверка перед включением SUBSCRIPTION_ENFORCED")
    print("Сейчас гейты", "ВКЛЮЧЕНЫ" if config.SUBSCRIPTION_ENFORCED else "выключены")
    print()

    async with db.pool.acquire() as conn:
        column = await conn.fetchval(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'services' AND column_name = 'paid_until'
            """
        )
        if not column:
            print(STOP, "в базе нет колонки services.paid_until — schema.sql не применён")
            return 1

        total = await conn.fetchval("SELECT count(*) FROM services WHERE idrecstatus = 0")
        dark = await conn.fetch(
            """
            SELECT idservice, service_name, city, paid_until
            FROM services
            WHERE idrecstatus = 0 AND (paid_until IS NULL OR paid_until <= now())
            ORDER BY paid_until NULLS FIRST
            """
        )
        soon = await conn.fetch(
            """
            SELECT service_name, city, paid_until
            FROM services
            WHERE idrecstatus = 0 AND paid_until > now()
              AND paid_until <= now() + interval '7 days'
            ORDER BY paid_until
            """
        )
        booked = await conn.fetchval(
            """
            SELECT count(*)
            FROM requests r
            JOIN services s ON s.idservice = r.idservice AND s.idrecstatus = 0
            WHERE r.idrecstatus = 0
              AND r.status = ANY($1::text[])
              AND r.scheduled_at > now()
              AND (s.paid_until IS NULL OR s.paid_until <= now())
            """,
            list(config.ACTIVE_STATUSES),
        )

    print(f"Активных сервисов: {total}")

    if dark:
        problems += 1
        print(STOP, f"погаснут сразу после включения: {len(dark)}")
        for row in dark[:20]:
            when = "срок не выдавался" if row["paid_until"] is None else f"истёк {row['paid_until']:%d.%m.%Y}"
            print(f"       {row['service_name']} ({row['city']}) — {when}")
            print(f"       /extend {row['idservice']} 30")
        if len(dark) > 20:
            print(f"       ... и ещё {len(dark) - 20}")
    else:
        print(OK, "сервисов с пустым или истёкшим сроком нет")

    if booked:
        print(WARN, f"у этих сервисов уже записаны клиенты: {booked} заявок в будущем")
        print("       записанные приедут в своё время, но новых заявок сервис не примет")

    if soon:
        print(WARN, f"истекает в ближайшую неделю: {len(soon)}")
        for row in soon:
            print(f"       {row['service_name']} ({row['city']}) — до {row['paid_until']:%d.%m.%Y}")

    if config.BOT_OWNER_IDS:
        print(OK, f"BOT_OWNER_IDS заполнен ({len(config.BOT_OWNER_IDS)})")
    else:
        problems += 1
        print(STOP, "BOT_OWNER_IDS пуст — продлить подписку не сможет никто, включая вас")

    free = [plan.label for plan in config.STAR_PLANS if plan.stars <= 0]
    if free:
        problems += 1
        print(STOP, "тарифы с нулевой ценой: " + ", ".join(free))
    else:
        print(OK, "цены тарифов заданы: " + ", ".join(
            f"{plan.label} — {plan.stars}" for plan in config.STAR_PLANS
        ))

    print()
    if problems:
        print("Включать рано: разберитесь с пунктами [стоп].")
    else:
        print("Можно включать: SUBSCRIPTION_ENFORCED=true и перезапуск.")
    return 1 if problems else 0


async def main() -> int:
    if not config.DATABASE_URL:
        print(STOP, "DATABASE_URL не задан")
        return 1
    await db.connect()
    try:
        return await report()
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
