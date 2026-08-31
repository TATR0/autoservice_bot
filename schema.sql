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

ALTER TABLE service_catalog
    ADD COLUMN IF NOT EXISTS price_rub int;

-- Цена в целых рублях: копейки в прайсе автосервиса не встречаются, а numeric
-- тянет округления на ровном месте. NULL — «не указана», это не то же самое,
-- что 0 («бесплатно»).
ALTER TABLE service_catalog DROP CONSTRAINT IF EXISTS chk_catalog_price;
ALTER TABLE service_catalog ADD  CONSTRAINT chk_catalog_price
    CHECK (price_rub IS NULL OR price_rub BETWEEN 0 AND 10000000);


-- ── Расписание сервиса ───────────────────────────────────────────────────────
-- Слоты записи нигде не хранятся: они вычисляются из этого шаблона, а занятость
-- берётся из заявок. Так отмена заявки освобождает время сама собой, без
-- отдельной логики возврата, которой было бы что ломать.

CREATE TABLE IF NOT EXISTS service_schedule (
    idservice    uuid        PRIMARY KEY REFERENCES services(idservice) ON DELETE CASCADE,
    work_from    time        NOT NULL DEFAULT '09:00',
    work_to      time        NOT NULL DEFAULT '18:00',
    slot_minutes int         NOT NULL DEFAULT 60,
    lunch_from   time,
    lunch_to     time,
    weekdays     smallint[]  NOT NULL DEFAULT '{1,2,3,4,5}',  -- ISO: 1=пн … 7=вс
    horizon_days int         NOT NULL DEFAULT 14,
    capacity     int         NOT NULL DEFAULT 1,
    updatedate   timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE service_schedule DROP CONSTRAINT IF EXISTS chk_schedule_hours;
ALTER TABLE service_schedule ADD  CONSTRAINT chk_schedule_hours
    CHECK (work_from < work_to);

ALTER TABLE service_schedule DROP CONSTRAINT IF EXISTS chk_schedule_step;
ALTER TABLE service_schedule ADD  CONSTRAINT chk_schedule_step
    CHECK (slot_minutes IN (30, 60, 90, 120));

-- Обед либо не задан вовсе, либо задан целиком и лежит внутри рабочих часов:
-- половинчатое состояние сделало бы нарезку неоднозначной
ALTER TABLE service_schedule DROP CONSTRAINT IF EXISTS chk_schedule_lunch;
ALTER TABLE service_schedule ADD  CONSTRAINT chk_schedule_lunch
    CHECK ((lunch_from IS NULL AND lunch_to IS NULL)
        OR (lunch_from IS NOT NULL AND lunch_to IS NOT NULL
            AND lunch_from < lunch_to
            AND lunch_from >= work_from AND lunch_to <= work_to));

ALTER TABLE service_schedule DROP CONSTRAINT IF EXISTS chk_schedule_horizon;
ALTER TABLE service_schedule ADD  CONSTRAINT chk_schedule_horizon
    CHECK (horizon_days BETWEEN 1 AND 60);

ALTER TABLE service_schedule DROP CONSTRAINT IF EXISTS chk_schedule_capacity;
ALTER TABLE service_schedule ADD  CONSTRAINT chk_schedule_capacity
    CHECK (capacity BETWEEN 1 AND 20);

-- Хранятся рабочие дни, а не выходные: пустой список выходных двусмыслен
-- («работаем всегда» или «не заполнили»), а пустой список рабочих дней запрещён
ALTER TABLE service_schedule DROP CONSTRAINT IF EXISTS chk_schedule_weekdays;
ALTER TABLE service_schedule ADD  CONSTRAINT chk_schedule_weekdays
    CHECK (array_length(weekdays, 1) BETWEEN 1 AND 7
       AND weekdays <@ ARRAY[1,2,3,4,5,6,7]::smallint[]);


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
    ADD COLUMN IF NOT EXISTS service_title text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS scheduled_at  timestamptz;

-- Колонка пережила прошлую фичу как страховка на случай отката. Бэкфил
-- проверен, приложение её не читает и не пишет — удаляем.
ALTER TABLE requests DROP COLUMN IF EXISTS service_type;

-- Срочность заменена временем записи: клиент выбирает окно в календаре, а
-- «высокая срочность» без часа ничего не обещала ни ему, ни сервису. Колонка
-- пережила выкат календаря как страховка на случай отката — она больше не
-- нужна. Констрейнт снимается вместе с ней, но названо это отдельной строкой:
-- на базе, где колонку уже удалили, DROP CONSTRAINT просто ничего не найдёт
ALTER TABLE requests DROP CONSTRAINT IF EXISTS chk_requests_urgency;
ALTER TABLE requests DROP COLUMN     IF EXISTS urgency;

-- Отметка об отправленном напоминании клиенту. Живёт в самой заявке, а не в
-- отдельной таблице: напоминание у заявки одно, и право на него занимается
-- условием прямо в UPDATE
ALTER TABLE requests ADD COLUMN IF NOT EXISTS reminder_sent_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_requests_service ON requests (idservice);
CREATE INDEX IF NOT EXISTS idx_requests_client  ON requests (idclienttg);
CREATE INDEX IF NOT EXISTS idx_requests_status  ON requests (status);
CREATE INDEX IF NOT EXISTS idx_requests_date    ON requests (createdate DESC);
CREATE INDEX IF NOT EXISTS idx_requests_catalog ON requests (idcatalog);

-- Занятость окна считается по этому индексу. Заявки без времени отсечены:
-- они остались с тех пор, когда клиент выбирал срочность, а не час
CREATE INDEX IF NOT EXISTS idx_requests_scheduled
    ON requests (idservice, scheduled_at) WHERE scheduled_at IS NOT NULL;

-- человекочитаемый номер заявки: клиенту показываем #000142, а не uuid
CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_seq ON requests (seq);

-- идемпотентность: повторный тап «Отправить» не создаст дубль
CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_client_uid
    ON requests (client_uid) WHERE client_uid IS NOT NULL;

ALTER TABLE requests DROP CONSTRAINT IF EXISTS chk_requests_status;
ALTER TABLE requests ADD  CONSTRAINT chk_requests_status CHECK (status IN
    ('new','accepted','called','in_progress','done','rejected','cancelled','service_closed'));


-- ── Подписка сервиса ─────────────────────────────────────────
-- Состояние подписки нигде не хранится: активна ⇔ paid_until > now().
-- Переключать нечего, поэтому нечему и разъехаться с реальностью.

-- Срок — момент, а не дата: «за 24 часа» иначе превратилось бы в «в полночь
-- накануне по серверному времени», а у сервисов свои зоны. Колонка пуста во
-- всех строках, поэтому смена типа сегодня бесплатна
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'services' AND column_name = 'paid_until'
                  AND data_type = 'date') THEN
        ALTER TABLE services ALTER COLUMN paid_until TYPE timestamptz;
    END IF;
