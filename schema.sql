-- ============================================================
-- AutoService Bot — схема базы данных v2 (Supabase / PostgreSQL)
-- ============================================================
-- Запустите файл в SQL-редакторе Supabase. Скрипт идемпотентный:
-- его можно выполнять и на пустой базе, и поверх схемы v1.
-- Все удаления «мягкие»: idrecstatus = 0 (активна) / -1 (удалена).
-- ============================================================


-- ── Пользователи ─────────────────────────────────────────────
-- Нужны, чтобы показывать админов по имени, знать кто заблокировал
-- бота и подставлять телефон в форму записи.
CREATE TABLE IF NOT EXISTS users (
    idusertg    bigint      PRIMARY KEY,
    username    text,
    first_name  text,
    last_name   text,
    phone       text,
    is_blocked  boolean     NOT NULL DEFAULT false,
    createdate  timestamptz NOT NULL DEFAULT now(),
    updatedate  timestamptz NOT NULL DEFAULT now()
);


-- ── Сервисы ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS services (
    idservice        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service_name     text        NOT NULL,
    service_number   text        NOT NULL,
    city             text        NOT NULL DEFAULT '',
    location_service text        NOT NULL DEFAULT '',
    owner_id         bigint      NOT NULL,           -- Telegram ID управляющего
    idrecstatus      smallint    NOT NULL DEFAULT 0, -- 0 активна / -1 удалена
    createdate       timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE services
    ADD COLUMN IF NOT EXISTS timezone   text        NOT NULL DEFAULT 'Europe/Moscow',
    ADD COLUMN IF NOT EXISTS plan       text        NOT NULL DEFAULT 'free',
    ADD COLUMN IF NOT EXISTS paid_until date,
    ADD COLUMN IF NOT EXISTS updatedate timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS deletedate timestamptz;

CREATE INDEX IF NOT EXISTS idx_services_city     ON services (lower(trim(city)));
CREATE INDEX IF NOT EXISTS idx_services_owner_id ON services (owner_id);

-- один и тот же сервис не регистрируем дважды
CREATE UNIQUE INDEX IF NOT EXISTS idx_services_unique_active
    ON services (owner_id, lower(trim(service_name)), lower(trim(city)))
    WHERE idrecstatus = 0;


-- ── Администраторы ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS admins (
    idadmins     uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    idservice    uuid        NOT NULL REFERENCES services(idservice) ON DELETE CASCADE,
    idusertg     bigint      NOT NULL,           -- Telegram ID администратора
    idrecstatus  smallint    NOT NULL DEFAULT 0, -- 0 активен / -1 удалён
    createdate   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_admins_service ON admins (idservice);
CREATE INDEX IF NOT EXISTS idx_admins_user    ON admins (idusertg);
CREATE UNIQUE INDEX IF NOT EXISTS idx_admins_unique ON admins (idservice, idusertg);


-- ── Каталог услуг сервиса ────────────────────────────────────────────────
-- Список услуг у каждого сервиса свой: детейлинг-студии не нужны «Ремонт
-- двигателя» и «Коробка передач». При регистрации копируется шаблонный
-- набор, дальше управляющий правит его сам.
CREATE TABLE IF NOT EXISTS service_catalog (
    idcatalog   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    idservice   uuid        NOT NULL REFERENCES services(idservice) ON DELETE CASCADE,
    title       text        NOT NULL,
    sort_order  int         NOT NULL DEFAULT 100,
    idrecstatus smallint    NOT NULL DEFAULT 0, -- 0 активна / -1 удалена
    createdate  timestamptz NOT NULL DEFAULT now(),
    deletedate  timestamptz
);

CREATE INDEX IF NOT EXISTS idx_catalog_service
    ON service_catalog (idservice) WHERE idrecstatus = 0;

-- в одном сервисе не может быть двух активных услуг с одинаковым названием
CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_unique_title
    ON service_catalog (idservice, lower(trim(title))) WHERE idrecstatus = 0;


-- ── Заявки клиентов ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS requests (
    idrequests   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    idservice    uuid        REFERENCES services(idservice) ON DELETE SET NULL,
    idclienttg   bigint,                          -- Telegram ID клиента
    client_name  text        NOT NULL,
    phone        text        NOT NULL,
    brand        text        NOT NULL DEFAULT '—',
    model        text        NOT NULL DEFAULT '—',
    plate        text        NOT NULL DEFAULT '—',
    service_type text        NOT NULL DEFAULT 'other', -- legacy: не читается приложением, ждёт удаления после проверки бэкфила
    urgency      text        NOT NULL DEFAULT 'low',
    comment      text                 DEFAULT '',
    status       text        NOT NULL DEFAULT 'new',
    idrecstatus  smallint    NOT NULL DEFAULT 0,  -- 0 активна / -1 удалена
    createdate   timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS seq        bigint GENERATED BY DEFAULT AS IDENTITY,
    ADD COLUMN IF NOT EXISTS client_uid text,
    ADD COLUMN IF NOT EXISTS handled_by bigint,
    ADD COLUMN IF NOT EXISTS updatedate timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS idcatalog     uuid REFERENCES service_catalog(idcatalog) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS service_title text NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_requests_service ON requests (idservice);
CREATE INDEX IF NOT EXISTS idx_requests_client  ON requests (idclienttg);
CREATE INDEX IF NOT EXISTS idx_requests_status  ON requests (status);
CREATE INDEX IF NOT EXISTS idx_requests_date    ON requests (createdate DESC);
CREATE INDEX IF NOT EXISTS idx_requests_catalog ON requests (idcatalog);

-- человекочитаемый номер заявки: клиенту показываем #000142, а не uuid
CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_seq ON requests (seq);

-- идемпотентность: повторный тап «Отправить» не создаст дубль
CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_client_uid
    ON requests (client_uid) WHERE client_uid IS NOT NULL;

ALTER TABLE requests DROP CONSTRAINT IF EXISTS chk_requests_status;
ALTER TABLE requests ADD  CONSTRAINT chk_requests_status CHECK (status IN
    ('new','accepted','called','in_progress','done','rejected','cancelled','service_closed'));

ALTER TABLE requests DROP CONSTRAINT IF EXISTS chk_requests_urgency;
ALTER TABLE requests ADD  CONSTRAINT chk_requests_urgency CHECK (urgency IN
    ('low','medium','high','urgent'));


-- ── История смены статусов ───────────────────────────────────
CREATE TABLE IF NOT EXISTS request_status_history (
    id          bigserial   PRIMARY KEY,
    idrequests  uuid        NOT NULL REFERENCES requests(idrequests) ON DELETE CASCADE,
    status_from text,
    status_to   text        NOT NULL,
    changed_by  bigint,
    note        text,
    changed_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rsh_request ON request_status_history (idrequests, changed_at);


-- ── Инвайты администраторов ──────────────────────────────────
CREATE TABLE IF NOT EXISTS admin_invites (
    token      text        PRIMARY KEY, -- sha256-отпечаток приглашения, не сам токен
    idservice  uuid        NOT NULL REFERENCES services(idservice) ON DELETE CASCADE,
    created_by bigint      NOT NULL,
    used_by    bigint,
    used_at    timestamptz,
    expires_at timestamptz NOT NULL DEFAULT now() + interval '7 days',
    createdate timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_invites_service ON admin_invites (idservice) WHERE used_at IS NULL;


-- ── Хранилище FSM (альтернатива Redis) ───────────────────────
CREATE TABLE IF NOT EXISTS fsm_storage (
    key        text        PRIMARY KEY,
    state      text,
    data       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    updatedate timestamptz NOT NULL DEFAULT now()
);


-- ── Роли пользователя ────────────────────────────────────────
CREATE OR REPLACE VIEW v_user_roles AS
SELECT s.owner_id AS idusertg, s.idservice, 'owner'::text AS role
  FROM services s WHERE s.idrecstatus = 0
UNION ALL
SELECT a.idusertg, a.idservice, 'admin'::text
  FROM admins a JOIN services s USING (idservice)
 WHERE a.idrecstatus = 0 AND s.idrecstatus = 0;


-- ── Триггер updatedate ───────────────────────────────────────
CREATE OR REPLACE FUNCTION touch_updatedate() RETURNS trigger AS $$
BEGIN NEW.updatedate = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_requests_touch ON requests;
CREATE TRIGGER trg_requests_touch BEFORE UPDATE ON requests
    FOR EACH ROW EXECUTE FUNCTION touch_updatedate();

DROP TRIGGER IF EXISTS trg_services_touch ON services;
CREATE TRIGGER trg_services_touch BEFORE UPDATE ON services
    FOR EACH ROW EXECUTE FUNCTION touch_updatedate();

DROP TRIGGER IF EXISTS trg_users_touch ON users;
CREATE TRIGGER trg_users_touch BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION touch_updatedate();


-- ── Бэкфил каталога услуг ────────────────────────────────────────────────
-- Идемпотентно: сервисам без каталога раздаётся шаблонный набор, старым
-- заявкам проставляются название услуги и ссылка на строку каталога.

INSERT INTO service_catalog (idservice, title, sort_order)
SELECT s.idservice, t.title, t.ord
  FROM services s
 CROSS JOIN (VALUES
        ('Диагностика',       10),
        ('Замена масла',      20),
        ('Шины и диски',      30),
        ('Тормозная система', 40),
        ('Ремонт двигателя',  50),
        ('Коробка передач',   60),
        ('Подвеска',          70),
        ('Кузовные работы',   80),
        ('Другое',            90)
      ) AS t(title, ord)
 WHERE s.idrecstatus = 0
   AND NOT EXISTS (SELECT 1 FROM service_catalog c WHERE c.idservice = s.idservice);

UPDATE requests r
   SET service_title = m.title
  FROM (VALUES
        ('diagnostic',   'Диагностика'),
        ('oil-change',   'Замена масла'),
        ('tires',        'Шины и диски'),
        ('brake',        'Тормозная система'),
        ('engine',       'Ремонт двигателя'),
        ('transmission', 'Коробка передач'),
        ('suspension',   'Подвеска'),
        ('body',         'Кузовные работы'),
        ('other',        'Другое')
       ) AS m(key, title)
 WHERE r.service_title = '' AND r.service_type = m.key;

-- ключа не нашлось в списке выше — заявка всё равно должна быть читаемой
UPDATE requests SET service_title = 'Другое' WHERE service_title = '';

UPDATE requests r
   SET idcatalog = c.idcatalog
  FROM service_catalog c
 WHERE r.idcatalog IS NULL
   AND c.idservice = r.idservice
   AND c.idrecstatus = 0
   AND lower(trim(c.title)) = lower(trim(r.service_title));


-- ── Row Level Security ───────────────────────────────────────
-- Политики не создаются намеренно: бот ходит в базу под владельцем таблиц
-- (RLS его не ограничивает), а anon/authenticated через PostgREST не получат
-- ничего. Без этого ФИО, телефоны и госномера читаются публичным anon-ключом.
ALTER TABLE users                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE services               ENABLE ROW LEVEL SECURITY;
ALTER TABLE admins                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_catalog        ENABLE ROW LEVEL SECURITY;
ALTER TABLE requests               ENABLE ROW LEVEL SECURITY;
ALTER TABLE request_status_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE admin_invites          ENABLE ROW LEVEL SECURITY;
ALTER TABLE fsm_storage            ENABLE ROW LEVEL SECURITY;
