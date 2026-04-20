-- ============================================================
-- AutoService Bot — схема базы данных (Supabase / PostgreSQL)
-- ============================================================
-- Запустите этот файл один раз в SQL-редакторе Supabase.
-- Все удаления «мягкие»: idrecstatus = 0 (активна) / -1 (удалена).
-- ============================================================

-- ── Сервисы ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS services (
    idservice        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service_name     text        NOT NULL,
    service_number   text        NOT NULL,
    city             text        NOT NULL DEFAULT '',
    location_service text        NOT NULL DEFAULT '',
    owner_id         bigint      NOT NULL,          -- Telegram ID управляющего
    idrecstatus      smallint    NOT NULL DEFAULT 0, -- 0 активна / -1 удалена
    createdate       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_services_city     ON services (lower(trim(city)));
CREATE INDEX IF NOT EXISTS idx_services_owner_id ON services (owner_id);


-- ── Администраторы ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS admins (
    idadmins     uuid     PRIMARY KEY DEFAULT gen_random_uuid(),
    idservice    uuid     NOT NULL REFERENCES services(idservice) ON DELETE CASCADE,
    idusertg     bigint   NOT NULL,          -- Telegram ID администратора
    idrecstatus  smallint NOT NULL DEFAULT 0, -- 0 активен / -1 удалён
    createdate   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_admins_service ON admins (idservice);
CREATE INDEX IF NOT EXISTS idx_admins_user    ON admins (idusertg);
CREATE UNIQUE INDEX IF NOT EXISTS idx_admins_unique ON admins (idservice, idusertg);


-- ── Заявки клиентов ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS requests (
    idrequests   uuid     PRIMARY KEY DEFAULT gen_random_uuid(),
    idservice    uuid     REFERENCES services(idservice) ON DELETE SET NULL,
    idclienttg   bigint,                     -- Telegram ID клиента
    client_name  text     NOT NULL,
    phone        text     NOT NULL,
    brand        text     NOT NULL DEFAULT '—',
    model        text     NOT NULL DEFAULT '—',
    plate        text     NOT NULL DEFAULT '—',
    service_type text     NOT NULL DEFAULT 'other',
    urgency      text     NOT NULL DEFAULT 'low',
    comment      text              DEFAULT '',
    status       text     NOT NULL DEFAULT 'new',  -- new / accepted / called / rejected
    idrecstatus  smallint NOT NULL DEFAULT 0,       -- 0 активна / -1 удалена
    createdate   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_requests_service  ON requests (idservice);
CREATE INDEX IF NOT EXISTS idx_requests_client   ON requests (idclienttg);
CREATE INDEX IF NOT EXISTS idx_requests_status   ON requests (status);
CREATE INDEX IF NOT EXISTS idx_requests_date     ON requests (createdate DESC);
