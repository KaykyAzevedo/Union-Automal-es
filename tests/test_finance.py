"""F13 — vendas e financeiro da equipe (app/services/finance.py + rotas /equipe/vendas e /equipe/carro/*)."""
from __future__ import annotations

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


def _expense(conn, car_id, cents, when="2026-10-01T10:00:00-03:00", desc="Polimento"):
    conn.execute("INSERT INTO car_expenses (car_id, description, amount_cents, created_at) VALUES (?, ?, ?, ?)",
                 (car_id, desc, cents, when))
    conn.commit()


@pytest.fixture
def stock(conn, car):
    """5 carros no ar: 2 Chevrolet, 1 BMW, 1 Land Rover, 1 Fiat sob consulta."""
    run_poll(conn, [
        car("1", "CHEVROLET TRACKER 1.0 TURBO LTZ 2026", price=12_000_000),
        car("2", "CHEVROLET ONIX 1.0 LT 2023", price=7_000_000),
        car("3", "BMW 320i 2.0 M SPORT 2022", price=25_000_000),
        car("4", "LAND ROVER RANGE ROVER SPORT 3.0 2023", price=80_000_000),
        car("5", "FIAT UNO MILLE 2010", price=None),
    ])
    return conn


# ---- parse ------------------------------------------------------------------------

@pytest.mark.parametrize("raw, cents", [
    (None, None), ("", None), ("   ", None), ("R$", None), ("45000", 4_500_000), ("45.000", 4_500_000),
    ("45.000,50", 4_500_050), ("45000,5", 4_500_050), ("R$ 1.250.000,00", 125_000_000), (" 0 ", 0),
    ("0,99", 99), ("20000000", 2_000_000_000),
])
def test_parse_money(raw, cents):
    assert finance.parse_money(raw) == cents


@pytest.mark.parametrize("raw", ["abc", "-5", "-45.000", "45,000", "1.23", "12.34.567", "45000.50", "1,234",
                                 "20000000,01", "100000000", "1e5", "45 000"])
def test_parse_money_invalid(raw):
    with pytest.raises(ValueError):
        finance.parse_money(raw)


def test_parse_month_and_info():
    assert finance.parse_month("2026-03", TODAY) == (2026, 3)
    for bad in (None, "", "2026-13", "2026-00", "abc", "26-10", "2026-1"):
        assert finance.parse_month(bad, TODAY) == (2026, 10)
    assert finance.month_info(2026, 10, TODAY) == {"key": "2026-10", "label": "Outubro/2026",
                                                    "prev": "2026-09", "next": None}
    assert finance.month_info(2026, 1, TODAY)["prev"] == "2025-12"
    assert finance.month_info(2025, 12, TODAY)["next"] == "2026-01"
    assert finance.month_info(2026, 9, TODAY)["next"] == "2026-10"


# ---- DB / marca ---------------------------------------------------------------------

def test_brand_filled_on_insert_refresh_and_backfill(stock, car):
    conn = stock
    brands = {r["external_id"]: r["brand"] for r in conn.execute("SELECT external_id, brand FROM cars")}
    assert brands == {"1": "Chevrolet", "2": "Chevrolet", "3": "BMW", "4": "Land Rover", "5": "Fiat"}
    conn.execute("UPDATE cars SET brand = NULL")
    conn.commit()
    for _ in range(2):  # migração idempotente
        db.migrate(conn)
        assert conn.execute("SELECT brand FROM cars WHERE external_id = '4'").fetchone()[0] == "Land Rover"
    conn.execute("UPDATE cars SET brand = 'Errada' WHERE external_id = '3'")
    run_poll(conn, [car("3", "BMW 320i 2.0 M SPORT 2022")])
    assert conn.execute("SELECT brand FROM cars WHERE external_id = '3'").fetchone()[0] == "BMW"


def test_schema_has_finance_columns_and_cascade(stock):
    conn = stock
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cars)")}
    assert {"brand", "cost_cents", "sale_price_cents"} <= cols
    assert "car_expenses" in db.TABLES
    _expense(conn, _id(conn, "1"), 1000)
    conn.execute("DELETE FROM cars WHERE external_id = '1'")
    assert conn.execute("SELECT COUNT(*) FROM car_expenses").fetchone()[0] == 0


