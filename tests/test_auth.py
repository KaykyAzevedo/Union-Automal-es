"""F5 — login por senha (Lupa: app/auth.py). Env lido a cada request."""
from __future__ import annotations

import pytest

from app import auth

PASSWORD = "senha-do-painel-123"


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    auth.reset_failures()
    yield
    auth.reset_failures()


@pytest.fixture
def locked(monkeypatch):
    """Painel com senha (como na nuvem, mas sem VERCEL)."""
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", "segredo-de-sessao-" + "x" * 20)


@pytest.fixture
def vercel(monkeypatch, locked):
    monkeypatch.setenv("VERCEL", "1")


def _login(client, password=PASSWORD, next_=None, **kw):
    data = {"password": password}
    if next_ is not None:
        data["next"] = next_
    return client.post("/login", data=data, follow_redirects=False, **kw)


# --- sem senha local --------------------------------------------------------

def test_local_without_password_is_open(client):
    assert client.get("/").status_code == 200
    assert client.get("/cars").status_code == 200
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "Sair" not in client.get("/").text


# --- protegido --------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/cars", "/editor", "/api/cars/1/prices"])
def test_unauthenticated_get_redirects_to_login_with_next(client, locked, path):
    r = client.get(path + "?a=1&b=2", follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/login?next=")
    from urllib.parse import parse_qs, urlsplit

    assert parse_qs(urlsplit(loc).query)["next"] == [f"{path}?a=1&b=2"]


@pytest.mark.parametrize("path", ["/tickets/1/done", "/alerts/1/dismiss", "/sold/1/dismiss",
                                  "/admin/check-now", "/admin/price-check-now"])
def test_unauthenticated_post_is_401_and_does_nothing(client, locked, site, path):
    r = client.post(path, follow_redirects=False)
    assert r.status_code == 401
    assert site.calls == 0


def test_unauthenticated_htmx_gets_hx_redirect(client, locked):
    r = client.post("/tickets/1/done", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401 and r.headers.get("HX-Redirect") == "/login"


@pytest.mark.parametrize("path", ["/login", "/health", "/static/style.css"])
def test_public_paths(client, locked, path):
    assert client.get(path, follow_redirects=False).status_code == 200


def test_login_ok_sets_cookie_and_opens_panel(client, locked):
    r = _login(client, next_="/cars")
    assert r.status_code == 303 and r.headers["location"] == "/cars"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{auth.COOKIE_NAME}=v2.admin.")
    low = cookie.lower()
    assert "httponly" in low and "samesite=lax" in low and "max-age" in low
    assert "secure" not in low  # http local
    page = client.get("/")
    assert page.status_code == 200 and "Sair" in page.text
    assert client.get("/cars").status_code == 200


def test_login_wrong_password(client, locked):
    r = _login(client, "errada")
    assert r.status_code == 401
    assert "Senha incorreta" in r.text
    assert auth.COOKIE_NAME not in r.headers.get("set-cookie", "")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_empty_password(client, locked):
    assert _login(client, "").status_code == 401


@pytest.mark.parametrize("target", ["//evil.com", "/\\evil.com", "http://evil.com/x", "https://evil.com",
                                    "javascript:alert(1)", "evil.com"])
def test_open_redirect_blocked(client, locked, target):
    r = _login(client, next_=target)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_forged_or_tampered_cookie_rejected(client, locked):
    good = _login(client).cookies[auth.COOKIE_NAME]
    client.cookies.clear()
    for bad in ("v1.123.abc", good[:-2] + ("00" if not good.endswith("00") else "11"), "lixo", ""):
        client.cookies.set(auth.COOKIE_NAME, bad)
        assert client.get("/", follow_redirects=False).status_code == 303, bad
    client.cookies.set(auth.COOKIE_NAME, good)
    assert client.get("/", follow_redirects=False).status_code == 200


def test_expired_cookie_rejected(locked):
    import time

    old = auth.make_session_token(now=time.time() - auth.SESSION_MAX_AGE - 10)
    assert auth.verify_session_token(old) is False
    assert auth.verify_session_token(auth.make_session_token()) is True


def test_password_change_invalidates_sessions(client, locked, monkeypatch):
    _login(client)
    assert client.get("/", follow_redirects=False).status_code == 200
    monkeypatch.setenv("APP_PASSWORD", "nova-senha")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_logout(client, locked):
    _login(client)
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    client.cookies.clear()  # o navegador apaga (Max-Age=0); garante que o cookie foi expirado
    assert "max-age=0" in r.headers["set-cookie"].lower() or "expires=" in r.headers["set-cookie"].lower()
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.get("/logout", follow_redirects=False).status_code == 303


def test_rate_limit_after_5_failures(client, locked):
    for _ in range(auth.MAX_FAILURES):
        assert _login(client, "errada").status_code == 401
    r = _login(client, "errada")
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    # até a senha certa é bloqueada durante a janela
    assert _login(client, PASSWORD).status_code == 429
    auth.reset_failures()
    assert _login(client, PASSWORD).status_code == 303


def test_x_forwarded_for_ignored_locally(client, locked):
    """Fora da Vercel, trocar X-Forwarded-For não pode furar o limite."""
    for i in range(auth.MAX_FAILURES):
        _login(client, "errada", headers={"X-Forwarded-For": f"10.0.0.{i}"})
    assert _login(client, "errada", headers={"X-Forwarded-For": "10.9.9.9"}).status_code == 429


# --- Vercel -----------------------------------------------------------------

@pytest.mark.parametrize("missing", ["APP_PASSWORD", "SESSION_SECRET"])
@pytest.mark.parametrize("path", ["/", "/login", "/health", "/cron/poll", "/editor"])
def test_vercel_without_config_is_503_everywhere(client, vercel, monkeypatch, site, missing, path):
    monkeypatch.delenv(missing)
    monkeypatch.setenv("CRON_SECRET", "c")
    r = client.get(path, headers={"Authorization": "Bearer c"}, follow_redirects=False)
    assert r.status_code == 503
    assert "Painel não configurado" in r.text
    assert site.calls == 0


def test_vercel_static_still_served_without_config(client, vercel, monkeypatch):
    monkeypatch.delenv("APP_PASSWORD")
    assert client.get("/static/style.css").status_code == 200


def test_vercel_cookie_is_secure(client, vercel):
    r = _login(client)
    assert r.status_code == 303 and "secure" in r.headers["set-cookie"].lower()


def test_vercel_rate_limit_per_forwarded_ip(client, vercel):
    for _ in range(auth.MAX_FAILURES):
        _login(client, "errada", headers={"X-Forwarded-For": "200.1.1.1"})
    assert _login(client, "errada", headers={"X-Forwarded-For": "200.1.1.1"}).status_code == 429
    assert _login(client, PASSWORD, headers={"X-Forwarded-For": "200.2.2.2, 10.0.0.1"}).status_code == 303


def test_check_password_constant_time_api(locked):
    assert auth.check_password(PASSWORD) is True
    assert auth.check_password(PASSWORD + " ") is False
    assert auth.check_password("") is False
