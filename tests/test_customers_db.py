"""F16 (Forja): tabela customers e migração — nos dois bancos. Só dados fictícios."""
from __future__ import annotations

import re

from app import db, pgcompat

COLUMNS = {"id", "name", "phone", "email", "birth_day", "birth_month", "birth_year", "car_text", "purchase_date",
           "notes", "created_by", "created_by_name", "created_at", "updated_at", "greeted_year", "greeted_by_name",
           "greeted_at"}
INSERT = """INSERT INTO customers (name, phone, birth_day, birth_month, created_by_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)"""


def _add(conn, name="Cliente Teste", phone="21900000001", day=12, month=10):
    stamp = db.to_iso(db.now())
    cur = conn.execute(INSERT, (name, phone, day, month, "Administrador", stamp, stamp))
    conn.commit()
    return cur.lastrowid


def test_registration():
    assert "customers" in db.TABLES and "customers" in pgcompat.ID_TABLES


def test_columns(conn):
    assert db._columns(conn, "customers") == COLUMNS


def test_insert_returns_id_and_optional_fields_are_null(conn):
    assert _add(conn) == 1
    assert _add(conn, "Outro Teste", "21900000002") == 2
    r = conn.execute("SELECT * FROM customers WHERE id = 1").fetchone()
    for col in ("email", "birth_year", "car_text", "purchase_date", "notes", "created_by", "greeted_year",
                "greeted_by_name", "greeted_at"):
        assert r[col] is None
    assert (r["birth_day"], r["birth_month"], r["created_by_name"]) == (12, 10, "Administrador")


def test_required_fields(conn):
    for sql in ("INSERT INTO customers (phone, birth_day, birth_month, created_by_name, created_at, updated_at) "
                "VALUES ('21900000003', 1, 1, 'x', 'x', 'x')",
                "INSERT INTO customers (name, birth_day, birth_month, created_by_name, created_at, updated_at) "
                "VALUES ('Teste', 1, 1, 'x', 'x', 'x')",
                "INSERT INTO customers (name, phone, created_by_name, created_at, updated_at) "
                "VALUES ('Teste', '21900000003', 'x', 'x', 'x')"):
        try:
            conn.execute(sql)
            conn.commit()
        except Exception:
            conn.rollback()
        else:
            raise AssertionError(sql)
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0


def test_birthday_index_exists(conn):
    if db.is_postgres(conn):
        names = {r[0] for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() AND tablename = 'customers'")}
    else:
        names = {r[1] for r in conn.execute("PRAGMA index_list(customers)")}
    assert "ix_customers_birthday" in names


def test_init_twice_keeps_data(conn):
    _add(conn)
    db.init_db(conn)
    db.init_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1
    assert db._columns(conn, "customers") == COLUMNS


def test_truncate_all_clears_customers(conn):
    _add(conn)
    db.truncate_all(conn)
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0
    assert _add(conn) == 1  # sequência reiniciada


def test_deleting_user_keeps_customer(conn):
    """created_by é só referência: sem FK, o cadastro sobrevive a qualquer mudança em users."""
    cid = _add(conn)
    conn.execute("UPDATE customers SET created_by = 3 WHERE id = ?", (cid,))
    conn.execute("DELETE FROM users WHERE id = 3")
    conn.commit()
    assert conn.execute("SELECT created_by FROM customers WHERE id = ?", (cid,)).fetchone()[0] == 3


def test_migration_from_f15_database(conn, tmp_path):
    """Banco da F15 (sem customers): init_db cria a tabela e o índice, 2x, sem tocar no resto."""
    if db.is_postgres(conn):
        conn.execute("DROP TABLE customers")
        conn.commit()
    else:
        old_schema, n = re.subn(r"CREATE TABLE IF NOT EXISTS customers \(.*?\n\);\nCREATE INDEX[^\n]*\n", "",
                                db.SCHEMA, flags=re.S)
        assert n == 1 and "customers" not in old_schema
        conn = db.connect(tmp_path / "f15.db")
        conn.executescript(old_schema)
        db.seed_users(conn)
    conn.execute("UPDATE users SET password_hash = 'h', pw_version = 4 WHERE username = 'beto'")
    conn.commit()
    db.init_db(conn)
    db.init_db(conn)
    assert db._columns(conn, "customers") == COLUMNS
    assert _add(conn) == 1
    r = conn.execute("SELECT password_hash, pw_version FROM users WHERE username = 'beto'").fetchone()
    assert tuple(r) == ("h", 4) and conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 6
    if not db.is_postgres(conn):
        conn.close()


def test_schema_has_no_semicolon_inside_comments():
    for line in db.SCHEMA.splitlines():
        assert ";" not in line.partition("--")[2], line
