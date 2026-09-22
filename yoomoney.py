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
from typing import Iterable, Mapping, NamedTuple
from urllib.parse import quote, urlencode

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

# Подписей у ЮMoney две. Действующая — sign: HMAC-SHA256 от всех параметров
# уведомления, кроме самой подписи. Устаревшая — sha1_hash по жёсткому списку
# полей; с 18 мая 2026 ЮMoney её больше не присылает, но проверку оставляем:
# она стоит десяти строк, а установка, где уведомления настроены давно, не
# должна перестать работать из-за нашего обновления.
#
# Порядок полей старой подписи задан ЮMoney и менять его нельзя: секрет встаёт
# предпоследним, перед label
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


def raw_pairs(raw: str) -> list[tuple[str, str]]:
    """Пары «ключ=значение» из тела запроса, ровно как их прислали."""
    pairs = []
    for part in raw.split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        pairs.append((key, value))
    return pairs


def _hmac_hex(base: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256,
    ).hexdigest()


def _joined(pairs: Iterable[tuple[str, str]]) -> str:
    """Подписываемая строка: без sign, по алфавиту, «ключ=значение» через «&»."""
    return "&".join(
        f"{key}={value}" for key, value in sorted(pairs) if key != "sign"
    )


def hmac_signature(form: Mapping[str, str], secret: str) -> str:
    """
    Действующая подпись ЮMoney: HMAC-SHA256 в HEX нижнего регистра.

    Значения кодируются заново по RFC 3986 — так велит документация. Ключи
    оставляем как есть: имена параметров у ЮMoney простые, кодировать в них
    нечего, а лишнее кодирование только разошлось бы с их строкой.
    """
    pairs = [(key, quote(str(value), safe="")) for key, value in form.items()]
    return _hmac_hex(_joined(pairs), secret)


def hmac_signature_as_sent(raw: str, secret: str) -> str:
    """
    То же, но значения берутся из тела нетронутыми.

    Нужна потому, что «перекодировать заново» и «оставить как прислали» — не
    одно и то же: пробел приходит плюсом, а возвращается как %20, и подпись
    разошлась бы на ровном месте. Обе строки посчитаны с секретом, так что
    признать уведомление по любой из них не ослабляет проверку: подделать
    нельзя ни ту, ни другую.
    """
    return _hmac_hex(_joined(raw_pairs(raw)), secret)


def diagnose(form: Mapping[str, str], raw: str, secret: str) -> str:
    """
    Чем объяснить несошедшуюся подпись. Пусто — ни одна догадка не подошла.

    Нужна ровно один раз в жизни установки: когда уведомления настроены, а
    подпись не сходится, разница между «не тот секрет» и «не та формула»
    снаружи не видна, а сам секрет показывать никому нельзя — значит считать
    приходится на месте. Догадки перебираются только для журнала; принимает
    платёж по-прежнему одна и та же документированная формула.
    """
    if signature_holds(form, secret, raw):
        # Уведомление отвергнуто не подписью, а чем-то после неё
        return "подпись как раз сошлась, дело не в ней"

    sign = str(form.get("sign") or "").lower()
    if sign:
        # Единственное, что здесь можно перепутать, — как готовятся значения:
        # ЮMoney кодирует их по RFC 3986, а разбор формы это кодирование снял
        guesses = {
            "значения подписаны без кодирования": _hmac_hex(
                _joined(list(form.items())), secret,
            ),
            "подписан и сам sign": _hmac_hex(
                "&".join(
                    f"{key}={quote(str(value), safe='')}"
                    for key, value in sorted(form.items())
                ),
                secret,
            ),
        }
        for explanation, expected in guesses.items():
            if hmac.compare_digest(expected, sign):
                return explanation
        return ""

    given = str(form.get("sha1_hash") or "").lower()
    if not given:
        return "в уведомлении нет ни sign, ни sha1_hash"

    # Значения как есть в теле, без раскодирования процентов и плюсов: если
    # ЮMoney подписывает их до кодирования, разойдётся ровно здесь
    undecoded = dict(
        part.split("=", 1) if "=" in part else (part, "")
        for part in raw.split("&") if part
    )
    # Плюс, обращённый в пробел разбором формы, — та же беда, но точечно:
    # в datetime он стоит перед часовым поясом
    replus = dict(form) | {
        "datetime": str(form.get("datetime") or "").replace(" ", "+")
    }
    no_label = dict(form) | {"label": ""}

    guesses = {
        "значения не раскодированы": signature(undecoded, secret),
        "плюс в datetime съеден разбором формы": signature(replus, secret),
        "подпись считается без метки": signature(no_label, secret),
        "вместо amount подписан withdraw_amount": signature(
            dict(form) | {"amount": str(form.get("withdraw_amount") or "")}, secret,
        ),
    }
    for explanation, expected in guesses.items():
        if hmac.compare_digest(expected, given):
            return explanation
    return ""


def signature_holds(form: Mapping[str, str], secret: str, raw: str = "") -> bool:
    """
    Подписано ли уведомление нашим секретом.

    Сначала действующая подпись sign, потом устаревшая sha1_hash — ровно в
    таком порядке: старую ЮMoney больше не присылает, и установка, где она
    ещё приходит, не должна закрыться из-за нашего обновления.
    """
    # compare_digest, а не ==: сравнение строк отваливается на первом
    # несовпавшем символе, и по времени ответа подпись подбирается побайтно
    sign = str(form.get("sign") or "").lower()
    if sign:
        candidates = [hmac_signature(form, secret)]
        if raw:
            candidates.append(hmac_signature_as_sent(raw, secret))
        return any(hmac.compare_digest(one, sign) for one in candidates)

    legacy = str(form.get("sha1_hash") or "").lower()
    if legacy:
        return hmac.compare_digest(signature(form, secret), legacy)
    return False


def parse_notification(
    form: Mapping[str, str], secret: str, raw: str = "",
) -> Notification:
    """
    Проверить подпись и разобрать уведомление. NotificationError — не наше.

    Сумма читается Decimal, а не float: деньги в двоичной дроби — это способ
    однажды не досчитаться копейки и не понять почему.
    """
    if not secret:
        raise NotificationError("Приём уведомлений выключен: нет секрета")

    if not signature_holds(form, secret, raw):
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
