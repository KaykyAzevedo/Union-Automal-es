"""F6 — perfis admin/equipe (Lupa: app/auth.py). A equipe só vê /equipe; varre TODAS as rotas
registradas para que uma rota nova não escape da allowlist."""
from __future__ import annotations

import re
from datetime import date

import httpx
import pytest
from fastapi.routing import APIRoute

from app import auth

ADMIN_PW = "senha-admin-123"
STAFF_PW = "senha-equipe-456"
STAFF_ALLOWED = ("/equipe", "/logout", "/health", "/favicon.ico", "/login")
_PARAM = re.compile(r"\{([^}:]+)(:[^}]+)?\}")


@pytest.fixture(autouse=True)
def _reset():
    auth.reset_failures()
    yield
    auth.reset_failures()


@pytest.fixture
def roles(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", ADMIN_PW)
    monkeypatch.setenv("STAFF_PASSWORD", STAFF_PW)
    monkeypatch.setenv("SESSION_SECRET", "segredo-" + "z" * 30)
    monkeypatch.setenv("CRON_SECRET", "cron-ok")


def login(client, pw, next_=None):
    data = {"password": pw}
    if next_:
        data["next"] = next_
    return client.post("/login", data=data, follow_redirects=False)


def all_routes():
    """(método, caminho concreto) de cada rota registrada; parâmetros viram '1'."""
    from app.main import app

    out = []
    for r in app.routes:
        if not isinstance(r, APIRoute):
            continue
        path = _PARAM.sub("1", r.path)
        for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
            out.append((m, path))
    for extra in ("/openapi.json", "/docs", "/redoc"):
        out.append(("GET", extra))
    return out


def staff_allowed(path):
    return path in STAFF_ALLOWED or path.startswith(("/equipe/", "/static/", "/cron/"))


FORBIDDEN = [(m, p) for m, p in all_routes() if not staff_allowed(p)]


def test_route_inventory_sane():
    paths = {p for _, p in all_routes()}
    assert "/equipe" in paths and "/" in paths and "/editor/1" in paths
    assert ("POST", "/tickets/1/done") in all_routes()
    assert len(FORBIDDEN) >= 15


# --- equipe ---------------------------------------------------------------

@pytest.fixture
def staff(client, roles):
    r = login(client, STAFF_PW)
    assert r.status_code == 303 and r.headers["location"] == "/equipe"
    assert r.cookies[auth.COOKIE_NAME].startswith("v2.staff.")
    return client


@pytest.mark.parametrize("method, path", FORBIDDEN)
def test_staff_blocked_everywhere(staff, site, method, path):
    r = staff.request(method, path, follow_redirects=False)
    if method == "GET":
        assert r.status_code == 303 and r.headers["location"] == "/equipe", (method, path, r.status_code)
    else:
        assert r.status_code == 403, (method, path, r.status_code)
    assert site.calls == 0


@pytest.mark.parametrize("method, path", [m_p for m_p in FORBIDDEN if m_p[0] != "GET"])
def test_staff_blocked_htmx(staff, method, path):
    r = staff.request(method, path, headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 403 and r.headers.get("HX-Redirect") == "/equipe"


@pytest.mark.parametrize("path", ["/", "/cars", "/editor/1", "/encarte/1/slide/0.png"])
def test_staff_head_blocked(staff, path):
    r = staff.head(path, follow_redirects=False)
    assert r.status_code in (303, 403, 405)
    assert r.status_code != 200


def test_staff_actions_have_no_effect(staff, site, car, conn):
    from app.services.sync import run_poll

    run_poll(conn, [car("1")])
    run_poll(conn, [car("1"), car("2")])
    for path in ("/tickets/1/done", "/admin/check-now", "/admin/price-check-now"):
        staff.post(path, follow_redirects=False)
    assert conn.execute("SELECT status FROM tickets").fetchone()[0] == "pending"
    assert site.calls == 0


def test_staff_sees_equipe(staff):
    r = staff.get("/equipe")
    assert r.status_code == 200
    for admin_only in ("/editor", "/admin/check-now", "hx-post", "/cars\"", "Verificar agora"):
        assert admin_only not in r.text, admin_only


def test_staff_login_page_redirects(staff):
    r = staff.get("/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/equipe"


@pytest.mark.parametrize("next_, expected", [
    ("/", "/equipe"), ("/editor/1", "/equipe"), ("/equipe/x", "/equipe/x"), ("//evil.com", "/equipe"),
])
def test_staff_login_next(client, roles, next_, expected):
    r = login(client, STAFF_PW, next_)
    assert r.headers["location"] == expected


def test_staff_cron_without_bearer_401(staff, site):
    assert staff.get("/cron/poll", follow_redirects=False).status_code == 401
    assert staff.get("/cron/price-check", follow_redirects=False).status_code == 401
    assert site.calls == 0


def test_staff_static_health_logout(staff):
    assert staff.get("/static/style.css").status_code == 200
    assert staff.get("/health").status_code == 200
    r = staff.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    staff.cookies.clear()
    assert staff.get("/equipe", follow_redirects=False).headers["location"].startswith("/login")


def test_forged_role_in_cookie_rejected(client, roles):
    token = login(client, STAFF_PW).cookies[auth.COOKIE_NAME]
    forged = token.replace("v2.staff.", "v2.admin.", 1)
    client.cookies.clear()
    client.cookies.set(auth.COOKIE_NAME, forged)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_old_v1_cookie_invalid(client, roles):
    client.cookies.set(auth.COOKIE_NAME, "v1.1790000000.deadbeef")
    assert client.get("/", follow_redirects=False).headers["location"].startswith("/login")


# --- admin ----------------------------------------------------------------

@pytest.fixture
def admin(client, roles):
    r = login(client, ADMIN_PW)
    assert r.status_code == 303 and r.cookies[auth.COOKIE_NAME].startswith("v2.admin.")
    return client


@pytest.mark.parametrize("method, path", all_routes())
def test_admin_reaches_every_route(admin, site, method, path):
    headers = {"Authorization": "Bearer cron-ok"} if path.startswith("/cron/") else {}
    r = admin.request(method, path, headers=headers, follow_redirects=False)
    assert r.status_code not in (401, 403), (method, path, r.status_code)
    if r.status_code in (302, 303, 307):
        loc = r.headers["location"]
        assert not loc.startswith(("/login", "/equipe")), (method, path, loc)


def test_admin_sees_equipe_and_panel(admin):
    assert admin.get("/equipe").status_code == 200
    assert admin.get("/").status_code == 200


# --- senhas / sessões -----------------------------------------------------

def test_password_decides_role(client, roles):
    assert login(client, ADMIN_PW).cookies[auth.COOKIE_NAME].startswith("v2.admin.")
    client.cookies.clear()
    assert login(client, STAFF_PW).cookies[auth.COOKIE_NAME].startswith("v2.staff.")
    client.cookies.clear()
    assert login(client, "nenhuma").status_code == 401


def test_staff_password_change_kills_only_staff(client, roles, monkeypatch):
    staff_tok = login(client, STAFF_PW).cookies[auth.COOKIE_NAME]
    client.cookies.clear()
    admin_tok = login(client, ADMIN_PW).cookies[auth.COOKIE_NAME]
    monkeypatch.setenv("STAFF_PASSWORD", "nova-equipe")
    assert auth.session_role(staff_tok) is None
    assert auth.session_role(admin_tok) == "admin"
    client.cookies.clear()
    client.cookies.set(auth.COOKIE_NAME, staff_tok)
    assert client.get("/equipe", follow_redirects=False).headers["location"].startswith("/login")


def test_admin_password_change_kills_only_admin(client, roles, monkeypatch):
    staff_tok = auth.make_session_token("staff")
    admin_tok = auth.make_session_token("admin")
    monkeypatch.setenv("APP_PASSWORD", "novo-admin")
    assert auth.session_role(admin_tok) is None
    assert auth.session_role(staff_tok) == "staff"


def test_same_password_for_both_is_admin(client, roles, monkeypatch):
    monkeypatch.setenv("STAFF_PASSWORD", ADMIN_PW)
    assert auth.password_role(ADMIN_PW) == "admin"
    assert login(client, ADMIN_PW).cookies[auth.COOKIE_NAME].startswith("v2.admin.")


def test_without_staff_password_staff_role_off(client, roles, monkeypatch):
    tok = auth.make_session_token("staff")
    monkeypatch.delenv("STAFF_PASSWORD")
    assert auth.password_role(STAFF_PW) is None
    assert auth.session_role(tok) is None
    assert login(client, STAFF_PW).status_code == 401


def test_local_only_staff_password_is_open_admin(client, monkeypatch):
    monkeypatch.setenv("STAFF_PASSWORD", STAFF_PW)
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/equipe").status_code == 200


def test_vercel_staff_without_admin_is_503(client, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("STAFF_PASSWORD", STAFF_PW)
    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
    assert client.get("/equipe", follow_redirects=False).status_code == 503


def test_rate_limit_shared_between_passwords(client, roles):
    for _ in range(auth.MAX_FAILURES):
        login(client, "errada")
    assert login(client, STAFF_PW).status_code == 429
    assert login(client, ADMIN_PW).status_code == 429


def test_unauthenticated_equipe_goes_to_login(client, roles):
    r = client.get("/equipe", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_unauthenticated_openapi_blocked(client, roles):
    for p in ("/openapi.json", "/docs"):
        assert client.get(p, follow_redirects=False).status_code in (303, 401, 404)


# --- data de cadastro (Last-Modified da foto) ---------------------------------

@pytest.mark.parametrize("header, expected", [
    ("Fri, 25 Sep 2026 19:23:46 GMT", date(2026, 9, 25)),
    ("Sat, 26 Sep 2026 02:30:00 GMT", date(2026, 9, 25)),   # 23:30 em São Paulo
    ("Sat, 26 Sep 2026 03:00:00 GMT", date(2026, 9, 26)),
    (None, None), ("", None), ("lixo", None),
])
def test_parse_last_modified(header, expected):
    from app.scraper.listed_at import parse_last_modified

    assert parse_last_modified(header) == expected


def test_fetch_listed_at():
    from app.scraper.listed_at import fetch_listed_at

    seen = []

    def handler(request):
        seen.append(request.method)
        if "404" in str(request.url):
            return httpx.Response(404)
        if "nohdr" in str(request.url):
            return httpx.Response(200)
        if "boom" in str(request.url):
            raise httpx.ConnectError("x", request=request)
        return httpx.Response(200, headers={"last-modified": "Fri, 25 Sep 2026 19:23:46 GMT"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert fetch_listed_at("https://www.autocerto.com/fotos/339/1/1_a.jpg", client) == date(2026, 9, 25)
    assert seen == ["HEAD"]
    for bad in ("https://x/404.jpg", "https://x/nohdr.jpg", "https://x/boom.jpg"):
        assert fetch_listed_at(bad, client) is None
    n = len(seen)
    assert fetch_listed_at(None, client) is None and len(seen) == n
