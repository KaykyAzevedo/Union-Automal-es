-- Schema SQLite do commit 1634fa5 (antes da F13). Usado por tests/test_finance_qa.py.

CREATE TABLE IF NOT EXISTS cars (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id  TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    name_key     TEXT NOT NULL,
    photo_url    TEXT,
    price_cents  INTEGER,
    url          TEXT NOT NULL,
    posted       INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    missing_count INTEGER NOT NULL DEFAULT 0,  -- coletas seguidas sem o carro
    sold_at      TEXT,
    in_baseline  INTEGER NOT NULL DEFAULT 0,  -- entrou no registro inicial (1ª coleta): entrada real desconhecida
    listed_at    TEXT,                         -- data de cadastro no site (Last-Modified da foto), se disponível
    listed_at_checked TEXT                     -- última tentativa de obter listed_at (1x por dia)
);
CREATE INDEX IF NOT EXISTS ix_cars_name_key ON cars(name_key);

CREATE TABLE IF NOT EXISTS price_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    car_id       INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    price_cents  INTEGER,
    recorded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_price_history_car ON price_history(car_id);

CREATE TABLE IF NOT EXISTS tickets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    car_id       INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | done
    created_at   TEXT NOT NULL,
    done_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets(status);

CREATE TABLE IF NOT EXISTS price_alerts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    car_id           INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    old_price_cents  INTEGER NOT NULL,
    new_price_cents  INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    dismissed        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sold_alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    car_id       INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL,
    dismissed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,          -- poll | price_check | daily
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    ok           INTEGER,
    summary      TEXT,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS detail_cache (
    external_id  TEXT PRIMARY KEY,
    data         TEXT NOT NULL,          -- JSON do CarDetail
    fetched_at   TEXT NOT NULL
);
