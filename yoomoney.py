"""
yoomoney.py — ссылка на оплату подписки переводом.

Обычная форма ЮMoney «Быстрая оплата»: сумма и назначение подставлены заранее,
человеку остаётся только подтвердить. Никаких ключей и секретов для этого не
нужно — ссылка собирается из номера кошелька, и его не жалко показать.

Самое важное здесь — label. ЮMoney возвращает его нетронутым в истории
переводов, и это единственная ниточка от пришедших денег к тому, кому
начислять дни: имя плательщика с сервисом не связано никак. Формат тот же,
что у payload счёта Telegram, — sub:<idservice>:<дней>, чтобы одна и та же
строка читалась одинаково при любом способе оплаты.
"""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal, InvalidOperation
from typing import Mapping, NamedTuple
from urllib.parse import urlencode

# Адрес формы. confirm.xml — исторический путь, он же работает и сегодня;
# конечная страница одна и та же
QUICKPAY_URL = "https://yoomoney.ru/quickpay/confirm"


def quickpay_link(wallet: str, *, rubles: int, label: str, target: str) -> str:
    """
    Ссылка на оплату с подставленной суммой.

    Отказ, а не ссылка «на всякий случай»: пустой кошелёк или нулевая сумма
    уводят человека на форму, где он либо заплатит неизвестно кому, либо
    увидит ошибку ЮMoney вместо нашей.
    """
    wallet = (wallet or "").strip()
    if not wallet:
        raise ValueError("Не задан кошелёк ЮMoney")
    if rubles <= 0:
        raise ValueError(f"Сумма должна быть больше нуля, а не {rubles}")
    if not label:
        raise ValueError("Без метки платёж не связать с сервисом")

    params = {
        "receiver": wallet,
        # shop, а не button: покупатель видит назначение платежа и сам выбирает,
        # платить картой или с кошелька
        "quickpay-form": "shop",
        "targets": target,
        # С копейками: ЮMoney принимает и целое, но в истории переводов сумма
        # всё равно с копейками, и глазом их проще сличать
        "sum": f"{rubles}.00",
        "label": label,
        # Лишние данные, за которые потом отвечать. Имя и почта плательщика
        # нам ни к чему: кто платил, написано в метке
        "need-fio": "false",
        "need-email": "false",
        "need-phone": "false",
        "need-address": "false",
    }
    return f"{QUICKPAY_URL}?{urlencode(params)}"


# ── Уведомление о переводе ───────────────────────────────────────────────────
#
# ЮMoney стучится на наш адрес при каждом входящем переводе. Адрес этот
# публичный, и единственное, что отличает настоящее уведомление от выдуманного
# кем угодно, — подпись секретом. Поэтому проверка подписи здесь не «на всякий
# случай»: без неё любой желающий продлевал бы себе подписку строкой в curl.

# Порядок полей задан ЮMoney и менять его нельзя: подпись считается по строке,
# склеенной именно так. Секрет встаёт предпоследним, перед label
SIGNED_FIELDS = (
    "notification_type", "operation_id", "amount", "currency",
    "datetime", "sender", "codepro",
)


class NotificationError(Exception):
    """Уведомление не наше или испорчено. Деньги по нему не начисляются."""


class Notification(NamedTuple):
    """Разобранное уведомление о переводе."""
    operation_id: str   # id операции у ЮMoney, по нему отсекаются повторы
    amount: Decimal     # сколько зачислено на кошелёк, уже без комиссии
    label: str          # наша метка: sub:<idservice>:<дней>
    sender: str         # номер кошелька отправителя, пусто при оплате картой
    codepro: bool       # перевод с защитным кодом — деньги ещё не наши
    unaccepted: bool    # перевод ждёт принятия — тоже ещё не наши
    test: bool          # кнопка «проверить» в настройках ЮMoney


def _flag(raw: str) -> bool:
    """ЮMoney шлёт булевы значения строками «true»/«false»."""
    return (raw or "").strip().lower() == "true"


def signature(form: Mapping[str, str], secret: str) -> str:
    """
    Подпись уведомления: sha1 от полей, склеенных через «&», с секретом внутри.

    Отсутствующее поле — пустая строка, а не ошибка: ЮMoney не присылает
    sender при оплате картой, и подпись считается ровно по пустому месту.
    """
    parts = [str(form.get(name) or "") for name in SIGNED_FIELDS]
    parts.append(secret)
    parts.append(str(form.get("label") or ""))
    return hashlib.sha1("&".join(parts).encode("utf-8")).hexdigest()


def parse_notification(form: Mapping[str, str], secret: str) -> Notification:
    """
    Проверить подпись и разобрать уведомление. NotificationError — не наше.

    Сумма читается Decimal, а не float: деньги в двоичной дроби — это способ
    однажды не досчитаться копейки и не понять почему.
    """
    if not secret:
        raise NotificationError("Приём уведомлений выключен: нет секрета")

    expected = signature(form, secret)
    got = str(form.get("sha1_hash") or "")
    # compare_digest, а не ==: сравнение строк отваливается на первом
    # несовпавшем символе, и по времени ответа подпись подбирается побайтно
    if not hmac.compare_digest(expected, got.lower()):
        raise NotificationError("Подпись не сошлась")

    operation_id = str(form.get("operation_id") or "").strip()
    if not operation_id:
        raise NotificationError("Уведомление без operation_id")

    try:
        amount = Decimal(str(form.get("amount") or "0"))
    except InvalidOperation:
        raise NotificationError(f"Сумма не читается: {form.get('amount')!r}") from None

    return Notification(
        operation_id=operation_id,
        amount=amount,
        label=str(form.get("label") or "").strip(),
        sender=str(form.get("sender") or "").strip(),
        codepro=_flag(form.get("codepro", "")),
        unaccepted=_flag(form.get("unaccepted", "")),
        test=_flag(form.get("test_notification", "")),
    )
