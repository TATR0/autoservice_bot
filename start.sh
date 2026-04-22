#!/bin/sh
# Запускает бота и API одновременно в одном контейнере/процессе Render.
# Render требует хотя бы один процесс слушающий PORT — это uvicorn (api.py).
# bot.py запускается фоном.

echo "▶ Starting bot in background..."
python bot.py &

echo "▶ Starting API on port ${PORT:-8080}..."
exec uvicorn api:app --host 0.0.0.0 --port "${PORT:-8080}"
