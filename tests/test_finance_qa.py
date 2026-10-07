"""F13 — QA independente do módulo Vendas/financeiro (Sentinela).

Complementa tests/test_finance.py (Forja): permissões varrendo app.routes, IDOR, XSS, contas
refeitas à mão, virada de mês no fuso de São Paulo, templates com dados reais e migração de
um banco no schema do commit 1634fa5 (tests/fixtures/schema_1634fa5.sql).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.routing import APIRoute

from app import auth, db
from app.services import finance, sync
from app.services.sync import run_poll
from conftest import FIXTURES, sqlite_only

SP = ZoneInfo("America/Sao_Paulo")
ADMIN_PW = "senha-admin-123"
STAFF_PW = "senha-equipe-456"
TODAY = date(2026, 10, 15)
_PARAM = re.compile(r"\{([^}:]+)(:[^}]+)?\}")
BAD_TEXT = re.compile(r"\bNone\b|Undefined|\bnan\b|\bNaN\b")

# Tudo o que a equipe pode alcançar além de login/logout/health/static/cron (este último exige Bearer).
STAFF_ROUTES = {
    ("GET", "/equipe"),
    ("GET", "/equipe/vendas"),
    ("GET", "/equipe/carro/1"),
    ("POST", "/equipe/carro/1/custo"),
    ("POST", "/equipe/carro/1/venda"),
    ("POST", "/equipe/carro/1/despesas"),
    ("POST", "/equipe/carro/1/despesas/1/excluir"),
}
PUBLIC = {"/login", "/logout", "/health", "/favicon.ico"}
FINANCE_POSTS = ("custo", "venda", "despesas", "despesas/1/excluir")


# ---- helpers -------------------------------------------------------------------------

def all_routes():
    from app.main import app

    out = []
    for r in app.routes:
        if isinstance(r, APIRoute):
            for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
                out.append((m, _PARAM.sub("1", r.path)))
    return out


def _id(conn, ext):
    return conn.execute("SELECT id FROM cars WHERE external_id = ?", (ext,)).fetchone()[0]


def _set(conn, ext, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE cars SET {sets} WHERE external_id = ?", (*cols.values(), ext))
    conn.commit()
    return _id(conn, ext)


def _sell(conn, ext, sold_at, **cols):
    return _set(conn, ext, active=0, sold_at=sold_at, **cols)


def _expense(conn, car_id, cents, desc="Polimento", when="2026-10-01T10:00:00-03:00"):
    cur = conn.execute(
        "INSERT INTO car_expenses (car_id, description, amount_cents, created_at) VALUES (?, ?, ?, ?)",
        (car_id, desc, cents, when))
    conn.commit()
    return cur.lastrowid


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _snapshot(conn):
    conn.commit()  # enxerga o que as rotas gravaram (Postgres)
    cars = [tuple(r) for r in conn.execute(
        "SELECT id, cost_cents, sale_price_cents FROM cars ORDER BY id")]
    exps = [tuple(r) for r in conn.execute(
        "SELECT id, car_id, description, amount_cents FROM car_expenses ORDER BY id")]
    return cars, exps


@pytest.fixture(autouse=True)
def _reset_failures():
    auth.reset_failures()
    yield
    auth.reset_failures()


@pytest.fixture
def roles(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", ADMIN_PW)
    monkeypatch.setenv("STAFF_PASSWORD", STAFF_PW)
    monkeypatch.setenv("SESSION_SECRET", "segredo-" + "q" * 30)
    monkeypatch.setenv("CRON_SECRET", "cron-ok")


def _login(client, pw):
    r = client.post("/login", data={"password": pw}, follow_redirects=False)
    assert r.status_code == 303
    return client


@pytest.fixture
def staff(client, roles):
    return _login(client, STAFF_PW)


@pytest.fixture
def two_cars(client, conn, car):
    """Carro A (id 1) com 1 despesa e carro B (id 2) com 1 despesa."""
    run_poll(conn, [car("1", "CHEVROLET ONIX 1.0 LT 2023", price=7_000_000),
                    car("2", "BMW 320i 2.0 M SPORT 2022", price=25_000_000)])
    a, b = _id(conn, "1"), _id(conn, "2")
    return {"a": a, "b": b, "exp_a": _expense(conn, a, 10_000, "Lavagem A"),
            "exp_b": _expense(conn, b, 20_000, "Pneu B")}


# =======================================================================================
# 1) Segurança / perfis
# =======================================================================================

def test_equipe_routes_are_exactly_the_contract():
    """Rota nova sob /equipe/* nasce liberada para a equipe (prefixo no middleware): este
    teste obriga a revisar a lista quando alguém adicionar uma."""
    equipe = {(m, p) for m, p in all_routes() if p == "/equipe" or p.startswith("/equipe/")}
    assert equipe == STAFF_ROUTES


def test_staff_reaches_only_team_area(staff, site, two_cars, conn):
    reached, blocked = set(), set()
    for method, path in all_routes():
        if path in PUBLIC or path.startswith(("/static/", "/cron/")):
            continue
        r = staff.request(method, path, follow_redirects=False)
        barred = (r.status_code == 303 and r.headers.get("location") == "/equipe") if method == "GET" \
            else r.status_code == 403
        (blocked if barred else reached).add((method, path))
        if (method, path) in STAFF_ROUTES:
            assert r.status_code in (200, 303), (method, path, r.status_code)
            assert not r.headers.get("location", "").startswith("/login")
    assert reached == STAFF_ROUTES
    assert len(blocked) >= 12 and ("POST", "/admin/check-now") in blocked and ("GET", "/") in blocked
    assert site.calls == 0


def test_staff_cron_needs_bearer(staff, site):
    for _, path in [mp for mp in all_routes() if mp[1].startswith("/cron/")]:
        assert staff.get(path, follow_redirects=False).status_code == 401, path
    assert site.calls == 0


def test_staff_pages_have_no_admin_links(staff, two_cars, conn):
    _sell(conn, "2", db.to_iso(db.now()))
    for path in ("/equipe", "/equipe/vendas", f"/equipe/carro/{two_cars['a']}"):
        html = staff.get(path).text
        for admin_only in ('href="/cars"', 'href="/editor"', 'href="/"', "/admin/", "Verificar agora"):
            assert admin_only not in html, (path, admin_only)
        assert 'href="/equipe/vendas"' in html


def test_anonymous_gets_nothing(client, roles, two_cars, conn):
    before = _snapshot(conn)
    for method, path in sorted(STAFF_ROUTES):
        r = client.request(method, path, data={"cost": "1", "sale_price": "1", "description": "x", "amount": "1"}
                           if method == "POST" else None, follow_redirects=False)
        if method == "GET":
            assert r.status_code == 303 and r.headers["location"].startswith("/login"), path
        else:
            assert r.status_code == 401, path
        assert "ONIX" not in r.text and "Lavagem" not in r.text
    r = client.post("/equipe/carro/1/custo", data={"cost": "1"}, headers={"HX-Request": "true"},
                    follow_redirects=False)
    assert r.status_code == 401 and r.headers.get("HX-Redirect") == "/login"
    assert _snapshot(conn) == before


@pytest.mark.parametrize("token", ["", "lixo", "v2.staff.1.deadbeef", "v2.admin.9999999999.00"])
def test_forged_cookie_cannot_write(client, roles, two_cars, conn, token):
    before = _snapshot(conn)
    client.cookies.set(auth.COOKIE_NAME, token)
    r = client.post(f"/equipe/carro/{two_cars['a']}/custo", data={"cost": "5"}, follow_redirects=False)
    assert r.status_code == 401
    assert _snapshot(conn) == before


def test_staff_cookie_dies_when_staff_password_changes(staff, two_cars, monkeypatch, conn):
    monkeypatch.setenv("STAFF_PASSWORD", "outra-senha-999")
    r = staff.post(f"/equipe/carro/{two_cars['a']}/custo", data={"cost": "5"}, follow_redirects=False)
    assert r.status_code == 401
    assert staff.get("/equipe/vendas", follow_redirects=False).headers["location"].startswith("/login")


@pytest.mark.parametrize("pw", [ADMIN_PW, STAFF_PW])
def test_missing_car_is_404_and_writes_nothing(client, roles, two_cars, conn, pw):
    _login(client, pw)
    before = _snapshot(conn)
    assert client.get("/equipe/carro/9999").status_code == 404
    for path in FINANCE_POSTS:
        r = client.post(f"/equipe/carro/9999/{path}", follow_redirects=False,
                        data={"cost": "10", "sale_price": "10", "description": "x", "amount": "10"})
        assert r.status_code == 404, path
    # a despesa 1 existe (é do carro A): o 404 do carro vem antes de qualquer exclusão
    assert _snapshot(conn) == before


@pytest.mark.parametrize("pw", [ADMIN_PW, STAFF_PW])
def test_idor_cannot_delete_expense_of_another_car(client, roles, two_cars, conn, pw):
    _login(client, pw)
    a, b = two_cars["a"], two_cars["b"]
    before = _snapshot(conn)
    r = client.post(f"/equipe/carro/{a}/despesas/{two_cars['exp_b']}/excluir", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/equipe/carro/{a}?msg=invalid"
    r = client.post(f"/equipe/carro/{b}/despesas/{two_cars['exp_a']}/excluir", follow_redirects=False)
    assert r.headers["location"] == f"/equipe/carro/{b}?msg=invalid"
    assert _snapshot(conn) == before
    # pelo carro certo, exclui só a dele
    r = client.post(f"/equipe/carro/{a}/despesas/{two_cars['exp_a']}/excluir", follow_redirects=False)
    assert r.headers["location"] == f"/equipe/carro/{a}?msg=saved"
    assert _snapshot(conn)[1] == [(two_cars["exp_b"], b, "Pneu B", 20_000)]
    # excluir de novo (replay) não derruba nada
    r = client.post(f"/equipe/carro/{a}/despesas/{two_cars['exp_a']}/excluir", follow_redirects=False)
    assert r.headers["location"].endswith("msg=invalid")


def test_car_page_lists_only_its_own_expenses(client, two_cars):
    html = client.get(f"/equipe/carro/{two_cars['a']}").text
    assert "Lavagem A" in html and "Pneu B" not in html
    assert f"/despesas/{two_cars['exp_b']}/excluir" not in html


def test_delete_expense_needs_post(client, two_cars, conn):
    r = client.get(f"/equipe/carro/{two_cars['a']}/despesas/{two_cars['exp_a']}/excluir", follow_redirects=False)
    assert r.status_code == 405
    assert _count(conn, "car_expenses") == 2


XSS = '<script>alert(1)</script>"><img src=x onerror=alert(2)>\'{{7*7}}'


def test_xss_in_description_is_escaped(client, two_cars, conn):
    a = two_cars["a"]
    r = client.post(f"/equipe/carro/{a}/despesas", data={"description": XSS, "amount": "10"},
                    follow_redirects=False)
    assert r.headers["location"].endswith("msg=saved")
    conn.commit()
    stored = conn.execute("SELECT description FROM car_expenses ORDER BY id DESC").fetchone()[0]
    assert stored == XSS  # guarda como digitado; o escape é na saída
    html = client.get(f"/equipe/carro/{a}").text
    assert "<script>alert(1)" not in html and "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "49" not in re.findall(r"exp-desc\">([^<]*)<", html)[0]  # sem SSTI: {{7*7}} fica literal
    # dentro de atributos (data-confirm / aria-label) as aspas também são escapadas
    for attr in re.findall(r'(?:data-confirm|aria-label)="([^"]*)"', html):
        assert "<" not in attr and ">" not in attr


def test_xss_in_car_name_is_escaped_everywhere(client, conn, car):
    name = '<img src=x onerror=alert(1)> UNO "2010" <b>x</b>'
    run_poll(conn, [car("1", name), car("2", "BMW X1 2020")])
    cid = _sell(conn, "1", db.to_iso(db.now()), cost_cents=100)
    for path in ("/equipe/vendas", f"/equipe/carro/{cid}", "/equipe"):
        html = client.get(path).text
        assert "<img src=x" not in html and "<b>x</b>" not in html, path
    assert "&lt;img src=x" in client.get("/equipe/vendas").text


@pytest.mark.parametrize("query", ["msg=<script>alert(1)</script>", "msg=saved%22%3E%3Cscript%3E", "msg=None"])
def test_msg_param_is_allowlisted(client, two_cars, query):
    html = client.get(f"/equipe/carro/{two_cars['a']}?{query}").text
    assert "<script>alert(1)" not in html
    assert 'class="flash' not in html


@pytest.mark.parametrize("mes", ['"><script>alert(1)</script>', "2026-13", "2026-00", "1999-12", "2026-1",
                                 "2026-10-01", "abcd-ef", "-1", "", " 2026-10 ", "2026-10;DROP TABLE cars"])
def test_bad_month_never_breaks_or_reflects(client, two_cars, mes):
    r = client.get("/equipe/vendas", params={"mes": mes})
    assert r.status_code == 200
    assert "<script>alert(1)" not in r.text and "DROP TABLE" not in r.text
    assert not BAD_TEXT.search(r.text)


@pytest.mark.parametrize("length, ok", [(1, True), (200, True), (201, False), (5000, False)])
def test_description_length_limit(client, two_cars, conn, length, ok):
    a = two_cars["a"]
    r = client.post(f"/equipe/carro/{a}/despesas", data={"description": "x" * length, "amount": "10"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/equipe/carro/{a}?msg={'saved' if ok else 'invalid'}"
    assert _count(conn, "car_expenses") == (3 if ok else 2)
    assert client.get(f"/equipe/carro/{a}").status_code == 200


@pytest.mark.parametrize("description", ["", "   ", "\t\n", " "])
def test_blank_description_is_invalid(client, two_cars, conn, description):
    r = client.post(f"/equipe/carro/{two_cars['a']}/despesas", data={"description": description, "amount": "10"},
                    follow_redirects=False)
    assert r.headers["location"].endswith("msg=invalid")
    assert _count(conn, "car_expenses") == 2


def test_description_whitespace_is_collapsed(client, two_cars, conn):
    client.post(f"/equipe/carro/{two_cars['a']}/despesas",
                data={"description": "  Troca   de\n óleo \t", "amount": "10"})
    conn.commit()
    assert conn.execute("SELECT description FROM car_expenses ORDER BY id DESC").fetchone()[0] == "Troca de óleo"


def test_sql_injection_in_fields_is_inert(client, two_cars, conn):
    a = two_cars["a"]
    evil = "x'); DROP TABLE car_expenses; --"
    client.post(f"/equipe/carro/{a}/despesas", data={"description": evil, "amount": "10"})
    r = client.post(f"/equipe/carro/{a}/custo", data={"cost": "1; UPDATE cars SET cost_cents = 1"},
                    follow_redirects=False)
    assert r.headers["location"].endswith("msg=invalid")
    conn.commit()
    assert _count(conn, "car_expenses") == 3
    assert conn.execute("SELECT description FROM car_expenses ORDER BY id DESC").fetchone()[0] == evil
    assert conn.execute("SELECT COUNT(*) FROM cars WHERE cost_cents IS NOT NULL").fetchone()[0] == 0


@pytest.mark.parametrize("field, path, raw", [
    ("cost", "custo", "-1"), ("cost", "custo", "abc"), ("cost", "custo", "1234.56"), ("cost", "custo", "R$ 45 mil"),
    ("cost", "custo", "100000000,01"), ("sale_price", "venda", "0"), ("sale_price", "venda", "0,00"),
    ("sale_price", "venda", "-10"), ("sale_price", "venda", "1e9"),
])
def test_invalid_amount_changes_nothing(client, two_cars, conn, field, path, raw):
    a = two_cars["a"]
    _set(conn, "1", cost_cents=111, sale_price_cents=222)
    r = client.post(f"/equipe/carro/{a}/{path}", data={field: raw}, follow_redirects=False)
    assert r.headers["location"] == f"/equipe/carro/{a}?msg=invalid"
    conn.commit()
    row = conn.execute("SELECT cost_cents, sale_price_cents FROM cars WHERE id = ?", (a,)).fetchone()
    assert tuple(row) == (111, 222)


@pytest.mark.parametrize("amount", ["0", "0,00", "-5", "", "dez", "1234.56"])
def test_invalid_expense_amount(client, two_cars, conn, amount):
    r = client.post(f"/equipe/carro/{two_cars['a']}/despesas", data={"description": "Pneu", "amount": amount},
                    follow_redirects=False)
    assert r.headers["location"].endswith("msg=invalid")
    assert _count(conn, "car_expenses") == 2


def test_cost_zero_is_allowed_and_differs_from_empty(client, two_cars, conn):
    a = two_cars["a"]
    assert client.post(f"/equipe/carro/{a}/custo", data={"cost": "0"},
                       follow_redirects=False).headers["location"].endswith("msg=saved")
    conn.commit()
    assert conn.execute("SELECT cost_cents FROM cars WHERE id = ?", (a,)).fetchone()[0] == 0
    assert 'value="0"' in client.get(f"/equipe/carro/{a}").text
    client.post(f"/equipe/carro/{a}/custo", data={"cost": ""})
    conn.commit()
    assert conn.execute("SELECT cost_cents FROM cars WHERE id = ?", (a,)).fetchone()[0] is None


def test_huge_ids_do_not_500(two_cars, site):
    from fastapi.testclient import TestClient

    from app.main import app

    big = "9" * 20
    with TestClient(app, raise_server_exceptions=False) as c:
        codes = [c.get(f"/equipe/carro/{big}").status_code,
                 c.post(f"/equipe/carro/{big}/custo", data={"cost": "1"}).status_code,
                 c.post(f"/equipe/carro/{two_cars['a']}/despesas/{big}/excluir", follow_redirects=False).status_code]
    assert 500 not in codes, codes


@pytest.mark.parametrize("path", ["/equipe/carro/abc", "/equipe/carro/1.5", "/equipe/carro/1e3"])
def test_non_numeric_id_is_422(client, path):
    assert client.get(path).status_code == 422


@pytest.mark.parametrize("cid", [0, -1])
def test_zero_and_negative_id_is_404(client, two_cars, cid):
    assert client.get(f"/equipe/carro/{cid}").status_code == 404
    assert client.post(f"/equipe/carro/{cid}/custo", data={"cost": "1"}, follow_redirects=False).status_code == 404


# =======================================================================================
# 2) Cálculos (contas refeitas à mão)
# =======================================================================================

# ---- parse de dinheiro ----------------------------------------------------------------

@pytest.mark.parametrize("raw, cents", [
    ("1.234,56", 123_456),             # milhar com ponto + centavos
    ("1234,56", 123_456),
    ("1.234", 123_400),                # ATENÇÃO: ponto é SEMPRE milhar → R$ 1.234,00 (nunca R$ 1,23)
    ("1.234.567,89", 123_456_789),
    ("1,5", 150),                      # 1 casa decimal = 50 centavos
    ("0,05", 5),
    ("00012", 1_200),
    ("  45000  ", 4_500_000),
    (" 45000 ", 4_500_000),  # espaço não separável (copiado de planilha)
    ("R$45000", 4_500_000),
    ("R$ 45.000,00", 4_500_000),
    ("r$ 10", 1_000),
    ("R$ 1.234,56", 123_456),     # exatamente o que o filtro brl imprime
    ("20.000.000,00", 2_000_000_000),
])
def test_parse_money_accepts(raw, cents):
    assert finance.parse_money(raw) == cents


@pytest.mark.parametrize("raw", [
    "1234.56",        # formato americano: recusado (não vira 1.234,56 nem 123.456)
    "1.234.56", "12.34", "1.2345", "1,234", "1.234,567", "1,2,3",
    "R$ 45 mil", "45 mil", "45k", "45 000", "4 5", "R$ R$ 5",
    "5,", ",50", ".", ",", "+5", "-0", "- 5", "1e3", "0x10", "١٢٣abc", "5%", "R$ -5",
    "20.000.000,01", "100.000.000,00", "999999999999",
])
def test_parse_money_rejects(raw):
    with pytest.raises(ValueError):
        finance.parse_money(raw)


def test_parse_money_ambiguous_dot_documented():
    """'1234.56' (ponto decimal) é recusado, mas '45.500' digitado por quem queria R$ 45,50
    vira R$ 45.500,00 — o contrato manda ponto = milhar. Fica registrado no relatório."""
    with pytest.raises(ValueError):
        finance.parse_money("1234.56")
    with pytest.raises(ValueError):
        finance.parse_money("45.50")
    assert finance.parse_money("45.500") == 4_550_000


@pytest.mark.parametrize("cents", [0, 5, 99, 100, 150, 4_500_000, 4_500_050, 123_456_789, 2_000_000_000])
def test_form_value_round_trips(client, conn, car, cents):
    """O valor que a página devolve no campo tem de ser aceito de volta sem mudar de valor."""
    run_poll(conn, [car("1", "BMW X1 2020")])
    cid = _set(conn, "1", cost_cents=cents)
    html = client.get(f"/equipe/carro/{cid}").text
    shown = re.search(r'id="cost"[^>]*value="([^"]*)"', html, re.S).group(1)
    assert finance.parse_money(shown) == cents


# ---- virada de mês no fuso de São Paulo ------------------------------------------------

@pytest.fixture
def five(conn, car):
    """5 carros no ar, entrada em 01/09/2026 10:00 BRT."""
    run_poll(conn, [
        car("1", "CHEVROLET TRACKER 1.0 TURBO LTZ 2026", price=12_000_000),
        car("2", "CHEVROLET ONIX 1.0 LT 2023", price=7_000_000),
        car("3", "BMW 320i 2.0 M SPORT 2022", price=25_000_000),
        car("4", "LAND ROVER RANGE ROVER SPORT 3.0 2023", price=80_000_000),
        car("5", "FIAT UNO MILLE 2010", price=None, photo=None),
    ])
    conn.execute("UPDATE cars SET first_seen = '2026-09-01T10:00:00-03:00'")
    conn.commit()
    return conn


@pytest.mark.parametrize("a, b", [
    ("2026-09-30T23:30:00-03:00", "2026-10-01T00:10:00-03:00"),   # gravado em BRT
    ("2026-10-01T02:30:00+00:00", "2026-10-01T03:10:00+00:00"),   # os mesmos instantes em UTC
    ("2026-10-01T04:30:00+02:00", "2026-10-01T05:10:00+02:00"),   # idem, outro offset
])
def test_month_turn_in_sao_paulo(five, a, b):
    conn = five
    sep_id = _sell(conn, "1", a)   # 30/09 23:30 BRT
    oct_id = _sell(conn, "2", b)   # 01/10 00:10 BRT
    sep = finance.sales_view(conn, "2026-09", TODAY)
    out = finance.sales_view(conn, "2026-10", TODAY)
    assert [s["car"]["id"] for s in sep["sales"]] == [sep_id]
    assert [s["car"]["id"] for s in out["sales"]] == [oct_id]
    assert sep["summary"]["revenue_cents"] == 12_000_000 and out["summary"]["revenue_cents"] == 7_000_000
    s, o = sep["sales"][0]["sold_at"], out["sales"][0]["sold_at"]
    assert (s.day, s.month, s.hour, s.minute) == (30, 9, 23, 30) and s.utcoffset().total_seconds() == -3 * 3600
    assert (o.day, o.month, o.hour, o.minute) == (1, 10, 0, 10)
    # 01/09 → 30/09 = 29 dias; 01/09 → 01/10 = 30 dias (datas locais, não diferença de horas)
    assert sep["sales"][0]["days_to_sell"] == 29 and out["sales"][0]["days_to_sell"] == 30
    # histórico de outubro: setembro e outubro com 1 venda cada
    assert [(h["key"], h["sold_count"]) for h in out["history"]][-2:] == [("2026-09", 1), ("2026-10", 1)]


@pytest.mark.parametrize("clock, month", [
    (datetime(2026, 9, 30, 23, 30, tzinfo=SP), "2026-09"),
    (datetime(2026, 10, 1, 0, 10, tzinfo=SP), "2026-10"),
])
def test_real_sale_stores_offset_and_lands_in_right_month(conn, car, monkeypatch, clock, month):
    """Fluxo real: 2 coletas sem o carro, com o relógio na virada do mês."""
    stock = [car("1", "CHEVROLET ONIX 2023", price=7_000_000), car("2", "BMW X1 2022", price=20_000_000)]
    monkeypatch.setattr(sync, "now", lambda: datetime(2026, 9, 1, 10, 0, tzinfo=SP))
    run_poll(conn, stock)
    monkeypatch.setattr(sync, "now", lambda: clock)
    run_poll(conn, stock[:1])
    run_poll(conn, stock[:1])
    raw = conn.execute("SELECT sold_at FROM cars WHERE external_id = '2'").fetchone()[0]
    assert raw.endswith("-03:00") and datetime.fromisoformat(raw) == clock   # salvo COM offset
    other = "2026-10" if month == "2026-09" else "2026-09"
    assert [s["car"]["name"] for s in finance.sales_view(conn, month, TODAY)["sales"]] == ["BMW X1 2022"]
    assert finance.sales_view(conn, other, TODAY)["sales"] == []


def test_naive_sold_at_is_read_as_sao_paulo(five):
    cid = _sell(five, "1", "2026-09-30T23:30:00")   # sem offset (banco antigo/edição manual)
    assert [s["car"]["id"] for s in finance.sales_view(five, "2026-09", TODAY)["sales"]] == [cid]


def test_year_turn(five):
    _sell(five, "1", "2026-12-31T23:59:59-03:00")
    _sell(five, "2", "2027-01-01T00:00:00-03:00")
    today = date(2027, 1, 10)
    dec = finance.sales_view(five, "2026-12", today)
    jan = finance.sales_view(five, "2027-01", today)
    assert dec["summary"]["sold_count"] == 1 and jan["summary"]["sold_count"] == 1
    assert dec["month"] == {"key": "2026-12", "label": "Dezembro/2026", "prev": "2026-11", "next": "2027-01"}
    assert jan["month"] == {"key": "2027-01", "label": "Janeiro/2027", "prev": "2026-12", "next": None}
    assert [h["key"] for h in jan["history"]] == ["2026-08", "2026-09", "2026-10", "2026-11", "2026-12", "2027-01"]
    assert [h["label"] for h in jan["history"]] == ["ago", "set", "out", "nov", "dez", "jan"]
    assert [h["sold_count"] for h in jan["history"]] == [0, 0, 0, 0, 1, 1]


@pytest.mark.parametrize("mes", [None, "", "lixo", "2026-13", "2026-00", "1999-12", "26-10", "2026/10", "2026-1"])
def test_invalid_month_falls_back_to_current(five, mes):
    v = finance.sales_view(five, mes, TODAY)
    assert v["month"]["key"] == "2026-10" and v["month"]["next"] is None


def test_default_month_is_today_in_sao_paulo(five, monkeypatch):
    # 01/10 01:00 UTC ainda é 30/09 22:00 em São Paulo
    monkeypatch.setattr(db, "now", lambda: datetime(2026, 9, 30, 22, 0, tzinfo=SP))
    assert finance.sales_view(five)["month"]["key"] == "2026-09"


# ---- reaparecimento ---------------------------------------------------------------------

def test_reappearance_removes_sale_and_keeps_costs(conn, car):
    stock = [car("1", "CHEVROLET ONIX 2023", price=7_000_000), car("2", "BMW X1 2022", price=20_000_000)]
    run_poll(conn, stock)
    run_poll(conn, stock[:1])
    run_poll(conn, stock[:1])
    bmw = _id(conn, "2")
    today = db.now().date()
    assert finance.set_cost(conn, bmw, "150.000") and finance.set_sale_price(conn, bmw, "190.000")
    assert finance.add_expense(conn, bmw, "Funilaria", "2.500,00")
    v = finance.sales_view(conn, None, today)
    s = v["summary"]
    # 190.000 − 150.000 − 2.500 = 37.500
    assert (s["sold_count"], s["revenue_cents"], s["profit_cents"]) == (1, 19_000_000, 3_750_000)
    assert (s["in_stock"], s["invested_cents"]) == (1, 0)

    run_poll(conn, stock)  # voltou ao site
    v = finance.sales_view(conn, None, today)
    s = v["summary"]
    assert v["sales"] == [] and v["brands"] == []
    assert (s["sold_count"], s["revenue_cents"], s["cost_cents"], s["expenses_cents"], s["profit_cents"]) == \
        (0, 0, 0, 0, 0)
    assert s["margin_pct"] is None and s["avg_days_to_sell"] is None
    assert all(h["sold_count"] == 0 and h["revenue_cents"] == 0 for h in v["history"])
    # custo e despesa continuam no carro: agora contam como dinheiro parado no estoque
    assert (s["in_stock"], s["invested_cents"]) == (2, 15_000_000 + 250_000)
    cv = finance.car_view(conn, bmw)
    assert cv["car"]["sold"] is False and cv["car"]["active"] is True and cv["car"]["sold_at"] is None
    assert cv["car"]["cost_cents"] == 15_000_000 and len(cv["expenses"]) == 1


def test_resale_after_reappearance_does_not_reuse_old_manual_price(conn, car):
    stock = [car("1", "CHEVROLET ONIX 2023", price=7_000_000), car("2", "BMW X1 2022", price=20_000_000)]
    run_poll(conn, stock)
    run_poll(conn, stock[:1])
    run_poll(conn, stock[:1])
    bmw = _id(conn, "2")
    finance.set_sale_price(conn, bmw, "190.000")
    run_poll(conn, stock)                 # falso vendido: voltou
    _set(conn, "2", price_cents=18_000_000)  # loja baixou o preço no site
    run_poll(conn, stock[:1])
    run_poll(conn, stock[:1])             # agora vendeu de verdade
    sale = finance.sales_view(conn, None, db.now().date())["sales"][0]
    assert (sale["sale_cents"], sale["sale_is_estimate"]) == (18_000_000, True)


# ---- venda manual × preço do site, lucro negativo -------------------------------------------

def test_manual_sale_price_overrides_site_price(five):
    conn = five
    cid = _sell(conn, "1", "2026-10-10T18:00:00-03:00", cost_cents=10_000_000)   # site: R$ 120.000
    _expense(conn, cid, 300_000)                                                  # R$ 3.000
    s = finance.sales_view(conn, "2026-10", TODAY)
    sale = s["sales"][0]
    # preço do site: 120.000 − 100.000 − 3.000 = 17.000
    assert (sale["sale_cents"], sale["sale_is_estimate"], sale["profit_cents"]) == (12_000_000, True, 1_700_000)
    assert s["summary"]["margin_pct"] == 14.2          # 17.000 / 120.000 = 14,1666…%

    assert finance.set_sale_price(conn, cid, "110.000,00")
    s = finance.sales_view(conn, "2026-10", TODAY)
    sale = s["sales"][0]
    # venda real: 110.000 − 100.000 − 3.000 = 7.000
    assert (sale["sale_cents"], sale["sale_is_estimate"], sale["profit_cents"]) == (11_000_000, False, 700_000)
    assert s["summary"]["revenue_cents"] == 11_000_000 and s["summary"]["margin_pct"] == 6.4   # 6,3636…%
    assert s["brands"][0]["revenue_cents"] == 11_000_000
    cv = finance.car_view(conn, cid)
    assert cv["car"]["price_cents"] == 12_000_000 and cv["car"]["sale_price_cents"] == 11_000_000

    assert finance.set_sale_price(conn, cid, "")   # vazio = volta ao preço do site
    sale = finance.sales_view(conn, "2026-10", TODAY)["sales"][0]
    assert (sale["sale_cents"], sale["sale_is_estimate"]) == (12_000_000, True)


def test_manual_sale_price_rescues_sob_consulta(five):
    conn = five
    cid = _sell(conn, "5", "2026-10-10T18:00:00-03:00", cost_cents=1_000_000)   # sob consulta, custo R$ 10.000
    sale = finance.sales_view(conn, "2026-10", TODAY)["sales"][0]
    assert sale["sale_cents"] is None and sale["profit_cents"] is None and sale["sale_is_estimate"] is True
    finance.set_sale_price(conn, cid, "14.500")
    v = finance.sales_view(conn, "2026-10", TODAY)
    assert v["sales"][0]["profit_cents"] == 450_000                       # 14.500 − 10.000
    assert v["summary"]["revenue_cents"] == 1_450_000 and v["summary"]["margin_pct"] == 31.0   # 4.500/14.500


def test_negative_profit(five):
    conn = five
    a = _sell(conn, "1", "2026-10-10T18:00:00-03:00", cost_cents=10_000_000, sale_price_cents=9_000_000)
    _expense(conn, a, 250_000)
    v = finance.sales_view(conn, "2026-10", TODAY)
    # 90.000 − 100.000 − 2.500 = −12.500 ; margem = −12.500 / 90.000 = −13,888…%
    assert v["sales"][0]["profit_cents"] == -1_250_000
    assert v["summary"]["profit_cents"] == -1_250_000 and v["summary"]["margin_pct"] == -13.9
    assert v["history"][-1]["profit_cents"] == -1_250_000 and v["history"][-1]["spend_cents"] == 10_250_000

    # um lucro e um prejuízo no mesmo mês: 15.000 − 12.500 = +2.500 sobre 160.000 de faturamento
    _sell(conn, "2", "2026-10-11T18:00:00-03:00", cost_cents=5_500_000)   # site R$ 70.000 → +15.000
    s = finance.sales_view(conn, "2026-10", TODAY)["summary"]
    assert (s["revenue_cents"], s["cost_cents"], s["expenses_cents"], s["profit_cents"]) == \
        (16_000_000, 15_500_000, 250_000, 250_000)
    assert s["margin_pct"] == 1.6   # 2.500 / 160.000 = 1,5625%


def test_cost_zero_counts_as_known_cost(five):
    conn = five
    _sell(conn, "2", "2026-10-11T18:00:00-03:00", cost_cents=0)   # ganho de troca: custo zero
    s = finance.sales_view(conn, "2026-10", TODAY)["summary"]
    assert s["missing_cost"] == 0 and s["profit_cents"] == 7_000_000 and s["margin_pct"] == 100.0


def test_mixed_month_by_hand(five):
    """Mês com os 4 tipos: com custo+venda manual, com custo+preço do site, sem custo, sob consulta sem custo."""
    conn = five
    a = _sell(conn, "1", "2026-10-03T10:00:00-03:00", cost_cents=9_500_000, sale_price_cents=11_800_000)
    b = _sell(conn, "3", "2026-10-07T10:00:00-03:00", cost_cents=21_000_000)       # site R$ 250.000
    c = _sell(conn, "4", "2026-10-09T10:00:00-03:00")                              # site R$ 800.000, sem custo
    d = _sell(conn, "5", "2026-10-12T10:00:00-03:00")                              # sob consulta, sem custo
    _expense(conn, a, 120_050)      # R$ 1.200,50
    _expense(conn, a, 34_950)       # R$   349,50  → A: 1.550,00
    _expense(conn, b, 500_000)      # R$ 5.000,00
    _expense(conn, c, 999_900)      # sem custo: fora do mês
    _set(conn, "2", cost_cents=5_000_000)                    # em estoque
    _expense(conn, _id(conn, "2"), 80_000)

    v = finance.sales_view(conn, "2026-10", TODAY)
    s = v["summary"]
    assert s["sold_count"] == 4 and s["missing_cost"] == 2
    assert s["revenue_cents"] == 11_800_000 + 25_000_000 + 80_000_000 + 0 == 116_800_000
    assert s["cost_cents"] == 9_500_000 + 21_000_000 == 30_500_000
    assert s["expenses_cents"] == 155_000 + 500_000 == 655_000
    # A: 118.000 − 95.000 − 1.550 = 21.450 ; B: 250.000 − 210.000 − 5.000 = 35.000
    assert s["profit_cents"] == 2_145_000 + 3_500_000 == 5_645_000
    # margem sobre o faturamento dos carros COM custo: 56.450 / 368.000 = 15,339…%
    assert s["margin_pct"] == 15.3
    # identidade do Forja: lucro = faturamento dos com custo − custo − despesas
    assert s["profit_cents"] == (11_800_000 + 25_000_000) - s["cost_cents"] - s["expenses_cents"]
    assert s["in_stock"] == 1 and s["invested_cents"] == 5_080_000
    # dias: 01/09 → 03/10 = 32, 07/10 = 36, 09/10 = 38, 12/10 = 41 → média 36,75 → 37
    assert [x["days_to_sell"] for x in v["sales"]] == [41, 38, 36, 32]
    assert s["avg_days_to_sell"] == 37

    assert [x["car"]["id"] for x in v["sales"]] == [d, c, b, a]
    by = {x["car"]["id"]: x for x in v["sales"]}
    assert by[c]["profit_cents"] is None and by[c]["expenses_cents"] == 999_900 and by[c]["cost_cents"] is None
    assert by[d]["sale_cents"] is None and by[d]["profit_cents"] is None

    # marcas: 1 venda cada (25%); desempate por faturamento: Land Rover 800k, BMW 250k, Chevrolet 118k, Fiat 0
    assert [(g["brand"], g["count"], g["revenue_cents"], g["share_pct"]) for g in v["brands"]] == [
        ("Land Rover", 1, 80_000_000, 25.0), ("BMW", 1, 25_000_000, 25.0),
        ("Chevrolet", 1, 11_800_000, 25.0), ("Fiat", 1, 0, 25.0)]
    assert sum(g["count"] for g in v["brands"]) == s["sold_count"]
    assert sum(g["revenue_cents"] for g in v["brands"]) == s["revenue_cents"]
    assert v["history"][-1] == {"key": "2026-10", "label": "out", "sold_count": 4, "revenue_cents": 116_800_000,
                                "spend_cents": 31_155_000, "profit_cents": 5_645_000}


def test_sob_consulta_with_cost_keeps_totals_consistent(five):
    conn = five
    cid = _sell(conn, "5", "2026-10-12T10:00:00-03:00", cost_cents=1_000_000)
    _expense(conn, cid, 50_000)
    s = finance.sales_view(conn, "2026-10", TODAY)["summary"]
    assert s["profit_cents"] == s["revenue_cents"] - s["cost_cents"] - s["expenses_cents"]


def test_sob_consulta_with_cost_current_numbers(five):
    """Vendido sob consulta com custo e sem venda digitada: fora dos totais, contado em missing_sale."""
    conn = five
    cid = _sell(conn, "5", "2026-10-12T10:00:00-03:00", cost_cents=1_000_000)
    _expense(conn, cid, 50_000)
    v = finance.sales_view(conn, "2026-10", TODAY)
    s = v["summary"]
    assert (s["revenue_cents"], s["cost_cents"], s["expenses_cents"], s["missing_cost"]) == (0, 0, 0, 0)
    assert s["missing_sale"] == 1 and s["profit_cents"] == 0
    assert s["margin_pct"] is None and v["sales"][0]["profit_cents"] is None
    assert (v["sales"][0]["cost_cents"], v["sales"][0]["expenses_cents"]) == (1_000_000, 50_000)
    assert v["history"][-1]["spend_cents"] == 0


# ---- mês sem vendas, marcas, histórico, valores grandes ---------------------------------------

def test_month_without_sales_among_months_with_sales(five):
    conn = five
    _sell(conn, "1", "2026-08-10T10:00:00-03:00", cost_cents=10_000_000)
    _sell(conn, "2", "2026-10-10T10:00:00-03:00", cost_cents=6_000_000)
    _set(conn, "3", cost_cents=20_000_000)
    v = finance.sales_view(conn, "2026-09", TODAY)
    s = v["summary"]
    assert v["sales"] == [] and v["brands"] == []
    assert (s["sold_count"], s["revenue_cents"], s["cost_cents"], s["expenses_cents"], s["profit_cents"],
            s["missing_cost"]) == (0, 0, 0, 0, 0, 0)
    assert s["margin_pct"] is None and s["avg_days_to_sell"] is None
    assert (s["in_stock"], s["invested_cents"]) == (3, 20_000_000)   # estoque é "hoje", não do mês
    assert v["month"] == {"key": "2026-09", "label": "Setembro/2026", "prev": "2026-08", "next": "2026-10"}
    # history termina no mês SELECIONADO (setembro), não em outubro
    assert [(h["key"], h["sold_count"]) for h in v["history"]] == [
        ("2026-04", 0), ("2026-05", 0), ("2026-06", 0), ("2026-07", 0), ("2026-08", 1), ("2026-09", 0)]
    assert v["history"][4] == {"key": "2026-08", "label": "ago", "sold_count": 1, "revenue_cents": 12_000_000,
                               "spend_cents": 10_000_000, "profit_cents": 2_000_000}


def test_future_month_is_empty_and_has_no_next(five):
    v = finance.sales_view(five, "2027-03", TODAY)
    assert v["month"]["key"] == "2027-03" and v["month"]["next"] is None and v["sales"] == []


def test_history_with_gaps_and_out_of_window_sales(five):
    conn = five
    _sell(conn, "1", "2026-04-30T23:59:00-03:00", cost_cents=1)         # 1 minuto antes da janela → fora
    _sell(conn, "2", "2026-05-01T00:00:00-03:00", cost_cents=6_000_000)  # 1º minuto da janela
    _sell(conn, "3", "2026-10-31T23:59:59-03:00")                        # último segundo, sem custo
    _sell(conn, "4", "2026-11-01T00:00:00-03:00", cost_cents=1)         # depois da janela → fora
    h = finance.sales_view(conn, "2026-10", date(2026, 11, 5))["history"]
    assert [x["key"] for x in h] == ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    assert [x["label"] for x in h] == ["mai", "jun", "jul", "ago", "set", "out"]
    assert [x["sold_count"] for x in h] == [1, 0, 0, 0, 0, 1]
    assert h[0] == {"key": "2026-05", "label": "mai", "sold_count": 1, "revenue_cents": 7_000_000,
                    "spend_cents": 6_000_000, "profit_cents": 1_000_000}
    for empty in h[1:5]:
        assert (empty["revenue_cents"], empty["spend_cents"], empty["profit_cents"]) == (0, 0, 0)
    assert h[5] == {"key": "2026-10", "label": "out", "sold_count": 1, "revenue_cents": 25_000_000,
                    "spend_cents": 0, "profit_cents": 0}   # sem custo: fatura, não gasta nem lucra


def test_brand_ranking_count_then_revenue_then_name(conn, car):
    run_poll(conn, [
        car("1", "FIAT ARGO 2021", price=6_000_000), car("2", "FIAT MOBI 2020", price=4_000_000),
        car("3", "BMW X1 2022", price=20_000_000),
        car("4", "AUDI A3 2020", price=10_000_000), car("5", "VOLVO XC40 2020", price=10_000_000),
        car("6", "JEEP COMPASS 2021", price=5_000_000), car("7", "JEEP RENEGADE 2020", price=5_000_000),
    ])
    for ext in "1234567":
        _sell(conn, ext, f"2026-10-0{ext}T12:00:00-03:00")
    brands = finance.sales_view(conn, "2026-10", TODAY)["brands"]
    # 2 vendas: Fiat (100k) = Jeep (100k) → empate total, ordem alfabética; 1 venda: BMW 200k, depois
    # Audi = Volvo (100k) → alfabética
    assert [(b["brand"], b["count"], b["revenue_cents"]) for b in brands] == [
        ("Fiat", 2, 10_000_000), ("Jeep", 2, 10_000_000),
        ("BMW", 1, 20_000_000), ("Audi", 1, 10_000_000), ("Volvo", 1, 10_000_000)]
    assert [b["share_pct"] for b in brands] == [28.6, 28.6, 14.3, 14.3, 14.3]   # 2/7 e 1/7


def test_brand_ranking_is_stable_between_calls(conn, car):
    run_poll(conn, [car("1", "VOLVO XC40 2020"), car("2", "AUDI A3 2020"), car("3", "BMW X1 2022")])
    for ext in "123":
        _sell(conn, ext, "2026-10-05T12:00:00-03:00")
    first = [b["brand"] for b in finance.sales_view(conn, "2026-10", TODAY)["brands"]]
    assert first == ["Audi", "BMW", "Volvo"]
    assert all([b["brand"] for b in finance.sales_view(conn, "2026-10", TODAY)["brands"]] == first for _ in range(3))


def test_sales_order_ties_broken_by_id(five):
    for ext in "123":
        _sell(five, ext, "2026-10-05T12:00:00-03:00")
    assert [s["car"]["id"] for s in finance.sales_view(five, "2026-10", TODAY)["sales"]] == \
        [_id(five, "3"), _id(five, "2"), _id(five, "1")]


def test_big_values_are_exact(five):
    conn = five
    top = finance.MAX_MONEY_CENTS   # R$ 20.000.000,00 (limite do formulário; cabe em int4)
    assert top == 2_000_000_000 < 2**31
    a = _id(conn, "1")
    assert finance.set_cost(conn, a, "20.000.000") and finance.set_sale_price(conn, a, "20000000,00")
    assert finance.add_expense(conn, a, "Importação", "19.999.999,99")
    assert not finance.set_cost(conn, _id(conn, "2"), "20.000.000,01")
    assert not finance.set_sale_price(conn, _id(conn, "2"), "100.000.000")
    assert not finance.add_expense(conn, _id(conn, "2"), "Importação", "20.000.000,01")
    _sell(conn, "1", "2026-10-02T10:00:00-03:00")
    _sell(conn, "3", "2026-10-03T10:00:00-03:00", cost_cents=top - 1, sale_price_cents=top)
    _sell(conn, "4", "2026-10-04T10:00:00-03:00", cost_cents=1, sale_price_cents=top)
    s = finance.sales_view(conn, "2026-10", TODAY)["summary"]
    assert s["revenue_cents"] == 6_000_000_000
    assert s["cost_cents"] == 3_999_999_999 + 1 and s["expenses_cents"] == 1_999_999_999
    # 6.000.000.000 − 4.000.000.000 − 1.999.999.999 = 1 centavo
    assert s["profit_cents"] == 1 and isinstance(s["profit_cents"], int)
    assert s["margin_pct"] == 0.0


def test_days_to_sell_uses_listed_at_and_never_negative(five):
    conn = five
    _sell(conn, "1", "2026-10-11T10:00:00-03:00", listed_at="2026-08-12")   # 12/08 → 11/10 = 60 dias
    _sell(conn, "2", "2026-10-11T10:00:00-03:00", listed_at="2026-12-25")   # foto mais nova que a entrada: usa 01/09 → 40
    _sell(conn, "3", "2026-08-20T10:00:00-03:00")                           # vendido "antes" da entrada → 0
    by = {s["car"]["name"][:9]: s for s in finance.sales_view(conn, "2026-10", TODAY)["sales"]}
    assert by["CHEVROLET"]["days_to_sell"] in (60, 40)
    days = sorted(s["days_to_sell"] for s in finance.sales_view(conn, "2026-10", TODAY)["sales"])
    assert days == [40, 60]
    assert finance.sales_view(conn, "2026-10", TODAY)["summary"]["avg_days_to_sell"] == 50
    assert finance.sales_view(conn, "2026-08", TODAY)["sales"][0]["days_to_sell"] == 0


def test_demo_cars_never_count(five):
    conn = five
    ts = "2026-10-05T12:00:00-03:00"
    conn.execute("""INSERT INTO cars (external_id, name, name_key, url, posted, active, first_seen, last_seen,
                                      sold_at, price_cents, cost_cents, brand)
                    VALUES ('demo-9', 'BMW DEMO', 'bmw demo', 'u', 1, 0, ?, ?, ?, 900000, 100000, 'BMW')""",
                 (ts, ts, ts))
    conn.commit()
    _expense(conn, _id(conn, "demo-9"), 5_000)
    v = finance.sales_view(conn, "2026-10", TODAY)
    assert v["sales"] == [] and v["brands"] == [] and v["summary"]["revenue_cents"] == 0
    assert v["summary"]["in_stock"] == 5 and v["summary"]["invested_cents"] == 0


def test_inactive_unsold_car_is_neither_sale_nor_stock(five):
    """1 falta só (ou desativado sem sold_at) não é venda; ativo com 1 falta ainda é estoque."""
    conn = five
    _set(conn, "1", missing_count=1, cost_cents=1_000)
    _set(conn, "2", active=0, cost_cents=2_000)   # fora do site, sem sold_at
    s = finance.sales_view(conn, "2026-10", TODAY)["summary"]
    assert s["sold_count"] == 0 and s["in_stock"] == 4 and s["invested_cents"] == 1_000


def test_car_view_totals_by_hand(five):
    conn = five
    cid = _id(conn, "3")   # BMW, site R$ 250.000, em estoque
    v = finance.car_view(conn, cid)
    assert v["totals"] == {"expenses_cents": 0, "sale_cents": 25_000_000, "sale_is_estimate": True,
                           "profit_cents": None}
    finance.set_cost(conn, cid, "200.000")
    finance.add_expense(conn, cid, "IPVA", "8.123,45")
    finance.add_expense(conn, cid, "Polimento", "350")
    v = finance.car_view(conn, cid)
    # lucro previsto: 250.000 − 200.000 − 8.473,45 = 41.526,55
    assert v["totals"] == {"expenses_cents": 847_345, "sale_cents": 25_000_000, "sale_is_estimate": True,
                           "profit_cents": 4_152_655}
    assert sorted(e["amount_cents"] for e in v["expenses"]) == [35_000, 812_345]
    assert all(e["created_at"].tzinfo is not None for e in v["expenses"])
    assert v["car"]["brand"] == "BMW" and v["car"]["sold"] is False


# =======================================================================================
# 3) Templates com dados reais
# =======================================================================================

@pytest.fixture
def scenario(client, conn, car):
    """Devolve função que monta o cenário pedido e retorna os ids relevantes."""
    def build(kind):
        this_month = db.to_iso(db.now())
        run_poll(conn, [
            car("1", "CHEVROLET ONIX 1.0 LT 2023", price=7_000_000),
            car("2", "BMW 320i 2.0 M SPORT 2022", price=25_000_000),
            car("3", "FIAT UNO MILLE 2010", price=None, photo=None),     # sob consulta e sem foto
            car("4", "CARRO SEM MARCA CONHECIDA", price=3_000_000, photo=None),
        ])
        ids = {ext: _id(conn, ext) for ext in "1234"}
        if kind == "sem_vendas":
            pass
        elif kind == "sem_custo":
            _sell(conn, "2", this_month)
        elif kind == "sob_consulta":
            _sell(conn, "3", this_month)
        elif kind == "sob_consulta_com_custo":
            _sell(conn, "3", this_month, cost_cents=500_000)
            _expense(conn, ids["3"], 10_000)
        elif kind == "sem_foto":
            _sell(conn, "4", this_month, cost_cents=2_000_000)
        elif kind == "prejuizo":
            _sell(conn, "1", this_month, cost_cents=8_000_000, sale_price_cents=6_900_050)
            _expense(conn, ids["1"], 150_000, "Funilaria")
        elif kind == "tudo":
            _sell(conn, "1", this_month, cost_cents=8_000_000, sale_price_cents=6_900_050)
            _expense(conn, ids["1"], 150_000, "Funilaria")
            _sell(conn, "2", this_month)
            _sell(conn, "3", this_month, cost_cents=0)
            _set(conn, "4", cost_cents=2_000_000)
            _expense(conn, ids["4"], 30_000, "Lavagem")
        return ids
    return build


SCENARIOS = ["sem_vendas", "sem_custo", "sob_consulta", "sob_consulta_com_custo", "sem_foto", "prejuizo", "tudo"]


@pytest.mark.parametrize("who", ["admin", "equipe", "sem_login_local"])
@pytest.mark.parametrize("kind", SCENARIOS)
def test_pages_render_clean(client, scenario, monkeypatch, kind, who):
    ids = scenario(kind)
    if who != "sem_login_local":
        monkeypatch.setenv("APP_PASSWORD", ADMIN_PW)
        monkeypatch.setenv("STAFF_PASSWORD", STAFF_PW)
        monkeypatch.setenv("SESSION_SECRET", "segredo-" + "q" * 30)
        _login(client, ADMIN_PW if who == "admin" else STAFF_PW)
    pages = ["/equipe/vendas", "/equipe/vendas?mes=2020-01", "/equipe"]
    pages += [f"/equipe/carro/{i}" for i in ids.values()]
    pages += [f"/equipe/carro/{ids['1']}?msg=saved", f"/equipe/carro/{ids['1']}?msg=invalid"]
    for path in pages:
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code)
        bad = BAD_TEXT.search(r.text)
        assert not bad, (path, r.text[max(0, bad.start() - 80):bad.end() + 80])
        assert "{{" not in r.text and "{%" not in r.text, path
        assert r.text.count("<svg") == r.text.count("</svg>")


def test_page_no_sales_message(client, scenario):
    scenario("sem_vendas")
    html = client.get("/equipe/vendas").text
    assert "Nenhuma venda em" in html and "Marca que mais vendeu" not in html
    assert "margem —" in html and "sem valor de compra" not in html
    assert "Em estoque hoje: <strong>4</strong> carros" in html


def test_page_sold_without_cost(client, scenario):
    ids = scenario("sem_custo")
    html = client.get("/equipe/vendas").text
    assert "1 carro sem valor de compra" in html and "informar custo" in html
    assert "R$ 250.000" in html and "preço do site" in html
    assert "Marca que mais vendeu" in html and ">BMW<" in html
    car_html = client.get(f"/equipe/carro/{ids['2']}").text
    assert "não informado" in car_html and "informe o custo" in car_html and "Vendido em" in car_html
    assert 'name="sale_price"' in car_html            # carro vendido: mostra o campo de venda real
    assert 'name="sale_price"' not in client.get(f"/equipe/carro/{ids['1']}").text   # em estoque: não


def test_page_sob_consulta(client, scenario):
    ids = scenario("sob_consulta")
    html = client.get("/equipe/vendas").text
    assert "FIAT UNO MILLE 2010" in html and "photo-placeholder" in html
    car_html = client.get(f"/equipe/carro/{ids['3']}").text
    assert "sob consulta" in car_html and "photo-placeholder" in car_html and "<img" not in car_html.split("<main")[-1]


def test_page_negative_profit(client, scenario):
    ids = scenario("prejuizo")
    # 69.000,50 − 80.000 − 1.500 = −12.499,50 ; margem −12.499,50/69.000,50 = −18,1%
    html = client.get("/equipe/vendas").text
    assert "−R$ 12.499,50" in html and "prejuízo" in html and "margem -18,1%" in html
    assert "R$ 69.000,50" in html and "is-neg" in html
    assert "-R$" not in html and "R$ -" not in html      # sinal sempre na frente, com o traço tipográfico
    car_html = client.get(f"/equipe/carro/{ids['1']}").text
    assert "−R$ 12.499,50" in car_html and "prejuízo" in car_html and "Funilaria" in car_html
    assert 'value="80.000"' in car_html and 'value="69.000,50"' in car_html


def test_page_everything_numbers(client, scenario):
    ids = scenario("tudo")
    html = client.get("/equipe/vendas").text
    # faturamento: 69.000,50 + 250.000 + 0 = 319.000,50 ; gasto: 80.000 + 0 + 1.500 = 81.500
    # lucro (só com custo e com venda): −12.499,50
    assert "R$ 319.000,50" in html and "R$ 81.500" in html and "−R$ 12.499,50" in html
    assert "1 carro sem valor de compra" in html
    assert "Em estoque hoje: <strong>1</strong> carro ·" in html and "R$ 20.300" in html
    team = client.get("/equipe").text
    assert "Custo <strong>R$ 20.000</strong>" in team and "Despesas <strong>R$ 300</strong>" in team
    assert "Investido <strong>R$ 20.300</strong>" in team
    assert f'href="/equipe/carro/{ids["4"]}"' in team


def test_team_tabs_and_admin_nav(client, scenario, monkeypatch):
    scenario("tudo")
    monkeypatch.setenv("APP_PASSWORD", ADMIN_PW)
    monkeypatch.setenv("STAFF_PASSWORD", STAFF_PW)
    monkeypatch.setenv("SESSION_SECRET", "segredo-" + "q" * 30)
    _login(client, ADMIN_PW)
    html = client.get("/equipe/vendas").text
    assert 'class="team-tabs"' in html and 'href="/cars"' in html and html.count('href="/equipe/vendas"') >= 2
    assert 'href="/equipe/vendas"' in client.get("/").text   # link no topo do admin


# =======================================================================================
# 4) Migração de um banco antigo (schema do commit 1634fa5)
# =======================================================================================

OLD_CARS = [
    # ext, nome, preço, ativo, sold_at
    ("5509733", "LAND ROVER RANGE ROVER SPORT 3.0 HSE 2023", 77_990_000, 1, None),
    ("5555997", "BMW 320i 2.0 M SPORT 2022", None, 1, None),
    ("5123456", "CHEVROLET ONIX 1.0 LT 2023", 7_000_000, 0, "2026-09-20T18:00:00-03:00"),
    ("5000001", "MERCEDES-BENZ C 300 2.0 2021", 30_000_000, 1, None),
    ("demo-1", "FIAT DEMO 2020", 1_000_000, 1, None),
]


def _columns(conn, table):
    return {r[1]: r[2] for r in conn.execute(f"PRAGMA table_info({table})")}


@pytest.fixture
def old_db(tmp_path, monkeypatch):
    path = tmp_path / "antigo.db"
    monkeypatch.setenv("UNION_DB_PATH", str(path))
    raw = sqlite3.connect(path)
    raw.executescript((FIXTURES / "schema_1634fa5.sql").read_text(encoding="utf-8"))
    ts = "2026-09-01T10:00:00-03:00"
    for ext, name, price, active, sold_at in OLD_CARS:
        raw.execute("""INSERT INTO cars (external_id, name, name_key, photo_url, price_cents, url, posted, active,
                                         first_seen, last_seen, missing_count, sold_at, in_baseline)
                       VALUES (?, ?, ?, ?, ?, 'u', 1, ?, ?, ?, ?, ?, 1)""",
                    (ext, name, name.lower(), f"https://img.test/{ext}.jpg", price, active, ts, ts,
                     0 if active else 2, sold_at))
    raw.execute("INSERT INTO price_history (car_id, price_cents, recorded_at) VALUES (1, 77990000, ?)", (ts,))
    raw.execute("INSERT INTO price_history (car_id, price_cents, recorded_at) VALUES (3, 7500000, ?)", (ts,))
    raw.execute("INSERT INTO tickets (car_id, status, created_at) VALUES (4, 'pending', ?)", (ts,))
    raw.execute("INSERT INTO sold_alerts (car_id, created_at, dismissed) VALUES (3, ?, 0)", (ts,))
    raw.execute("INSERT INTO runs (kind, started_at, finished_at, ok) VALUES ('daily', ?, ?, 1)", (ts, ts))
    raw.commit()
    raw.close()
    return path


def _dump(conn, skip=("brand", "cost_cents", "sale_price_cents")):
    out = {}
    for table in ("cars", "price_history", "tickets", "sold_alerts", "runs", "detail_cache"):
        cols = [c for c in _columns(conn, table) if c not in skip]
        out[table] = [tuple(r) for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY 1")]
    return out


@sqlite_only
def test_old_schema_fixture_is_really_old(old_db):
    raw = sqlite3.connect(old_db)
    assert not {"brand", "cost_cents", "sale_price_cents"} & set(_columns(raw, "cars"))
    assert raw.execute("SELECT 1 FROM sqlite_master WHERE name = 'car_expenses'").fetchone() is None
    raw.close()


@sqlite_only
def test_migration_from_1634fa5_twice(old_db):
    raw = sqlite3.connect(old_db)
    before = _dump(raw)
    raw.close()

    db.init_db()
    conn = db.connect()
    try:
        cols = _columns(conn, "cars")
        assert (cols["brand"], cols["cost_cents"], cols["sale_price_cents"]) == ("TEXT", "INTEGER", "INTEGER")
        exp = _columns(conn, "car_expenses")
        assert set(exp) == {"id", "car_id", "description", "amount_cents", "created_at"}
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='ix_car_expenses_car'").fetchone()
        assert _dump(conn) == before                                   # nada perdido nem alterado
        brands = dict(conn.execute("SELECT external_id, brand FROM cars").fetchall())
        assert brands == {"5509733": "Land Rover", "5555997": "BMW", "5123456": "Chevrolet",
                          "5000001": "Mercedes-Benz", "demo-1": "Fiat"}
        assert conn.execute("SELECT COUNT(*) FROM cars WHERE cost_cents IS NOT NULL "
                            "OR sale_price_cents IS NOT NULL").fetchone()[0] == 0

        # a equipe trabalha no banco migrado…
        onix = conn.execute("SELECT id FROM cars WHERE external_id = '5123456'").fetchone()[0]
        assert finance.set_cost(conn, onix, "60.000") and finance.add_expense(conn, onix, "Pneus", "1.800")
        conn.execute("UPDATE cars SET brand = 'Marca Corrigida' WHERE external_id = '5000001'")
        conn.commit()
        after_first = _dump(conn, skip=())
        expenses = [tuple(r) for r in conn.execute("SELECT * FROM car_expenses")]
    finally:
        conn.close()

    db.init_db()   # …e a 2ª migração (novo deploy) não mexe em nada
    db.init_db()
    conn = db.connect()
    try:
        assert _dump(conn, skip=()) == after_first
        assert [tuple(r) for r in conn.execute("SELECT * FROM car_expenses")] == expenses
        assert conn.execute("SELECT brand FROM cars WHERE external_id = '5000001'").fetchone()[0] == "Marca Corrigida"
        assert len(_columns(conn, "cars")) == len(cols)               # sem coluna duplicada

        v = finance.sales_view(conn, "2026-09", date(2026, 10, 7))
        # venda antiga aparece com o último preço do site: 70.000 − 60.000 − 1.800 = 8.200
        assert [(s["car"]["name"], s["sale_cents"], s["profit_cents"], s["car"]["brand"]) for s in v["sales"]] == [
            ("CHEVROLET ONIX 1.0 LT 2023", 7_000_000, 820_000, "Chevrolet")]
        assert v["summary"]["in_stock"] == 3                          # demo fora
        # apagar o carro leva as despesas junto (ON DELETE CASCADE na tabela nova)
        conn.execute("DELETE FROM cars WHERE id = ?", (onix,))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM car_expenses").fetchone()[0] == 0
    finally:
        conn.close()


@sqlite_only
def test_migrated_db_serves_pages(old_db, site):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:     # o lifespan roda init_db no banco antigo
        for path in ("/equipe/vendas?mes=2026-09", "/equipe/vendas", "/equipe", "/equipe/carro/3", "/equipe/carro/2", "/"):
            r = client.get(path)
            assert r.status_code == 200, path
            assert not BAD_TEXT.search(r.text), path
        assert "CHEVROLET ONIX" in client.get("/equipe/vendas?mes=2026-09").text


@sqlite_only
def test_migration_fills_brand_for_odd_names(old_db):
    raw = sqlite3.connect(old_db)
    ts = "2026-09-01T10:00:00-03:00"
    for i, name in enumerate(["", "   ", "2020", "çãé ü"]):
        raw.execute("""INSERT INTO cars (external_id, name, name_key, url, posted, active, first_seen, last_seen)
                       VALUES (?, ?, ?, 'u', 1, 1, ?, ?)""", (f"x{i}", name, name, ts, ts))
    raw.commit()
    raw.close()
    db.init_db()
    conn = db.connect()
    try:
        rows = conn.execute("SELECT brand FROM cars").fetchall()
        assert all(r[0] for r in rows)                                # nunca NULL/vazio depois da migração
        assert conn.execute("SELECT brand FROM cars WHERE external_id = 'x0'").fetchone()[0] == "Outros"
    finally:
        conn.close()
