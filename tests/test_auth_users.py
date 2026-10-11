"""F15 — login individual (Lupa: app/auth.py + app/services/users.py). Senhas sintéticas de teste."""
from __future__ import annotations

import logging

import pytest

from app import auth, db
from app.services import users
from conftest import sqlite_only

ADMIN_PW = "senha-do-admin-123"
SHARED_PW = "senha-unica-equipe-456"
NEW_PW = "minha-senha-nova-789"


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    auth.reset_failures()
    yield
    auth.reset_failures()


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", ADMIN_PW)
    monkeypatch.setenv("STAFF_PASSWORD", SHARED_PW)
    monkeypatch.setenv("SESSION_SECRET", "segredo-de-sessao-" + "y" * 20)


def _uid(conn, username):
    return next(u["id"] for u in users.list_users(conn) if u["username"] == username)


def _login(client, username="", password="", next_=None, **kw):
    data = {"username": username, "password": password}
    if next_ is not None:
        data["next"] = next_
    return client.post("/login", data=data, follow_redirects=False, **kw)


@pytest.fixture
def temp(conn):
    """Senha provisória do arthur (must_change=1)."""
    return users.reset_password(conn, _uid(conn, "arthur"))


@pytest.fixture
def ready(conn):
    """beto com senha definitiva (must_change=0)."""
    uid = _uid(conn, "beto")
    assert users.change_password(conn, uid, users.reset_password(conn, uid), NEW_PW) == "saved"
    return uid


def _give_everyone_a_password(conn):
    for u in users.list_users(conn):
        if u["active"] and not u["has_password"]:
            users.reset_password(conn, u["id"])


# --- admin inalterado ----------------------------------------------------------------

@pytest.mark.parametrize("username", ["", "admin", " ADMIN "])
def test_admin_login_with_empty_or_admin_username(client, locked, username):
    r = _login(client, username, ADMIN_PW)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert f"{auth.COOKIE_NAME}=v2.admin." in r.headers["set-cookie"]
    assert client.get("/cars").status_code == 200
    assert client.get("/equipe").status_code == 200


def test_admin_password_does_not_work_under_an_employee_username(client, locked, temp):
    r = _login(client, "arthur", ADMIN_PW)
    assert r.status_code == 401 and auth.COOKIE_NAME not in r.headers.get("set-cookie", "")
    assert _login(client, "equipe", ADMIN_PW).status_code == 401


def test_admin_is_never_sent_to_change_password(client, locked):
    _login(client, "", ADMIN_PW)
    assert client.get("/", follow_redirects=False).status_code == 200


# --- funcionário: login, must_change, sessão -------------------------------------------

def test_employee_first_login_is_forced_to_change_password(client, locked, temp):
    r = _login(client, " Arthur ", temp, next_="/equipe")
    assert r.status_code == 303 and r.headers["location"] == "/equipe/senha"
    assert f"{auth.COOKIE_NAME}=v3." in r.headers["set-cookie"]
    assert temp not in r.headers["set-cookie"] and temp not in r.headers["location"]
    for path in ("/equipe", "/equipe/vendas", "/equipe/carro/1", "/", "/cars", "/admin/usuarios"):
        g = client.get(path, follow_redirects=False)
        assert g.status_code == 303, path
        assert g.headers["location"] in ("/equipe/senha", "/equipe"), path
    # dentro da área da equipe o destino é sempre a troca de senha
    assert client.get("/equipe", follow_redirects=False).headers["location"] == "/equipe/senha"
    h = client.get("/equipe", headers={"HX-Request": "true"}, follow_redirects=False)
    assert h.status_code == 403 and h.headers["hx-redirect"] == "/equipe/senha"
    p = client.post("/equipe/carro/1/despesas", data={"description": "x", "amount": "1"}, follow_redirects=False)
    assert p.status_code == 403
    assert client.get("/equipe/senha", follow_redirects=False).status_code == 200
    assert client.get("/login", follow_redirects=False).headers["location"] == "/equipe/senha"
    assert client.get("/logout", follow_redirects=False).status_code == 303