# ---- cálculos -----------------------------------------------------------------------

def test_sales_view_calculations(stock):
    conn = stock
    a = _sell(conn, "1", "2026-10-10T18:00:00-03:00", cost_cents=10_000_000, sale_price_cents=11_500_000)
    b = _sell(conn, "2", "2026-10-12T18:00:00-03:00", cost_cents=6_000_000)          # venda = preço do site
    c = _sell(conn, "3", "2026-10-05T18:00:00-03:00")                                 # sem custo
    _expense(conn, a, 50_000)
    _expense(conn, a, 25_000)
    _expense(conn, c, 99_000)  # carro sem custo: despesa fica fora do mês
    conn.execute("UPDATE cars SET cost_cents = 70000000 WHERE external_id = '4'")
    _expense(conn, _id(conn, "4"), 300_000)
    conn.commit()

    v = finance.sales_view(conn, None, TODAY)
    assert v["month"]["key"] == "2026-10"
    s = v["summary"]
    assert s["sold_count"] == 3 and s["missing_cost"] == 1
    assert s["revenue_cents"] == 11_500_000 + 7_000_000 + 25_000_000
    assert s["cost_cents"] == 16_000_000 and s["expenses_cents"] == 75_000
    assert s["profit_cents"] == (11_500_000 - 10_000_000 - 75_000) + (7_000_000 - 6_000_000)
    assert s["margin_pct"] == round(s["profit_cents"] * 100 / 18_500_000, 1)
    assert s["in_stock"] == 2 and s["invested_cents"] == 70_000_000 + 300_000
    assert isinstance(s["avg_days_to_sell"], int)
    assert set(s) == {"sold_count", "revenue_cents", "cost_cents", "expenses_cents", "profit_cents", "margin_pct",
                      "missing_cost", "missing_sale", "avg_days_to_sell", "invested_cents", "in_stock"}

    assert [x["car"]["id"] for x in v["sales"]] == [b, a, c]  # mais recentes primeiro
    sale_a = v["sales"][1]
    assert (sale_a["sale_cents"], sale_a["sale_is_estimate"], sale_a["expenses_cents"]) == (11_500_000, False, 75_000)
    assert sale_a["profit_cents"] == 1_425_000 and sale_a["car"]["brand"] == "Chevrolet"
    sale_b = v["sales"][0]
    assert sale_b["sale_is_estimate"] is True and sale_b["profit_cents"] == 1_000_000
    sale_c = v["sales"][2]
    assert sale_c["cost_cents"] is None and sale_c["profit_cents"] is None and sale_c["expenses_cents"] == 99_000
    assert sale_c["sold_at"].tzinfo is not None and sale_c["days_to_sell"] is not None

    assert [(g["brand"], g["count"]) for g in v["brands"]] == [("Chevrolet", 2), ("BMW", 1)]
    assert v["brands"][0]["revenue_cents"] == 18_500_000
    assert v["brands"][0]["share_pct"] == 66.7 and v["brands"][1]["share_pct"] == 33.3

    h = v["history"]
    assert [x["key"] for x in h] == ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    assert h[0]["label"] == "mai"
    assert h[-1] == {"key": "2026-10", "label": "out", "sold_count": 3, "revenue_cents": s["revenue_cents"],
                     "spend_cents": 16_075_000, "profit_cents": s["profit_cents"]}
    assert h[-2]["sold_count"] == 0 and h[-2]["revenue_cents"] == 0


def test_brand_tie_broken_by_revenue(stock):
    conn = stock
    _sell(conn, "2", "2026-10-02T12:00:00-03:00")
    _sell(conn, "3", "2026-10-03T12:00:00-03:00")
    v = finance.sales_view(conn, "2026-10", TODAY)
    assert [g["brand"] for g in v["brands"]] == ["BMW", "Chevrolet"]


