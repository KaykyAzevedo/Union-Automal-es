"""F15 (Forja): tabela users, seed dos funcionários e migração — nos dois bancos."""
from __future__ import annotations

import re

from app import db, pgcompat

SEED = [("arthur", "Arthur", "comprador,vendedor"), ("marcelo", "Marcelo", "comprador,vendedor"),
        ("beto", "Beto", "vendedor"), ("thiago", "Thiago", "vendedor"),
        ("haroldo", "Haroldo", "documentador"), ("ruan", "Ruan", "chefe")]
COLUMNS = {"id", "username", "name", "roles", "password_hash", "must_change", "pw_version", "active",
           "created_at", "last_login"}


def _users(conn) -> list[tuple]:
    return [tuple(r) for r in conn.execute("SELECT username, name, roles FROM users ORDER BY id")]


def test_table_and_registration():
    assert "users" in db.TABLES and "users" in pgcompat.ID_TABLES
    assert db.SEED_USERS == SEED


def test_columns(conn):
    assert db._columns(conn, "users") == COLUMNS


def test_seed_creates_staff_without_password(conn):
    assert _users(conn) == SEED
    for r in conn.execute("SELECT * FROM users"):
        assert r["password_hash"] is None and r["last_login"] is None
        assert (r["must_change"], r["pw_version"], r["active"]) == (1, 0, 1)
        assert db.from_iso(r["created_at"]) is not None


def test_init_twice_does_not_duplicate(conn):
    db.init_db(conn)
    db.init_db(conn)
    assert _users(conn) == SEED


def test_seed_never_overwrites_or_reactivates(conn):
    conn.execute("""UPDATE users SET name = 'Roberto', roles = 'chefe', password_hash = 'h', must_change = 0,
                           pw_version = 7, active = 0, last_login = '2026-10-11T10:00:00-03:00'
                    WHERE username = 'beto'""")
    conn.commit()
    db.init_db(conn)
    r = conn.execute("SELECT * FROM users WHERE username = 'beto'").fetchone()
    assert (r["name"], r["roles"], r["password_hash"], r["must_change"], r["pw_version"], r["active"]) == \
        ("Roberto", "chefe", "h", 0, 7, 0)
    assert r["last_login"] == "2026-10-11T10:00:00-03:00"
    assert len(_users(conn)) == len(SEED)


def test_seed_only_inserts_missing_and_keeps_others(conn):
    conn.execute("DELETE FROM users WHERE username = 'thiago'")
    cur = conn.execute("INSERT INTO users (username, name, roles, created_at) VALUES ('nova', 'Nova', 'vendedor', ?)",
                       (db.to_iso(db.now()),))
    conn.commit()
    assert cur.lastrowid == 7  # ID_TABLES: lastrowid também no Postgres
    db.seed_users(conn)
    names = [u[0] for u in _users(conn)]
    assert sorted(names) == sorted([u[0] for u in SEED] + ["nova"])


def test_seed_does_not_burn_ids(conn):
    """Rodar o seed de novo (toda partida a frio na Vercel) não pode gastar a sequência."""
    for _ in range(3):
        db.seed_users(conn)
    cur = conn.execute("INSERT INTO users (username, name, roles, created_at) VALUES ('x1', 'X', '', ?)",
                       (db.to_iso(db.now()),))
    conn.commit()
    assert cur.lastrowid == len(SEED) + 1


def test_username_is_unique(conn):
    try:
        conn.execute("INSERT INTO users (username, name, roles, created_at) VALUES ('arthur', 'Outro', '', 'x')")
        conn.commit()
    except Exception:
        conn.rollback()
    else:
        raise AssertionError("username repetido foi aceito")
    assert _users(conn) == SEED


def test_insert_on_conflict_returns_no_id(conn):
    cur = conn.execute("""INSERT INTO users (username, name, roles, created_at) VALUES ('arthur', 'Outro', '', 'x')
                          ON CONFLICT (username) DO NOTHING""")
    conn.commit()
    assert cur.rowcount == 0 and _users(conn) == SEED
    if db.is_postgres(conn):
        assert cur.lastrowid is None


def test_truncate_all_reseeds(conn):
    conn.execute("UPDATE users SET active = 0")
    conn.commit()
    db.truncate_all(conn)
    assert _users(conn) == SEED
    assert conn.execute("SELECT MIN(id), MAX(id), SUM(active) FROM users").fetchone()[:3] == (1, 6, 6)


def test_migration_from_database_without_users(conn, tmp_path):
    """Banco anterior à F15 (sem a tabela): init_db cria e semeia, sem tocar nos carros."""
    if db.is_postgres(conn):
        conn.execute("DROP TABLE users")
        conn.commit()
    else:
        old_schema, n = re.subn(r"CREATE TABLE IF NOT EXISTS users \(.*?\n\);\n", "", db.SCHEMA, flags=re.S)
        assert n == 1
        conn = db.connect(tmp_path / "old.db")
        conn.executescript(old_schema)
    conn.execute("""INSERT INTO cars (external_id, name, name_key, url, first_seen, last_seen)
                    VALUES ('9', 'Fiat Uno', 'fiat uno', 'u', '2026-10-01T10:00:00-03:00', '2026-10-01T10:00:00-03:00')""")
    conn.commit()
    db.init_db(conn)
    db.init_db(conn)
    assert _users(conn) == SEED
    assert conn.execute("SELECT COUNT(*) FROM cars").fetchone()[0] == 1
    if not db.is_postgres(conn):
        conn.close()


def test_schema_has_no_semicolon_inside_comments():
    for line in db.SCHEMA.splitlines():
        assert ";" not in line.partition("--")[2], line
