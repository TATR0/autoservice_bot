# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — builder: устанавливаем зависимости в изолированный слой
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Системные зависимости нужны только на этапе сборки asyncpg (C-extension)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Устанавливаем пакеты в отдельную папку, чтобы не тащить dev-мусор дальше
RUN pip install --upgrade pip \
 && pip install --prefix=/install --no-cache-dir -r requirements.txt


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — runtime: минимальный образ без компилятора
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Не писать .pyc, не буферизировать stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Только runtime-либы (libpq нужна asyncpg в рантайме)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Копируем установленные пакеты из builder
COPY --from=builder /install /usr/local

# Копируем исходники
COPY . .

# Порт для FastAPI (api.py)
# Бот (bot.py) порт не открывает — он polling-клиент
EXPOSE 8080

# ─────────────────────────────────────────────────────────────────────────────
# CMD — по умолчанию запускает бота.
# Для API-сервиса переопределите CMD в docker-compose или на платформе:
#   command: uvicorn api:app --host 0.0.0.0 --port 8080
# ─────────────────────────────────────────────────────────────────────────────
CMD ["python", "bot.py"]
