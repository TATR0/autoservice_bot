"""
validators.py — нормализация и проверка пользовательского ввода.

Одни и те же правила применяются и к WebApp-форме (POST /api/requests),
и к шагам FSM в боте.
"""

from __future__ import annotations

import html
import re

import config


class ValidationError(ValueError):
    """Понятная человеку ошибка валидации — текст можно показывать пользователю."""


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES_RE = re.compile(r"\s+")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


def h(value: object) -> str:
    """
    Экранирование для parse_mode=HTML.

    Без него имя вида `<b>Вася` роняет отправку сообщения целиком
    (`can't parse entities`) — заявка создана, а админ о ней не узнает.
    """
    return html.escape(str(value if value is not None else ""), quote=False)


def clean_text(
    raw: object,
    *,
    field: str,
    min_len: int = 0,
    max_len: int = 255,
    multiline: bool = False,
) -> str:
    """Убрать управляющие символы, схлопнуть пробелы, проверить длину."""
    text = _CONTROL_RE.sub("", str(raw or ""))
    if multiline:
        text = "\n".join(_SPACES_RE.sub(" ", line).strip() for line in text.splitlines())
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    else:
        text = _SPACES_RE.sub(" ", text).strip()

    if len(text) < min_len:
        raise ValidationError(f"«{field}»: минимум {min_len} символа(ов).")
    return text[:max_len]


def normalize_phone(raw: object) -> str:
    """Привести телефон к виду +7XXXXXXXXXX (или +<код><номер> для не-РФ)."""
    digits = re.sub(r"\D", "", str(raw or ""))

    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits

    if not 10 <= len(digits) <= 15:
        raise ValidationError("Некорректный номер телефона. Пример: +7 999 123-45-67")
    return "+" + digits


def format_phone(phone: str) -> str:
    """+79991234567 → +7 (999) 123-45-67. Прочие форматы возвращаются как есть."""
    if re.fullmatch(r"\+7\d{10}", phone or ""):
        d = phone[2:]
        return f"+7 ({d[0:3]}) {d[3:6]}-{d[6:8]}-{d[8:10]}"
    return phone


def normalize_plate(raw: object) -> str:
    plate = re.sub(r"[\s\-]", "", str(raw or "")).upper()
    if not 5 <= len(plate) <= 12:
        raise ValidationError("Госномер должен быть длиной 5–12 символов.")
    return plate


def normalize_city(raw: object) -> str:
    city = clean_text(raw, field="Город", min_len=2, max_len=60)
    # «мОсКвА» → «Москва», «нижний новгород» → «Нижний Новгород»
    return " ".join(
        "-".join(p[:1].upper() + p[1:].lower() for p in word.split("-"))
        for word in city.split(" ")
    )


def validate_urgency(raw: object) -> str:
    value = str(raw or "").strip()
    if value not in config.URGENCY_LABELS:
        raise ValidationError("Неизвестная срочность.")
    return value


def validate_uuid(raw: object, *, field: str) -> str:
    """
    Проверить UUID из недоверенного источника — тела запроса или callback_data.

    Без проверки asyncpg бросает DataError на мусорной строке, и вместо
    понятного отказа клиент получает 500.
    """
    value = str(raw or "").strip()
    if not _UUID_RE.match(value):
        raise ValidationError(f"«{field}»: некорректный идентификатор.")
    return value


def validate_service_title(raw: object) -> str:
    """Название услуги, которое вводит управляющий."""
    return clean_text(raw, field="Название услуги", min_len=2, max_len=40)


def validate_request_fields(payload: dict) -> dict:
    """Проверить и нормализовать поля заявки из WebApp. Бросает ValidationError."""
    return {
        "client_name":  clean_text(payload.get("client_name"), field="Имя", min_len=2, max_len=60),
        "phone":        normalize_phone(payload.get("phone")),
        "brand":        clean_text(payload.get("brand"), field="Марка", min_len=1, max_len=40),
        "model":        clean_text(payload.get("model"), field="Модель", min_len=1, max_len=40),
        "plate":        normalize_plate(payload.get("plate")),
        "urgency":      validate_urgency(payload.get("urgency")),
        "comment":      clean_text(
            payload.get("comment"), field="Комментарий", max_len=500, multiline=True
        ),
    }
