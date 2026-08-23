"""
database.py — единственный модуль для работы с PostgreSQL (Supabase).

Все удаления «мягкие» через idrecstatus:
  0  — активная запись
 -1  — удалена / неактивна
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import Mapping
import datetime as dt
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Sequence
from uuid import uuid4

import asyncpg

import config
import slots
import subscription

logger = logging.getLogger(__name__)


class ForeignClientUid(Exception):
    """
    client_uid уже занят заявкой другого клиента.

    Свой uid форма генерирует случайно, поэтому в норме такого не бывает —
    это либо подбор чужого идентификатора, либо сломанный клиент.
    """


class SlotTaken(Exception):
    """Все места на выбранное время разобраны."""


def _new_id() -> str:
    return str(uuid4())


def _token_digest(token: str) -> str:
    """
    В базе лежит отпечаток приглашения, а не само приглашение.

    Токен из ссылки равносилен правам администратора: кто им владеет, тот
    видит персональные данные всех клиентов сервиса. Утёкший дамп базы не
    должен раздавать действующие приглашения. Соль не нужна — токен и так
    случайный на 128 бит, перебору не поддаётся.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def _is_local(dsn: str) -> bool:
    return "localhost" in dsn or "127.0.0.1" in dsn


class Database:
    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        if self._pool is not None:
            return

        kwargs: dict[str, Any] = {}
        # Supabase требует TLS. Если режим не задан в DSN — включаем его явно.
        if "sslmode" not in config.DATABASE_URL and not _is_local(config.DATABASE_URL):
            kwargs["ssl"] = "require"

        self._pool = await asyncpg.create_pool(
            dsn=config.DATABASE_URL,
            min_size=config.DB_POOL_MIN,
            max_size=config.DB_POOL_MAX,
            # Обязательно для transaction pooler'а Supabase (порт 6543),
            # безвредно для session pooler'а.
            statement_cache_size=0,
            command_timeout=10,
            **kwargs,
        )
        logger.info("✅ Подключено к PostgreSQL (pool %s–%s)", config.DB_POOL_MIN, config.DB_POOL_MAX)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("Database pool closed")

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._pool

    # ── users ────────────────────────────────────────────────────────────────

    async def upsert_user(
        self,
        idusertg: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (idusertg, username, first_name, last_name)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (idusertg) DO UPDATE
                SET username   = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name  = EXCLUDED.last_name,
                    is_blocked = false
                """,
                idusertg, username, first_name, last_name,
            )

    async def get_user(self, idusertg: int) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM users WHERE idusertg=$1", idusertg)

    async def set_user_blocked(self, idusertg: int, blocked: bool = True) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET is_blocked=$2 WHERE idusertg=$1", idusertg, blocked
            )

    async def set_user_phone(self, idusertg: int, phone: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (idusertg, phone) VALUES ($1,$2)
                ON CONFLICT (idusertg) DO UPDATE SET phone = EXCLUDED.phone
                """,
                idusertg, phone,
            )

    def user_title(self, user: asyncpg.Record | None, tg_id: int) -> str:
        """Человекочитаемое имя пользователя, если оно известно."""
        if not user:
            return f"ID {tg_id}"
        parts = [p for p in (user["first_name"], user["last_name"]) if p]
        name = " ".join(parts)
        if user["username"]:
            return f"{name} (@{user['username']})" if name else f"@{user['username']}"
        return name or f"ID {tg_id}"

    # ── services ─────────────────────────────────────────────────────────────

    async def create_service(
        self,
        *,
        name: str,
        phone: str,
        city: str,
        address: str,
        owner_tg_id: int,
        timezone: str | None = None,
    ) -> str:
        """Создать сервис, вернуть idservice. Владелец сразу становится админом."""
        idservice = _new_id()
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO services
                    (idservice, service_name, service_number, city,
                     location_service, owner_id, timezone, idrecstatus)
                VALUES ($1,$2,$3,$4,$5,$6,$7, 0)
                """,
                idservice, name, phone, city, address, owner_tg_id,
                timezone or config.DEFAULT_TIMEZONE,
            )
            await conn.execute(
                """
                INSERT INTO admins (idadmins, idservice, idusertg, idrecstatus)
                VALUES ($1,$2,$3, 0)
                ON CONFLICT (idservice, idusertg) DO UPDATE SET idrecstatus = 0
                """,
                _new_id(), idservice, owner_tg_id,
            )
            # Пробный период — это просто срок, проставленный при регистрации:
            # отдельной сущности «триал» нет и заводить её незачем
            paid_until = subscription.extend(
                None, dt.datetime.now(dt.timezone.utc), config.TRIAL_DAYS
            )
            await conn.execute(
                "UPDATE services SET paid_until=$2 WHERE idservice=$1",
                idservice, paid_until,
            )
            await conn.execute(
                """
                INSERT INTO subscription_payments (idservice, days, paid_until, source)
                VALUES ($1,$2,$3,'trial')
                """,
                idservice, config.TRIAL_DAYS, paid_until,
            )
            # Триал длится ровно столько, за сколько предупреждаем, поэтому
            # стадия «5d» подошла бы прямо сейчас. Дату называет приветствие
            await conn.execute(
                """
                INSERT INTO subscription_reminders (idservice, paid_until, stage)
                VALUES ($1,$2,$3)
                """,
                idservice, paid_until, subscription.STAGE_5D,
            )
            # Сервис без услуг не должен существовать даже мгновение:
            # в форме записи клиенту было бы нечего выбрать.
            await self._seed_catalog(conn, idservice)
            # Расписание по умолчанию — часть создания сервиса, а не отдельный
            # шаг: сервис без расписания не может принять ни одной заявки
            await conn.execute(
                "INSERT INTO service_schedule (idservice) VALUES ($1) "
                "ON CONFLICT (idservice) DO NOTHING",
                idservice,
            )
        return idservice

    async def get_service(self, idservice: str) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM services WHERE idservice=$1 AND idrecstatus=0",
                idservice,
            )

    async def get_services_by_city(self, city: str) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT * FROM services
                WHERE LOWER(TRIM(city))=LOWER(TRIM($1)) AND idrecstatus=0
                  -- Не оплатил — не продаёт новое время. Кабинет управляющего
                  -- при этом открыт: get_service такого фильтра не получает
                  AND paid_until > now()
                ORDER BY service_name
                """,
                city,
            )

    # ── service_catalog ──────────────────────────────────────────────────────

    async def _seed_catalog(self, conn: asyncpg.Connection, idservice: str) -> None:
        """Раздать сервису шаблонный набор услуг. Вызывается внутри транзакции."""
        await conn.executemany(
            """
            INSERT INTO service_catalog (idcatalog, idservice, title, sort_order, idrecstatus)
            VALUES ($1,$2,$3,$4,0)
            """,
            [
                (_new_id(), idservice, title, (i + 1) * 10)
                for i, title in enumerate(config.DEFAULT_SERVICE_TITLES)
            ],
        )

    async def get_catalog(self, idservice: str) -> list[asyncpg.Record]:
        """Активные услуги сервиса в порядке показа."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT * FROM service_catalog
                WHERE idservice=$1 AND idrecstatus=0
                ORDER BY sort_order, title
                """,
                idservice,
            )

    async def get_catalog_item(
        self, idservice: str, idcatalog: str
    ) -> asyncpg.Record | None:
        """
        Одна активная услуга сервиса.

        Фильтр по idservice обязателен: без него клиент подставил бы в заявку
        idcatalog чужого сервиса.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT * FROM service_catalog
                WHERE idcatalog=$1 AND idservice=$2 AND idrecstatus=0
                """,
                idcatalog, idservice,
            )

    async def add_catalog_item(
        self, idservice: str, title: str, price_rub: int | None = None
    ) -> asyncpg.Record | None:
        """
        Добавить услугу. None — активная услуга с таким названием уже есть.

        Ранее удалённая услуга воскресает, а не создаётся заново: так заявки,
        оформленные на неё до удаления, снова оказываются слинкованы со своей
        строкой каталога. Цена при воскрешении перезаписывается переданной:
        управляющий заводит услугу заново и указывает актуальную цену, а не
        наследует прошлогоднюю.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            revived = await conn.fetchrow(
                """
                UPDATE service_catalog SET idrecstatus=0, deletedate=NULL, price_rub=$3
                WHERE idcatalog = (
                        SELECT idcatalog FROM service_catalog
                         WHERE idservice=$1 AND idrecstatus=-1
                           AND lower(trim(title))=lower(trim($2))
                         ORDER BY deletedate DESC NULLS LAST
                         LIMIT 1)
                  AND NOT EXISTS (
                        SELECT 1 FROM service_catalog
                         WHERE idservice=$1 AND idrecstatus=0
                           AND lower(trim(title))=lower(trim($2)))
                RETURNING *
                """,
                idservice, title, price_rub,
            )
            if revived:
                return revived

            # DO NOTHING вместо исключения: активный дубликат — не ошибка
            # уровня БД, а понятный ответ пользователю «такая услуга уже есть»
            return await conn.fetchrow(
                """
                INSERT INTO service_catalog
                    (idcatalog, idservice, title, price_rub, sort_order, idrecstatus)
                VALUES ($1,$2,$3,$4,$5,0)
                ON CONFLICT (idservice, lower(trim(title))) WHERE idrecstatus = 0
                DO NOTHING
                RETURNING *
                """,
                _new_id(), idservice, title, price_rub, 1000,
            )

    async def set_catalog_item_price(
        self, idservice: str, idcatalog: str, price_rub: int | None
    ) -> asyncpg.Record | None:
        """
        Изменить цену услуги. None — услуги нет, она удалена или чужая.

        Отдельный метод, а не «обновить услугу целиком»: переименование услуг
        сознательно вне области, и смешивать эти операции незачем.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                UPDATE service_catalog SET price_rub=$3
                WHERE idcatalog=$1 AND idservice=$2 AND idrecstatus=0
                RETURNING *
                """,
                idcatalog, idservice, price_rub,
            )

    async def get_catalog_items(
        self,
        idservice: str,
        idcatalogs: Sequence[str],
        *,
        only_active: bool = True,
    ) -> list[asyncpg.Record]:
        """
        Услуги сервиса по списку идентификаторов, одним запросом.

        Фильтр по idservice обязателен: без него клиент подставил бы в заявку
        услуги чужого сервиса. only_active=False нужен, чтобы назвать удалённую
        услугу по имени в тексте отказа, а не отделаться безличным «одна из
        услуг недоступна».
        """
        if not idcatalogs:
            return []
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT * FROM service_catalog
                WHERE idservice=$1 AND idcatalog = ANY($2::uuid[])
                  AND (NOT $3::bool OR idrecstatus = 0)
                ORDER BY sort_order, title
                """,
                idservice, list(idcatalogs), only_active,
            )

    async def delete_catalog_item(
        self, idservice: str, idcatalog: str
    ) -> asyncpg.Record | None:
        """
        Мягко удалить услугу. None — удалять нечего или она последняя.

        Правило «хотя бы одна услуга» живёт в самом запросе, а не в хендлере:
        иначе управляющий с двух устройств удалил бы две последние услуги
        одновременно. Простого подсчёта в подзапросе недостаточно: в READ
        COMMITTED каждая параллельная транзакция видит свой снапшот и не
        замечает чужой незакоммиченный UPDATE — write skew, обе проверки
        пройдут одновременно. Поэтому сначала блокируем CTE (`FOR UPDATE`)
        все активные строки услуги сервиса: вторая транзакция встанет в
        очередь на эти строки, дождётся коммита первой и уже увидит
        актуальное количество. `ORDER BY idcatalog` в CTE обязателен —
        без единого порядка блокировки две параллельные транзакции могут
        захватывать строки в разной последовательности и словить deadlock.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                WITH locked AS (
                    SELECT idcatalog FROM service_catalog
                     WHERE idservice=$2 AND idrecstatus=0
                     ORDER BY idcatalog
                     FOR UPDATE
                )
                UPDATE service_catalog SET idrecstatus=-1, deletedate=now()
                WHERE idcatalog=$1 AND idservice=$2 AND idrecstatus=0
                  AND (SELECT count(*) FROM locked) > 1
                RETURNING *
                """,
                idcatalog, idservice,
            )

    async def count_requests_by_catalog(self, idservice: str, idcatalog: str) -> int:
        """Сколько заявок содержит эту услугу — для текста подтверждения."""
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT count(DISTINCT rs.idrequests)
                FROM request_services rs
                JOIN requests r ON r.idrequests = rs.idrequests
                WHERE r.idservice=$1 AND rs.idcatalog=$2 AND r.idrecstatus=0
                """,
                idservice, idcatalog,
            )
        return value or 0

    async def get_owned_services(self, owner_tg_id: int) -> list[asyncpg.Record]:
        """Сервисы, где пользователь — управляющий (owner)."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM services WHERE owner_id=$1 AND idrecstatus=0 ORDER BY service_name",
                owner_tg_id,
            )

    async def get_admin_services(self, admin_tg_id: int) -> list[asyncpg.Record]:
        """Сервисы, в которых пользователь числится активным администратором."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT s.* FROM services s
                JOIN admins a ON s.idservice=a.idservice
                WHERE a.idusertg=$1 AND a.idrecstatus=0 AND s.idrecstatus=0
                ORDER BY s.service_name
                """,
                admin_tg_id,
            )

    async def get_user_services(self, tg_id: int) -> list[asyncpg.Record]:
        """Все сервисы пользователя с максимальной ролью (owner > admin)."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT DISTINCT ON (s.idservice) s.*, v.role
                FROM v_user_roles v
                JOIN services s ON s.idservice = v.idservice
                WHERE v.idusertg = $1
                ORDER BY s.idservice, (v.role = 'owner') DESC
                """,
                tg_id,
            )

    async def count_owned_services(self, owner_tg_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT count(*) FROM services WHERE owner_id=$1 AND idrecstatus=0",
                owner_tg_id,
            )

    async def find_duplicate_service(self, owner_tg_id: int, name: str, city: str) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT * FROM services
                WHERE owner_id=$1 AND idrecstatus=0
                  AND lower(trim(service_name))=lower(trim($2))
                  AND lower(trim(city))=lower(trim($3))
                """,
                owner_tg_id, name, city,
            )

    async def close_service(self, idservice: str) -> list[asyncpg.Record]:
        """
        Каскадное закрытие сервиса в одной транзакции:
        сервис → удалён, админы → сняты, живые заявки → service_closed.

        Возвращает заявки, клиентов которых надо уведомить.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            affected = await conn.fetch(
                """
                UPDATE requests SET status='service_closed'
                WHERE idservice=$1 AND status = ANY($2::text[])
                RETURNING idrequests, idclienttg, seq, status
                """,
                idservice, list(config.ACTIVE_STATUSES),
            )
            if affected:
                await conn.executemany(
                    """
                    INSERT INTO request_status_history (idrequests, status_from, status_to, note)
                    VALUES ($1, NULL, 'service_closed', 'сервис удалён управляющим')
                    """,
                    [(r["idrequests"],) for r in affected],
                )
            await conn.execute(
                "UPDATE admins SET idrecstatus=-1 WHERE idservice=$1 AND idrecstatus=0",
                idservice,
            )
            await conn.execute(
                "UPDATE admin_invites SET used_at=now() WHERE idservice=$1 AND used_at IS NULL",
                idservice,
            )
            await conn.execute(
                "UPDATE services SET idrecstatus=-1, deletedate=now() WHERE idservice=$1",
                idservice,
            )
        return affected

    # ── Подписка ─────────────────────────────────────────────────────────────

    async def extend_subscription(
        self,
        idservice: str,
        *,
        days: int,
        source: str = "manual",
        external_id: str | None = None,
        granted_by: int | None = None,
    ) -> datetime | None:
        """
        Продлить подписку и записать продление в журнал — одной транзакцией.

        Порознь нельзя: однажды появится либо срок без следа в журнале, либо
        запись о платеже, который ничего не продлил.

        Строка сервиса блокируется на время расчёта: два продления подряд не
        должны посчитать новый срок от одного и того же остатка.
        """
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT paid_until FROM services"
                " WHERE idservice=$1 AND idrecstatus=0 FOR UPDATE",
                idservice,
            )
            if row is None:
                return None
            paid_until = subscription.extend(row["paid_until"], now, days)
            await conn.execute(
                "UPDATE services SET paid_until=$2 WHERE idservice=$1",
                idservice, paid_until,
            )
            await conn.execute(
                """
                INSERT INTO subscription_payments
                    (idservice, days, paid_until, source, external_id, granted_by)
                VALUES ($1,$2,$3,$4,$5,$6)
                """,
                idservice, days, paid_until, source, external_id, granted_by,
            )
        return paid_until

    def service_link(self, idservice: str) -> str:
        return f"https://t.me/{config.BOT_USERNAME}?start=SVC_{idservice}"

    def invite_link(self, token: str) -> str:
        return f"https://t.me/{config.BOT_USERNAME}?start=ADM_{token}"

    # ── schedule ─────────────────────────────────────────────────────────────

    _SCHEDULE_FIELDS = (
        "work_from", "work_to", "slot_minutes",
        "lunch_from", "lunch_to", "weekdays", "horizon_days", "capacity",
    )

    async def get_schedule(self, idservice: str) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM service_schedule WHERE idservice=$1", idservice
            )

    async def update_schedule(self, idservice: str, **fields) -> asyncpg.Record | None:
        """
        Правка расписания. Имена полей сверяются с белым списком: они приходят
        из хендлеров, и подстановка их в SQL без проверки — дыра.
        """
        unknown = set(fields) - set(self._SCHEDULE_FIELDS)
        if unknown:
            raise ValueError(f"Неизвестные поля расписания: {sorted(unknown)}")
        if not fields:
            return await self.get_schedule(idservice)

        columns = list(fields)
        assignments = ", ".join(f"{name}=${i}" for i, name in enumerate(columns, 2))
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                f"UPDATE service_schedule SET {assignments}, updatedate=now() "
                "WHERE idservice=$1 RETURNING *",
                idservice, *(fields[name] for name in columns),
            )

    async def get_taken_slots(
        self, idservice: str, since: datetime, until: datetime
    ) -> dict[datetime, int]:
        """Сколько живых заявок на каждый момент времени в окне."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT scheduled_at, count(*) AS taken FROM requests
                 WHERE idservice=$1 AND idrecstatus=0
                   AND scheduled_at BETWEEN $2 AND $3
                   AND status = ANY($4::text[])
                 GROUP BY scheduled_at
                """,
                idservice, since, until, list(config.SLOT_HOLDING_STATUSES),
            )
        return {row["scheduled_at"]: row["taken"] for row in rows}

    async def free_slots(self, service: Mapping) -> dict[date, list[time]]:
        """
        Свободные окна сервиса на весь горизонт записи.

        Один расчёт на три места: форма, приём заявки и карточка расписания.
        Разъехавшись, они показывали бы клиенту одни окна, принимали другие,
        а управляющему считали третьи.

        Расписания нет — окон нет: сервис ещё не открыл запись. Такой строки
        не должно существовать (она заводится вместе с сервисом), но политика
        на этот случай обязана быть одна, а не три разные.
        """
        idservice = str(service["idservice"])
        schedule = await self.get_schedule(idservice)
        if schedule is None:
            return {}

        now = datetime.now(timezone.utc)
        taken = await self.get_taken_slots(
            idservice, now, now + timedelta(days=schedule["horizon_days"] + 1)
        )
        return slots.free_slots(schedule, service["timezone"], now, taken)

    # ── admins ───────────────────────────────────────────────────────────────

    async def add_admin(self, idservice: str, admin_tg_id: int) -> None:
        """Добавить/восстановить администратора."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO admins (idadmins, idservice, idusertg, idrecstatus)
                VALUES ($1,$2,$3, 0)
                ON CONFLICT (idservice, idusertg) DO UPDATE SET idrecstatus = 0
                """,
                _new_id(), idservice, admin_tg_id,
            )

    async def remove_admin(self, idservice: str, admin_tg_id: int) -> None:
        """Мягкое удаление администратора."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE admins SET idrecstatus=-1 WHERE idservice=$1 AND idusertg=$2",
                idservice, admin_tg_id,
            )

    async def get_active_admins(self, idservice: str) -> list[asyncpg.Record]:
        """Активные администраторы сервиса вместе с профилем пользователя."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT a.*, u.username, u.first_name, u.last_name, u.is_blocked
                FROM admins a
                LEFT JOIN users u ON u.idusertg = a.idusertg
                WHERE a.idservice=$1 AND a.idrecstatus=0
                ORDER BY a.createdate
                """,
                idservice,
            )

    async def get_staff_ids(self, idservice: str) -> list[int]:
        """Кому слать уведомления: активные админы + владелец, без дублей и блокировок."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT v.idusertg
                FROM v_user_roles v
                LEFT JOIN users u ON u.idusertg = v.idusertg
                WHERE v.idservice=$1 AND COALESCE(u.is_blocked, false) = false
                """,
                idservice,
            )
        return [r["idusertg"] for r in rows]

    async def is_admin(self, idservice: str, tg_id: int) -> bool:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM admins WHERE idservice=$1 AND idusertg=$2 AND idrecstatus=0)",
                idservice, tg_id,
            )

    async def is_owner(self, idservice: str, tg_id: int) -> bool:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM services WHERE idservice=$1 AND owner_id=$2 AND idrecstatus=0)",
                idservice, tg_id,
            )

    async def has_access(self, idservice: str, tg_id: int) -> bool:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM v_user_roles WHERE idservice=$1 AND idusertg=$2)",
                idservice, tg_id,
            )

    # ── инвайты администраторов ──────────────────────────────────────────────

    async def create_invite(self, idservice: str, created_by: int) -> str:
        # 22 символа из безопасного алфавита — влезает в 64-символьный payload /start
        token = secrets.token_urlsafe(16)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO admin_invites (token, idservice, created_by, expires_at)
                VALUES ($1,$2,$3, now() + ($4 || ' days')::interval)
                """,
                _token_digest(token), idservice, created_by, str(config.INVITE_TTL_DAYS),
            )
        # Наружу отдаём сам токен — в базе остался только его отпечаток
        return token

    async def get_valid_invite(self, token: str) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT i.*, s.service_name, s.owner_id
                FROM admin_invites i
                JOIN services s ON s.idservice = i.idservice
                WHERE i.token=$1 AND i.used_at IS NULL AND i.expires_at > now()
                  AND s.idrecstatus = 0
                """,
                _token_digest(token),
            )

    async def use_invite(self, token: str, used_by: int) -> asyncpg.Record | None:
        """
        Погасить инвайт и выдать права. Условный UPDATE защищает от
        повторного использования одной ссылки двумя людьми.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            invite = await conn.fetchrow(
                """
                UPDATE admin_invites SET used_by=$2, used_at=now()
                WHERE token=$1 AND used_at IS NULL AND expires_at > now()
                RETURNING *
                """,
                _token_digest(token), used_by,
            )
            if invite is None:
                return None
            await conn.execute(
                """
                INSERT INTO admins (idadmins, idservice, idusertg, idrecstatus)
                VALUES ($1,$2,$3, 0)
                ON CONFLICT (idservice, idusertg) DO UPDATE SET idrecstatus = 0
                """,
                _new_id(), invite["idservice"], used_by,
            )
        return invite

    # ── requests ─────────────────────────────────────────────────────────────

    async def create_request(
        self,
        *,
        idservice: str,
        client_tg_id: int,
        client_name: str,
        phone: str,
        brand: str,
        model: str,
        plate: str,
        scheduled_at: datetime | None = None,
        services: list[dict],
        comment: str,
        client_uid: str | None = None,
    ) -> tuple[asyncpg.Record, bool]:
        """
        Создать заявку. Возвращает (запись, is_duplicate).

        is_duplicate=True — заявка с таким client_uid уже была создана
        (повторный тап «Отправить»), возвращается существующая.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            # Блокировка строки расписания делает проверку и вставку неделимыми.
            # Без неё два одновременных клиента оба проходят проверку до того,
            # как любой из них вставит строку, и одно место уходит дважды.
            if scheduled_at is not None:
                # Ждать блокировку дольше нескольких секунд бессмысленно: клиент
                # сидит перед формой. Без своего таймаута ожидание упирается в
                # command_timeout соединения, а тот бросает CancelledError —
                # клиент получает 500 вместо «время только что заняли».
                await conn.execute("SET LOCAL lock_timeout = '3s'")
                try:
                    schedule = await conn.fetchrow(
                        "SELECT capacity FROM service_schedule WHERE idservice=$1 FOR UPDATE",
                        idservice,
                    )
                except asyncpg.exceptions.LockNotAvailableError:
                    # Строку три секунды держит другая запись на этот же сервис.
                    # Для клиента это неотличимо от занятого места, и ответ тот же
                    logger.info(
                        "Не дождались блокировки расписания сервиса %s", idservice
                    )
                    raise SlotTaken(scheduled_at) from None
                if schedule is None:
                    raise SlotTaken(scheduled_at)

                # Повторный тап не должен занимать второе место: заявка с этим
                # client_uid уже существует и своё место уже держит
                already = await conn.fetchval(
                    "SELECT count(*) FROM requests "
                    " WHERE idservice=$1 AND scheduled_at=$2 AND idrecstatus=0 "
                    "   AND status = ANY($3::text[]) "
                    "   AND ($4::text IS NULL OR client_uid IS DISTINCT FROM $4)",
                    idservice, scheduled_at,
                    list(config.SLOT_HOLDING_STATUSES), client_uid,
                )
                if already >= schedule["capacity"]:
                    raise SlotTaken(scheduled_at)

            row = await conn.fetchrow(
                """
                INSERT INTO requests
                    (idrequests, idservice, idclienttg, client_name, phone,
                     brand, model, plate, comment, client_uid,
                     scheduled_at, status, idrecstatus)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'new',0)
                ON CONFLICT (client_uid) WHERE client_uid IS NOT NULL DO NOTHING
                RETURNING *
                """,
                _new_id(), idservice, client_tg_id, client_name, phone,
                brand, model, plate, comment, client_uid,
                scheduled_at,
            )
            if row is None:
                # Фильтр по idclienttg обязателен: без него, подобрав чужой
                # client_uid, клиент получил бы номер и сервис чужой заявки.
                existing = await conn.fetchrow(
                    "SELECT * FROM requests WHERE client_uid=$1 AND idclienttg=$2",
                    client_uid, client_tg_id,
                )
                if existing is None:
                    raise ForeignClientUid(client_uid)
                return existing, True

            # Позиции пишутся в той же транзакции: заявки без услуг
            # существовать не должно даже мгновение
            await conn.executemany(
                """
                INSERT INTO request_services
                    (idrequestservice, idrequests, idcatalog, title, price_rub, position)
                VALUES ($1,$2,$3,$4,$5,$6)
                """,
                [
                    (
                        _new_id(),
                        row["idrequests"],
                        item["idcatalog"],
                        item["title"],
                        item.get("price_rub"),
                        index,
                    )
                    for index, item in enumerate(services)
                ],
            )

            await conn.execute(
                """
                INSERT INTO request_status_history (idrequests, status_from, status_to, changed_by)
                VALUES ($1, NULL, 'new', $2)
                """,
                row["idrequests"], client_tg_id,
            )
        return row, False

    async def get_request_services(self, idrequests: str) -> list[asyncpg.Record]:
        """Позиции заявки в том порядке, в каком их выбирал клиент."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM request_services WHERE idrequests=$1 ORDER BY position",
                idrequests,
            )

    async def get_request(self, idrequests: str) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT r.*, s.service_name
                FROM requests r
                LEFT JOIN services s ON s.idservice = r.idservice
                WHERE r.idrequests=$1
                """,
                idrequests,
            )

    async def update_request_status(
        self,
        idrequests: str,
        new_status: str,
        *,
        changed_by: int | None,
        allowed_from: Sequence[str],
        note: str | None = None,
    ) -> asyncpg.Record | None:
        """
        Условная смена статуса: сработает, только если текущий статус
        входит в allowed_from. Вернуть None → заявку уже обработал кто-то другой.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                WITH prev AS (SELECT status FROM requests WHERE idrequests=$1)
                UPDATE requests
                   SET status=$2, handled_by=COALESCE($3, handled_by)
                 WHERE idrequests=$1 AND idrecstatus=0 AND status = ANY($4::text[])
                RETURNING *, (SELECT status FROM prev) AS status_from
                """,
                idrequests, new_status, changed_by, list(allowed_from),
            )
            if row is None:
                return None
            await conn.execute(
                """
                INSERT INTO request_status_history
                    (idrequests, status_from, status_to, changed_by, note)
                VALUES ($1,$2,$3,$4,$5)
                """,
                idrequests, row["status_from"], new_status, changed_by, note,
            )
        return row

    async def get_service_requests(
        self, idservice: str, *, limit: int = 20, statuses: Iterable[str] | None = None
    ) -> list[asyncpg.Record]:
        query = """
            SELECT r.*
                  ,(SELECT string_agg(rs.title, ', ' ORDER BY rs.position)
                      FROM request_services rs
                     WHERE rs.idrequests = r.idrequests) AS services_summary
            FROM requests r
            WHERE r.idservice=$1 AND r.idrecstatus=0
        """
        args: list[Any] = [idservice]
        if statuses:
            args.append(list(statuses))
            query += f" AND r.status = ANY(${len(args)}::text[])"
        args.append(limit)
        query += f" ORDER BY r.createdate DESC LIMIT ${len(args)}"

        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def get_client_requests(
        self, client_tg_id: int, *, limit: int = 10
    ) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT r.*, s.service_name
                      ,(SELECT string_agg(rs.title, ', ' ORDER BY rs.position)
                          FROM request_services rs
                         WHERE rs.idrequests = r.idrequests) AS services_summary
                FROM requests r
                LEFT JOIN services s ON r.idservice=s.idservice
                WHERE r.idclienttg=$1 AND r.idrecstatus=0
                ORDER BY r.createdate DESC LIMIT $2
                """,
                client_tg_id, limit,
            )

    # ── антиспам ─────────────────────────────────────────────────────────────

    async def seconds_since_last_request(self, client_tg_id: int) -> float | None:
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT EXTRACT(EPOCH FROM (now() - max(createdate)))
                FROM requests WHERE idclienttg=$1
                """,
                client_tg_id,
            )
        return float(value) if value is not None else None

    async def count_active_client_requests(self, client_tg_id: int, idservice: str) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT count(*) FROM requests
                WHERE idclienttg=$1 AND idservice=$2 AND idrecstatus=0
                  AND status = ANY($3::text[])
                """,
                client_tg_id, idservice, list(config.ACTIVE_STATUSES),
            )

    # ── статистика ───────────────────────────────────────────────────────────

    async def get_service_stats(self, idservice: str) -> asyncpg.Record | None:
        """Сводка по сервису. Дневные метрики считаются по таймзоне сервиса."""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT
                  count(r.idrequests)                                                    AS total,
                  count(r.idrequests) FILTER (
                      WHERE r.createdate >= date_trunc('day', now() AT TIME ZONE s.timezone)
                                            AT TIME ZONE s.timezone)                     AS today,
                  count(r.idrequests) FILTER (WHERE r.createdate >= now() - interval '7 days')  AS week,
                  count(r.idrequests) FILTER (WHERE r.createdate >= now() - interval '30 days') AS month,
                  count(r.idrequests) FILTER (WHERE r.status = 'new')                    AS st_new,
                  count(r.idrequests) FILTER (
                      WHERE r.status IN ('accepted','called','in_progress'))              AS st_work,
                  count(r.idrequests) FILTER (WHERE r.status = 'done')                   AS st_done,
                  count(r.idrequests) FILTER (WHERE r.status = 'rejected')               AS st_rejected,
                  count(r.idrequests) FILTER (WHERE r.status = 'cancelled')              AS st_cancelled,
                  count(r.idrequests) FILTER (
                      WHERE r.status = 'new' AND r.createdate < now() - interval '24 hours') AS overdue
                FROM services s
                LEFT JOIN requests r
                       ON r.idservice = s.idservice AND r.idrecstatus = 0
                WHERE s.idservice = $1
                GROUP BY s.idservice
                """,
                idservice,
            )

    async def get_service_breakdown(self, idservice: str) -> list[asyncpg.Record]:
        """
        Сколько раз заказывали каждую услугу. Считаем по позициям, а не по
        заявкам: заявка с тремя работами — это три заказанные услуги.
        Группировка по снимку названия оставляет в статистике и те услуги,
        которые из каталога уже удалили.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT rs.title AS title, count(*) AS cnt
                FROM request_services rs
                JOIN requests r ON r.idrequests = rs.idrequests
                WHERE r.idservice=$1 AND r.idrecstatus=0
                GROUP BY rs.title ORDER BY cnt DESC
                """,
                idservice,
            )

    async def get_avg_reaction_seconds(self, idservice: str) -> float | None:
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT EXTRACT(EPOCH FROM avg(h.first_change - r.createdate))
                FROM requests r
                JOIN LATERAL (
                    SELECT min(changed_at) AS first_change
                    FROM request_status_history
                    WHERE idrequests = r.idrequests AND status_to <> 'new'
                ) h ON h.first_change IS NOT NULL
                WHERE r.idservice = $1 AND r.createdate >= now() - interval '30 days'
                """,
                idservice,
            )
        return float(value) if value is not None else None


db = Database()
