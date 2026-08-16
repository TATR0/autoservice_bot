"""
validators.py — нормализация и проверка пользовательского ввода.

Одни и те же правила применяются и к WebApp-форме (POST /api/requests),
и к шагам FSM в боте.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, time
from zoneinfo import ZoneInfo



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


def validate_price(raw: object) -> int | None:
    """
    Цена услуги в рублях. None — цена не указана.

    Управляющий вводит её текстом, поэтому принимаем и «3 000», и «3000 ₽»:
    отказ из-за пробела внутри числа выглядит придиркой, а не проверкой.
    """
    text = clean_text(raw, field="Цена", max_len=20)
    if text in {"-", "—", "–", ""}:
        return None

    compact = re.sub(r"\s", "", text)
    compact = re.sub(r"(₽|руб\.?|р\.?)$", "", compact, flags=re.IGNORECASE)
    if not compact.isdecimal():
        raise ValidationError(
            "Введите число рублей, например 3000, или «-», чтобы не указывать цену."
        )

    value = int(compact)
    if value > 10_000_000:
        raise ValidationError("Цена не может быть больше 10 000 000 ₽.")
    return value


_TIME_RANGE_RE = re.compile(
    r"^(\d{1,2})(?::(\d{2}))?\s*[-—–]\s*(\d{1,2})(?::(\d{2}))?$"
)


def _parse_time(hour: str, minute: str | None, *, field: str) -> time:
    value_h, value_m = int(hour), int(minute or 0)
    if value_h > 23 or value_m > 59:
        raise ValidationError(f"«{field}»: такого времени не бывает.")
    return time(value_h, value_m)


def validate_time_range(raw: object, *, field: str) -> tuple[time, time]:
    """
    Диапазон времени: «9-18» или «09:00-18:00».

    Короткая форма принимается намеренно: управляющий пишет часы работы так,
    как сказал бы их вслух, и отказ из-за пропущенных нулей выглядит
    придиркой, а не проверкой.
    """
    text = clean_text(raw, field=field, max_len=20)
    match = _TIME_RANGE_RE.match(text.replace(" ", ""))
    if not match:
        raise ValidationError(
            f"«{field}»: введите диапазон, например 9-18 или 09:00-18:00."
        )

    start = _parse_time(match.group(1), match.group(2), field=field)
    end = _parse_time(match.group(3), match.group(4), field=field)
    if start >= end:
        raise ValidationError(f"«{field}»: начало должно быть раньше конца.")
    return start, end


def validate_lunch(raw: object) -> tuple[time, time] | None:
    """Обед. None — обеда нет; «-» убирает его, как и у цены."""
    text = clean_text(raw, field="Обед", max_len=20)
    if text in {"-", "—", "–", ""}:
        return None
    return validate_time_range(text, field="Обед")


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def validate_lunch_on_grid(
    lunch: tuple[time, time] | None, *, work_from: time, slot_minutes: int
) -> tuple[time, time] | None:
    """
    Обед обязан занимать целое число окон записи.

    Иначе он всё равно съедает окно целиком — окно, задетое обедом хотя бы
    краем, продать нельзя, — но управляющий об этом не догадывается: он ввёл
    45 минут, а из расписания пропал час. Расхождение между тем, что человек
    задал, и тем, что случилось, дороже гибкости в четверть часа.

    Ошибка называет ближайший подходящий диапазон: считать за управляющего
    дешевле, чем заставлять его делить в уме.
    """
    if lunch is None:
        return None

    start, end = lunch
    opens = _minutes(work_from)
    offset_start = _minutes(start) - opens
    offset_end = _minutes(end) - opens

    if offset_start % slot_minutes == 0 and offset_end % slot_minutes == 0:
        return lunch

    start_aligned, end_aligned = snap_lunch_to_grid(
        lunch, work_from=work_from, slot_minutes=slot_minutes
    )
    raise ValidationError(
        f"Обед должен занимать целое число окон по {slot_minutes} мин. "
        f"Ближайший подходящий: {start_aligned:%H:%M}-{end_aligned:%H:%M}"
    )


def snap_lunch_to_grid(
    lunch: tuple[time, time], *, work_from: time, slot_minutes: int
) -> tuple[time, time]:
    """
    Растянуть обед до целого числа окон.

    Наружу, а не внутрь: обед, ставший короче задуманного, отправил бы клиента
    на время, когда сервис ещё обедает.
    """
    start, end = lunch
    opens = _minutes(work_from)
    offset_start = _minutes(start) - opens
    offset_end = _minutes(end) - opens

    aligned_start = offset_start - offset_start % slot_minutes
    aligned_end = -(-offset_end // slot_minutes) * slot_minutes
    return (
        time((opens + aligned_start) // 60, (opens + aligned_start) % 60),
        time((opens + aligned_end) // 60, (opens + aligned_end) % 60),
    )


def _validate_int(raw: object, *, field: str, low: int, high: int, hint: str) -> int:
    text = clean_text(raw, field=field, max_len=10)
    compact = re.sub(r"\s", "", text)
    if not compact.isdecimal():
        raise ValidationError(hint)
    value = int(compact)
    if not low <= value <= high:
        raise ValidationError(hint)
    return value


def validate_capacity(raw: object) -> int:
    """Сколько машин сервис принимает в одно время."""
    return _validate_int(
        raw, field="Машин за раз", low=1, high=20,
        hint="Введите число от 1 до 20.",
    )


def validate_horizon(raw: object) -> int:
    """На сколько дней вперёд открыта запись."""
    return _validate_int(
        raw, field="Горизонт", low=1, high=60,
        hint="Введите число дней от 1 до 60.",
    )


def validate_catalog_ids(raw: object) -> list[str]:
    """
    Список выбранных клиентом услуг. Порядок сохраняется — в нём заявка
    показывается администратору. Дубликаты схлопываются: повторный элемент
    ничего не добавляет, а в базе на него стоит уникальный индекс.
    """
    if not isinstance(raw, (list, tuple)):
        raise ValidationError("Выберите хотя бы одну услугу.")

    chosen: list[str] = []
    for value in raw:
        item = validate_uuid(value, field="Услуга")
        if item not in chosen:
            chosen.append(item)

    if not chosen:
        raise ValidationError("Выберите хотя бы одну услугу.")
    return chosen


def validate_scheduled_at(raw: object, *, free: dict, tz: str) -> datetime:
    """
    Момент записи из формы: «2026-08-17 10:00» в зоне сервиса.

    Проверяется не формат, а принадлежность реально свободному окну: payload
    можно отправить в обход формы, поэтому список окон — единственный источник
    истины, а не то, что прислал клиент.
    """
    text = clean_text(raw, field="Время записи", max_len=20)
    try:
        moment = datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValidationError("Выберите время записи.") from None

    if moment.time() not in free.get(moment.date(), []):
        raise ValidationError("Это время только что заняли. Выберите другое.")
    return moment.replace(tzinfo=ZoneInfo(tz))


def validate_request_fields(payload: dict) -> dict:
    """Проверить и нормализовать поля заявки из WebApp. Бросает ValidationError."""
    return {
        "client_name":  clean_text(payload.get("client_name"), field="Имя", min_len=2, max_len=60),
        "phone":        normalize_phone(payload.get("phone")),
        "brand":        clean_text(payload.get("brand"), field="Марка", min_len=1, max_len=40),
        "model":        clean_text(payload.get("model"), field="Модель", min_len=1, max_len=40),
        "plate":        normalize_plate(payload.get("plate")),
        "comment":      clean_text(
            payload.get("comment"), field="Комментарий", max_len=500, multiline=True
        ),
    }
