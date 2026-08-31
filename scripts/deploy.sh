#!/bin/sh
# Выкат на VPS одной командой. Она же — обновление: шаги одинаковые.
#
#     ./scripts/deploy.sh
#
# Порядок такой, чтобы неудача обходилась дёшево: сначала проверки, потом
# сборка, и только потом остановка работающего бота.
set -eu

cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.vps.yml"

say() {
    echo ""
    echo "── $* ───────────────────────────────────────────" | cut -c1-72
}

if ! docker compose version >/dev/null 2>&1; then
    echo "Нет docker compose. Установка на Ubuntu:"
    echo "    curl -fsSL https://get.docker.com | sh"
    exit 1
fi

if [ ! -f .env ]; then
    echo "Нет .env. Возьмите за основу пример и заполните:"
    echo "    cp .env.example .env && nano .env"
    exit 1
fi

say "Сборка образа"
# Собираем до остановки старого контейнера: неудачная сборка не должна
# оставлять сервер без работающего бота
$COMPOSE build

say "Проверка окружения и схемы базы"
# В контейнере, а не на хосте: там уже есть asyncpg и тот же самый .env
$COMPOSE run --rm --no-deps app python scripts/preflight_deploy.py

say "Запуск"
$COMPOSE up -d

say "Жду, пока бот ответит"
attempt=0
until $COMPOSE exec -T app python -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" \
        >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        echo "Бот не ответил за 60 секунд. Логи:"
        $COMPOSE logs --tail 50 app
        exit 1
    fi
    sleep 2
done
echo "Отвечает."

say "Вебхук"
# Не сразу: вебхук ставится на старте, а старт мог упереться в сеть Telegram
$COMPOSE exec -T app python scripts/check_webhook.py

say "Готово"
$COMPOSE ps
echo ""
echo "Логи:      $COMPOSE logs -f app"
echo "Остановка: $COMPOSE down"