def test_empty_month_and_sale_without_any_price(stock):
    conn = stock
    v = finance.sales_view(conn, "2026-10", TODAY)
    s = v["summary"]
    assert s["sold_count"] == 0 and s["revenue_cents"] == 0 and s["profit_cents"] == 0
    assert s["margin_pct"] is None and s["avg_days_to_sell"] is None and s["missing_cost"] == 0
    assert v["brands"] == [] and v["sales"] == [] and len(v["history"]) == 6
    _sell(conn, "5", "2026-10-02T12:00:00-03:00", cost_cents=100_000)  # sob consulta e sem venda digitada
    v = finance.sales_view(conn, "2026-10", TODAY)
    assert v["sales"][0]["sale_cents"] is None and v["sales"][0]["profit_cents"] is None
    s = v["summary"]  # sem valor de venda: fora de custo/lucro, contado em missing_sale
    assert (s["revenue_cents"], s["cost_cents"], s["profit_cents"]) == (0, 0, 0)
    assert (s["missing_sale"], s["missing_cost"]) == (1, 0)
    assert v["history"][-1]["spend_cents"] == 0


def test_month_boundary_uses_sao_paulo(stock):
    conn = stock
    _sell(conn, "1", "2026-11-01T01:30:00+00:00")   # 31/10 22:30 em SP → outubro
    _sell(conn, "2", "2026-10-01T02:00:00+00:00")   # 30/09 23:00 em SP → setembro
    _sell(conn, "3", "2026-10-01T00:00:00-03:00")   # 1º/10 00:00 em SP → outubro
    today = date(2026, 11, 2)
    october = finance.sales_view(conn, "2026-10", today)
    assert sorted(x["car"]["name"][:3] for x in october["sales"]) == ["BMW", "CHE"]
    assert (october["sales"][0]["sold_at"].month, october["sales"][0]["sold_at"].day) == (10, 31)
    september = finance.sales_view(conn, "2026-09", today)
    assert [x["car"]["name"] for x in september["sales"]] == ["CHEVROLET ONIX 1.0 LT 2023"]
    assert finance.sales_view(conn, "2026-11", today)["sales"] == []
    assert october["month"]["next"] == "2026-11" and september["month"]["prev"] == "2026-08"


def test_reappeared_car_leaves_sales_and_demo_is_excluded(conn, car):
    stock = [car("1", "CHEVROLET ONIX 2023"), car("2", "BMW X1 2022")]
    run_poll(conn, stock)
    run_poll(conn, stock[:1])
    run_poll(conn, stock[:1])  # 2 faltas seguidas → vendido
    today = db.now().date()
    assert [s["car"]["name"] for s in finance.sales_view(conn, None, today)["sales"]] == ["BMW X1 2022"]
    conn.execute("UPDATE cars SET cost_cents = 5000000 WHERE external_id = '2'")
    conn.commit()
    run_poll(conn, stock)  # reaparece
    v = finance.sales_view(conn, None, today)
    assert v["sales"] == [] and v["summary"]["sold_count"] == 0
    assert v["summary"]["in_stock"] == 2 and v["summary"]["invested_cents"] == 5_000_000

    ts = db.to_iso(db.now())
    conn.execute("""INSERT INTO cars (external_id, name, name_key, url, posted, active, first_seen, last_seen,
                                      sold_at, price_cents, cost_cents)
                    VALUES ('demo-1', 'DEMO CAR', 'demo car', 'u', 1, 0, ?, ?, ?, 900000, 100000)""", (ts, ts, ts))
    conn.execute("""INSERT INTO cars (external_id, name, name_key, url, posted, active, first_seen, last_seen,
                                      cost_cents)
                    VALUES ('demo-2', 'DEMO B', 'demo b', 'u', 1, 1, ?, ?, 777)""", (ts, ts))
    conn.commit()
    v = finance.sales_view(conn, None, today)
    assert v["sales"] == [] and v["summary"]["in_stock"] == 2 and v["summary"]["invested_cents"] == 5_000_000


