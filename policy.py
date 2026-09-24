"""
policy.py — политика обработки персональных данных для формы записи.

Галочка «согласен на обработку» без документа, к которому она отсылает, —
пустая: человек соглашается неизвестно на что. Документ собирается здесь, а
не лежит готовым файлом, потому что оператор у каждой формы свой: данные
собирает тот автосервис, в который записываются, и называть его надо по
имени, с его телефоном и адресом.

Срок хранения подставляется настоящий, из PII_RETENTION_DAYS. Обещание
стереть данные через год, когда чистка выключена, хуже отсутствия обещания:
первое — неправда, второе — всего лишь умолчание.
"""

from __future__ import annotations

from html import escape
from typing import Any, Mapping

import config
from validators import format_phone

# Ровно те поля, которые стирает db.anonymize_old_requests и /forget_me.
# Список здесь и список там обязаны совпадать: расходятся — политика врёт
COLLECTED = "имя, телефон, марка и модель машины, госномер, комментарий"


def _days_word(days: int) -> str:
    if days % 100 in range(11, 15):
        return "дней"
    return {1: "день", 2: "дня", 3: "дня", 4: "дня"}.get(days % 10, "дней")


def _operator(service: Mapping[str, Any] | None) -> str:
    """Кто оператор: конкретный сервис или, если его не назвали, тот, куда записываются."""
    if not service:
        return (
            "<p>Оператор ваших данных — автосервис, в который вы записываетесь. "
            "Его название, телефон и адрес видны в карточке сервиса, там же, "
            "где вы выбирали услугу.</p>"
        )

    name = escape(str(service.get("service_name") or ""))
    phone = escape(format_phone(str(service.get("service_number") or "")))
    where = ", ".join(
        escape(str(service.get(field) or ""))
        for field in ("city", "location_service")
        if service.get(field)
    )

    lines = [f"<p>Оператор ваших данных — <b>{name}</b>.</p>", "<ul>"]
    if phone:
        lines.append(f"<li>Телефон: {phone}</li>")
    if where:
        lines.append(f"<li>Адрес: {where}</li>")
    lines.append("</ul>")
    return "".join(lines)


def _storage(retention_days: int) -> str:
    if retention_days > 0:
        return (
            f"<p>{COLLECTED.capitalize()} хранятся "
            f"<b>{retention_days} {_days_word(retention_days)}</b> с момента "
            "обращения, после чего стираются из заявки сами. У сервиса остаётся "
            "обезличенная запись: дата, услуга и то, что обращение было. "
            "По ней вас не найти.</p>"
        )
    # Молчать об этом нельзя: человеку важно знать, что срок не задан, —
    # тогда он воспользуется правом стереть данные сам, не дожидаясь чистки
    return (
        f"<p>{COLLECTED.capitalize()} хранятся, пока вы не попросите их удалить: "
        "автоматического срока сервис не установил. Как удалить — ниже.</p>"
    )


def render(service: Mapping[str, Any] | None = None,
           retention_days: int | None = None) -> str:
    """Готовая HTML-страница политики. Всё из базы — через escape."""
    if retention_days is None:
        retention_days = config.PII_RETENTION_DAYS

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Обработка персональных данных</title>
<style>
  body {{ margin: 0; padding: 20px 18px 48px; max-width: 700px;
         font: 15px/1.6 -apple-system, "Segoe UI", Roboto, sans-serif;
         color: #1c1c1e; background: #fff; }}
  h1 {{ font-size: 20px; margin: 0 0 20px; }}
  h2 {{ font-size: 16px; margin: 28px 0 8px; }}
  ul {{ margin: 8px 0; padding-left: 20px; }}
  p {{ margin: 8px 0; }}
  code {{ background: #f1f1f4; border-radius: 4px; padding: 1px 5px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #e9e9ea; background: #17181a; }}
    code {{ background: #2a2b2e; }}
  }}
</style>
</head>
<body>
<h1>Обработка персональных данных</h1>

{_operator(service)}

<h2>Что собирается</h2>
<p>Из формы записи: {COLLECTED}, а также выбранные услуги и время визита.</p>
<p>Из Telegram: ваш числовой идентификатор, имя и @username — их передаёт сам
Telegram, когда вы пишете боту. Телефон сохраняется, только если вы отправили
его боту сами, чтобы не вводить заново в следующий раз.</p>

<h2>Зачем</h2>
<p>Чтобы сервис принял запись, подтвердил её, позвонил вам при необходимости и
напомнил о визите. Ни для чего другого эти данные не используются: рассылок нет,
третьим лицам они не передаются, не продаются и не публикуются.</p>

<h2>Где хранится</h2>
<p>На сервере, который обслуживает бот записи. Владелец бота хранит данные по
поручению автосервиса и своих целей в них не имеет: он их не читает, не
передаёт и не использует. Доступ к вашим заявкам есть у сотрудников сервиса,
которых назначил управляющий.</p>

<h2>Сколько хранится</h2>
{_storage(retention_days)}

<h2>Как отозвать согласие и удалить данные</h2>
<p>Отправьте боту команду <code>/forget_me</code>. Из ваших заявок пропадут
{COLLECTED}; сохранённый телефон и профиль в боте тоже сотрутся. Отменить это
нельзя, и это не требует ничьего разрешения.</p>
<p>Одно исключение: пока у вас есть незакрытая заявка, данные по ней не
стираются — по ней вас ждут. Отмените запись или дождитесь визита, и повторите
команду.</p>
<p>Можно и просто позвонить в сервис по телефону выше и попросить удалить
данные — результат тот же.</p>
</body>
</html>
"""
