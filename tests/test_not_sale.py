"""F14 — "Não foi venda" (anúncio retirado): cars.not_sale, finance.set_not_sale, rota
POST /equipe/carro/{id}/nao-venda, `removed` em /equipe/vendas e sold_30d do aging."""
from __future__ import annotations

import re
from datetime import date

import pytest

from app import auth, db
from app.services import aging, finance
from app.services.sync import run_poll

ADMIN_PW = "senha-admin-123"
STAFF_PW = "senha-equipe-456"
TODAY = date(2026, 10, 15)


def _id(conn, ext):
    return conn.execute("SELECT id FROM cars WHERE external_id = ?", (ext,)).fetchone()[0]


def _sell(conn, ext, sold_at, **cols):
    sets = "".join(f", {k} = ?" for k in cols)
    conn.execute(f"UPDATE cars SET active = 0, sold_at = ?{sets} WHERE external_id = ?",
                 (sold_at, *cols.values(), ext))
    conn.commit()
    return _id(conn, ext)


def _flag(conn, car_id):
    return conn.execute("SELECT not_sale FROM cars WHERE id = ?", (car_id,)).fetchone()[0]


@pytest.fixture
def stock(conn, car):
    """4 carros no ar; 1 e 2 (Chevrolet) e 3 (BMW) saem do site em outubro, 4 (Land Rover) fica."""
    run_poll(conn, [
        car("1", "CHEVROLET TRACKER 1.0 TURBO LTZ 2026", price=12_000_000),
        car("2", "CHEVROLET ONIX 1.0 LT 2023", price=7_000_000),
        car("3", "BMW 320i 2.0 M SPORT 2022", price=25_000_000),
        car("4", "LAND ROVER RANGE ROVER SPORT 3.0 2023", price=80_000_000),
    ])
    conn.execute("UPDATE cars SET first_seen = '2026-09-01T10:00:00-03:00'")
    conn.execute("UPDATE cars SET cost_cents = 70000000 WHERE external_id = '4'")
    ids = {
        "a": _sell(conn, "1", "2026-10-11T18:00:00-03:00", cost_cents=10_000_000, sale_price_cents=11_500_000),
        "b": _sell(conn, "2", "2026-10-06T18:00:00-03:00", cost_cents=6_000_000),
        "c": _sell(conn, "3", "2026-10-03T18:00:00-03:00", cost_cents=20_000_000),
        "d": _id(conn, "4"),
    }
    conn.execute("INSERT INTO car_expenses (car_id, description, amount_cents, created_at) VALUES (?, ?, ?, ?)",
                 (ids["c"], "Polimento", 50_000, "2026-10-01T10:00:00-03:00"))
    conn.commit()
    return conn, ids


# ---- schema / migração ---------------------------------------------------------------

def test_column_exists_defaults_to_zero_and_migration_is_idempotent(stock):
    conn, ids = stock
    assert "not_sale" in db._columns(conn, "cars")
    assert ("cars", "not_sale", "INTEGER NOT NULL DEFAULT 0") in db.MIGRATIONS
    assert {_flag(conn, i) for i in ids.values()} == {0}
    finance.set_not_sale(conn, ids["a"], "1")
    for _ in range(2):
        db.migrate(conn)
        assert _flag(conn, ids["a"]) == 1 and _flag(conn, ids["b"]) == 0


def test_migration_adds_column_to_old_database(conn, tmp_path):
    """Banco anterior à F14 (sem a coluna): migrate cria com 0 e preserva os dados."""
    if db.is_postgres(conn):
        conn.execute("ALTER TABLE cars DROP COLUMN not_sale")
    else:  # SQLite não derruba coluna com comentário no CREATE: monta um banco com o schema antigo
        old_schema, n = re.subn(r",(\s*--[^\n]*)?\n\s*not_sale[^\n]*\n", "\n", db.SCHEMA)
        assert n == 1
        conn = db.connect(tmp_path / "old.db")
        conn.executescript(old_schema)
    conn.execute("""INSERT INTO cars (external_id, name, name_key, url, posted, active, first_seen, last_seen,
                                      sold_at, cost_cents)
                    VALUES ('77', 'FIAT UNO 2010', 'fiat uno 2010', 'u', 1, 0, ?, ?, ?, 123)""",
                 ("2026-09-01T10:00:00-03:00",) * 3)
    conn.commit()
    assert "not_sale" not in db._columns(conn, "cars")
    for _ in range(2):
        db.migrate(conn)
        assert "not_sale" in db._columns(conn, "cars")
    row = conn.execute("SELECT not_sale, cost_cents, brand FROM cars WHERE external_id = '77'").fetchone()
    assert tuple(row) == (0, 123, "Fiat")