def test_car_view_and_writes(stock):
    conn = stock
    cid = _id(conn, "1")
    assert finance.car_view(conn, 9999) is None
    v = finance.car_view(conn, cid)
    assert v["car"] == {"id": cid, "name": "CHEVROLET TRACKER 1.0 TURBO LTZ 2026", "photo_url": v["car"]["photo_url"],
                        "brand": "Chevrolet", "price_cents": 12_000_000, "active": True, "sold": False,
                        "sold_at": None, "cost_cents": None, "sale_price_cents": None}
    assert v["expenses"] == []
    assert v["totals"] == {"expenses_cents": 0, "sale_cents": 12_000_000, "sale_is_estimate": True,
                           "profit_cents": None}

    assert finance.set_cost(conn, cid, "100.000") is True
    assert finance.set_sale_price(conn, cid, "115.000,50") is True
    assert finance.add_expense(conn, cid, "  Troca   de pneus ", "2.500,00") is True
    assert finance.add_expense(conn, cid, "Documentação", "800") is True
    v = finance.car_view(conn, cid)
    assert sorted((e["description"], e["amount_cents"]) for e in v["expenses"]) == [
        ("Documentação", 80_000), ("Troca de pneus", 250_000)]
    assert v["expenses"][0]["created_at"].tzinfo is not None
    assert v["totals"] == {"expenses_cents": 330_000, "sale_cents": 11_500_050, "sale_is_estimate": False,
                           "profit_cents": 11_500_050 - 10_000_000 - 330_000}

    # inválidos não gravam
    assert finance.set_cost(conn, cid, "-1") is False and finance.set_cost(conn, cid, "abc") is False
    assert finance.set_sale_price(conn, cid, "0") is False
    for desc, amount in (("", "100"), ("Pneu", ""), ("Pneu", "0"), ("Pneu", "-5"), ("Pneu", "x"), ("x" * 201, "10")):
        assert finance.add_expense(conn, cid, desc, amount) is False
    assert finance.car_view(conn, cid)["totals"]["expenses_cents"] == 330_000
    assert finance.car_view(conn, cid)["car"]["cost_cents"] == 10_000_000

    # excluir despesa: só do próprio carro
    eid = v["expenses"][0]["id"]
    assert finance.delete_expense(conn, _id(conn, "2"), eid) is False
    assert finance.delete_expense(conn, cid, eid) is True and finance.delete_expense(conn, cid, eid) is False
    # vazio limpa custo e volta a venda ao preço do site
    assert finance.set_cost(conn, cid, "") is True and finance.set_sale_price(conn, cid, " ") is True
    v = finance.car_view(conn, cid)
    assert v["car"]["cost_cents"] is None and v["car"]["sale_price_cents"] is None
    assert v["totals"]["sale_is_estimate"] is True and v["totals"]["profit_cents"] is None
    assert finance.set_cost(conn, cid, "0") is True and finance.car_view(conn, cid)["car"]["cost_cents"] == 0


def test_team_view_gets_cost_and_expenses(stock):
    conn = stock
    cid = _id(conn, "1")
    finance.set_cost(conn, cid, "90000")
    _expense(conn, cid, 12_345)
    cars = {c["id"]: c for c in aging.team_view(conn)["cars"]}
    assert cars[cid]["cost_cents"] == 9_000_000 and cars[cid]["expenses_cents"] == 12_345
    other = cars[_id(conn, "2")]
    assert other["cost_cents"] is None and other["expenses_cents"] == 0


# ---- rotas ----------------------------------------------------------------------------

@pytest.fixture
def seeded(client, car):
    conn = db.connect()
    run_poll(conn, [car("1", "CHEVROLET TRACKER 1.0 TURBO LTZ 2026", price=12_000_000),
                    car("2", "BMW 320i 2.0 M SPORT 2022", price=25_000_000)])
    _sell(conn, "2", db.to_iso(db.now()), cost_cents=20_000_000)
    ids = {"1": _id(conn, "1"), "2": _id(conn, "2")}
    conn.close()
    return ids


def _car(cid):
    conn = db.connect()
    try:
        return finance.car_view(conn, cid)
    finally:
        conn.close()


