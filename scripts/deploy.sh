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
    echo "Нет .env. Заполнить пример и сгенерировать секреты:"
    echo "    ./scripts/init_env.sh"
    exit 1
fi

# Имя базы и пользователь нужны здесь для pg_isready и psql. Умолчания те же,
# что в docker-compose.yml: пустая строка в .env не должна ломать проверку
PGDB=$(grep "^POSTGRES_DB=" .env | head -n 1 | cut -d= -f2- || true)
PGUSER=$(grep "^POSTGRES_USER=" .env | head -n 1 | cut -d= -f2- || true)
[ -n "$PGDB" ] || PGDB="autoservice"
[ -n "$PGUSER" ] || PGUSER="autoservice"

# Порт HTTPS занимают чаще, чем кажется: VPN, панель, чужой веб-сервер.
# Docker сообщит об этом сам, но уже после сборки и остановки бота
HTTPSPORT=$(grep "^HTTPS_PORT=" .env | head -n 1 | cut -d= -f2- || true)
[ -n "$HTTPSPORT" ] || HTTPSPORT=443
# Если наш Caddy уже работает, порт занят им — это не помеха, а норма
if ! docker ps -q --filter name=autoservice_caddy --filter status=running | grep -q . &&
   command -v ss >/dev/null 2>&1 &&
   ss -ltn "sport = :$HTTPSPORT" 2>/dev/null | tail -n +2 | grep -q .; then
    echo "Порт $HTTPSPORT уже занят:"
    ss -ltnp "sport = :$HTTPSPORT" 2>/dev/null | tail -n +2
    echo ""
    echo "Caddy на него не встанет. Выберите свободный порт из тех, куда"
    echo "Telegram доставляет вебхук — 443, 80, 88, 8443 — и впишите в .env:"
    echo "    HTTPS_PORT=8443"
    echo "    BASE_URL=https://<ваш-домен>:8443"
    exit 1
fi

say "Сборка образа"
# Собираем до остановки старого контейнера: неудачная сборка не должна
# оставлять сервер без работающего бота
$COMPOSE build

say "База"
# Раньше бота: ему в неё подключаться, а проверке схемы — читать её
$COMPOSE up -d db
attempt=0
until $COMPOSE exec -T db pg_isready -U "$PGUSER" -d "$PGDB" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        echo "База не поднялась за 60 секунд. Логи:"
        $COMPOSE logs --tail 50 db
        exit 1
    fi
    sleep 2
done
echo "Отвечает."

if grep -q "^DATABASE_URL=.*@db:" .env; then
    say "Схема"
    # Каждый выкат: schema.sql идемпотентный, а забытая миграция выглядит
    # как случайно пропавшая половина бота
    $COMPOSE exec -T db psql -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDB" -q < schema.sql
    echo "Применена."
else
    echo "DATABASE_URL смотрит не в контейнер db — схему не трогаю."
fi

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
echo "Остановка: $COMPOSE down    (данные базы остаются в томе pgdata)"