END $$;

-- Журнал продлений: единственное место, где меняется paid_until.
-- Источник — открытый набор: 'trial', 'backfill', 'manual', завтра провайдер
CREATE TABLE IF NOT EXISTS subscription_payments (
    idpayment   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    idservice   uuid        NOT NULL REFERENCES services(idservice) ON DELETE CASCADE,
    days        integer     NOT NULL,
    paid_until  timestamptz NOT NULL,   -- каким срок стал после этого продления
    source      text        NOT NULL DEFAULT 'manual',
    external_id text,                   -- id платежа у провайдера, NULL для ручных
    granted_by  bigint,                 -- Telegram id продлившего вручную
    createdate  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_subscription_payments_service
    ON subscription_payments (idservice, createdate DESC);

-- Повторная доставка того же платежа упрётся сюда и не продлит подписку
-- дважды. Вебхуки повторяются всегда, это не редкий случай
CREATE UNIQUE INDEX IF NOT EXISTS idx_subscription_payments_external
    ON subscription_payments (source, external_id) WHERE external_id IS NOT NULL;

ALTER TABLE subscription_payments
    ADD COLUMN IF NOT EXISTS refunded_at timestamptz;

-- Журнал помнит и отобранные дни: возврат звёзд и ручное укорачивание. Ноль
-- по-прежнему отвергается — начисление на ноль дней это опечатка, а не операция
ALTER TABLE subscription_payments DROP CONSTRAINT IF EXISTS chk_subscription_payments_days;
ALTER TABLE subscription_payments ADD  CONSTRAINT chk_subscription_payments_days
    CHECK (days <> 0);

-- Отметки об отправленных напоминаниях. В ключ входит сам срок: после
-- продления новый срок получает свои напоминания, а напомнить повторно про
-- старый невозможно
CREATE TABLE IF NOT EXISTS subscription_reminders (
    idreminder uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    idservice  uuid        NOT NULL REFERENCES services(idservice) ON DELETE CASCADE,
    paid_until timestamptz NOT NULL,
    stage      text        NOT NULL,
    createdate timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_subscription_reminders_once
    ON subscription_reminders (idservice, paid_until, stage);

ALTER TABLE subscription_reminders DROP CONSTRAINT IF EXISTS chk_subscription_reminders_stage;
ALTER TABLE subscription_reminders ADD  CONSTRAINT chk_subscription_reminders_stage
    CHECK (stage IN ('5d','24h','expired'));

-- Бэкфил. Без него в момент выката каждый живой сервис окажется просроченным:
-- пропадёт из поиска и перестанет принимать заявки, тихо, среди рабочего дня.
-- WHERE paid_until IS NULL — повторный прогон схемы никому ничего не перепишет
WITH granted AS (
    UPDATE services SET paid_until = now() + interval '30 days'
     WHERE idrecstatus = 0 AND paid_until IS NULL
 RETURNING idservice, paid_until
)
INSERT INTO subscription_payments (idservice, days, paid_until, source)
SELECT idservice, 30, paid_until, 'backfill' FROM granted;


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


-- ── Позиции заявки ───────────────────────────────────────────────────────
-- Одна строка на одну выбранную клиентом услугу. title и price_rub —
-- снимки на момент оформления: управляющий вправе поменять цену завтра, но
-- клиенту обещали сегодняшнюю. position хранит порядок, в котором клиент
-- видел услуги, чтобы карточка администратору совпадала с формой.
CREATE TABLE IF NOT EXISTS request_services (
    idrequestservice uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    idrequests       uuid        NOT NULL REFERENCES requests(idrequests) ON DELETE CASCADE,
    idcatalog        uuid        REFERENCES service_catalog(idcatalog) ON DELETE SET NULL,
    title            text        NOT NULL,
    price_rub        int,
    position         int         NOT NULL DEFAULT 0,
    createdate       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_request_services_request
    ON request_services (idrequests);
CREATE INDEX IF NOT EXISTS idx_request_services_catalog
    ON request_services (idcatalog);

-- одну и ту же услугу нельзя добавить в заявку дважды
CREATE UNIQUE INDEX IF NOT EXISTS idx_request_services_unique
    ON request_services (idrequests, idcatalog) WHERE idcatalog IS NOT NULL;


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

-- ключа не нашлось в списке выше — заявка всё равно должна быть читаемой
UPDATE requests SET service_title = 'Другое' WHERE service_title = '';

UPDATE requests r
   SET idcatalog = c.idcatalog
  FROM service_catalog c
 WHERE r.idcatalog IS NULL
   AND c.idservice = r.idservice
   AND c.idrecstatus = 0
   AND lower(trim(c.title)) = lower(trim(r.service_title));

-- Существующие заявки: у каждой ровно одна услуга, она становится позицией.
INSERT INTO request_services (idrequests, idcatalog, title, position)
SELECT r.idrequests, r.idcatalog, r.service_title, 0
  FROM requests r
 WHERE r.service_title <> ''
   AND NOT EXISTS (
        SELECT 1 FROM request_services rs WHERE rs.idrequests = r.idrequests);

-- Каждому существующему сервису — расписание по умолчанию. Альтернатива
-- («нет строки — значит не настроено») означала бы, что после выката все живые
-- сервисы перестают принимать заявки, пока управляющий не дойдёт до настроек.
INSERT INTO service_schedule (idservice)
SELECT idservice FROM services
ON CONFLICT (idservice) DO NOTHING;


-- ── Row Level Security ───────────────────────────────────────
-- Политики не создаются намеренно: бот ходит в базу под владельцем таблиц
-- (RLS его не ограничивает), а anon/authenticated через PostgREST не получат
-- ничего. Без этого ФИО, телефоны и госномера читаются публичным anon-ключом.
ALTER TABLE users                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE services               ENABLE ROW LEVEL SECURITY;
ALTER TABLE admins                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_catalog        ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_schedule       ENABLE ROW LEVEL SECURITY;
ALTER TABLE requests               ENABLE ROW LEVEL SECURITY;
ALTER TABLE request_status_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE request_services        ENABLE ROW LEVEL SECURITY;
ALTER TABLE admin_invites          ENABLE ROW LEVEL SECURITY;
ALTER TABLE fsm_storage            ENABLE ROW LEVEL SECURITY;
