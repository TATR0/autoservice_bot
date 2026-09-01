#!/bin/sh
# Залить дамп в базу бота. Нужно ровно один раз — при переезде со сторонней
# базы (Supabase) на свою:
#
#     ./scripts/import_dump.sh backups/autoservice-20260901-120000.dump
#
# Дамп берётся тем же сервисом backup, только у чужой базы:
#     docker compose run --rm -e BACKUP_DATABASE_URL="<строка Supabase>" backup
#
# Операция разрушительная: содержимое текущей базы заменяется содержимым
# дампа. Поэтому скрипт сначала снимает копию того, что затрёт, и спрашивает.
set -eu

cd "$(dirname "$0")/.."

DUMP="${1:-}"
if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
    echo "Укажите файл дампа: ./scripts/import_dump.sh backups/autoservice-....dump"
    exit 1
fi

DB="${POSTGRES_DB:-autoservice}"
USER="${POSTGRES_USER:-autoservice}"
if [ -f .env ]; then
    DB=$(grep "^POSTGRES_DB=" .env | head -n 1 | cut -d= -f2- || true)
    USER=$(grep "^POSTGRES_USER=" .env | head -n 1 | cut -d= -f2- || true)
    [ -n "$DB" ] || DB="autoservice"
    [ -n "$USER" ] || USER="autoservice"
fi

COMPOSE="docker compose"

echo "Файл:     $DUMP ($(wc -c < "$DUMP") Б)"
echo "База:     $DB (пользователь $USER, контейнер db)"
echo ""
echo "Всё, что сейчас в базе $DB, будет заменено содержимым дампа."
printf "Продолжить? Введите имя базы (%s): " "$DB"
read -r answer
if [ "$answer" != "$DB" ]; then
    echo "Отменено."
    exit 1
fi

echo ""
echo "── Поднимаю базу ───────────────────────────────────────"
$COMPOSE up -d db
attempt=0
until $COMPOSE exec -T db pg_isready -U "$USER" -d "$DB" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        echo "База не поднялась за 60 секунд. Логи: $COMPOSE logs db"
        exit 1
    fi
    sleep 2
done

echo "── Снимаю копию текущего состояния ─────────────────────"
mkdir -p backups
before="backups/before-import-$(date -u '+%Y%m%d-%H%M%S').dump"
if $COMPOSE exec -T db pg_dump --format=custom --no-owner --no-privileges \
        -U "$USER" -d "$DB" > "$before" 2>/dev/null && [ -s "$before" ]; then
    echo "Прежнее состояние: $before"
else
    # Пустая база — копировать нечего, и это нормальный случай переезда
    rm -f "$before"
    echo "Прежнего состояния нет (база пуста) — копия не нужна"
fi

echo "── Заливаю дамп ────────────────────────────────────────"
# --clean --if-exists: дамп несёт свои CREATE TABLE, и без очистки они
# упрутся в таблицы, созданные schema.sql при первом старте.
# Ошибки не глушим кодом возврата: pg_restore ругается и на безобидное
# (роли, расширения Supabase), поэтому решает следующий шаг, а не этот
$COMPOSE exec -T db pg_restore --clean --if-exists --no-owner --no-privileges \
    -U "$USER" -d "$DB" < "$DUMP" || echo "(pg_restore закончил с замечаниями — смотрим схему ниже)"

echo "── Догоняю схему до текущей версии ─────────────────────"
# Дамп мог быть снят до последней миграции. schema.sql идемпотентный
$COMPOSE exec -T db psql -v ON_ERROR_STOP=1 -U "$USER" -d "$DB" -q < schema.sql
echo "Схема применена."

echo ""
echo "── Что доехало ─────────────────────────────────────────"
$COMPOSE exec -T db psql -U "$USER" -d "$DB" -At -c \
    "SELECT 'сервисов: ' || count(*) FROM services
     UNION ALL SELECT 'заявок: ' || count(*) FROM requests
     UNION ALL SELECT 'платежей: ' || count(*) FROM subscription_payments"

echo ""
echo "Готово. Дальше: ./scripts/deploy.sh"
