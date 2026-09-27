"""Conexão SQLite e schema."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "union.db"
TZ = ZoneInfo("America/Sao_Paulo")

SCHEMA = """
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
    sold_at      TEXT
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
    kind         TEXT NOT NULL,          -- poll | price_check
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    ok           INTEGER,
    summary      TEXT,
    error        TEXT
);
"""


def now() -> datetime:
    return datetime.now(TZ).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    return dt.isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def db_path() -> Path:
    return Path(os.environ.get("UNION_DB_PATH") or DEFAULT_DB_PATH)


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Abre conexão. `path` > env UNION_DB_PATH > data/union.db. Aceita ':memory:'."""
    target = str(path) if path is not None else str(db_path())
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if target != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


# colunas adicionadas depois da 1ª versão: (tabela, coluna, definição)
MIGRATIONS = [
    ("cars", "missing_count", "INTEGER NOT NULL DEFAULT 0"),
    ("cars", "sold_at", "TEXT"),
]


def migrate(conn: sqlite3.Connection) -> None:
    """Adiciona colunas que faltam em bancos antigos. Idempotente, não apaga dados."""
    for table, column, definition in MIGRATIONS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.commit()


def init_db(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        conn.executescript(SCHEMA)
        migrate(conn)
        conn.commit()
    finally:
        if own:
            conn.close()


def get_conn():
    """Dependency FastAPI: uma conexão por request."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