# ---- regras ---------------------------------------------------------------------------

def test_mark_removes_from_everything_that_is_sale_and_undo_restores(stock):
    conn, ids = stock
    before = finance.sales_view(conn, "2026-10", TODAY)
    s = before["summary"]
    assert s["sold_count"] == 3 and before["removed"] == []
    assert s["revenue_cents"] == 11_500_000 + 7_000_000 + 25_000_000
    assert s["cost_cents"] == 36_000_000 and s["expenses_cents"] == 50_000
    assert [(g["brand"], g["count"]) for g in before["brands"]] == [("Chevrolet", 2), ("BMW", 1)]
    assert s["avg_days_to_sell"] == 36          # (40 + 35 + 32) / 3 = 35,67
    assert aging.team_view(conn, TODAY)["summary"]["sold_30d"] == 3

    assert finance.set_not_sale(conn, ids["c"], "1") is True     # o BMW não foi venda
    assert _flag(conn, ids["c"]) == 1
    v = finance.sales_view(conn, "2026-10", TODAY)
    s = v["summary"]
    assert s["sold_count"] == 2 and [x["car"]["id"] for x in v["sales"]] == [ids["a"], ids["b"]]
    assert s["revenue_cents"] == 18_500_000 and s["cost_cents"] == 16_000_000 and s["expenses_cents"] == 0
    assert s["profit_cents"] == 1_500_000 + 1_000_000
    assert s["margin_pct"] == round(2_500_000 * 100 / 18_500_000, 1)
    assert (s["missing_cost"], s["missing_sale"]) == (0, 0)
    assert s["avg_days_to_sell"] == 38          # (40 + 35) / 2 = 37,5 → meio para cima
    assert [(g["brand"], g["count"], g["share_pct"]) for g in v["brands"]] == [("Chevrolet", 2, 100.0)]
    assert v["history"][-1] == {"key": "2026-10", "label": "out", "sold_count": 2, "revenue_cents": 18_500_000,
                                "spend_cents": 16_000_000, "profit_cents": 2_500_000}
    # continua fora do estoque
    assert (s["in_stock"], s["invested_cents"]) == (1, 70_000_000)
    assert (s["in_stock"], s["invested_cents"]) == (before["summary"]["in_stock"], before["summary"]["invested_cents"])
    assert aging.team_view(conn, TODAY)["summary"]["sold_30d"] == 2
    assert ids["c"] not in {c["id"] for c in aging.team_view(conn, TODAY)["cars"]}
    # aparece em removed
    assert len(v["removed"]) == 1
    removed = v["removed"][0]
    assert set(removed) == {"car", "sold_at"}
    assert removed["car"] == {"id": ids["c"], "name": "BMW 320i 2.0 M SPORT 2022",
                              "photo_url": removed["car"]["photo_url"], "brand": "BMW"}
    assert removed["sold_at"].tzinfo is not None and (removed["sold_at"].month, removed["sold_at"].day) == (10, 3)
    # custo e despesas preservados
    cv = finance.car_view(conn, ids["c"])
    assert cv["car"]["not_sale"] is True and cv["car"]["sold"] is True and cv["car"]["cost_cents"] == 20_000_000
    assert cv["totals"]["expenses_cents"] == 50_000 and len(cv["expenses"]) == 1
    assert finance.car_view(conn, ids["a"])["car"]["not_sale"] is False

    assert finance.set_not_sale(conn, ids["c"], "1") is True     # marcar de novo: continua marcado
    assert finance.set_not_sale(conn, ids["c"], "0") is True     # desfaz
    after = finance.sales_view(conn, "2026-10", TODAY)
    assert after == before
    assert aging.team_view(conn, TODAY)["summary"]["sold_30d"] == 3
    assert finance.car_view(conn, ids["c"])["car"]["not_sale"] is False


