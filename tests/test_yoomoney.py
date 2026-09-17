"""Ссылка на оплату переводом. Ни сети, ни базы — чистая сборка адреса."""

from urllib.parse import parse_qs, urlsplit

import pytest

import yoomoney

WALLET = "4100111122223333"
SERVICE_ID = "11111111-1111-1111-1111-111111111111"
LABEL = f"sub:{SERVICE_ID}:30"


def params(url: str) -> dict:
    """Параметры ссылки по одному значению на имя — так их проще сличать."""
    return {name: values[0] for name, values in parse_qs(urlsplit(url).query).items()}


def test_link_carries_the_wallet_and_the_sum():
    url = yoomoney.quickpay_link(WALLET, rubles=590, label=LABEL, target="Подписка")
    got = params(url)
    assert got["receiver"] == WALLET
    assert got["sum"] == "590.00"


def test_label_survives_intact():
    """
    По метке и только по ней деньги связываются с сервисом: имя плательщика
    о сервисе не говорит ничего.
    """
    url = yoomoney.quickpay_link(WALLET, rubles=1490, label=LABEL, target="Подписка")
    assert params(url)["label"] == LABEL


def test_target_with_quotes_and_cyrillic_does_not_break_the_link():
    """Название сервиса попадает в назначение платежа как есть."""
    target = 'Подписка «Авто&Сервис», 3 месяца'
    url = yoomoney.quickpay_link(WALLET, rubles=1490, label=LABEL, target=target)
    assert params(url)["targets"] == target
    # Кавычки и амперсанд не должны разорвать строку запроса
    assert url.count("?") == 1


def test_personal_data_is_not_requested():
    """Лишние данные плательщика — это ответственность за них без надобности."""
    got = params(yoomoney.quickpay_link(WALLET, rubles=590, label=LABEL, target="П"))
    assert got["need-fio"] == "false"
    assert got["need-email"] == "false"


def test_sum_prices_from_the_config_are_all_valid():
    """Тарифы из конфига должны собираться в ссылку без исключений."""
    import config

    for plan in config.PLANS:
        url = yoomoney.quickpay_link(
            WALLET, rubles=plan.rubles, label=LABEL, target=plan.label
        )
        assert params(url)["sum"] == f"{plan.rubles}.00"


@pytest.mark.parametrize("wallet, rubles, label", [
    ("", 590, LABEL),           # кошелёк не настроен
    ("   ", 590, LABEL),        # пробелы — тот же пустой кошелёк
    (WALLET, 0, LABEL),         # бесплатный тариф на форме оплаты бессмыслен
    (WALLET, -100, LABEL),      # и тем более отрицательный
    (WALLET, 590, ""),          # без метки платёж не привязать к сервису
])
def test_broken_arguments_refuse_instead_of_building_a_bad_link(wallet, rubles, label):
    """
    Лучше отказ, чем ссылка «на всякий случай»: по ней человек либо заплатит
    неизвестно кому, либо увидит чужую ошибку вместо нашей.
    """
    with pytest.raises(ValueError):
        yoomoney.quickpay_link(wallet, rubles=rubles, label=label, target="Подписка")
