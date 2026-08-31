#!/usr/bin/env python3
"""
Что Telegram думает о нашем вебхуке.

Бот, который «просто молчит», почти всегда молчит по одной из трёх причин:
вебхук стоит не на тот адрес, сертификат не принят, апдейты копятся с
ошибкой доставки. Всё это Telegram честно рассказывает — надо спросить.

    python scripts/check_webhook.py

Код возврата 1 — вебхук не в порядке.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Bot  # noqa: E402

import config  # noqa: E402


async def main() -> int:
    if not config.BOT_TOKEN:
        print("[стоп] BOT_TOKEN не задан")
        return 1

    bot = Bot(token=config.BOT_TOKEN)
    try:
        me = await bot.get_me()
        info = await bot.get_webhook_info()
    finally:
        await bot.session.close()

    expected = f"{config.BASE_URL}/webhook/{config.WEBHOOK_SECRET}" if config.BASE_URL else ""
    problems = 0

    print(f"Бот: @{me.username} (id {me.id})")
    # Секрет в URL не печатаем: вывод скрипта попадает в логи выката
    print(f"Вебхук: {info.url.split('/webhook/')[0] or '— не установлен'}/webhook/***")

    if not info.url:
        print("[стоп] вебхук не установлен — бот не получит ни одного сообщения")
        problems += 1
    elif expected and info.url != expected:
        print(f"[стоп] адрес не тот, что ждёт этот .env: должен быть {config.BASE_URL}/webhook/***")
        problems += 1

    if info.last_error_message:
        print(f"[стоп] последняя ошибка доставки: {info.last_error_message} ({info.last_error_date})")
        problems += 1

    if info.pending_update_count:
        print(f"[ !  ] в очереди {info.pending_update_count} апдейтов — Telegram не смог их отдать")

    print(f"Одновременных доставок: {info.max_connections}")

    if problems:
        print("\nВебхук не в порядке.")
        return 1
    print("\nВебхук на месте, ошибок доставки нет.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