def test_all_removed_month_is_empty_and_removed_is_per_month_newest_first(stock):
    conn, ids = stock
    _sell(conn, "4", "2026-09-20T12:00:00-03:00")
    for key in "abc":
        assert finance.set_not_sale(conn, ids[key], "1")
    assert finance.set_not_sale(conn, ids["d"], "1")
    v = finance.sales_view(conn, "2026-10", TODAY)
    s = v["summary"]
    assert v["sales"] == [] and v["brands"] == []
    assert (s["sold_count"], s["revenue_cents"], s["cost_cents"], s["profit_cents"]) == (0, 0, 0, 0)
    assert s["margin_pct"] is None and s["avg_days_to_sell"] is None
    assert [r["car"]["id"] for r in v["removed"]] == [ids["a"], ids["b"], ids["c"]]
    assert all(h["sold_count"] == 0 and h["revenue_cents"] == 0 for h in v["history"])
    september = finance.sales_view(conn, "2026-09", TODAY)
    assert [r["car"]["id"] for r in september["removed"]] == [ids["d"]] and september["sales"] == []
    assert finance.sales_view(conn, "2026-08", TODAY)["removed"] == []
    assert aging.team_view(conn, TODAY)["summary"]["sold_30d"] == 0


def test_active_car_and_bad_values_are_invalid(stock):
    conn, ids = stock
    assert finance.set_not_sale(conn, ids["d"], "1") is False      # carro ativo
    assert finance.set_not_sale(conn, ids["d"], "0") is False
    assert finance.set_not_sale(conn, 999_999, "1") is False       # não existe
    for bad in (None, "", "2", "-1", "true", "on", " 1", "1 ", "01", "1.0"):
        assert finance.set_not_sale(conn, ids["a"], bad) is False, bad
    assert {_flag(conn, i) for i in ids.values()} == {0}
    assert finance.set_not_sale(conn, ids["a"], "0") is True and _flag(conn, ids["a"]) == 0  # desfazer sem marca


def test_reappearance_resets_not_sale_and_next_sale_counts(conn, car):
    stock = [car("1", "CHEVROLET ONIX 2023", price=7_000_000), car("2", "BMW X1 2022", price=20_000_000)]
    run_poll(conn, stock)
    run_poll(conn, stock[:1])
    run_poll(conn, stock[:1])                    # BMW sumiu 2x → "vendido"
    bmw = _id(conn, "2")
    finance.set_cost(conn, bmw, "150.000")
    finance.add_expense(conn, bmw, "Pneus", "2.000")
    assert finance.set_not_sale(conn, bmw, "1") is True
    today = db.now().date()
    v = finance.sales_view(conn, None, today)
    assert v["sales"] == [] and [r["car"]["id"] for r in v["removed"]] == [bmw]

    run_poll(conn, stock)                        # anúncio voltou
    assert _flag(conn, bmw) == 0
    cv = finance.car_view(conn, bmw)
    assert cv["car"]["active"] is True and cv["car"]["not_sale"] is False
    assert cv["car"]["cost_cents"] == 15_000_000 and cv["totals"]["expenses_cents"] == 200_000
    v = finance.sales_view(conn, None, today)
    assert v["sales"] == [] and v["removed"] == []
    assert v["summary"]["in_stock"] == 2 and v["summary"]["invested_cents"] == 15_200_000
    assert finance.set_not_sale(conn, bmw, "1") is False      # ativo de novo

    run_poll(conn, stock[:1])
    run_poll(conn, stock[:1])                    # agora saiu de verdade: conta como venda
    v = finance.sales_view(conn, None, today)
    assert [s["car"]["id"] for s in v["sales"]] == [bmw] and v["removed"] == []
    assert aging.team_view(conn, today)["summary"]["sold_30d"] == 1


