"""
database.py — единственный модуль для работы с PostgreSQL (Supabase).

Все методы используют «мягкое удаление» через idrecstatus:
  0  — активная запись
 -1  — удалена / неактивна
"""

from __future__ import annotations

import logging
import ssl
from uuid import uuid4

import asyncpg

from config import DATABASE_URL, BOT_USERNAME

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────────────────────────────────────

def _new_id() -> str:
    return str(uuid4())


def _ssl_ctx() -> ssl.SSLContext:
    """Контекст SSL для Supabase (require, но без проверки hostname)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────────────────────

class Database:
    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            dsn=DATABASE_URL,
            min_size=1,
            max_size=10,
            ssl=_ssl_ctx(),
        )
        logger.info("✅ Подключено к Supabase PostgreSQL")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            logger.info("Database pool closed")

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._pool

    # ── services ──────────────────────────────────────────────────────────────

    async def create_service(
        self,
        *,
        name: str,
        phone: str,
        city: str,
        address: str,
        owner_tg_id: int,
    ) -> str:
        """Создать сервис, вернуть idservice."""
        idservice = _new_id()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO services
                    (idservice, service_name, service_number, city,
                     location_service, owner_id, idrecstatus)
                VALUES ($1,$2,$3,$4,$5,$6, 0)
                """,
                idservice, name.strip(), phone.strip(),
                city.strip(), address.strip(), owner_tg_id,
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
                ORDER BY service_name
                """,
                city,
            )

    async def get_owned_services(self, owner_tg_id: int) -> list[asyncpg.Record]:
        """Сервисы, где пользователь — управляющий (owner)."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM services WHERE owner_id=$1 AND idrecstatus=0 ORDER BY service_name",
                owner_tg_id,
            )

    async def soft_delete_service(self, idservice: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE services SET idrecstatus=-1 WHERE idservice=$1",
                idservice,
            )

    def service_link(self, idservice: str) -> str:
        return f"https://t.me/{BOT_USERNAME.lstrip('@')}?start=SVC_{idservice}"

    # ── admins ────────────────────────────────────────────────────────────────

    async def add_admin(self, idservice: str, admin_tg_id: int) -> None:
        """Добавить/восстановить администратора."""
        async with self.pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT idadmins FROM admins WHERE idservice=$1 AND idusertg=$2",
                idservice, admin_tg_id,
            )
            if existing:
                await conn.execute(
                    "UPDATE admins SET idrecstatus=0 WHERE idservice=$1 AND idusertg=$2",
                    idservice, admin_tg_id,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO admins (idadmins, idservice, idusertg, idrecstatus)
                    VALUES ($1,$2,$3, 0)
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
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM admins WHERE idservice=$1 AND idrecstatus=0",
                idservice,
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

    async def is_admin(self, idservice: str, tg_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM admins WHERE idservice=$1 AND idusertg=$2 AND idrecstatus=0",
                idservice, tg_id,
            )
            return row is not None

    async def is_owner(self, idservice: str, tg_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM services WHERE idservice=$1 AND owner_id=$2 AND idrecstatus=0",
                idservice, tg_id,
            )
            return row is not None

    # ── requests ──────────────────────────────────────────────────────────────

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
        service_type: str,
        urgency: str,
        comment: str,
    ) -> str:
        idrequest = _new_id()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO requests
                    (idrequests, idservice, idclienttg, client_name, phone,
                     brand, model, plate, service_type, urgency, comment,
                     status, idrecstatus)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'new',0)
                """,
                idrequest, idservice, client_tg_id, client_name, phone,
                brand, model, plate, service_type, urgency, comment,
            )
        return idrequest

    async def get_request(self, idrequests: str) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM requests WHERE idrequests=$1",
                idrequests,
            )

    async def set_request_status(self, idrequests: str, status: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE requests SET status=$1 WHERE idrequests=$2",
                status, idrequests,
            )

    async def get_service_requests(
        self, idservice: str, *, limit: int = 50
    ) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT * FROM requests
                WHERE idservice=$1 AND idrecstatus=0
                ORDER BY createdate DESC
                LIMIT $2
                """,
                idservice, limit,
            )

    async def get_client_requests(
        self, client_tg_id: int, *, limit: int = 10
    ) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT r.*, s.service_name FROM requests r
                LEFT JOIN services s ON r.idservice=s.idservice
                WHERE r.idclienttg=$1 AND r.idrecstatus=0
                ORDER BY r.createdate DESC LIMIT $2
                """,
                client_tg_id, limit,
            )


db = Database()
