"""F14 — "Não foi venda" (anúncio retirado): QA independente (Sentinela).

Complementa tests/test_not_sale.py (Forja): permissões, contas refeitas à mão antes/depois de
marcar e de desfazer, virada de mês no fuso de São Paulo, entradas inválidas, reaparecimento,
migração a partir dos schemas 1634fa5 e 6a50942 (produção antes da F14) nos dois bancos e telas.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date

import pytest

from app import auth, db
from app.pgcompat import split_statements
from app.services import aging, finance
from app.services.sync import run_poll
from conftest import FIXTURES, PG, requires_pg, sqlite_only

ADMIN_PW = "senha-admin-123"
STAFF_PW = "senha-equipe-456"
TODAY = date(2026, 10, 15)
BAD_TEXT = re.compile(r"\bNone\b|Undefined|\bNaN\b")


# ---- helpers -------------------------------------------------------------------------

def _id(conn, ext):
    return conn.execute("SELECT id FROM cars WHERE external_id = ?", (ext,)).fetchone()[0]


def _set(conn, ext, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE cars SET {sets} WHERE external_id = ?", (*cols.values(), ext))
    conn.commit()
    return _id(conn, ext)


def _sell(conn, ext, sold_at, **cols):
    return _set(conn, ext, active=0, sold_at=sold_at, **cols)


def _expense(conn, car_id, cents, desc="Polimento"):
    conn.execute("INSERT INTO car_expenses (car_id, description, amount_cents, created_at) VALUES (?, ?, ?, ?)",
                 (car_id, desc, cents, "2026-10-01T10:00:00-03:00"))
    conn.commit()


def _row(car_id):
    """Estado gravado, lido por uma conexão nova (enxerga o que as rotas gravaram nos 2 bancos)."""
    c = db.connect()
    try:
        r = c.execute("SELECT not_sale, active, sold_at, cost_cents, sale_price_cents FROM cars WHERE id = ?",
                      (car_id,)).fetchone()
        return None if r is None else tuple(r)
    finally:
        c.close()


def _flags():
    c = db.connect()
    try:
        return [tuple(r) for r in c.execute("SELECT id, not_sale FROM cars ORDER BY id")]
    finally:
        c.close()


@pytest.fixture(autouse=True)
def _reset_failures():
    auth.reset_failures()
    yield
    auth.reset_failures()


@pytest.fixture
def roles(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", ADMIN_PW)
    monkeypatch.setenv("STAFF_PASSWORD", STAFF_PW)
    monkeypatch.setenv("SESSION_SECRET", "segredo-" + "n" * 30)
    monkeypatch.setenv("CRON_SECRET", "cron-ok")


def _login(client, pw):
    assert client.post("/login", data={"password": pw}, follow_redirects=False).status_code == 303
    return client


def _mark(client, car_id, value="1", **kw):
    data = {} if value is None else {"value": value}
    return client.post(f"/equipe/carro/{car_id}/nao-venda", data=data, follow_redirects=False, **kw)


@pytest.fixture
def lot(conn, car):
    """6 carros, entrada em 01/09/2026. Vendidos: A, B, C, E em outubro; D em 30/09 23:30 BRT
    (gravado em UTC, já 01/10). O Audi continua no estoque com custo e despesa."""
    run_poll(conn, [
        car("1", "CHEVROLET TRACKER 1.0 TURBO LTZ 2026", price=12_000_000),
        car("2", "CHEVROLET ONIX 1.0 LT 2023", price=7_000_000),
        car("3", "BMW 320i 2.0 M SPORT 2022", price=25_000_000),
        car("4", "LAND ROVER RANGE ROVER SPORT 3.0 2023", price=80_000_000),
        car("5", "FIAT UNO MILLE 2010", price=3_000_000, photo=None),
        car("6", "AUDI A3 SEDAN 2020", price=10_000_000),
    ])
    conn.execute("UPDATE cars SET first_seen = '2026-09-01T10:00:00-03:00'")
    conn.commit()
    ids = {
        "A": _sell(conn, "1", "2026-10-03T10:00:00-03:00", cost_cents=10_000_000),
        "B": _sell(conn, "2", "2026-10-07T10:00:00-03:00", cost_cents=6_000_000, sale_price_cents=6_800_000),
        "C": _sell(conn, "3", "2026-10-09T10:00:00-03:00", cost_cents=20_000_000),
        "D": _sell(conn, "4", "2026-10-01T02:30:00+00:00", cost_cents=70_000_000),   # 30/09 23:30 em SP
        "E": _sell(conn, "5", "2026-10-12T10:00:00-03:00"),                          # sem custo
        "F": _set(conn, "6", cost_cents=8_000_000),
    }
    _expense(conn, ids["A"], 200_000)
    _expense(conn, ids["C"], 500_000)
    _expense(conn, ids["F"], 100_000)
    return ids


def _oct(conn):
    return finance.sales_view(conn, "2026-10", TODAY)


def _nums(summary):
    return {k: summary[k] for k in ("sold_count", "revenue_cents", "cost_cents", "expenses_cents", "profit_cents",
                                    "margin_pct", "missing_cost", "avg_days_to_sell", "invested_cents", "in_stock")}


# =======================================================================================
# 1) Permissões
# =======================================================================================

@pytest.mark.parametrize("pw", [ADMIN_PW, STAFF_PW])
def test_admin_and_staff_can_mark_and_undo(client, roles, lot, pw):
    _login(client, pw)
    c = lot["C"]
    r = _mark(client, c, "1")
    assert r.status_code == 303 and r.headers["location"] == f"/equipe/carro/{c}?msg=saved"
    assert _row(c)[0] == 1
    r = _mark(client, c, "0")
    assert r.headers["location"] == f"/equipe/carro/{c}?msg=saved"
    assert _row(c)[0] == 0


def test_anonymous_cannot_mark(client, roles, lot):
    c = lot["C"]
    before = _flags()
    assert _mark(client, c, "1").status_code == 401
    r = _mark(client, c, "1", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers.get("HX-Redirect") == "/login"
    r = client.get(f"/equipe/carro/{c}/nao-venda", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    for token in ("lixo", "v2.staff.1.deadbeef", "v2.admin.9999999999.00"):
        client.cookies.set(auth.COOKIE_NAME, token)
        assert _mark(client, c, "1").status_code == 401
    assert _flags() == before


def test_staff_password_change_kills_access(client, roles, lot, monkeypatch):
    _login(client, STAFF_PW)
    monkeypatch.setenv("STAFF_PASSWORD", "trocada-999")
    assert _mark(client, lot["C"], "1").status_code == 401
    assert _row(lot["C"])[0] == 0


def test_staff_still_blocked_elsewhere(client, roles, lot, site):
    _login(client, STAFF_PW)
    assert _mark(client, lot["C"], "1").headers["location"].endswith("msg=saved")
    for path in ("/admin/check-now", "/admin/price-check-now", "/tickets/1/done", "/sold/1/dismiss"):
        assert client.post(path, follow_redirects=False).status_code == 403, path
    for path in ("/", "/cars", "/editor", "/api/cars/1/prices"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/equipe", path
    assert client.get("/cron/daily", follow_redirects=False).status_code == 401
    assert site.calls == 0


def test_only_post_is_accepted(client, lot):
    c = lot["C"]
    for method in ("GET", "PUT", "DELETE", "PATCH"):
        assert client.request(method, f"/equipe/carro/{c}/nao-venda", follow_redirects=False).status_code == 405
    assert _row(c)[0] == 0


def test_nao_venda_is_the_only_new_equipe_route():
    from fastapi.routing import APIRoute

    from app.main import app

    equipe = sorted((m, r.path) for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/equipe")
                    for m in r.methods - {"HEAD", "OPTIONS"})
    assert equipe == sorted([
        ("GET", "/equipe"), ("GET", "/equipe/vendas"), ("GET", "/equipe/carro/{car_id}"),
        ("GET", "/equipe/senha"), ("POST", "/equipe/senha"),   # F15
        ("GET", "/equipe/clientes"), ("GET", "/equipe/clientes/novo"),   # F16
        ("GET", "/equipe/clientes/{customer_id}"), ("POST", "/equipe/clientes"),
        ("POST", "/equipe/clientes/{customer_id}"), ("POST", "/equipe/clientes/{customer_id}/parabens"),
        ("POST", "/equipe/clientes/{customer_id}/excluir"),
        ("POST", "/equipe/carro/{car_id}/custo"), ("POST", "/equipe/carro/{car_id}/venda"),
        ("POST", "/equipe/carro/{car_id}/nao-venda"), ("POST", "/equipe/carro/{car_id}/despesas"),
        ("POST", "/equipe/carro/{car_id}/despesas/{expense_id}/excluir"),
    ])


# =======================================================================================
# 2) Cálculos antes / depois de marcar / depois de desfazer
# =======================================================================================

def test_october_before_marking_by_hand(lot, conn):
    v = _oct(conn)
    # A 120.000 + B 68.000 (digitado) + C 250.000 + E 30.000 = 468.000
    # custo A 100 + B 60 + C 200 = 360.000 ; despesas A 2.000 + C 5.000 = 7.000
    # lucro A 18.000 + B 8.000 + C 45.000 = 71.000 ; margem 71/438 = 16,21%
    # dias 32, 36, 38, 41 → 36,75 → 37
    assert _nums(v["summary"]) == {
        "sold_count": 4, "revenue_cents": 46_800_000, "cost_cents": 36_000_000, "expenses_cents": 700_000,
        "profit_cents": 7_100_000, "margin_pct": 16.2, "missing_cost": 1, "avg_days_to_sell": 37,
        "invested_cents": 8_100_000, "in_stock": 1}
    assert [(b["brand"], b["count"], b["revenue_cents"], b["share_pct"]) for b in v["brands"]] == [
        ("Chevrolet", 2, 18_800_000, 50.0), ("BMW", 1, 25_000_000, 25.0), ("Fiat", 1, 3_000_000, 25.0)]
    assert [s["car"]["id"] for s in v["sales"]] == [lot["E"], lot["C"], lot["B"], lot["A"]]
    assert v["removed"] == []
    assert [(h["key"], h["sold_count"], h["revenue_cents"], h["spend_cents"], h["profit_cents"])
            for h in v["history"]][-2:] == [("2026-09", 1, 80_000_000, 70_000_000, 10_000_000),
                                            ("2026-10", 4, 46_800_000, 36_700_000, 7_100_000)]


def test_marking_removes_car_from_every_sale_number(lot, conn):
    assert finance.set_not_sale(conn, lot["C"], "1")
    v = _oct(conn)
    # sem o BMW: 120.000 + 68.000 + 30.000 = 218.000 ; custo 160.000 ; despesas 2.000
    # lucro 18.000 + 8.000 = 26.000 ; margem 26/188 = 13,83% ; dias 32, 36, 41 → 36,33 → 36
    assert _nums(v["summary"]) == {
        "sold_count": 3, "revenue_cents": 21_800_000, "cost_cents": 16_000_000, "expenses_cents": 200_000,
        "profit_cents": 2_600_000, "margin_pct": 13.8, "missing_cost": 1, "avg_days_to_sell": 36,
        "invested_cents": 8_100_000, "in_stock": 1}       # retirado NÃO volta para o estoque
    assert [(b["brand"], b["count"], b["revenue_cents"], b["share_pct"]) for b in v["brands"]] == [
        ("Chevrolet", 2, 18_800_000, 66.7), ("Fiat", 1, 3_000_000, 33.3)]
    assert lot["C"] not in [s["car"]["id"] for s in v["sales"]]
    assert v["history"][-1] == {"key": "2026-10", "label": "out", "sold_count": 3, "revenue_cents": 21_800_000,
                                "spend_cents": 16_200_000, "profit_cents": 2_600_000}
    assert len(v["removed"]) == 1
    removed = v["removed"][0]
    assert set(removed) == {"car", "sold_at"}
    assert removed["car"] == {"id": lot["C"], "name": "BMW 320i 2.0 M SPORT 2022",
                              "photo_url": "https://img.test/3.jpg", "brand": "BMW"}
    assert (removed["sold_at"].day, removed["sold_at"].month) == (9, 10) and removed["sold_at"].tzinfo is not None
    # custo e despesas ficam guardados no carro
    cv = finance.car_view(conn, lot["C"])
    assert cv["car"]["not_sale"] is True and cv["car"]["sold"] is True and cv["car"]["active"] is False
    assert cv["car"]["cost_cents"] == 20_000_000 and cv["totals"]["expenses_cents"] == 500_000


def test_undo_restores_exactly_the_previous_view(lot, conn):
    before_oct, before_sep = _oct(conn), finance.sales_view(conn, "2026-09", TODAY)
    before_team = aging.team_view(conn, TODAY)["summary"]
    for key in ("C", "D", "E"):
        assert finance.set_not_sale(conn, lot[key], "1")
    assert _oct(conn) != before_oct
    for key in ("E", "C", "D"):
        assert finance.set_not_sale(conn, lot[key], "0")
    assert _oct(conn) == before_oct
    assert finance.sales_view(conn, "2026-09", TODAY) == before_sep
    assert aging.team_view(conn, TODAY)["summary"] == before_team
    assert all(flag == 0 for _, flag in _flags())


def test_marking_top_brand_changes_the_ranking(lot, conn):
    """O motivo da F14: anúncio retirado não pode eleger a marca mais vendida."""
    assert _oct(conn)["brands"][0]["brand"] == "Chevrolet"
    finance.set_not_sale(conn, lot["A"], "1")
    finance.set_not_sale(conn, lot["B"], "1")
    v = _oct(conn)
    # sobram BMW 250.000 (lucro 45.000) e Fiat 30.000 (sem custo)
    assert [(b["brand"], b["count"], b["share_pct"]) for b in v["brands"]] == [("BMW", 1, 50.0), ("Fiat", 1, 50.0)]
    assert _nums(v["summary"])["revenue_cents"] == 28_000_000 and v["summary"]["profit_cents"] == 4_500_000
    assert v["summary"]["margin_pct"] == 18.0 and v["summary"]["avg_days_to_sell"] == 40   # (38+41)/2 = 39,5 → 40
    assert [r["car"]["id"] for r in v["removed"]] == [lot["B"], lot["A"]]                    # mais recentes primeiro


def test_all_sales_of_month_removed(lot, conn):
    for key in ("A", "B", "C", "E"):
        finance.set_not_sale(conn, lot[key], "1")
    v = _oct(conn)
    s = v["summary"]
    assert (s["sold_count"], s["revenue_cents"], s["cost_cents"], s["expenses_cents"], s["profit_cents"],
            s["missing_cost"]) == (0, 0, 0, 0, 0, 0)
    assert s["margin_pct"] is None and s["avg_days_to_sell"] is None
    assert v["sales"] == [] and v["brands"] == []
    assert [r["car"]["id"] for r in v["removed"]] == [lot["E"], lot["C"], lot["B"], lot["A"]]
    assert (s["in_stock"], s["invested_cents"]) == (1, 8_100_000)


def test_removed_follows_sao_paulo_month(lot, conn):
    """D saiu em 30/09 23:30 BRT (01/10 02:30 UTC): é de SETEMBRO na lista de retirados também."""
    finance.set_not_sale(conn, lot["D"], "1")
    finance.set_not_sale(conn, lot["C"], "1")
    sep, out = finance.sales_view(conn, "2026-09", TODAY), _oct(conn)
    assert [r["car"]["id"] for r in sep["removed"]] == [lot["D"]]
    assert [r["car"]["id"] for r in out["removed"]] == [lot["C"]]
    d = sep["removed"][0]["sold_at"]
    assert (d.day, d.month, d.hour, d.minute) == (30, 9, 23, 30)
    assert (sep["summary"]["sold_count"], sep["summary"]["revenue_cents"]) == (0, 0)
    # no histórico de outubro, setembro zera
    assert [(h["key"], h["sold_count"], h["revenue_cents"], h["spend_cents"], h["profit_cents"])
            for h in out["history"]][-2] == ("2026-09", 0, 0, 0, 0)
    assert finance.sales_view(conn, "2026-08", TODAY)["removed"] == []
    assert finance.sales_view(conn, "2026-11", date(2026, 11, 20))["removed"] == []


@pytest.mark.parametrize("sold_at, month", [
    ("2026-10-31T23:59:59-03:00", "2026-10"), ("2026-11-01T00:00:00-03:00", "2026-11"),
    ("2026-11-01T02:59:59+00:00", "2026-10"), ("2026-11-01T03:00:00+00:00", "2026-11"),
])
def test_removed_month_boundary(conn, car, sold_at, month):
    run_poll(conn, [car("1", "BMW X1 2022"), car("2", "AUDI A3 2020")])
    cid = _sell(conn, "1", sold_at, not_sale=1)
    today = date(2026, 11, 20)
    other = "2026-11" if month == "2026-10" else "2026-10"
    assert [r["car"]["id"] for r in finance.sales_view(conn, month, today)["removed"]] == [cid]
    assert finance.sales_view(conn, other, today)["removed"] == []


def test_sold_30d_on_team_page(lot, conn):
    # hoje 15/10 → janela desde 15/09: A, B, C, E (outubro) e D (30/09) = 5
    assert aging.team_view(conn, TODAY)["summary"]["sold_30d"] == 5
    finance.set_not_sale(conn, lot["C"], "1")
    finance.set_not_sale(conn, lot["D"], "1")
    summary = aging.team_view(conn, TODAY)["summary"]
    assert summary["sold_30d"] == 3
    assert summary["total"] == 1 if "total" in summary else True     # retirado não volta para o estoque
    assert [c["id"] for c in aging.team_view(conn, TODAY)["cars"]] == [lot["F"]]
    finance.set_not_sale(conn, lot["C"], "0")
    assert aging.team_view(conn, TODAY)["summary"]["sold_30d"] == 4
    # fora da janela continua fora, marcado ou não
    assert aging.team_view(conn, date(2026, 12, 1))["summary"]["sold_30d"] == 0


def test_demo_car_never_shows_in_removed(lot, conn):
    ts = "2026-10-05T12:00:00-03:00"
    conn.execute("""INSERT INTO cars (external_id, name, name_key, url, posted, active, first_seen, last_seen,
                                      sold_at, price_cents, not_sale)
                    VALUES ('demo-9', 'BMW DEMO', 'bmw demo', 'u', 1, 0, ?, ?, ?, 900000, 1)""", (ts, ts, ts))
    conn.commit()
    assert _oct(conn)["removed"] == []


def test_costs_can_still_be_edited_while_not_sale(lot, conn):
    c = lot["C"]
    finance.set_not_sale(conn, c, "1")
    assert finance.set_cost(conn, c, "210.000") and finance.add_expense(conn, c, "IPVA", "1.000")
    assert _oct(conn)["summary"]["cost_cents"] == 16_000_000          # continua fora do mês
    finance.set_not_sale(conn, c, "0")
    s = _oct(conn)["summary"]
    # desfez: entra com os números novos → custo 100+60+210 = 370.000 ; despesas 2.000+5.000+1.000
    assert (s["sold_count"], s["cost_cents"], s["expenses_cents"]) == (4, 37_000_000, 800_000)
    assert s["profit_cents"] == 1_800_000 + 800_000 + (25_000_000 - 21_000_000 - 600_000)


# =======================================================================================
# 3) Entradas inválidas
# =======================================================================================

def test_active_car_is_invalid(client, lot):
    f = lot["F"]
    for value in ("1", "0"):
        r = _mark(client, f, value)
        assert r.status_code == 303 and r.headers["location"] == f"/equipe/carro/{f}?msg=invalid"
    assert _row(f)[0] == 0


def test_inactive_but_not_sold_car_is_invalid(client, lot, conn):
    """Fora do site sem sold_at (ex.: 1 falta só, ou desativado à mão) não é 'vendido'."""
    f = _set(conn, "6", active=0, missing_count=1)
    assert _mark(client, f, "1").headers["location"].endswith("msg=invalid")
    assert _row(f)[0] == 0
    assert not finance.set_not_sale(conn, f, "1")


@pytest.mark.parametrize("car_id", [9999, 0, -1, 2**31 - 1, 2**31, 2**63, int("9" * 20)])
def test_missing_or_out_of_range_id_is_404(client, lot, car_id):
    before = _flags()
    for value in ("1", "0"):
        assert _mark(client, car_id, value).status_code == 404
    assert _flags() == before


@pytest.mark.parametrize("car_id", ["abc", "1.5", "1e3", "0x1", "1;DROP TABLE cars"])
def test_non_numeric_id_is_rejected(client, lot, car_id):
    assert _mark(client, car_id, "1").status_code in (404, 422)
    assert _flags() == [(i, 0) for i, _ in _flags()]


@pytest.mark.parametrize("value", [None, "", " ", "2", "-1", "10", "01", "1.0", " 1", "1 ", "true", "on", "sim",
                                   "１", "1; UPDATE cars SET not_sale = 1", "1\x00", "0x1", "null"])
def test_invalid_value_writes_nothing(client, lot, value):
    c = lot["C"]
    before = _flags()
    r = _mark(client, c, value)
    assert r.status_code == 303 and r.headers["location"] == f"/equipe/carro/{c}?msg=invalid"
    assert _flags() == before
    # e também não desfaz um carro já marcado
    conn = db.connect()
    finance.set_not_sale(conn, c, "1")
    conn.close()
    assert _mark(client, c, value).headers["location"].endswith("msg=invalid")
    assert _row(c)[0] == 1


def test_value_must_come_in_the_form_body(client, lot):
    c = lot["C"]
    r = client.post(f"/equipe/carro/{c}/nao-venda?value=1", follow_redirects=False)
    assert r.headers["location"].endswith("msg=invalid") and _row(c)[0] == 0


def test_marking_twice_is_idempotent(client, lot):
    c = lot["C"]
    for _ in range(3):
        assert _mark(client, c, "1").headers["location"].endswith("msg=saved")
    assert _row(c)[0] == 1
    for _ in range(2):
        assert _mark(client, c, "0").headers["location"].endswith("msg=saved")
    assert _row(c)[0] == 0


def test_mark_touches_only_that_car_and_only_that_column(client, lot):
    c = lot["C"]
    before_c, before_all = _row(c), _flags()
    _mark(client, c, "1")
    assert _row(c) == (1, *before_c[1:])
    assert [f for f in _flags() if f[0] != c] == [f for f in before_all if f[0] != c]


# =======================================================================================
# 4) Reaparecimento
# =======================================================================================

def test_reappearance_clears_flag_and_next_sale_counts(conn, car):
    stock = [car("1", "CHEVROLET ONIX 2023", price=7_000_000), car("2", "BMW X1 2022", price=20_000_000),
             car("3", "AUDI A3 2020", price=10_000_000)]
    run_poll(conn, stock)
    run_poll(conn, stock[:1])
    run_poll(conn, stock[:1])                      # BMW e Audi "vendidos"
    bmw, audi = _id(conn, "2"), _id(conn, "3")
    today = db.now().date()
    assert finance.set_not_sale(conn, bmw, "1") and finance.set_not_sale(conn, audi, "1")
    assert finance.set_cost(conn, bmw, "150.000")
    v = finance.sales_view(conn, None, today)
    assert v["sales"] == [] and sorted(r["car"]["id"] for r in v["removed"]) == [bmw, audi]
    assert v["summary"]["in_stock"] == 1

    run_poll(conn, stock[:2])                      # só o BMW volta ao site
    assert _row(bmw)[:3] == (0, 1, None)
    assert _row(audi)[0] == 1                      # quem não voltou continua marcado
    v = finance.sales_view(conn, None, today)
    assert [r["car"]["id"] for r in v["removed"]] == [audi] and v["sales"] == []
    assert (v["summary"]["in_stock"], v["summary"]["invested_cents"]) == (2, 15_000_000)
    assert finance.car_view(conn, bmw)["car"]["not_sale"] is False
    assert not finance.set_not_sale(conn, bmw, "1")           # de volta ao estoque: não dá para marcar

    run_poll(conn, stock[:1])
    run_poll(conn, stock[:1])                      # agora o BMW sai de novo: é venda de verdade
    assert _row(bmw)[0] == 0
    v = finance.sales_view(conn, None, today)
    assert [s["car"]["id"] for s in v["sales"]] == [bmw]
    # 200.000 − 150.000 = 50.000
    assert (v["summary"]["sold_count"], v["summary"]["revenue_cents"], v["summary"]["profit_cents"]) == \
        (1, 20_000_000, 5_000_000)
    assert aging.team_view(conn, today)["summary"]["sold_30d"] == 1


def test_one_miss_after_reappearance_does_not_resurrect_flag(conn, car):
    stock = [car("1", "CHEVROLET ONIX 2023"), car("2", "BMW X1 2022")]
    run_poll(conn, stock)
    run_poll(conn, stock[:1])
    run_poll(conn, stock[:1])
    bmw = _id(conn, "2")
    finance.set_not_sale(conn, bmw, "1")
    run_poll(conn, stock)
    run_poll(conn, stock[:1])                      # 1 falta só
    assert _row(bmw)[:2] == (0, 1)


def test_reappearance_via_daily_job(client, site, car, monkeypatch):
    """Mesmo caminho da produção: /cron/daily."""
    monkeypatch.setenv("CRON_SECRET", "s")
    hdr = {"Authorization": "Bearer s"}
    stock = [car("1", "CHEVROLET ONIX 2023"), car("2", "BMW X1 2022")]
    for cars in (stock, stock[:1], stock[:1]):
        site.cars = cars
        assert client.get("/cron/daily", headers=hdr).status_code == 200
    conn = db.connect()
    bmw = _id(conn, "2")
    conn.close()
    assert _mark(client, bmw, "1").headers["location"].endswith("msg=saved")
    assert "Retirados do site" in client.get("/equipe/vendas").text
    site.cars = stock
    assert client.get("/cron/daily", headers=hdr).status_code == 200
    assert _row(bmw)[:3] == (0, 1, None)
    assert "Retirados do site" not in client.get("/equipe/vendas").text


# =======================================================================================
# 5) Migração e schema
# =======================================================================================

def test_schema_comments_have_no_semicolon_and_split_cleanly():
    for line in db.SCHEMA.splitlines():
        if "--" in line:
            assert ";" not in line.split("--", 1)[1], line
    for schema in (db.SCHEMA, db.PG_SCHEMA):
        statements = split_statements(schema)
        assert all(s.upper().startswith("CREATE ") and "--" not in s for s in statements)
        cars = next(s for s in statements if s.startswith("CREATE TABLE IF NOT EXISTS cars"))
        assert re.search(r"not_sale\s+INTEGER NOT NULL DEFAULT 0\s*\)$", cars)
        assert cars.count("(") == cars.count(")")
    assert ("cars", "not_sale", "INTEGER NOT NULL DEFAULT 0") in db.MIGRATIONS


OLD = {  # schema → colunas de cars que ele ainda não tem
    "schema_1634fa5.sql": ("brand", "cost_cents", "sale_price_cents", "not_sale"),
    "schema_6a50942.sql": ("not_sale",),
}
TS = "2026-09-01T10:00:00-03:00"
OLD_CARS = [
    ("5509733", "LAND ROVER RANGE ROVER SPORT 3.0 HSE 2023", 77_990_000, 1, None),
    ("5555997", "BMW 320i 2.0 M SPORT 2022", None, 1, None),
    ("5123456", "CHEVROLET ONIX 1.0 LT 2023", 7_000_000, 0, "2026-09-20T18:00:00-03:00"),
    ("5000001", "MERCEDES-BENZ C 300 2.0 2021", 30_000_000, 0, "2026-10-02T18:00:00-03:00"),
    ("demo-1", "FIAT DEMO 2020", 1_000_000, 1, None),
]


def _fill_old(execute, has_finance):
    for ext, name, price, active, sold_at in OLD_CARS:
        execute("""INSERT INTO cars (external_id, name, name_key, photo_url, price_cents, url, posted, active,
                                     first_seen, last_seen, missing_count, sold_at, in_baseline)
                   VALUES (?, ?, ?, ?, ?, 'u', 1, ?, ?, ?, ?, ?, 1)""",
                (ext, name, name.lower(), f"https://img.test/{ext}.jpg", price, active, TS, TS,
                 0 if active else 2, sold_at))
    execute("INSERT INTO price_history (car_id, price_cents, recorded_at) VALUES (1, 77990000, ?)", (TS,))
    execute("INSERT INTO tickets (car_id, status, created_at) VALUES (2, 'pending', ?)", (TS,))
    execute("INSERT INTO sold_alerts (car_id, created_at, dismissed) VALUES (3, ?, 0)", (TS,))
    execute("INSERT INTO runs (kind, started_at, finished_at, ok) VALUES ('daily', ?, ?, 1)", (TS, TS))
    if has_finance:   # produção hoje: marca já preenchida pela F13 e a equipe já lançou custos
        for car_id, brand in ((1, "Land Rover"), (2, "BMW"), (5, "Fiat")):
            execute("UPDATE cars SET brand = ? WHERE id = ?", (brand, car_id))
        execute("UPDATE cars SET brand = 'Chevrolet', cost_cents = 6000000, sale_price_cents = 6900000 WHERE id = 3")
        execute("UPDATE cars SET brand = 'Mercedes-Benz', cost_cents = 25000000 WHERE id = 4")
        execute("INSERT INTO car_expenses (car_id, description, amount_cents, created_at) VALUES (3, 'Pneus', 180000, ?)",
                (TS,))


def _cols(conn, table):
    if db.is_postgres(conn):
        out = [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = ? "
            "ORDER BY ordinal_position", (table,))]
    else:
        out = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    conn.commit()
    return out


def _dump(conn, skip):
    tables = ["cars", "price_history", "tickets", "sold_alerts", "runs", "detail_cache"]
    if "car_expenses" in _tables(conn):
        tables.append("car_expenses")
    out = {}
    for table in tables:
        cols = [c for c in _cols(conn, table) if c not in skip]
        out[table] = [tuple(r) for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY 1")]
    conn.commit()   # sem transação aberta lendo `cars`: o ALTER TABLE do init_db esperaria o lock
    return out


def _tables(conn):
    if db.is_postgres(conn):
        out = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    else:
        out = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.commit()
    return out


def _check_migrated(conn, schema, before):
    """Depois de init_db: coluna nova com 0, dados idênticos, F14 funcionando."""
    skip = OLD[schema]
    assert "not_sale" in _cols(conn, "cars") and "car_expenses" in _tables(conn)
    after = _dump(conn, skip)
    after.pop("car_expenses", None) if "car_expenses" not in before else None
    assert after == before
    assert [tuple(r) for r in conn.execute("SELECT DISTINCT not_sale FROM cars")] == [(0,)]
    conn.commit()
    today = date(2026, 10, 11)
    out = finance.sales_view(conn, "2026-10", today)
    assert [s["car"]["name"] for s in out["sales"]] == ["MERCEDES-BENZ C 300 2.0 2021"] and out["removed"] == []
    sep = finance.sales_view(conn, "2026-09", today)
    assert [s["car"]["name"] for s in sep["sales"]] == ["CHEVROLET ONIX 1.0 LT 2023"]
    if schema == "schema_6a50942.sql":
        # custos da produção preservados: 69.000 − 60.000 − 1.800 = 7.200
        assert (sep["sales"][0]["sale_cents"], sep["sales"][0]["profit_cents"]) == (6_900_000, 720_000)
        assert out["sales"][0]["profit_cents"] == 5_000_000
    assert aging.team_view(conn, today)["summary"]["sold_30d"] == 2
    conn.commit()


def _use_f14_then_remigrate(conn, schema):
    merc = _id(conn, "5000001")
    assert finance.set_not_sale(conn, merc, "1")
    assert not finance.set_not_sale(conn, _id(conn, "5509733"), "1")      # ativo
    full = _dump(conn, ())
    db.init_db()
    db.init_db()
    assert _dump(conn, ()) == full                                         # 2ª/3ª migração não desmarca nada
    assert len(_cols(conn, "cars")) == len(set(_cols(conn, "cars")))
    out = finance.sales_view(conn, "2026-10", date(2026, 10, 11))
    assert out["sales"] == [] and [r["car"]["id"] for r in out["removed"]] == [merc]
    conn.commit()
    with pytest.raises(Exception):                                         # NOT NULL de verdade nos 2 bancos
        conn.execute("UPDATE cars SET not_sale = NULL WHERE id = ?", (merc,))
    conn.rollback()


@sqlite_only
@pytest.mark.parametrize("schema", sorted(OLD))
def test_sqlite_migration_from_old_schema_twice(tmp_path, monkeypatch, schema):
    path = tmp_path / "antigo.db"
    monkeypatch.setenv("UNION_DB_PATH", str(path))
    raw = sqlite3.connect(path)
    raw.executescript((FIXTURES / schema).read_text(encoding="utf-8"))
    _fill_old(raw.execute, has_finance="car_expenses" in (FIXTURES / schema).read_text(encoding="utf-8"))
    raw.commit()
    raw.close()
    conn = db.connect()
    try:
        assert not set(OLD[schema]) & set(_cols(conn, "cars"))
        before = _dump(conn, ())
        db.init_db()
        _check_migrated(conn, schema, before)
        db.init_db()
        _check_migrated(conn, schema, before)
        _use_f14_then_remigrate(conn, schema)
    finally:
        conn.close()


@pytest.fixture
def empty_pg():
    conn = db.connect()
    for table in _tables(conn):
        conn.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    conn.commit()
    yield conn
    conn.close()
    db.init_db()


@requires_pg
@pytest.mark.parametrize("schema", sorted(OLD))
def test_postgres_migration_from_old_schema_twice(empty_pg, schema):
    conn = empty_pg
    text = (FIXTURES / schema).read_text(encoding="utf-8")
    conn.executescript(text.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"))
    _fill_old(conn.execute, has_finance="car_expenses" in text)
    conn.commit()
    assert not set(OLD[schema]) & set(_cols(conn, "cars"))
    before = _dump(conn, ())
    db.init_db()
    _check_migrated(conn, schema, before)
    db.init_db()
    _check_migrated(conn, schema, before)
    _use_f14_then_remigrate(conn, schema)
    kind = [tuple(r) for r in conn.execute(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'cars' AND column_name = 'not_sale'")]
    conn.commit()
    assert kind == [("integer", "NO", "0")]


@requires_pg
def test_postgres_boot_on_production_schema_serves_f14(empty_pg, site):
    """Cold start da Vercel com o banco como está hoje em produção (6a50942)."""
    from fastapi.testclient import TestClient

    from app.main import app

    conn = empty_pg
    text = (FIXTURES / "schema_6a50942.sql").read_text(encoding="utf-8")
    conn.executescript(text.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"))
    _fill_old(conn.execute, has_finance=True)
    conn.commit()
    with TestClient(app) as client:
        for path in ("/health", "/", "/equipe", "/equipe/vendas?mes=2026-10", "/equipe/carro/4", "/equipe/carro/1"):
            assert client.get(path).status_code == 200, path
        assert _mark(client, 4, "1").headers["location"] == "/equipe/carro/4?msg=saved"
        html = client.get("/equipe/vendas?mes=2026-10").text
        assert "Retirados do site" in html and "Nenhuma venda em" in html
        assert "não conta como venda" in client.get("/equipe/carro/4").text
        assert _mark(client, 1, "1").headers["location"].endswith("msg=invalid")
        assert _mark(client, 4, "0").headers["location"].endswith("msg=saved")
        assert "Retirados do site" not in client.get("/equipe/vendas?mes=2026-10").text


def test_new_car_defaults_to_zero(conn, car):
    run_poll(conn, [car("1")])
    assert [tuple(r) for r in conn.execute("SELECT not_sale FROM cars")] == [(0,)]


# =======================================================================================
# 6) Telas: vendido / não-venda / ativo
# =======================================================================================

def _main(html):
    return html.split("<main", 1)[-1]


@pytest.mark.parametrize("who", ["admin", "equipe", "local"])
def test_car_page_states(client, lot, monkeypatch, who):
    if who != "local":
        monkeypatch.setenv("APP_PASSWORD", ADMIN_PW)
        monkeypatch.setenv("STAFF_PASSWORD", STAFF_PW)
        monkeypatch.setenv("SESSION_SECRET", "segredo-" + "n" * 30)
        _login(client, ADMIN_PW if who == "admin" else STAFF_PW)
    c, f = lot["C"], lot["F"]

    sold = client.get(f"/equipe/carro/{c}")
    assert sold.status_code == 200 and not BAD_TEXT.search(sold.text)
    html = _main(sold.text)
    assert "Vendido em 09/10/2026" in html and "Anúncio retirado" not in html.split("notsale-ask")[0]
    assert f'action="/equipe/carro/{c}/nao-venda"' in html and 'name="value" value="1"' in html
    assert 'name="value" value="0"' not in html and "não conta como venda" not in html
    assert 'name="sale_price"' in html and "R$ 45.000" in html          # lucro 250.000 − 200.000 − 5.000
    assert "data-confirm=" in html

    assert _mark(client, c, "1").headers["location"].endswith("msg=saved")
    marked = client.get(f"/equipe/carro/{c}?msg=saved")
    assert marked.status_code == 200 and not BAD_TEXT.search(marked.text)
    html = _main(marked.text)
    assert "Este carro não conta como venda" in html and "Anúncio retirado em 09/10/2026" in html
    assert "Vendido em" not in html
    assert 'name="value" value="0"' in html and "Foi venda sim (desfazer)" in html
    assert 'name="value" value="1"' not in html
    assert 'name="sale_price"' not in html                              # bloco de valor de venda escondido
    assert "R$ 45.000" not in html                                      # lucro não aparece para retirado
    assert 'name="cost"' in html and 'value="200.000"' in html and "Polimento" in html   # custos continuam

    assert _mark(client, c, "0").headers["location"].endswith("msg=saved")
    html = _main(client.get(f"/equipe/carro/{c}").text)
    assert "Vendido em 09/10/2026" in html and 'name="value" value="1"' in html and "R$ 45.000" in html

    active = client.get(f"/equipe/carro/{f}")
    assert active.status_code == 200 and not BAD_TEXT.search(active.text)
    html = _main(active.text)
    assert "Em estoque" in html and "nao-venda" not in html and "não conta como venda" not in html


@pytest.mark.parametrize("who", ["admin", "equipe"])
def test_sales_page_states(client, lot, roles, who):
    _login(client, ADMIN_PW if who == "admin" else STAFF_PW)
    c = lot["C"]
    before = client.get("/equipe/vendas?mes=2026-10").text
    assert "Retirados do site" not in before and "BMW 320i" in before

    _mark(client, c, "1")
    r = client.get("/equipe/vendas?mes=2026-10")
    assert r.status_code == 200 and not BAD_TEXT.search(r.text)
    html = r.text
    assert "Retirados do site" in html and "(não contam como venda)" in html
    box = html.split('id="retirados"', 1)[1].split("</details>", 1)[0]
    assert "BMW 320i 2.0 M SPORT 2022" in box and f'href="/equipe/carro/{c}"' in box and "saiu do site em 09/10" in box
    assert box.count("<li") == 1
    sale_list = html.split('id="vendas-lista"', 1)[1].split('id="retirados"', 1)[0]
    assert "BMW 320i" not in sale_list and sale_list.count("sale-card") == 3
    assert "Chevrolet" in html.split("Marca que mais vendeu", 1)[1][:400]
    assert "R$ 218.000" in html                                          # faturamento sem o BMW

    # setembro não mostra retirados de outubro
    assert "Retirados do site" not in client.get("/equipe/vendas?mes=2026-09").text
    _mark(client, c, "0")
    assert "Retirados do site" not in client.get("/equipe/vendas?mes=2026-10").text


def test_sales_page_when_every_sale_was_removed(client, lot):
    for key in ("A", "B", "C", "E"):
        _mark(client, lot[key], "1")
    r = client.get("/equipe/vendas?mes=2026-10")
    assert r.status_code == 200 and not BAD_TEXT.search(r.text)
    assert "Nenhuma venda em" in r.text and "Marca que mais vendeu" not in r.text
    box = r.text.split('id="retirados"', 1)[1].split("</details>", 1)[0]
    assert box.count("<li") == 4 and "photo-placeholder" in box        # o Fiat não tem foto


def test_team_page_counts_only_real_sales(client, lot):
    assert client.get("/equipe").status_code == 200
    for key in ("A", "B", "C", "D", "E"):
        _mark(client, lot[key], "1")
    r = client.get("/equipe")
    assert r.status_code == 200 and not BAD_TEXT.search(r.text)
    assert "BMW 320i" not in r.text and "AUDI A3 SEDAN 2020" in r.text   # retirado não volta para o estoque


def test_removed_car_name_is_escaped(client, conn, car):
    name = '<img src=x onerror=alert(1)> UNO "2010"'
    run_poll(conn, [car("1", name), car("2", "BMW X1 2020")])
    cid = _sell(conn, "1", db.to_iso(db.now()), not_sale=1)
    for path in ("/equipe/vendas", f"/equipe/carro/{cid}"):
        html = client.get(path).text
        assert "<img src=x" not in html and "&lt;img src=x" in html, path


def test_pg_flag_matches_conftest():
    assert PG == bool(db.database_url())
