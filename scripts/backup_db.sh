#!/bin/sh
# Резервная копия базы: снимок pg_dump, хранение последних BACKUP_KEEP штук.
#
# Запускается сам из docker compose (сервис backup) — отдельной установки не
# требует. Ручной снимок: docker compose run --rm backup /usr/local/bin/backup_db.sh
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP="${BACKUP_KEEP:-14}"
INTERVAL_HOURS="${BACKUP_INTERVAL_HOURS:-24}"
# Своя переменная — на случай, если для дампа выдан отдельный доступ
DSN="${BACKUP_DATABASE_URL:-${DATABASE_URL:-}}"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') backup: $*"
}

# Молчащий бэкап хуже отсутствующего: про отсутствующий хотя бы знаешь.
alert() {
    text="$1"
    [ -n "${BOT_TOKEN:-}" ] || return 0
    ids="${BOT_OWNER_IDS:-${MASTER_CHAT_ID:-}}"
    [ -n "$ids" ] || return 0
    for chat in $(echo "$ids" | tr ',' ' '); do
        [ "$chat" = "0" ] && continue
        printf '{"chat_id":"%s","text":"%s"}' "$chat" "$text" > /tmp/alert.json
        # Телом JSON, а не полем формы: busybox wget не умеет urlencode, и
        # кириллица в post-data доехала бы мусором
        wget -qO- --header='Content-Type: application/json' \
            --post-file=/tmp/alert.json \
            "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" >/dev/null 2>&1 || true
        rm -f /tmp/alert.json
    done
}

make_backup() {
    if [ -z "$DSN" ]; then
        log "DATABASE_URL пуст — копировать нечего"
        return 1
    fi
    mkdir -p "$BACKUP_DIR"
    target="$BACKUP_DIR/autoservice-$(date -u '+%Y%m%d-%H%M%S').dump"

    # Сначала в .part, потом переименование: оборванный снимок не должен
    # выглядеть как готовый — именно его и возьмут при восстановлении
    if ! pg_dump --format=custom --no-owner --no-privileges \
            --file="$target.part" "$DSN"; then
        rm -f "$target.part"
        log "pg_dump не отработал"
        return 1
    fi

    size=$(wc -c < "$target.part")
    if [ "$size" -lt 1024 ]; then
        rm -f "$target.part"
        log "снимок подозрительно мал ($size Б) — не сохраняю"
        return 1
    fi

    mv "$target.part" "$target"
    log "готово: $target ($size Б)"
}

rotate() {
    ls -1t "$BACKUP_DIR"/autoservice-*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | \
    while read -r old; do
        rm -f "$old"
        log "удалён старый снимок: $old"
    done
}

round() {
    if make_backup; then
        rotate
        return 0
    fi
    alert "⚠️ Резервная копия базы не сделана. Логи: docker logs autoservice_backup"
    return 1
}

case "${1:-once}" in
    --loop)
        log "цикл запущен: раз в ${INTERVAL_HOURS} ч, хранить ${KEEP} снимков"
        while true; do
            # Неудача круга не должна ронять цикл: следующий снимок важнее
            round || true
            sleep "$((INTERVAL_HOURS * 3600))"
        done
        ;;
    *)
        round
        ;;
esac
