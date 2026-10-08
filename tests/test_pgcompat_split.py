"""Divisão de scripts SQL do pgcompat (roda SEM Postgres).

Incidente F13: um `;` dentro de um comentário `--` do SCHEMA quebrava o
executescript no Postgres (script.split(';')) e derrubava a inicialização em
produção; o SQLite não sofria. Aqui cada script usado em executescript passa pela
mesma função de divisão e cada comando resultante tem de ser SQL completo.
"""
from __future__ import annotations

import re
import sqlite3

import pytest

from app import db
from app.pgcompat import split_statements

STARTS = ("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX", "ALTER TABLE", "INSERT INTO", "UPDATE ",
          "DELETE FROM", "DROP ")
SCRIPTS = {"SCHEMA": db.SCHEMA, "PG_SCHEMA": db.PG_SCHEMA}


def _outside_strings(sql: str) -> str:
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def _assert_complete(statement: str) -> None:
    bare = _outside_strings(statement)
    assert statement.lstrip().upper().startswith(STARTS), f"fragmento solto: {statement[:80]!r}"
    assert bare.count("(") == bare.count(")"), f"parênteses desbalanceados: {statement[:80]!r}"
    assert "--" not in bare and "/*" not in bare and ";" not in bare, statement[:80]
    assert statement == statement.strip() and statement


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_schema_splits_into_complete_statements(name):
    statements = split_statements(SCRIPTS[name])
    for statement in statements:
        _assert_complete(statement)
    tables = [s for s in statements if s.upper().startswith("CREATE TABLE")]
    names = {re.match(r"CREATE TABLE IF NOT EXISTS (\w+)", s).group(1) for s in tables}
    assert names == set(db.TABLES)          # nenhuma tabela perdida nem partida ao meio
    creates = len(re.findall(r"^CREATE ", SCRIPTS[name], flags=re.M))
    assert len(statements) == creates       # 1 comando por CREATE do script


def test_split_schema_runs_statement_by_statement_and_matches_executescript():
    """Os comandos divididos, executados um a um, criam o mesmo banco que o script inteiro."""
    whole, split = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    whole.executescript(db.SCHEMA)
    for statement in split_statements(db.SCHEMA):
        split.execute(statement)

    def shape(conn):
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {t: [tuple(c)[1:] for c in conn.execute(f"PRAGMA table_info({t})")] for t in tables}, \
            sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'"))

    assert shape(split) == shape(whole)
    assert {"brand", "cost_cents", "sale_price_cents"} <= {c[0] for c in shape(split)[0]["cars"]}
    assert "car_expenses" in shape(split)[0]


def test_pg_schema_uses_serial_and_no_sqlite_only_syntax():
    joined = "\n".join(split_statements(db.PG_SCHEMA))
    assert "AUTOINCREMENT" not in joined and "SERIAL PRIMARY KEY" in joined
    assert "PRAGMA" not in joined.upper()


def test_regression_semicolon_inside_comment():
    script = """
    CREATE TABLE a (
        x INTEGER,          -- valor real; NULL → usa outro (o ; do incidente)
        y TEXT              -- outro; mais; um
    );
    -- comentário solto; com ponto e vírgula
    CREATE INDEX ix_a ON a(x);  -- fim; de linha
    """
    statements = split_statements(script)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE a (") and statements[0].endswith(")")
    assert statements[1] == "CREATE INDEX ix_a ON a(x)"
    for statement in statements:
        _assert_complete(statement)
    conn = sqlite3.connect(":memory:")
    for statement in statements:
        conn.execute(statement)
    assert [r[1] for r in conn.execute("PRAGMA table_info(a)")] == ["x", "y"]


def test_semicolons_and_dashes_inside_strings_are_kept():
    script = "INSERT INTO t (a) VALUES ('x; -- não é comentário'); INSERT INTO t (a) VALUES ('it''s; ok');"
    assert split_statements(script) == ["INSERT INTO t (a) VALUES ('x; -- não é comentário')",
                                        "INSERT INTO t (a) VALUES ('it''s; ok')"]
    assert split_statements('CREATE TABLE "a;b" (x INTEGER)') == ['CREATE TABLE "a;b" (x INTEGER)']


def test_block_comments_empty_statements_and_missing_final_semicolon():
    assert split_statements("/* a; b */ CREATE TABLE t (x INTEGER) ;;\n ; -- só comentário;\n") == \
        ["CREATE TABLE t (x INTEGER)"]
    assert split_statements("CREATE TABLE t (x INTEGER)") == ["CREATE TABLE t (x INTEGER)"]
    assert split_statements("") == [] and split_statements("-- nada; aqui\n\n") == []
    assert split_statements("CREATE TABLE t (x INTEGER) -- sem quebra de linha no fim; ok") == \
        ["CREATE TABLE t (x INTEGER)"]


def test_schema_has_no_semicolon_inside_comments():
    """Cinto e suspensório: mesmo com a divisão robusta, não deixe `;` em comentário do SCHEMA."""
    for line in db.SCHEMA.splitlines():
        if "--" in line:
            assert ";" not in line.split("--", 1)[1], line


def test_pg_executescript_uses_split_statements():
    """PgConnection.executescript executa exatamente os comandos de split_statements."""
    from app.pgcompat import PgConnection

    class FakeRaw:
        def __init__(self):
            self.seen = []

        def execute(self, sql):
            self.seen.append(sql)

    conn = PgConnection.__new__(PgConnection)
    conn._conn = FakeRaw()
    conn.executescript(db.PG_SCHEMA)
    assert conn._conn.seen == split_statements(db.PG_SCHEMA)
    assert all(s.upper().startswith(("CREATE TABLE", "CREATE INDEX")) for s in conn._conn.seen)


def test_car_expenses_gets_lastrowid_on_postgres():
    from app.pgcompat import ID_TABLES, translate

    assert "car_expenses" in ID_TABLES
    sql, returning = translate("INSERT INTO car_expenses (car_id, description, amount_cents, created_at) "
                               "VALUES (?, ?, ?, ?)")
    assert returning and sql.endswith("VALUES (%s, %s, %s, %s) RETURNING id")
    assert translate("SELECT 1 FROM cars WHERE external_id NOT LIKE 'demo-%'")[0].endswith("'demo-%%'")
