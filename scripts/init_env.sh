#!/bin/sh
# Заполнить .env тем, что должно быть случайным: секретом вебхука и паролем
# базы. Запускается один раз перед первым выкатом:
#
#     ./scripts/init_env.sh
#
# Идемпотентен и осторожен: то, что вы задали руками, не трогает — только
# сообщает, если два места разошлись. Домен, токен и почту всё равно
# придётся вписать самому, их не угадать.
set -eu

cd "$(dirname "$0")/.."

ENV_FILE=".env"
PLACEHOLDER="замените-на-случайную-строку"

if [ ! -f "$ENV_FILE" ]; then
    cp .env.example "$ENV_FILE"
    # 600: в файле пароль базы и токен бота, а домашний каталог на VPS
    # читаем всеми по умолчанию
    chmod 600 "$ENV_FILE"
    echo "Создан .env из .env.example"
fi

# Только буквы и цифры: пароль уезжает в строку подключения, а там @ : / ?
# значат не то, и однажды это выяснится в самый неподходящий момент
random() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 24
    else
        od -An -tx1 -N 24 /dev/urandom | tr -d " \n"
    fi
}

get() {
    grep "^$1=" "$ENV_FILE" 2>/dev/null | head -n 1 | cut -d= -f2- || true
}

put() {
    name="$1"
    value="$2"
    tmp="$ENV_FILE.tmp"
    # awk, а не sed: значение может содержать & и \, которые sed истолкует
    # по-своему. И запись через временный файл — чтобы обрыв не съел .env
    if grep -q "^$name=" "$ENV_FILE"; then
        awk -v name="$name" -v value="$value" \
            'index($0, name "=") == 1 { print name "=" value; next } { print }' \
            "$ENV_FILE" > "$tmp"
    else
        cp "$ENV_FILE" "$tmp"
        printf '%s=%s\n' "$name" "$value" >> "$tmp"
    fi
    mv "$tmp" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
}

is_unset() {
    [ -z "$1" ] || [ "$1" = "$PLACEHOLDER" ]
}

changed=0

secret=$(get WEBHOOK_SECRET)
if is_unset "$secret"; then
    put WEBHOOK_SECRET "$(random)"
    echo "WEBHOOK_SECRET: сгенерирован"
    changed=1
else
    echo "WEBHOOK_SECRET: уже задан, не трогаю"
fi

# .env мог быть создан до того, как порт стал настройкой. Умолчание — то же
# самое, что было зашито раньше, так что поведение не меняется
if [ -z "$(get HTTPS_PORT)" ]; then
    put HTTPS_PORT 443
    echo "HTTPS_PORT: не было в .env, поставил 443"
    changed=1
else
    echo "HTTPS_PORT: уже задан, не трогаю"
fi

password=$(get POSTGRES_PASSWORD)
if is_unset "$password"; then
    password=$(random)
    put POSTGRES_PASSWORD "$password"
    echo "POSTGRES_PASSWORD: сгенерирован"
    changed=1
else
    echo "POSTGRES_PASSWORD: уже задан, не трогаю"
fi

user=$(get POSTGRES_USER)
name=$(get POSTGRES_DB)
[ -n "$user" ] || user="autoservice"
[ -n "$name" ] || name="autoservice"
internal="postgresql://$user:$password@db:5432/$name?sslmode=disable"

dsn=$(get DATABASE_URL)
case "$dsn" in
    "" | *"$PLACEHOLDER"*)
        put DATABASE_URL "$internal"
        echo "DATABASE_URL: собран для базы в контейнере db"
        changed=1
        ;;
    *"@db:"*)
        if [ "$dsn" != "$internal" ]; then
            # Не подправляю молча: расхождение может быть и осмысленным —
            # другой порт, другое имя базы. Но если разошёлся пароль,
            # бот не подключится, и понять почему будет неоткуда
            echo ""
            echo "! DATABASE_URL и POSTGRES_* разошлись. Ожидалось:"
            echo "    $internal"
            echo "  В .env сейчас:"
            echo "    $dsn"
            echo "  Поправьте ту строку, которая неверна, и запустите снова."
        else
            echo "DATABASE_URL: совпадает с POSTGRES_*, не трогаю"
        fi
        ;;
    *)
        echo "DATABASE_URL: задан на внешнюю базу, не трогаю"
        echo "  (база в контейнере db тогда просто не используется)"
        ;;
esac

echo ""
if [ "$changed" = "1" ]; then
    echo "Готово. Осталось вписать руками: BOT_TOKEN, BOT_USERNAME, DOMAIN,"
    echo "BASE_URL, ACME_EMAIL, BOT_OWNER_IDS — их не сгенерировать."
else
    echo "Всё уже заполнено, ничего не менял."
fi
echo "Проверить: ./scripts/deploy.sh (он начнёт с проверки .env)"
