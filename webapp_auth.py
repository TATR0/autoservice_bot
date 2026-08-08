"""
webapp_auth.py — проверка подписи Telegram WebApp initData.

Без этой проверки любой желающий может дёрнуть POST /api/requests
и создать заявку от чужого имени. idclienttg берём ТОЛЬКО отсюда.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

import config


class InitDataError(ValueError):
    """initData отсутствует, подделан или протух."""


def verify_init_data(init_data: str, *, max_age: int | None = None) -> dict:
    """Вернуть объект `user` из initData или бросить InitDataError."""
    if not init_data:
        raise InitDataError("initData отсутствует")

    max_age = config.INIT_DATA_MAX_AGE if max_age is None else max_age

    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError as exc:
        raise InitDataError("initData не разобран") from exc

    received = pairs.pop("hash", "")
    if not received:
        raise InitDataError("в initData нет hash")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated, received):
        raise InitDataError("подпись initData не совпала")

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as exc:
        raise InitDataError("некорректный auth_date") from exc

    if max_age and time.time() - auth_date > max_age:
        raise InitDataError("initData просрочен — переоткройте форму")

    raw_user = pairs.get("user")
    if not raw_user:
        raise InitDataError("в initData нет пользователя")

    try:
        user = json.loads(raw_user)
    except json.JSONDecodeError as exc:
        raise InitDataError("не удалось разобрать пользователя") from exc

    if not isinstance(user, dict) or not user.get("id"):
        raise InitDataError("в initData нет id пользователя")

    return user
