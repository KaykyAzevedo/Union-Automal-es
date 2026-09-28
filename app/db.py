"""Conexão (SQLite local ou Postgres na nuvem) e schema.

- DATABASE_URL (ou POSTGRES_URL) definido → Postgres via app.pgcompat (mesma API
  do sqlite3: `?`, row["col"], lastrowid). Lido a cada connect().
- Senão → SQLite em UNION_DB_PATH, ou /tmp/union/union.db na Vercel (VERCEL=1,
  efêmero!), ou data/union.db.
"""
from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "union.db"
TZ = ZoneInfo("America/Sao_Paulo")
log = logging.getLogger(__name__)

TABLES = ["cars", "price_history", "tickets", "price_alerts", "sold_alerts", "runs", "detail_cache"]

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
    kind         TEXT NOT NULL,          -- poll | price_check
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
"""
# Postgres: mesmo schema, só a PK autoincremento muda
PG_SCHEMA = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")


def now() -> datetime:
    return datetime.now(TZ).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    return dt.isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def is_vercel() -> bool:
    return os.environ.get("VERCEL") == "1"


def database_url() -> str | None:
    return os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or None


def db_path() -> Path:
    if os.environ.get("UNION_DB_PATH"):
        return Path(os.environ["UNION_DB_PATH"])
    return Path("/tmp/union/union.db") if is_vercel() else DEFAULT_DB_PATH


def is_postgres(conn) -> bool:
    return getattr(conn, "is_postgres", False)


def connect(path: str | Path | None = None):
    """Abre conexão. `path` explícito → sempre SQLite (aceita ':memory:').
    Sem `path`: DATABASE_URL/POSTGRES_URL → Postgres; senão SQLite em db_path()."""
    if path is None and database_url():
        from .pgcompat import PgConnection

        return PgConnection(database_url())
    target = str(path) if path is not None else str(db_path())
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: o FastAPI abre a dependência get_conn numa thread do
    # pool e roda o endpoint em outra. Cada conexão continua sendo de UMA requisição,
    # usada sequencialmente, então é seguro.
    conn = sqlite3.connect(target, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if target != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


# colunas adicionadas depois da 1ª versão: (tabela, coluna, definição)
MIGRATIONS = [
    ("cars", "missing_count", "INTEGER NOT NULL DEFAULT 0"),
    ("cars", "sold_at", "TEXT"),
    ("cars", "in_baseline", "INTEGER NOT NULL DEFAULT 0"),
    ("cars", "listed_at", "TEXT"),
    ("cars", "listed_at_checked", "TEXT"),
]


def migrate(conn) -> None:
    """Adiciona colunas que faltam em bancos antigos. Idempotente, não apaga dados."""
    for table, column, definition in MIGRATIONS:
        if is_postgres(conn):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
            continue
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    # Backfill único para bancos anteriores à F6: o registro inicial são os carros com o
    # menor first_seen (todos inseridos com o mesmo timestamp). Só roda se ninguém tiver a flag.
    conn.execute(
        """UPDATE cars SET in_baseline = 1
           WHERE external_id NOT LIKE 'demo-%'
             AND first_seen = (SELECT MIN(first_seen) FROM cars WHERE external_id NOT LIKE 'demo-%')
             AND NOT EXISTS (SELECT 1 FROM cars WHERE in_baseline = 1)"""
    )
    conn.commit()


class LockTimeout(Exception):
    """Não foi possível obter o lock dentro do prazo (outra instância está trabalhando)."""


def lock_url() -> str | None:
    """URL DIRETA (sem pooler) para a conexão que segura locks de sessão.

    Com PgBouncer em modo transação (URL pooled do Neon), um lock de sessão fica no
    backend errado. Ordem: DATABASE_URL_UNPOOLED > POSTGRES_URL_NON_POOLING > URL
    principal sem o "-pooler" do host Neon > URL principal."""
    url = os.environ.get("DATABASE_URL_UNPOOLED") or os.environ.get("POSTGRES_URL_NON_POOLING")
    if url:
        return url
    url = database_url()
    return url.replace("-pooler.", ".", 1) if url and "-pooler." in url else url


@contextlib.contextmanager
def advisory_lock(conn, key: str, wait: float = 20.0, required: bool = True):
    """Lock entre instâncias (Postgres); no SQLite não faz nada e rende True.

    Seguro com pooler e com função morta no meio: usa uma conexão PRÓPRIA, direta e
    autocommit (fechá-la solta o lock), e nunca espera para sempre: tenta
    pg_try_advisory_lock até `wait` segundos. Sem lock no prazo: levanta
    LockTimeout se `required`, senão rende False (o chamador segue sem lock).
    Commits na conexão de trabalho `conn` não afetam o lock."""
    if not is_postgres(conn):
        yield True
        return
    import time

    import psycopg

    from .pgcompat import normalize_url

    lock_conn = psycopg.connect(normalize_url(lock_url()), autocommit=True, connect_timeout=10,
                                prepare_threshold=None)
    try:
        deadline = time.monotonic() + wait
        while True:
            acquired = lock_conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (key,)).fetchone()[0]
            if acquired or time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        if not acquired:
            log.warning("lock %r ocupado há mais de %.0fs", key, wait)
            if required:
                raise LockTimeout(key)
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    lock_conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
                except Exception:  # a conexão vai ser fechada de qualquer jeito
                    pass
    finally:
        lock_conn.close()


def init_db(conn=None) -> None:
    """Cria tabelas e aplica migrações (idempotente, nos dois bancos)."""
    own = conn is None
    conn = conn or connect()
    try:
        # várias instâncias frias da Vercel podem subir juntas: serializa o DDL
        with advisory_lock(conn, "union:init_db", wait=15, required=False):
            conn.executescript(PG_SCHEMA if is_postgres(conn) else SCHEMA)
            migrate(conn)
            conn.commit()
    finally:
        if own:
            conn.close()


def truncate_all(conn) -> None:
    """Zera todas as tabelas (testes). Postgres: TRUNCATE ... RESTART IDENTITY CASCADE."""
    if is_postgres(conn):
        conn.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
    else:
        for table in reversed(TABLES):
            conn.execute(f"DELETE FROM {table}")
        if conn.execute("SELECT name FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
            conn.execute("DELETE FROM sqlite_sequence")
    conn.commit()


def get_conn():
    """Dependency FastAPI: uma conexão por request."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
