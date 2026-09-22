#!/bin/sh
# Вывоз резервных копий за пределы машины.
#
# Снимки из ./backups лежат на том же диске, что и база. Они спасают от
# ошибочного удаления и неудачного обновления, но не от пропавшего сервера:
# он унесёт базу и все копии разом. Здесь копии уезжают в чужое хранилище.
#
# Куда — задаёт BACKUP_REMOTE, адрес rclone: yadisk:autoservice-backups,
# sftp-home:/backups, s3:bucket/путь. Пусто — вывоза нет; выкат это не
# останавливает, но и молчать об этом нельзя: учётные данные чужого
# хранилища взяться сами не могут, их заводят один раз руками.
#
# Запускается сам из docker compose (сервис offsite).
# Разовый прогон: docker compose run --rm --entrypoint /bin/sh offsite \
#                     /usr/local/bin/backup_offsite.sh
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
REMOTE="${BACKUP_REMOTE:-}"
KEEP="${BACKUP_KEEP:-14}"
INTERVAL_HOURS="${BACKUP_INTERVAL_HOURS:-24}"
# Чаще, чем делается снимок: круг дешёвый (уже увезённое rclone пропускает),
# а неудачу хранилища лучше заметить в тот же день, а не через сутки
TICK_HOURS="${BACKUP_OFFSITE_TICK_HOURS:-1}"
CONFIG="${BACKUP_RCLONE_CONFIG:-/config/rclone/rclone.conf}"

# Метка неудачи. Письмо уходит на переходе «было хорошо — стало плохо»:
# иначе отключившееся на неделю хранилище пишет владельцу каждый час, и на
# восьмой день он перестаёт читать эти письма вовсе — вместе с настоящими
FAILED="/tmp/offsite-failed"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') offsite: $*"
}

# Отправка в Telegram повторяет backup_db.sh. Общий файл стоил бы дороже:
# его пришлось бы монтировать в оба контейнера, и забытый монтаж ронял бы
# не вывоз копий, а сам снимок базы.
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

fail() {
    log "$1"
    if [ ! -f "$FAILED" ]; then
        : > "$FAILED"
        alert "⚠️ Копии базы не уезжают с сервера: $1. Логи: docker logs autoservice_offsite"
    fi
    return 1
}

recovered() {
    [ -f "$FAILED" ] || return 0
    rm -f "$FAILED"
    alert "✅ Копии базы снова уезжают в хранилище."
}

round() {
    if [ ! -f "$CONFIG" ]; then
        fail "нет настроек rclone ($CONFIG)"
        return 1
    fi

    # Старше срока хранения не возим: в хранилище такие уже удалены как
    # старые, и копировать их туда заново — вечный круг
    age="$((KEEP * INTERVAL_HOURS))h"

    if ! rclone --config "$CONFIG" copy "$BACKUP_DIR" "$REMOTE" \
            --include "autoservice-*.dump" --max-age "$age"; then
        fail "rclone не смог скопировать снимки"
        return 1
    fi

    # Столько же снимков, сколько на машине: хранилище не должно расти вечно
    rclone --config "$CONFIG" delete "$REMOTE" \
        --include "autoservice-*.dump" --min-age "$age" || true

    # Проверка, а не вера: копия, которую никто не смотрел, — не копия.
    # Вдвое больше интервала — запас на один пропущенный снимок
    fresh=$(rclone --config "$CONFIG" lsf "$REMOTE" \
        --include "autoservice-*.dump" --max-age "$((INTERVAL_HOURS * 2))h" \
        2>/dev/null | head -n 1 || true)
    if [ -z "$fresh" ]; then
        fail "в хранилище нет снимка свежее $((INTERVAL_HOURS * 2)) ч"
        return 1
    fi

    recovered
    log "вывезено, свежий снимок: $fresh"
}

if [ -z "$REMOTE" ]; then
    log "BACKUP_REMOTE пуст — копии остаются только на этой машине"
    # Не выходим: контейнер с restart=unless-stopped перезапускался бы
    # без конца и засыпал логи одной и той же строкой
    [ "${1:-once}" = "--loop" ] || exit 0
    while true; do sleep 86400; done
fi

case "${1:-once}" in
    --loop)
        log "цикл запущен: раз в ${TICK_HOURS} ч в ${REMOTE}"
        while true; do
            # Неудача круга не должна ронять цикл: хранилище могло просто
            # не ответить, и через час стоит попробовать снова
            round || true
            sleep "$((TICK_HOURS * 3600))"
        done
        ;;
    *)
        round
        ;;
esac