def test_only_reappeared_car_is_reset(conn, car):
    stock = [car("1", "CHEVROLET ONIX 2023"), car("2", "BMW X1 2022"), car("3", "FIAT UNO 2010")]
    run_poll(conn, stock)
    run_poll(conn, stock[:1])
    run_poll(conn, stock[:1])
    bmw, fiat = _id(conn, "2"), _id(conn, "3")
    assert finance.set_not_sale(conn, bmw, "1") and finance.set_not_sale(conn, fiat, "1")
    run_poll(conn, stock[:2])                    # só o BMW volta
    assert (_flag(conn, bmw), _flag(conn, fiat)) == (0, 1)


# ---- rotas ------------------------------------------------------------------------------

@pytest.fixture
def seeded(client, car):
    conn = db.connect()
    run_poll(conn, [car("1", "CHEVROLET TRACKER 1.0 TURBO LTZ 2026", price=12_000_000),
                    car("2", "BMW 320i 2.0 M SPORT 2022", price=25_000_000)])
    _sell(conn, "2", db.to_iso(db.now()), cost_cents=20_000_000)
    ids = {"active": _id(conn, "1"), "sold": _id(conn, "2")}
    conn.close()
    return ids


def _state(cid):
    conn = db.connect()
    try:
        return finance.car_view(conn, cid)["car"]["not_sale"], finance.sales_view(conn)
    finally:
        conn.close()


def _post(client, cid, **data):
    return client.post(f"/equipe/carro/{cid}/nao-venda", data=data, follow_redirects=False)


def test_route_marks_and_undoes(client, seeded):
    sold, active = seeded["sold"], seeded["active"]
    ok, bad = f"/equipe/carro/{sold}?msg=saved", f"/equipe/carro/{sold}?msg=invalid"

    r = _post(client, sold, value="1")
    assert r.status_code == 303 and r.headers["location"] == ok
    flag, view = _state(sold)
    assert flag is True and view["sales"] == [] and [x["car"]["id"] for x in view["removed"]] == [sold]
    assert view["summary"]["sold_count"] == 0 and view["summary"]["revenue_cents"] == 0
    assert client.get("/equipe/vendas").status_code == 200
    assert client.get(f"/equipe/carro/{sold}?msg=saved").status_code == 200

    for data in ({}, {"value": ""}, {"value": "2"}, {"value": "sim"}):
        r = _post(client, sold, **data)
        assert r.status_code == 303 and r.headers["location"] == bad
    assert _state(sold)[0] is True               # inválido não muda nada

    r = _post(client, sold, value="0")
    assert r.status_code == 303 and r.headers["location"] == ok
    flag, view = _state(sold)
    assert flag is False and [x["car"]["id"] for x in view["sales"]] == [sold] and view["removed"] == []

    r = _post(client, active, value="1")         # carro ativo
    assert r.status_code == 303 and r.headers["location"] == f"/equipe/carro/{active}?msg=invalid"
    assert _state(active)[0] is False
    assert _post(client, 9999, value="1").status_code == 404
    assert _post(client, "9" * 20, value="1").status_code == 404
    assert client.post(f"/equipe/carro/abc/nao-venda", data={"value": "1"}).status_code == 422
    assert client.get(f"/equipe/carro/{sold}/nao-venda").status_code == 405


@pytest.mark.parametrize("pw", [ADMIN_PW, STAFF_PW])
def test_route_open_to_admin_and_staff_only_when_logged(client, seeded, monkeypatch, pw):
    monkeypatch.setenv("APP_PASSWORD", ADMIN_PW)
    monkeypatch.setenv("STAFF_PASSWORD", STAFF_PW)
    monkeypatch.setenv("SESSION_SECRET", "segredo-" + "z" * 30)
    auth.reset_failures()
    sold = seeded["sold"]
    assert _post(client, sold, value="1").status_code == 401     # sem login: nada grava
    assert _state(sold)[0] is False
    assert client.post("/login", data={"password": pw}, follow_redirects=False).status_code == 303
    r = _post(client, sold, value="1")
    assert r.status_code == 303 and r.headers["location"].endswith("msg=saved")
    assert _state(sold)[0] is True
    assert client.get("/equipe/vendas").status_code == 200
    assert client.get(f"/equipe/carro/{sold}").status_code == 200
    r = _post(client, sold, value="0")
    assert r.status_code == 303 and _state(sold)[0] is False
    auth.reset_failures()
