"""Schema no Postgres REAL (só com TEST_DATABASE_URL): init_db num banco vazio e migração de um
banco no schema do commit 1634fa5 (antes da F13) — o caminho que derrubou a produção em 92f1407
(`;` dentro de comentário do SCHEMA + executescript dividindo em `;`)."""
from __future__ import annotations

from datetime import date

import pytest

from app import db
from app.services import finance
from conftest import FIXTURES, requires_pg

pytestmark = requires_pg
OLD_SCHEMA = (FIXTURES / "schema_1634fa5.sql").read_text(encoding="utf-8").replace(
    "INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
TS = "2026-09-01T10:00:00-03:00"
OLD_CARS = [
    ("5509733", "LAND ROVER RANGE ROVER SPORT 3.0 HSE 2023", 77_990_000, 1, None),
    ("5555997", "BMW 320i 2.0 M SPORT 2022", None, 1, None),
    ("5123456", "CHEVROLET ONIX 1.0 LT 2023", 7_000_000, 0, "2026-09-20T18:00:00-03:00"),
    ("5000001", "MERCEDES-BENZ C 300 2.0 2021", 30_000_000, 1, None),
    ("demo-1", "FIAT DEMO 2020", 1_000_000, 1, None),
]
NEW_COLS = ("brand", "cost_cents", "sale_price_cents")


# Os helpers de leitura fazem commit: transação aberta com SELECT em `cars` seguraria o lock e o
# ALTER TABLE do init_db (outra conexão) ficaria esperando para sempre.

def _tables(conn):
    out = {r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    conn.commit()
    return out


def _columns(conn, table):
    out = {r[0]: r[1] for r in conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = ? ORDER BY ordinal_position", (table,))}
    conn.commit()
    return out


@pytest.fixture
def empty_pg():
    """Banco sem nenhuma tabela (como um Neon recém-criado)."""
    conn = db.connect()
    for table in _tables(conn):
        conn.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    conn.commit()
    assert _tables(conn) == set()
    yield conn
    conn.close()
    db.init_db()   # devolve o schema atual para os próximos testes


@pytest.fixture
def old_pg(empty_pg):
    """Banco no schema 1634fa5 com dados, como a produção antes da F13."""
    conn = empty_pg
    conn.executescript(OLD_SCHEMA)
    for ext, name, price, active, sold_at in OLD_CARS:
        conn.execute("""INSERT INTO cars (external_id, name, name_key, photo_url, price_cents, url, posted, active,
                                          first_seen, last_seen, missing_count, sold_at, in_baseline)
                        VALUES (?, ?, ?, ?, ?, 'u', 1, ?, ?, ?, ?, ?, 1)""",
                     (ext, name, name.lower(), f"https://img.test/{ext}.jpg", price, active, TS, TS,
                      0 if active else 2, sold_at))
    conn.execute("INSERT INTO price_history (car_id, price_cents, recorded_at) VALUES (1, 77990000, ?)", (TS,))
    conn.execute("INSERT INTO price_history (car_id, price_cents, recorded_at) VALUES (3, 7500000, ?)", (TS,))
    conn.execute("INSERT INTO tickets (car_id, status, created_at) VALUES (4, 'pending', ?)", (TS,))
    conn.execute("INSERT INTO sold_alerts (car_id, created_at, dismissed) VALUES (3, ?, 0)", (TS,))
    conn.execute("INSERT INTO runs (kind, started_at, finished_at, ok) VALUES ('daily', ?, ?, 1)", (TS, TS))
    conn.commit()
    assert not set(NEW_COLS) & set(_columns(conn, "cars")) and "car_expenses" not in _tables(conn)
    return conn


def _dump(conn, skip=NEW_COLS):
    out = {}
    for table in ("cars", "price_history", "tickets", "sold_alerts", "runs", "detail_cache"):
        cols = [c for c in _columns(conn, table) if c not in skip]
        out[table] = [tuple(r) for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY 1")]
    conn.commit()
    return out


def test_init_db_from_empty_database_twice(empty_pg):
    conn = empty_pg
    db.init_db()
    conn.commit()
    assert _tables(conn) == set(db.TABLES)
    cars = _columns(conn, "cars")
    assert (cars["brand"], cars["cost_cents"], cars["sale_price_cents"]) == ("text", "integer", "integer")
    assert list(_columns(conn, "car_expenses")) == ["id", "car_id", "description", "amount_cents", "created_at"]
    indexes = {r[0] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
    assert {"ix_cars_name_key", "ix_car_expenses_car"} <= indexes
    db.init_db()   # cold start seguinte: nada muda, nada quebra
    conn.commit()
    assert _columns(conn, "cars") == cars and _tables(conn) == set(db.TABLES)
    # serial + RETURNING id nas tabelas novas
    conn.execute("""INSERT INTO cars (external_id, name, name_key, url, posted, active, first_seen, last_seen)
                    VALUES ('1', 'BMW X1', 'bmw x1', 'u', 1, 1, ?, ?)""", (TS, TS))
    cur = conn.execute("INSERT INTO car_expenses (car_id, description, amount_cents, created_at) "
                       "VALUES (1, 'x', 100, ?)", (TS,))
    assert cur.lastrowid == 1
    conn.commit()


def test_every_schema_statement_runs_alone_on_postgres(empty_pg):
    """Cada comando que o executescript manda para o Postgres tem de ser SQL completo."""
    from app.pgcompat import split_statements

    statements = split_statements(db.PG_SCHEMA)
    assert len(statements) >= len(db.TABLES)
    for statement in statements:
        assert statement.upper().startswith("CREATE "), statement[:60]
        empty_pg.execute(statement)
    empty_pg.commit()
    assert _tables(empty_pg) == set(db.TABLES)


def test_migration_from_1634fa5_on_postgres(old_pg):
    conn = old_pg
    before = _dump(conn)
    db.init_db()
    conn.commit()
    cols = _columns(conn, "cars")
    assert (cols["brand"], cols["cost_cents"], cols["sale_price_cents"]) == ("text", "integer", "integer")
    assert "car_expenses" in _tables(conn)
    assert _dump(conn) == before                                        # nenhum dado perdido/alterado
    assert dict(tuple(r) for r in conn.execute("SELECT external_id, brand FROM cars")) == {
        "5509733": "Land Rover", "5555997": "BMW", "5123456": "Chevrolet",
        "5000001": "Mercedes-Benz", "demo-1": "Fiat"}
    assert conn.execute("SELECT COUNT(*) FROM cars WHERE cost_cents IS NOT NULL "
                        "OR sale_price_cents IS NOT NULL").fetchone()[0] == 0

    onix = conn.execute("SELECT id FROM cars WHERE external_id = '5123456'").fetchone()[0]
    assert finance.set_cost(conn, onix, "60.000") and finance.add_expense(conn, onix, "Pneus", "1.800")
    conn.execute("UPDATE cars SET brand = 'Marca Corrigida' WHERE external_id = '5000001'")
    conn.commit()
    after_first = _dump(conn, skip=())

    db.init_db()
    db.init_db()
    conn.commit()
    assert _dump(conn, skip=()) == after_first
    assert _columns(conn, "cars") == cols
    v = finance.sales_view(conn, "2026-09", date(2026, 10, 7))
    # 70.000 (último preço do site) − 60.000 − 1.800 = 8.200
    assert [(s["car"]["name"], s["sale_cents"], s["profit_cents"], s["car"]["brand"]) for s in v["sales"]] == [
        ("CHEVROLET ONIX 1.0 LT 2023", 7_000_000, 820_000, "Chevrolet")]
    assert v["summary"]["in_stock"] == 3
    # serial continua de onde o banco antigo parou
    cur = conn.execute("""INSERT INTO cars (external_id, name, name_key, url, posted, active, first_seen, last_seen)
                          VALUES ('novo', 'AUDI A3', 'audi a3', 'u', 1, 1, ?, ?)""", (TS, TS))
    assert cur.lastrowid == len(OLD_CARS) + 1
    conn.execute("DELETE FROM cars WHERE id = ?", (onix,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM car_expenses").fetchone()[0] == 0   # ON DELETE CASCADE


def test_app_boots_and_serves_finance_on_migrated_postgres(old_pg, site):
    """Cold start da Vercel num banco antigo: o lifespan migra e as páginas da F13 respondem."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        for path in ("/health", "/", "/equipe", "/equipe/vendas", "/equipe/vendas?mes=2026-09",
                     "/equipe/carro/3", "/equipe/carro/2"):
            r = client.get(path)
            assert r.status_code == 200, (path, r.status_code)
            assert "None" not in r.text and "Undefined" not in r.text, path
        assert "CHEVROLET ONIX" in client.get("/equipe/vendas?mes=2026-09").text

        def post(path, **data):
            r = client.post(f"/equipe/carro/3/{path}", data=data, follow_redirects=False)
            assert r.status_code == 303, (path, r.status_code)
            return r.headers["location"].rsplit("=", 1)[1]

        assert post("custo", cost="60.000") == "saved"
        assert post("venda", sale_price="68.500,50") == "saved"
        assert post("despesas", description="Pneus", amount="1.800") == "saved"
        assert post("custo", cost="abc") == "invalid"
        assert post("despesas/999/excluir") == "invalid"
        html = client.get("/equipe/carro/3").text
        # 68.500,50 − 60.000 − 1.800 = 6.700,50
        assert "R$ 6.700,50" in html and "Pneus" in html and 'value="68.500,50"' in html
        assert "R$ 6.700,50" in client.get("/equipe/vendas?mes=2026-09").text
        assert post("despesas/1/excluir") == "saved"
        assert client.post("/equipe/carro/999/custo", data={"cost": "1"}, follow_redirects=False).status_code == 404
    old_pg.commit()
    row = old_pg.execute("SELECT cost_cents, sale_price_cents FROM cars WHERE id = 3").fetchone()
    assert tuple(row) == (6_000_000, 6_850_050)
    assert old_pg.execute("SELECT COUNT(*) FROM car_expenses").fetchone()[0] == 0