def test_routes_render_and_save(client, seeded):
    cid = seeded["1"]
    r = client.get("/equipe/vendas")
    assert r.status_code == 200 and "BMW" in r.text
    assert client.get("/equipe/vendas?mes=2020-01").status_code == 200
    assert client.get("/equipe/vendas?mes=lixo").status_code == 200
    assert client.get(f"/equipe/carro/{cid}").status_code == 200
    assert client.get(f"/equipe/carro/{cid}?msg=saved").status_code == 200
    assert client.get(f"/equipe/carro/{seeded['2']}").status_code == 200
    assert client.get("/equipe/carro/9999").status_code == 404
    assert client.get("/equipe").status_code == 200

    def post(path, **data):
        r = client.post(f"/equipe/carro/{cid}/{path}", data=data, follow_redirects=False)
        assert r.status_code == 303
        return r.headers["location"]

    ok, bad = f"/equipe/carro/{cid}?msg=saved", f"/equipe/carro/{cid}?msg=invalid"
    assert post("custo", cost="100.000,00") == ok
    assert post("venda", sale_price="118000") == ok
    assert post("despesas", description="Polimento", amount="350,5") == ok
    assert post("custo", cost="abc") == bad and post("venda", sale_price="-3") == bad
    assert post("despesas", description="", amount="10") == bad
    assert post("despesas", description="Pneu", amount="dez") == bad
    v = _car(cid)
    assert v["car"]["cost_cents"] == 10_000_000 and v["car"]["sale_price_cents"] == 11_800_000
    assert [(e["description"], e["amount_cents"]) for e in v["expenses"]] == [("Polimento", 35_050)]
    eid = v["expenses"][0]["id"]
    assert post("despesas/9999/excluir") == bad
    assert post(f"despesas/{eid}/excluir") == ok and _car(cid)["expenses"] == []
    assert post("custo", cost="") == ok and post("venda") == ok
    v = _car(cid)
    assert v["car"]["cost_cents"] is None and v["car"]["sale_price_cents"] is None
    for path in ("custo", "venda", "despesas", "despesas/1/excluir"):
        assert client.post(f"/equipe/carro/9999/{path}", data={}, follow_redirects=False).status_code == 404


@pytest.mark.parametrize("pw", [ADMIN_PW, STAFF_PW])
def test_routes_open_to_admin_and_staff(client, seeded, monkeypatch, pw):
    monkeypatch.setenv("APP_PASSWORD", ADMIN_PW)
    monkeypatch.setenv("STAFF_PASSWORD", STAFF_PW)
    monkeypatch.setenv("SESSION_SECRET", "segredo-" + "z" * 30)
    auth.reset_failures()
    cid = seeded["1"]
    # sem login: GET vai para o login, POST é 401 e nada grava
    assert client.get("/equipe/vendas", follow_redirects=False).headers["location"].startswith("/login")
    assert client.post(f"/equipe/carro/{cid}/custo", data={"cost": "1"}, follow_redirects=False).status_code == 401
    assert _car(cid)["car"]["cost_cents"] is None

    assert client.post("/login", data={"password": pw}, follow_redirects=False).status_code == 303
    assert client.get("/equipe/vendas").status_code == 200
    assert client.get(f"/equipe/carro/{cid}").status_code == 200
    r = client.post(f"/equipe/carro/{cid}/custo", data={"cost": "95.000"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("msg=saved")
    r = client.post(f"/equipe/carro/{cid}/despesas", data={"description": "Lavagem", "amount": "80"},
                    follow_redirects=False)
    assert r.status_code == 303
    v = _car(cid)
    assert v["car"]["cost_cents"] == 9_500_000 and v["totals"]["expenses_cents"] == 8_000
    r = client.post(f"/equipe/carro/{cid}/despesas/{v['expenses'][0]['id']}/excluir", follow_redirects=False)
    assert r.status_code == 303 and _car(cid)["expenses"] == []
    r = client.post(f"/equipe/carro/{cid}/venda", data={"sale_price": "119.900"}, follow_redirects=False)
    assert r.status_code == 303 and _car(cid)["car"]["sale_price_cents"] == 11_990_000
    if pw == STAFF_PW:  # equipe continua fora do resto
        assert client.get("/cars", follow_redirects=False).status_code == 303
        assert client.post("/tickets/1/done", follow_redirects=False).status_code == 403
    auth.reset_failures()