def test_must_change_cannot_be_bypassed_with_trailing_or_nested_paths(client, locked, temp):
    _login(client, "arthur", temp)
    for path in ("/equipe/senha/", "/equipe/senha/x", "/equipe/senhaX", "/equipe/?a=/equipe/senha"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/equipe/senha", path


def test_employee_after_changing_password_uses_the_team_area_only(client, locked, conn, ready):
    r = _login(client, "beto", NEW_PW, next_="/equipe/vendas")
    assert r.status_code == 303 and r.headers["location"] == "/equipe/vendas"
    assert client.get("/equipe", follow_redirects=False).status_code == 200
    for path in ("/", "/cars", "/editor", "/admin/usuarios"):
        g = client.get(path, follow_redirects=False)
        assert g.status_code == 303 and g.headers["location"] == "/equipe", path
    assert client.post("/admin/check-now", follow_redirects=False).status_code == 403
    assert client.post("/admin/usuarios", data={"name": "X", "username": "xx", "roles": "chefe"},
                       follow_redirects=False).status_code == 403
    assert client.post(f"/admin/usuarios/{ready}/senha", follow_redirects=False).status_code == 403
    h = client.post("/admin/check-now", headers={"HX-Request": "true"}, follow_redirects=False)
    assert h.status_code == 403 and h.headers["hx-redirect"] == "/equipe"
    assert users.list_users(conn) and not any(u["username"] == "xx" for u in users.list_users(conn))


def test_next_outside_team_area_is_ignored_for_employee(client, locked, ready):
    assert _login(client, "beto", NEW_PW, next_="/cars").headers["location"] == "/equipe"
    assert _login(client, "beto", NEW_PW, next_="//evil.com").headers["location"] == "/equipe"


def test_session_drops_when_password_is_reset(client, locked, conn, ready):
    _login(client, "beto", NEW_PW)
    assert client.get("/equipe", follow_redirects=False).status_code == 200
    users.reset_password(conn, ready)                       # admin gerou outra provisória
    r = client.get("/equipe", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert _login(client, "beto", NEW_PW).status_code == 401  # a senha antiga também morreu


def test_session_drops_when_user_is_deactivated_and_stays_dead_after_reactivation(client, locked, conn, ready):
    _login(client, "beto", NEW_PW)
    users.set_active(conn, ready, False)
    assert client.get("/equipe", follow_redirects=False).headers["location"].startswith("/login")
    assert _login(client, "beto", NEW_PW).status_code == 401
    cookie = client.cookies.get(auth.COOKIE_NAME)
    users.set_active(conn, ready, True)                     # pw_version subiu ao desativar
    client.cookies.set(auth.COOKIE_NAME, cookie)
    assert client.get("/equipe", follow_redirects=False).headers["location"].startswith("/login")
    assert _login(client, "beto", NEW_PW).status_code == 303  # mas pode entrar de novo com a senha


def test_own_password_change_needs_new_cookie_and_login_response_provides_it(client, locked, conn):
    uid = _uid(conn, "thiago")
    temp_pw = users.reset_password(conn, uid)
    _login(client, "thiago", temp_pw)
    old = client.cookies.get(auth.COOKIE_NAME)
    assert users.change_password(conn, uid, temp_pw, NEW_PW) == "saved"
    assert auth.resolve_token(old).role is None             # cookie antigo (pw_version velho) caiu
    fresh = auth.make_user_token(users.get_user(conn, uid))
    session = auth.resolve_token(fresh)
    assert session.role == auth.STAFF and session.user["id"] == uid and session.must_change is False


def test_change_password_route_keeps_the_employee_logged_in(client, locked, conn, temp):
    _login(client, "arthur", temp)
    r = client.post("/equipe/senha", data={"current": temp, "new": NEW_PW, "confirm": NEW_PW},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/equipe"
    assert NEW_PW not in r.headers.get("set-cookie", "") and NEW_PW not in r.headers["location"]
    assert client.get("/equipe", follow_redirects=False).status_code == 200
    assert _login(client, "arthur", temp).status_code == 401
    assert _login(client, "arthur", NEW_PW).headers["location"] == "/equipe"


# --- sessão forjada ---------------------------------------------------------------------

def test_forged_or_tampered_employee_cookie_is_rejected(client, locked, conn, ready):
    good = auth.make_user_token(users.get_user(conn, ready))
    assert auth.resolve_token(good).role == auth.STAFF
    version, uid, pwv, ts, sig = good.split(".")
    other = _uid(conn, "ruan")
    forged = [
        f"v3.{other}.{pwv}.{ts}.{sig}",                     # trocar o id
        f"v3.{uid}.{int(pwv) + 1}.{ts}.{sig}",              # trocar o pw_version
        f"v3.{uid}.{pwv}.{int(ts) + 5}.{sig}",              # trocar a data
        f"v3.{uid}.{pwv}.{ts}.{'0' * len(sig)}",            # assinatura inventada
        f"v2.admin.{ts}.{sig}",                             # virar admin com a assinatura de funcionário
        f"v2.staff.{ts}.{sig}",
        f"v3.{uid}.{pwv}.{ts}", "v3....", "v3.a.b.c.d", "lixo", "",
    ]
    for token in forged:
        assert auth.resolve_token(token).role is None, token
        client.cookies.set(auth.COOKIE_NAME, token)
        assert client.get("/equipe", follow_redirects=False).status_code == 303
        assert client.get("/", follow_redirects=False).headers["location"].startswith("/login")


def test_employee_cookie_signed_with_another_secret_is_rejected(locked, conn, ready, monkeypatch):
    token = auth.make_user_token(users.get_user(conn, ready))
    monkeypatch.setenv("SESSION_SECRET", "outro-segredo-" + "z" * 20)
    assert auth.resolve_token(token).role is None


def test_expired_employee_cookie_is_rejected(locked, conn, ready):
    import time

    user = users.get_user(conn, ready)
    old = auth.make_user_token(user, now=time.time() - auth.SESSION_MAX_AGE - 10)
    assert auth.resolve_token(old).role is None
    assert auth.resolve_token(auth.make_user_token(user)).role == auth.STAFF


def test_cookie_of_user_without_password_is_rejected(locked, conn):
    user = users.get_user(conn, _uid(conn, "ruan"))          # nunca recebeu senha
    assert auth.resolve_token(auth.make_user_token(user)).role is None


# --- não revelar se o usuário existe -------------------------------------------------------

def test_same_error_for_unknown_user_wrong_password_inactive_and_passwordless(client, locked, conn, ready):
    users.set_active(conn, _uid(conn, "haroldo"), False)
    answers = [
        _login(client, "naoexiste", "qualquer-senha-1"),
        _login(client, "beto", "senha-errada-1"),            # existe, senha errada
        _login(client, "ruan", "qualquer-senha-1"),          # existe, sem senha
        _login(client, "haroldo", "qualquer-senha-1"),       # desativado
    ]
    auth.reset_failures()
    answers.append(_login(client, "admin", "senha-errada-1"))
    for r in answers:
        assert r.status_code == 401
        assert auth.LOGIN_ERROR in r.text
        assert auth.COOKIE_NAME not in r.headers.get("set-cookie", "")
    bodies = {r.text.replace("naoexiste", "U").replace("beto", "U").replace("ruan", "U")
              .replace("haroldo", "U").replace("admin", "U") for r in answers}
    assert len(bodies) == 1                                  # resposta idêntica, a menos do usuário digitado


def test_unknown_user_costs_a_password_hash_too(conn, monkeypatch):
    calls = []
    real = users.verify_password
    monkeypatch.setattr(users, "verify_password", lambda pw, stored: calls.append(1) or real(pw, stored))
    users.authenticate(conn, "naoexiste", "qualquer-senha-1")
    users.authenticate(conn, "ruan", "qualquer-senha-1")     # sem senha
    assert len(calls) == 2                                   # tempo parecido com o de usuário real


def test_rate_limit_also_covers_employee_logins(client, locked, ready):
    for _ in range(auth.MAX_FAILURES):
        assert _login(client, "beto", "senha-errada-1").status_code == 401
    r = _login(client, "beto", NEW_PW)                       # certa, mas bloqueado
    assert r.status_code == 429 and "Retry-After" in r.headers
    assert _login(client, "", ADMIN_PW).status_code == 429
    auth.reset_failures()
    assert _login(client, "beto", NEW_PW).status_code == 303


# --- transição da senha única ---------------------------------------------------------------

@pytest.mark.parametrize("username", ["", "equipe", " Equipe "])
def test_shared_password_works_while_someone_has_no_password(client, locked, username):
    r = _login(client, username, SHARED_PW)
    assert r.status_code == 303 and r.headers["location"] == "/equipe"
    assert f"{auth.COOKIE_NAME}=v2.staff." in r.headers["set-cookie"]
    assert client.get("/equipe", follow_redirects=False).status_code == 200
    assert client.get("/cars", follow_redirects=False).headers["location"] == "/equipe"
    assert auth.shared_password_active() is True


def test_shared_password_is_not_a_personal_password(client, locked, ready):
    assert _login(client, "beto", SHARED_PW).status_code == 401
    assert _login(client, "admin", SHARED_PW).status_code == 401


def test_shared_password_stops_when_all_active_users_have_passwords(client, locked, conn):
    _login(client, "", SHARED_PW)
    assert client.get("/equipe", follow_redirects=False).status_code == 200
    _give_everyone_a_password(conn)
    assert auth.shared_password_active() is False
    r = client.get("/equipe", follow_redirects=False)        # sessão antiga da senha única cai
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert _login(client, "", SHARED_PW).status_code == 401
    assert _login(client, "equipe", SHARED_PW).status_code == 401
    assert _login(client, "", ADMIN_PW).status_code == 303   # admin não muda
    users.create_user(conn, "novato", "Novato", "vendedor")  # novo sem senha → volta a valer
    assert auth.shared_password_active() is True
    assert _login(client, "equipe", SHARED_PW).status_code == 303


def test_deactivated_user_without_password_does_not_keep_shared_password_alive(client, locked, conn):
    for u in users.list_users(conn):
        if u["username"] == "ruan":
            users.set_active(conn, u["id"], False)
        else:
            users.reset_password(conn, u["id"])
    assert auth.shared_password_active() is False
    assert _login(client, "", SHARED_PW).status_code == 401


def test_shared_password_never_works_when_not_configured(client, locked, monkeypatch):
    monkeypatch.delenv("STAFF_PASSWORD")
    assert auth.shared_password_active() is False
    assert _login(client, "equipe", SHARED_PW).status_code == 401
    assert _login(client, "equipe", "").status_code == 401
    assert _login(client, "", "").status_code == 401


def test_employee_login_works_after_shared_password_ends(client, locked, conn, ready):
    _give_everyone_a_password(conn)
    assert _login(client, "beto", NEW_PW).status_code == 303
    assert client.get("/equipe", follow_redirects=False).status_code == 200


@sqlite_only
def test_database_without_users_table_keeps_legacy_shared_password(client, locked, conn):
    conn.execute("DROP TABLE users")
    conn.commit()
    assert auth.shared_password_active() is True
    assert _login(client, "", SHARED_PW).status_code == 303
    assert _login(client, "arthur", "qualquer-senha-1").status_code == 401   # sem tabela, ninguém individual
    assert _login(client, "", ADMIN_PW).status_code == 303


def test_database_error_fails_closed(locked, monkeypatch, conn, ready):
    token = auth.make_user_token(users.get_user(conn, ready))
    shared = auth.make_session_token(auth.STAFF)
    admin = auth.make_session_token(auth.ADMIN)

    def boom(*a, **k):
        raise RuntimeError("banco fora do ar")

    monkeypatch.setattr(db, "connect", boom)
    assert auth.resolve_token(token).role is None
    assert auth.resolve_token(shared).role is None
    assert auth.resolve_token(admin).role == auth.ADMIN      # admin não depende do banco


# --- contexto e vazamentos ------------------------------------------------------------------

def test_current_user_in_state_and_templates(client, locked, ready):
    _login(client, "beto", NEW_PW)
    r = client.get("/equipe")
    assert r.status_code == 200 and "Beto" in r.text


def test_current_user_is_none_for_admin_and_shared(locked, conn, ready):
    assert auth.resolve_token(auth.make_session_token(auth.ADMIN)).user is None
    shared = auth.resolve_token(auth.make_session_token(auth.STAFF))
    assert shared.role == auth.STAFF and shared.user is None and shared.shared is True
    assert shared.must_change is False
    session = auth.resolve_token(auth.make_user_token(users.get_user(conn, ready)))
    assert session.user["name"] == "Beto" and session.user["roles_label"] == "Vendedor"
    assert "password_hash" not in session.user


def test_passwords_never_reach_logs_cookies_or_urls(client, locked, conn, caplog):
    caplog.set_level(logging.DEBUG)
    uid = _uid(conn, "marcelo")
    temp_pw = users.reset_password(conn, uid)
    wrong = _login(client, "marcelo", "senha-errada-zzz")
    ok = _login(client, "marcelo", temp_pw)
    for r in (wrong, ok):
        blob = r.text + " ".join(f"{k}: {v}" for k, v in r.headers.items())
        assert temp_pw not in blob and "senha-errada-zzz" not in blob
    assert temp_pw not in caplog.text and "senha-errada-zzz" not in caplog.text
    assert ADMIN_PW not in caplog.text and SHARED_PW not in caplog.text


def test_local_without_app_password_stays_open_as_admin(client, conn):
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/admin/usuarios", follow_redirects=False).status_code == 200
    assert _login(client, "arthur", "x").status_code == 303   # login desligado: entra como admin


# --- pós-QA F15: 'union' gravado pela tela antiga nos gerenciadores de senha -----------------

@pytest.mark.parametrize("username", ["union", "UNION", " Union "])
def test_legacy_union_username_works_as_empty_for_admin(client, locked, username):
    r = _login(client, username, ADMIN_PW)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert f"{auth.COOKIE_NAME}=v2.admin." in r.headers["set-cookie"]
    assert client.get("/cars").status_code == 200


def test_legacy_union_username_works_for_shared_password_only_during_transition(client, locked, conn):
    r = _login(client, "union", SHARED_PW)
    assert r.status_code == 303 and r.headers["location"] == "/equipe"
    assert f"{auth.COOKIE_NAME}=v2.staff." in r.headers["set-cookie"]
    _give_everyone_a_password(conn)
    assert _login(client, "union", SHARED_PW).status_code == 401      # transição acabou
    assert _login(client, "union", ADMIN_PW).status_code == 303       # admin segue


def test_legacy_union_username_gives_nothing_without_the_right_password(client, locked, conn, ready):
    for password in ("", "union", "senha-errada-1", NEW_PW):
        r = _login(client, "union", password)
        assert r.status_code == 401 and auth.LOGIN_ERROR in r.text
        assert auth.COOKIE_NAME not in r.headers.get("set-cookie", "")
        auth.reset_failures()
    assert not any(u["username"] == "union" for u in users.list_users(conn))


def test_weak_password_is_refused_on_the_change_route(client, locked, conn, temp):
    _login(client, "arthur", temp)
    for weak in ("12345678", "arthur2026", "senha123"):
        r = client.post("/equipe/senha", data={"current": temp, "new": weak, "confirm": weak},
                        follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/equipe/senha?msg=weak", weak
    assert users.get_user(conn, _uid(conn, "arthur"))["must_change"] is True
