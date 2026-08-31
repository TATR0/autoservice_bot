"""
retention.py — срок хранения персональных данных клиентов.

Отдельный круг, а не третий заход в цикле напоминаний: чистка суточная,
напоминания часовые, и складывать разные ритмы в один таймер значит либо
ходить в базу впустую каждый час, либо чистить раз в сутки то, о чём надо
напомнить сейчас.
"""

import asyncio
import logging

import config
from database import db

logger = logging.getLogger(__name__)

DAY_SECONDS = 24 * 60 * 60


async def purge_forever(interval: float = DAY_SECONDS) -> None:
    """
    Раз в сутки обезличивать заявки старше срока хранения.

    Первый круг — сразу на старте, как и у напоминаний: перезапуск не должен
    откладывать чистку на сутки.

    Ненастроенный срок хранения (PII_RETENTION_DAYS = 0) круг не отменяет —
    сама чистка в этом случае ничего не делает и в базу не ходит. Так
    включение срока не требует помнить ещё и про отдельный запуск.

    Цикл не умирает: обрыв базы стоит попробовать снова завтра.
    """
    while True:
        try:
            purged = await db.anonymize_old_requests(config.PII_RETENTION_DAYS)
            if purged:
                logger.info("Срок хранения: обезличено заявок %d", purged)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Круг чистки персональных данных не отработал")
        await asyncio.sleep(interval)
