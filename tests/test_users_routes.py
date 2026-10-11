"""F15 (Forja): rotas /equipe/senha e /admin/usuarios* (app/main.py).
A lógica de senha e sessão é da Lupa (app/auth.py, app/services/users.py) e tem testes próprios."""
from __future__ import annotations

import logging
import re

import pytest

from app import auth, db
from app.services import users

ADMIN_PW = "senha-admin-123"
STAFF_PW = "senha-equipe-456"
NEW_PW = "minha-senha-nova-9"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", ADMIN_PW)
    monkeypatch.setenv("STAFF_PASSWORD", STAFF_PW)
    monkeypatch.setenv("SESSION_SECRET", "segredo-" + "z" * 30)
    auth.reset_failures()
    yield
    auth.reset_failures()


def login(client, username, password):
    client.cookies.clear()
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def as_admin(client):
    assert login(client, "", ADMIN_PW).status_code == 303
    return client


def user_id(conn, username="beto") -> int:
    return conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]


def row(username="beto"):
    c = db.connect()
    try:
        return dict(c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone())
    finally:
        c.close()


def temp_password(client, uid) -> str:
    """Gera a provisória como admin e lê da tela (único lugar em que ela aparece)."""
    r = as_admin(client).post(f"/admin/usuarios/{uid}/senha")
    assert r.status_code == 200
    c = db.connect()
    try:
        for candidate in set(re.findall(r"[A-Za-z0-9]{10,}", r.text)):
            if users.verify_password(candidate, c.execute(
                    "SELECT password_hash FROM users WHERE id = ?", (uid,)).fetchone()[0]):
                return candidate
    finally:
        c.close()
    raise AssertionError("senha provisória não apareceu na página")


def as_staff_user(client, conn, username="beto") -> str:
    """Funcionário logado já com senha definitiva; devolve a senha."""
    uid = user_id(conn, username)
    temp = temp_password(client, uid)
    assert login(client, username, temp).status_code == 303
    r = client.post("/equipe/senha", data={"current": temp, "new": NEW_PW, "confirm": NEW_PW},
                    follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe")
    return NEW_PW


# ---------- /admin/usuarios ----------

def test_list_users_page(client, conn):
    r = as_admin(client).get("/admin/usuarios")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    for name in ("Arthur", "Marcelo", "Beto", "Thiago", "Haroldo", "Ruan"):
        assert name in r.text
    assert "Comprador e vendedor" in r.text


def test_users_context(client, conn, monkeypatch):
    from app import main

    seen = {}
    original = main.templates.TemplateResponse

    def spy(request, name, ctx=None, **kw):
        seen[name] = ctx
        return original(request, name, ctx, **kw)

    monkeypatch.setattr(main.templates, "TemplateResponse", spy)
    as_admin(client).get("/admin/usuarios?msg=saved")
    ctx = seen["usuarios.html"]
    assert {"users", "new_password", "msg", "shared_password_active", "role_options"} <= set(ctx)
    assert ctx["msg"] == "saved" and ctx["new_password"] is None
    assert ctx["shared_password_active"] is True  # todos sem senha + STAFF_PASSWORD configurada
    assert [o["key"] for o in ctx["role_options"]] == ["comprador", "vendedor", "documentador", "chefe"]
    first = ctx["users"][0]
    assert {"id", "username", "name", "roles_label", "roles", "active", "has_password", "must_change",
            "last_login"} <= set(first)
    assert "password_hash" not in first and first["roles"] == ["comprador", "vendedor"]
    as_admin(client).get("/admin/usuarios?msg=<script>")
    assert seen["usuarios.html"]["msg"] is None


def test_create_user(client, conn):
    c = as_admin(client)
    r = c.post("/admin/usuarios", data={"name": "João", "username": "joao", "roles": ["comprador", "vendedor"]},
               follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/admin/usuarios?msg=saved")
    new = row("joao")
    assert (new["name"], new["roles"], new["password_hash"], new["active"]) == ("João", "comprador,vendedor", None, 1)
    r = c.post("/admin/usuarios", data={"name": "Outro", "username": "joao", "roles": "vendedor"},
               follow_redirects=False)
    assert r.headers["location"] == "/admin/usuarios?msg=exists"


@pytest.mark.parametrize("data", [
    {"name": "X", "username": "com espaço", "roles": "vendedor"},
    {"name": "X", "username": "a", "roles": "vendedor"},
    {"name": "X", "username": "novo", "roles": "dono"},
    {"name": "X", "username": "admin", "roles": "vendedor"},
    {"name": "", "username": "novo", "roles": "vendedor"},
    {},
])
def test_create_user_invalid(client, conn, data):
    r = as_admin(client).post("/admin/usuarios", data=data, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/admin/usuarios?msg=invalid")
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 6


def test_reset_password_shows_once_and_stores_only_hash(client, conn, caplog):
    uid = user_id(conn)
    caplog.set_level(logging.DEBUG)
    r = as_admin(client).post(f"/admin/usuarios/{uid}/senha", follow_redirects=False)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "location" not in r.headers and "set-cookie" not in r.headers
    saved = row()
    temp = next(t for t in set(re.findall(r"[A-Za-z0-9]{10,}", r.text))
                if users.verify_password(t, saved["password_hash"]))
    assert len(temp) >= 10 and not set(temp) & set("0O1l")
    assert (saved["must_change"], saved["pw_version"]) == (1, 1)
    assert temp not in str(saved) and temp not in caplog.text and temp not in str(r.url)
    # a senha não reaparece: nem na lista, nem em outra página
    assert temp not in client.get("/admin/usuarios").text
    # gerar de novo troca a senha: a anterior deixa de valer
    client.post(f"/admin/usuarios/{uid}/senha")
    assert not users.verify_password(temp, row()["password_hash"]) and row()["pw_version"] == 2


@pytest.mark.parametrize("bad", ["999", "0", "-1", str(2**31), str(2**63)])
def test_reset_and_active_unknown_user_404(client, conn, bad):
    c = as_admin(client)
    assert c.post(f"/admin/usuarios/{bad}/senha").status_code == 404
    assert c.post(f"/admin/usuarios/{bad}/ativo", data={"value": "0"}).status_code == 404
    assert conn.execute("SELECT COUNT(*) FROM users WHERE password_hash IS NOT NULL OR active = 0").fetchone()[0] == 0


def test_deactivate_and_reactivate(client, conn):
    uid = user_id(conn)
    c = as_admin(client)
    r = c.post(f"/admin/usuarios/{uid}/ativo", data={"value": "0"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/admin/usuarios?msg=saved")
    assert row()["active"] == 0
    c.post(f"/admin/usuarios/{uid}/ativo", data={"value": "1"})
    assert row()["active"] == 1
    for value in ("", "2", "sim"):
        r = c.post(f"/admin/usuarios/{uid}/ativo", data={"value": value}, follow_redirects=False)
        assert r.headers["location"] == "/admin/usuarios?msg=invalid"
    assert row()["active"] == 1


def test_deactivating_drops_the_session(client, conn):
    as_staff_user(client, conn)
    assert client.get("/equipe", follow_redirects=False).status_code == 200
    staff_cookies = dict(client.cookies)
    as_admin(client).post(f"/admin/usuarios/{user_id(conn)}/ativo", data={"value": "0"})
    client.cookies.clear()
    client.cookies.update(staff_cookies)
    assert client.get("/equipe", follow_redirects=False).status_code == 303
    assert login(client, "beto", NEW_PW).status_code == 401


ADMIN_ROUTES = [("GET", "/admin/usuarios"), ("POST", "/admin/usuarios"), ("POST", "/admin/usuarios/3/senha"),
                ("POST", "/admin/usuarios/3/ativo")]


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_admin_routes_closed_to_anonymous_and_staff(client, conn, method, path):
    client.cookies.clear()
    r = client.request(method, path, data={"value": "0"}, follow_redirects=False)
    assert r.status_code in (303, 401) and "usuarios" not in r.headers.get("location", "").split("?")[0]
    as_staff_user(client, conn, "arthur")
    r = client.request(method, path, data={"value": "0"}, follow_redirects=False)
    assert r.status_code in (303, 403)
    assert r.headers.get("location", "/equipe") == "/equipe"
    login(client, "", STAFF_PW)  # senha única (transição)
    r = client.request(method, path, data={"value": "0"}, follow_redirects=False)
    assert r.status_code in (303, 403)
    assert row("beto")["active"] == 1 and row("beto")["password_hash"] is None


# ---------- /equipe/senha ----------

def test_first_access_forces_password_change(client, conn):
    temp = temp_password(client, user_id(conn))
    r = login(client, "beto", temp)
    assert r.status_code == 303
    r = client.get("/equipe", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe/senha")
    page = client.get("/equipe/senha")
    assert page.status_code == 200 and "Primeiro acesso: crie a sua senha" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert temp not in page.text


def test_change_password_flow(client, conn):
    temp = temp_password(client, user_id(conn))
    login(client, "beto", temp)

    def post(current, new, confirm):
        r = client.post("/equipe/senha", data={"current": current, "new": new, "confirm": confirm},
                        follow_redirects=False)
        assert r.status_code == 303
        return r.headers["location"]

    assert post("errada-12345", NEW_PW, NEW_PW) == "/equipe/senha?msg=wrong"
    assert post(temp, "curta", "curta") == "/equipe/senha?msg=weak"
    assert post(temp, NEW_PW, NEW_PW + "x") == "/equipe/senha?msg=weak"
    assert post(temp, temp, temp) == "/equipe/senha?msg=same"
    assert row()["must_change"] == 1 and users.verify_password(temp, row()["password_hash"])
    assert post(temp, NEW_PW, NEW_PW) == "/equipe"
    saved = row()
    assert saved["must_change"] == 0 and users.verify_password(NEW_PW, saved["password_hash"])
    assert NEW_PW not in str(saved)
    # o cookie foi renovado: segue logado, agora sem ser obrigado a trocar
    r = client.get("/equipe", follow_redirects=False)
    assert r.status_code == 200 and "Olá, Beto" in r.text
    page = client.get("/equipe/senha?msg=saved")
    assert page.status_code == 200 and "Primeiro acesso" not in page.text
    assert login(client, "beto", temp).status_code == 401
    assert login(client, "beto", NEW_PW).status_code == 303


def test_password_page_is_only_for_named_users(client, conn):
    client.cookies.clear()
    assert client.get("/equipe/senha", follow_redirects=False).status_code == 303
    r = client.post("/equipe/senha", data={"current": "a", "new": NEW_PW, "confirm": NEW_PW}, follow_redirects=False)
    assert r.status_code == 401
    r = as_admin(client).get("/equipe/senha", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/")
    login(client, "", STAFF_PW)
    r = client.get("/equipe/senha", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe")
    r = client.post("/equipe/senha", data={"current": STAFF_PW, "new": NEW_PW, "confirm": NEW_PW},
                    follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe")
    assert conn.execute("SELECT COUNT(*) FROM users WHERE password_hash IS NOT NULL").fetchone()[0] == 0


def test_current_user_only_for_named_users(client, conn):
    as_staff_user(client, conn, "arthur")
    assert "Olá, Arthur" in client.get("/equipe").text
    assert "Olá, Arthur" in client.get("/equipe/vendas").text
    assert "Olá," not in as_admin(client).get("/equipe").text
    login(client, "", STAFF_PW)
    assert "Olá," not in client.get("/equipe").text
