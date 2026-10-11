"""F15 (Sentinela): QA de SEGURANÇA do login individual por funcionário. Tenta burlar o login por
fora, sem reaproveitar os helpers dos testes da Lupa/Forja: cookie forjado, sessão antiga, must_change,
enumeração de usuário, vazamento de senha, perfis, transição da senha única, entradas hostis,
troca de senha, limite de tentativas, migração + seed e reenvio do POST que gera a senha.

As senhas daqui são inventadas para o teste. As provisórias nascem e morrem dentro de cada teste."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import sqlite3
import time

import anyio
import pytest

from app import auth, db
from app.services import users
from app.services.sync import run_poll
from conftest import FIXTURES, PG, requires_pg, sqlite_only
from test_not_sale_qa import _cols, _dump, _fill_old, _tables, empty_pg  # noqa: F401  (empty_pg é fixture)

ADMIN_PW = "adm-qa-f15-0001"
STAFF_PW = "eqp-qa-f15-0002"
SECRET = "segredo-qa-f15-" + "k" * 30
OWN = "propria-qa-f15-777"
OTHER = "outra-qa-f15-888"
SEED = ["arthur", "marcelo", "beto", "thiago", "haroldo", "ruan"]
COOKIE = auth.COOKIE_NAME


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", ADMIN_PW)
    monkeypatch.setenv("STAFF_PASSWORD", STAFF_PW)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("CRON_SECRET", "cron-qa-f15")
    auth.reset_failures()
    yield
    auth.reset_failures()


# ---------- helpers (sempre conexão nova: nada de transação aberta segurando lock no Postgres) ----------

def _db(fn):
    c = db.connect()
    try:
        return fn(c)
    finally:
        c.close()


def uid(username: str) -> int:
    return _db(lambda c: c.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0])


def urow(username: str) -> dict:
    return _db(lambda c: dict(c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()))


def all_users() -> list[tuple]:
    return _db(lambda c: [tuple(r) for r in c.execute("SELECT * FROM users ORDER BY id")])


def give_temp(username: str) -> str:
    return _db(lambda c: users.reset_password(c, uid(username)))


def give_own(username: str, password: str = OWN) -> str:
    temp = give_temp(username)
    assert _db(lambda c: users.change_password(c, uid(username), temp, password)) == "saved"
    return password


def everyone_gets_a_password() -> None:
    def fill(c):
        for row in c.execute("SELECT id FROM users WHERE active = 1 AND password_hash IS NULL").fetchall():
            users.reset_password(c, row[0])
    _db(fill)


def set_active(username: str, value: bool) -> None:
    assert _db(lambda c: users.set_active(c, uid(username), value))


def login(client, username: str, password: str, **extra):
    client.cookies.clear()
    return client.post("/login", data={"username": username, "password": password, **extra},
                       follow_redirects=False)


def token(client) -> str:
    value = client.cookies.get(COOKIE)
    assert value
    return value


def use(client, value: str):
    client.cookies.clear()
    client.cookies.set(COOKIE, value)
    return client


def is_in(client) -> bool:
    r = client.get("/equipe", follow_redirects=False)
    assert r.status_code in (200, 303), r.status_code
    if r.status_code == 303:
        assert r.headers["location"].startswith("/login")
    return r.status_code == 200


def is_admin(client) -> bool:
    return client.get("/admin/usuarios", follow_redirects=False).status_code == 200


def staff_cookie(client, username: str = "beto", password: str = OWN) -> str:
    give_own(username, password)
    assert login(client, username, password).status_code == 303
    return token(client)


def raw(method: str, path: str, cookie: str | None = None, hx: bool = False, form: str = ""):
    """Chama o app ASGI com o caminho EXATO (o httpx normalizaria `..` e afins)."""
    from app.main import app

    headers = [(b"host", b"testserver")]
    if cookie:
        headers.append((b"cookie", f"{COOKIE}={cookie}".encode()))
    if hx:
        headers.append((b"hx-request", b"true"))
    if form:
        headers.append((b"content-type", b"application/x-www-form-urlencoded"))
        headers.append((b"content-length", str(len(form)).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": headers, "client": ("testclient", 50000), "server": ("testserver", 80)}
    sent = []

    async def receive():
        return {"type": "http.request", "body": form.encode(), "more_body": False}

    async def send(message):
        sent.append(message)

    async def main():
        await app(scope, receive, send)

    anyio.run(main)
    head = {k.decode().lower(): v.decode() for k, v in sent[0]["headers"]}
    return sent[0]["status"], head, b"".join(m.get("body", b"") for m in sent[1:]).decode("utf-8", "replace")


def old_cookie(role: str, password: str, issued: int | None = None, secret: str = SECRET) -> str:
    """Cookie v2 calculado à mão com a fórmula do commit c62d56e (antes da F15)."""
    payload = f"v2.{role}.{int(time.time()) if issued is None else issued}"
    key = hashlib.sha256(f"{secret}\x00{role}\x00{password}".encode()).digest()
    return f"{payload}.{hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()}"


def user_sig(payload: str, secret: str = SECRET) -> str:
    key = hashlib.sha256(f"{secret}\x00user".encode()).digest()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


# =======================================================================================
# (1) sessão: cookie forjado / alterado / antigo
# =======================================================================================

def test_cookie_format_is_what_the_attacker_sees(client):
    cookie = staff_cookie(client)
    version, user_id, pwv, issued, sig = cookie.split(".")
    assert (version, int(user_id), int(pwv)) == ("v3", uid("beto"), urow("beto")["pw_version"])
    assert sig == user_sig(f"v3.{user_id}.{pwv}.{issued}") and len(sig) == 64
    assert OWN not in cookie and "beto" not in cookie


def test_tampered_employee_cookie_is_rejected(client):
    cookie = staff_cookie(client)
    give_own("arthur", OTHER)
    version, user_id, pwv, issued, sig = cookie.split(".")
    other = uid("arthur")
    forged = [
        f"v3.{other}.{pwv}.{issued}.{sig}",                       # id de outro funcionário
        f"v3.{other}.{urow('arthur')['pw_version']}.{issued}.{sig}",
        f"v3.{user_id}.{int(pwv) + 1}.{issued}.{sig}",            # pw_version adiantado
        f"v3.{user_id}.{int(pwv) - 1}.{issued}.{sig}",
        f"v3.{user_id}.{pwv}.{int(issued) + 1}.{sig}",            # data alterada
        f"v3.{user_id}.{pwv}.{issued}.{sig.upper()}",
        f"v3.{user_id}.{pwv}.{issued}.{sig[:-1]}",
        f"v3.{user_id}.{pwv}.{issued}.",
        f"v3.{user_id}.{pwv}.{issued}",
        f"v3. {user_id}.{pwv}.{issued}.{sig}",                    # int() aceita espaço, +, _ e zeros
        f"v3.+{user_id}.{pwv}.{issued}.{sig}",
        f"v3.0{user_id}.{pwv}.{issued}.{sig}",
        f"v3.{user_id}.{pwv}_0.{issued}.{sig}",
        f"v3.{user_id}.{pwv}.{issued}.{sig}.extra",
        f"v4.{user_id}.{pwv}.{issued}.{sig}",
        f"V3.{user_id}.{pwv}.{issued}.{sig}",
        # perfil → admin reaproveitando a assinatura de funcionário
        f"v2.admin.{issued}.{sig}",
        f"v3.admin.{pwv}.{issued}.{sig}",
        f"v2.admin.{issued}.{user_sig(f'v2.admin.{issued}', 'chute')}",
        # assinado com outro segredo, com a senha do admin como segredo, sem segredo
        f"v3.{user_id}.{pwv}.{issued}.{user_sig(f'v3.{user_id}.{pwv}.{issued}', 'chute')}",
        f"v3.{user_id}.{pwv}.{issued}.{user_sig(f'v3.{user_id}.{pwv}.{issued}', '')}",
        f"v3.{user_id}.{pwv}.{issued}.{hashlib.sha256(f'v3.{user_id}.{pwv}.{issued}'.encode()).hexdigest()}",
        "v3....", "v3", "....", "admin", "v2.admin", "null", "true", "",
    ]
    for value in forged:
        if not value:
            continue
        use(client, value)
        assert not is_in(client), value
        r = client.get("/", follow_redirects=False)
        assert (r.status_code, r.headers["location"][:6]) == (303, "/login"), value
        assert client.post("/admin/usuarios", data={"name": "X", "username": "xx", "roles": "chefe"},
                           follow_redirects=False).status_code == 401, value
    assert is_in(use(client, cookie))                              # o original segue valendo
    assert len(all_users()) == len(SEED)


def test_employee_cannot_turn_any_cookie_into_admin(client):
    """Funcionário conhece o próprio cookie e a senha única; nenhum dos dois vira admin."""
    cookie = staff_cookie(client)
    issued = cookie.split(".")[3]
    shared = old_cookie("staff", STAFF_PW)
    assert is_in(use(client, shared)) and not is_admin(client)
    candidates = [
        shared.replace("v2.staff.", "v2.admin."),
        old_cookie("admin", STAFF_PW),                             # assina "admin" com a senha que ele conhece
        old_cookie("admin", OWN), old_cookie("admin", ""), old_cookie("admin", "None"),
        old_cookie("staff", STAFF_PW).replace("staff", "ADMIN"),
        f"v2.admin.{issued}.{cookie.split('.')[4]}",
    ]
    for value in candidates:
        use(client, value)
        assert not is_admin(client), value
        assert client.get("/", follow_redirects=False).status_code == 303, value


def test_old_cookie_dies_after_password_reset(client):
    cookie = staff_cookie(client)
    assert is_in(client)
    give_temp("beto")
    assert not is_in(use(client, cookie))
    r = client.post("/equipe/senha", data={"current": OWN, "new": OTHER, "confirm": OTHER}, follow_redirects=False)
    assert r.status_code == 401                                    # nem troca a senha com a sessão morta
    assert login(client, "beto", OWN).status_code == 401           # a senha antiga também morreu


def test_old_cookie_dies_on_deactivation_and_stays_dead_after_reactivation(client):
    cookie = staff_cookie(client)
    set_active("beto", False)
    assert not is_in(use(client, cookie))
    assert login(client, "beto", OWN).status_code == 401
    set_active("beto", True)
    assert not is_in(use(client, cookie))                          # reativar não ressuscita a sessão antiga
    assert login(client, "beto", OWN).status_code == 303           # mas ele entra de novo com a própria senha
    assert is_in(client) and token(client) != cookie


def test_reactivating_twice_or_deactivating_others_does_not_revive(client):
    cookie = staff_cookie(client)
    for _ in range(3):
        set_active("beto", False)
        set_active("beto", True)
    assert not is_in(use(client, cookie))
    fresh = staff_cookie(client, "arthur", OTHER)
    set_active("beto", False)
    assert is_in(use(client, fresh))                               # desativar um não derruba o outro


def test_own_password_change_kills_other_devices(client):
    phone = staff_cookie(client)
    assert login(client, "beto", OWN).status_code == 303
    r = client.post("/equipe/senha", data={"current": OWN, "new": OTHER, "confirm": OTHER}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe")
    desktop = token(client)
    assert is_in(client) and desktop != phone
    assert not is_in(use(client, phone))
    assert is_in(use(client, desktop))


def test_cookie_of_deleted_or_unknown_user_is_rejected(client):
    cookie = staff_cookie(client)
    issued = int(time.time())
    ghost = f"v3.999999.0.{issued}"
    assert not is_in(use(client, f"{ghost}.{user_sig(ghost)}"))    # assinatura certa, usuário inexistente
    huge = f"v3.{2 ** 63}.0.{issued}"
    assert not is_in(use(client, f"{huge}.{user_sig(huge)}"))      # id fora de int4/int8: recusa, não quebra
    _db(lambda c: (c.execute("DELETE FROM users WHERE username = 'beto'"), c.commit()))
    assert not is_in(use(client, cookie))


def test_expired_and_future_cookies_are_rejected(client):
    give_own("beto")
    row = urow("beto")
    for age, ok in ((0, True), (29 * 86400, True), (31 * 86400, False), (-3600, False)):
        payload = f"v3.{row['id']}.{row['pw_version']}.{int(time.time()) - age}"
        assert is_in(use(client, f"{payload}.{user_sig(payload)}")) is ok, age
    assert not is_admin(use(client, old_cookie("admin", ADMIN_PW, int(time.time()) - 31 * 86400)))


def test_employee_session_depends_only_on_secret_and_database(client, monkeypatch):
    cookie = staff_cookie(client)
    monkeypatch.setenv("STAFF_PASSWORD", "outra-senha-unica")      # trocar a senha única não derruba funcionário
    assert is_in(use(client, cookie))
    monkeypatch.delenv("STAFF_PASSWORD")
    assert is_in(use(client, cookie))
    monkeypatch.setenv("SESSION_SECRET", "segredo-trocado-" + "w" * 30)
    assert not is_in(use(client, cookie))                          # trocar o segredo derruba todo mundo


# =======================================================================================
# (5) admin: APP_PASSWORD inalterada e NÃO deslogado pelo deploy
# =======================================================================================

def test_admin_cookie_issued_before_f15_still_works(client):
    for age in (0, 3600, 29 * 86400):
        use(client, old_cookie("admin", ADMIN_PW, int(time.time()) - age))
        assert is_admin(client), age
        assert client.get("/", follow_redirects=False).status_code == 200
        assert client.get("/equipe/vendas", follow_redirects=False).status_code == 200
    # e o cookie que o app emite hoje é byte a byte o do formato antigo
    assert auth.make_session_token(auth.ADMIN, now=1_800_000_000) == old_cookie("admin", ADMIN_PW, 1_800_000_000)
    assert auth.make_session_token(auth.STAFF, now=1_800_000_000) == old_cookie("staff", STAFF_PW, 1_800_000_000)


def test_admin_survives_everything_that_happens_to_employees(client):
    admin = old_cookie("admin", ADMIN_PW)
    give_own("beto")
    everyone_gets_a_password()
    set_active("arthur", False)
    assert is_admin(use(client, admin))
    r = client.get("/equipe/senha", follow_redirects=False)        # admin não tem senha própria para trocar
    assert (r.status_code, r.headers["location"]) == (303, "/")
    r = client.post("/equipe/senha", data={"current": ADMIN_PW, "new": OTHER, "confirm": OTHER},
                    follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/")
    assert login(client, "", ADMIN_PW).status_code == 303 and is_admin(client)


@pytest.mark.parametrize("username", ["", "admin", "ADMIN", "  Admin  "])
def test_admin_logs_in_with_app_password(client, username):
    r = login(client, username, ADMIN_PW)
    assert (r.status_code, r.headers["location"]) == (303, "/")
    assert token(client).startswith("v2.admin.") and is_admin(client)


@pytest.mark.parametrize("username", ["beto", "arthur", "equipe", "administrador", "root", "admin2", "admin\x00"])
def test_admin_password_under_any_other_username_fails(client, username):
    give_own("beto")
    assert login(client, username, ADMIN_PW).status_code == 401
    assert COOKIE not in client.cookies


@pytest.mark.parametrize("username", ["union", "UNION", "  Union  "])
def test_username_saved_by_the_old_login_screen_still_works(client, username):
    """A tela antiga gravava username='union' nos gerenciadores de senha: vale como usuário vazio."""
    r = login(client, username, ADMIN_PW)
    assert (r.status_code, r.headers["location"]) == (303, "/") and is_admin(client)
    r = login(client, username, STAFF_PW)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe")
    assert token(client).startswith("v2.staff.") and is_in(client) and not is_admin(client)


def test_legacy_username_opens_nothing_else(client):
    give_own("beto")
    for password in (OWN, "", "union", "x"):
        auth.reset_failures()
        assert login(client, "union", password).status_code == 401  # não é conta: só apelido de vazio
    everyone_gets_a_password()
    auth.reset_failures()
    assert login(client, "union", STAFF_PW).status_code == 401      # segue a transição da senha única
    assert login(client, "union", ADMIN_PW).status_code == 303
    use(client, old_cookie("admin", ADMIN_PW))                      # e ninguém cadastra um funcionário "union"
    for username in ("union", "UNION", " Union "):
        r = client.post("/admin/usuarios", data={"name": "Falso", "username": username, "roles": "chefe"},
                        follow_redirects=False)
        assert r.headers["location"] == "/admin/usuarios?msg=invalid"
    assert len(all_users()) == len(SEED)


# =======================================================================================
# (5) funcionário e senha única: só /equipe*
# =======================================================================================

def _routes():
    from app.main import app

    out = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if not methods:
            continue
        path = re.sub(r"\{[^}]+\}", "1", route.path)
        out += [(m, path) for m in sorted(methods - {"HEAD", "OPTIONS"})]
    return sorted(set(out))


def _is_public(path):
    return path in ("/login", "/logout", "/health", "/favicon.ico") or path.startswith(("/static/", "/cron/"))


@pytest.mark.parametrize("kind", ["employee", "shared"])
def test_staff_sweep_everything_outside_equipe_is_blocked(client, site, kind):
    if kind == "employee":
        cookie = staff_cookie(client)
    else:
        assert login(client, "equipe", STAFF_PW).status_code == 303
        cookie = token(client)
    before = all_users()
    blocked = 0
    for method, path in _routes():
        if _is_public(path):
            continue
        r = use(client, cookie).request(method, path, follow_redirects=False)
        if path == "/equipe" or path.startswith("/equipe/"):
            assert r.status_code != 403 and not r.headers.get("location", "").startswith("/login"), (method, path)
            continue
        blocked += 1
        if method == "GET":
            assert (r.status_code, r.headers["location"]) == (303, "/equipe"), (method, path)
        else:
            assert r.status_code == 403, (method, path, r.status_code)
        hx = use(client, cookie).request(method, path, headers={"HX-Request": "true"}, follow_redirects=False)
        assert (hx.status_code, hx.headers.get("hx-redirect"), hx.text) == (403, "/equipe", ""), (method, path)
    assert blocked >= 16 and site.calls == 0
    assert all_users() == before
    assert {("GET", "/admin/usuarios"), ("POST", "/admin/usuarios"), ("POST", "/admin/usuarios/1/senha"),
            ("POST", "/admin/usuarios/1/ativo")} <= set(_routes())


@pytest.mark.parametrize("kind", ["employee", "shared", "must_change", "anonymous"])
def test_nobody_but_admin_manages_users(client, kind):
    give_own("arthur", OTHER)
    target = urow("arthur")
    if kind == "employee":
        cookie = staff_cookie(client)
    elif kind == "must_change":
        assert login(client, "beto", give_temp("beto")).status_code == 303
        cookie = token(client)
    elif kind == "shared":
        assert login(client, "", STAFF_PW).status_code == 303
        cookie = token(client)
    else:
        cookie = None
    before = all_users()
    denied = 401 if kind == "anonymous" else 403
    for path, data in ((f"/admin/usuarios/{target['id']}/senha", {}),
                       (f"/admin/usuarios/{target['id']}/ativo", {"value": "0"}),
                       ("/admin/usuarios", {"name": "Invasor", "username": "invasor", "roles": "chefe"}),
                       (f"/admin/usuarios/{target['id']}/senha/", {}), ("/admin/usuarios/", {})):
        client.cookies.clear()
        if cookie:
            client.cookies.set(COOKIE, cookie)
        r = client.post(path, data=data, follow_redirects=False)
        assert r.status_code == denied, (path, r.status_code)
        assert "newpw" not in r.text and "scrypt" not in r.text
    r = client.get("/admin/usuarios", follow_redirects=False)
    assert r.status_code == 303 and "Usuários da equipe" not in r.text
    assert all_users() == before
    assert login(client, "arthur", OTHER).status_code == 303       # a senha do alvo não foi trocada


def test_require_role_dependency_is_a_second_lock_on_admin_routes():
    """Mesmo sem o middleware, as rotas /admin/usuarios* têm a dependência de admin."""
    from app.main import app

    for route in app.routes:
        if getattr(route, "path", "").startswith("/admin/usuarios"):
            names = {getattr(d.call, "__qualname__", "") for d in route.dependant.dependencies}
            assert any("require_role" in n for n in names), route.path


def test_anonymous_sweep(client):
    for method, path in _routes():
        if _is_public(path):
            continue
        client.cookies.clear()
        r = client.request(method, path, follow_redirects=False)
        if method == "GET":
            assert r.status_code == 303 and r.headers["location"].startswith("/login?next="), path
        else:
            assert r.status_code == 401, (method, path)
    assert client.get("/cron/daily").status_code == 401


# =======================================================================================
# (2) must_change: contornar por variação de caminho, método, HTMX, barra, caixa, query
# =======================================================================================

BYPASS_PATHS = [
    "/equipe", "/equipe/", "/equipe/vendas", "/equipe/carro/1", "/equipe/carro/1/custo",
    "/equipe/senha/", "/equipe/senha/x", "/equipe/senha/..", "/equipe/senha/../vendas", "/equipe/senha/../../",
    "/equipe/senha/../../admin/usuarios", "/equipe/./senha", "/equipe//senha", "//equipe/senha",
    "/equipe/senha;x=1", "/equipe/senha.html", "/equipe/senha%2f", "/equipe/senha%00", "/equipe/senha ",
    "/equipe/SENHA", "/Equipe/Senha", "/EQUIPE/SENHA", "/equipe/senha\\", "/equipe/senhas",
    "/", "/cars", "/admin/usuarios", "/admin/usuarios/1/senha", "/equipe/../admin/usuarios",
]


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_must_change_cannot_be_bypassed(client, conn, car, method):
    run_poll(conn, [car("1")])
    conn.commit()
    temp = give_temp("beto")
    assert login(client, "beto", temp).headers["location"] == "/equipe/senha"
    cookie = token(client)
    before = urow("beto")
    form = f"current={temp}&new={OWN}&confirm={OWN}&cost=50.000"
    for path in BYPASS_PATHS:
        status, head, body = raw(method, path, cookie, form=form if method in ("POST", "PUT", "PATCH") else "")
        if method in ("GET", "HEAD"):
            assert status == 303 and head["location"] in ("/equipe/senha", "/equipe"), (path, status)
        else:
            assert status == 403, (path, status)
        assert "Estoque" not in body and "Vendas" not in body and "R$" not in body, path
        status, head, body = raw(method, path, cookie, hx=True)
        assert status == 403 and head["hx-redirect"] in ("/equipe/senha", "/equipe") and body == "", path
    assert urow("beto") == before                                   # nenhuma variação trocou a senha
    assert _db(lambda c: c.execute("SELECT cost_cents FROM cars").fetchone()[0]) is None


def test_must_change_query_string_does_not_help(client):
    assert login(client, "beto", give_temp("beto"), next="/equipe/vendas").headers["location"] == "/equipe/senha"
    for path in ("/equipe?next=/equipe/senha", "/equipe/vendas?path=/equipe/senha", "/equipe?/equipe/senha",
                 "/equipe/vendas?mes=2026-10#/equipe/senha", "/equipe/carro/1?msg=saved"):
        r = client.get(path, follow_redirects=False)
        assert (r.status_code, r.headers["location"]) == (303, "/equipe/senha"), path
    r = client.get("/login?next=/equipe/vendas", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe/senha")


def test_must_change_what_stays_open_and_how_it_ends(client):
    temp = give_temp("beto")
    assert login(client, "beto", temp).status_code == 303
    page = client.get("/equipe/senha?msg=wrong")
    assert page.status_code == 200 and "Primeiro acesso" in page.text
    assert 'href="/equipe/vendas"' not in page.text and "team-tabs" not in page.text       # sem navegação
    assert client.get("/health").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/cron/daily").status_code == 401
    for method in ("PUT", "DELETE", "PATCH"):
        assert client.request(method, "/equipe/senha").status_code == 405
    assert urow("beto")["must_change"] == 1
    r = client.post("/equipe/senha", data={"current": temp, "new": OWN, "confirm": OWN}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe")
    assert is_in(client) and client.get("/equipe/vendas").status_code == 200
    give_temp("beto")                                               # nova provisória: volta a exigir a troca
    assert not is_in(client)


def test_must_change_survives_logout_and_new_login(client):
    temp = give_temp("beto")
    for _ in range(2):
        assert login(client, "beto", temp).headers["location"] == "/equipe/senha"
        assert client.get("/equipe", follow_redirects=False).headers["location"] == "/equipe/senha"
        client.post("/logout")
    r = client.post("/equipe/senha", data={"current": temp, "new": OWN, "confirm": OWN}, follow_redirects=False)
    assert r.status_code == 401                                     # deslogado não troca senha de ninguém


@pytest.mark.parametrize("path", ["/static/../admin/usuarios", "/static/../equipe/vendas", "/static/..%2fequipe",
                                  "/cron/../admin/usuarios", "/cron/../equipe", "/static/../../app/auth.py",
                                  "/static/..\\..\\app\\auth.py"])
def test_public_prefixes_do_not_reach_private_pages(client, path):
    """/static/ e /cron/ passam pelo middleware sem sessão: não podem resolver para outra rota."""
    temp_cookie = None
    assert login(client, "beto", give_temp("beto")).status_code == 303
    temp_cookie = token(client)
    for cookie in (None, temp_cookie):
        for method in ("GET", "POST"):
            status, _, body = raw(method, path, cookie)
            assert status in (404, 405), (path, status)
            assert "Usuários da equipe" not in body and "def " not in body


# =======================================================================================
# (3) enumeração de usuário
# =======================================================================================

def _failed_login(client, username, password):
    auth.reset_failures()
    r = login(client, username, password)
    body = r.text.replace(f'value="{username}"', 'value="X"')
    heads = {k: v for k, v in r.headers.items() if k.lower() not in ("content-length",)}
    return r.status_code, body, heads


def test_login_failure_is_identical_for_every_kind_of_user(client, monkeypatch):
    give_own("beto")                       # ativo com senha
    give_own("arthur", OTHER)
    set_active("arthur", False)            # desativado (com senha)
    give_temp("thiago")                    # só provisória
    users._burn("aquecimento")             # o hash de comparação nasce na 1ª vez
    calls = []
    real = hashlib.scrypt
    monkeypatch.setattr(hashlib, "scrypt", lambda *a, **kw: (calls.append(1), real(*a, **kw))[1])
    cases = {
        "inexistente": ("fulano", "qualquer-coisa-1"),
        "sem senha": ("marcelo", "qualquer-coisa-1"),
        "sem senha, senha vazia": ("marcelo", ""),
        "desativado, senha errada": ("arthur", "qualquer-coisa-1"),
        "desativado, senha CERTA": ("arthur", OTHER),
        "senha errada": ("beto", "qualquer-coisa-1"),
        "provisória errada": ("thiago", "qualquer-coisa-1"),
        "senha única no usuário dele": ("beto", STAFF_PW),
        "senha de outro": ("beto", OTHER),
    }
    logins_before = {u: urow(u)["last_login"] for u in SEED}
    seen = {}
    for label, (username, password) in cases.items():
        calls.clear()
        seen[label] = _failed_login(client, username, password)
        assert len(calls) == 1, (label, len(calls))                # mesmo custo de hash: o tempo não entrega
        assert COOKIE not in client.cookies
    reference = seen["inexistente"]
    assert reference[0] == 401 and auth.LOGIN_ERROR in reference[1]
    for label, got in seen.items():
        assert got == reference, label
    assert {u: urow(u)["last_login"] for u in SEED} == logins_before
    for word in ("desativado", "inativo", "não existe", "inexistente", "sem senha", "provisória"):
        assert word not in reference[1].lower()


def test_login_page_and_blocked_page_do_not_reveal_users(client):
    give_own("beto")
    html = client.get("/login").text
    for name in SEED:
        assert name not in html.replace("Ex.: arthur", "").lower()
    for username in ("beto", "fulano"):                            # o bloqueio também é igual
        auth.reset_failures()
        for _ in range(auth.MAX_FAILURES):
            assert login(client, username, "errada-1").status_code == 401
        r = login(client, username, "errada-1")
        assert r.status_code == 429 and "Muitas tentativas" in r.text
    r = client.get("/login?username=beto&password=" + OWN, follow_redirects=False)
    assert r.status_code == 200 and OWN not in r.text and COOKIE not in client.cookies


# =======================================================================================
# (4) senhas nunca em URL, cookie, log, banco em texto, nem no HTML fora da resposta única
# =======================================================================================

NEWPW_RE = re.compile(r'id="newpw-pass"[^>]*>([^<]+)<')


def _generate(client, username):
    """Como admin: gera a provisória pela tela e devolve (resposta, senha lida do HTML)."""
    use(client, old_cookie("admin", ADMIN_PW))
    r = client.post(f"/admin/usuarios/{uid(username)}/senha", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    found = NEWPW_RE.findall(r.text)
    assert len(found) == 1
    return r, found[0].strip()


def _whole_database() -> str:
    def dump(c):
        return "\n".join(repr(tuple(r)) for table in db.TABLES for r in c.execute(f"SELECT * FROM {table}"))
    return _db(dump)


def test_passwords_leak_nowhere(client, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    seen_urls, seen_cookies, pages = [], [], []

    def note(r):
        seen_urls.append(str(r.request.url) + " -> " + r.headers.get("location", ""))
        seen_cookies.append(r.headers.get("set-cookie", ""))
        return r

    shown, temp = _generate(client, "beto")
    note(shown)
    assert shown.headers["cache-control"] == "no-store" and "set-cookie" not in shown.headers
    assert shown.text.count(temp) == 1                              # só na caixa, nem em data-*, nem em script
    assert re.fullmatch(r"[a-km-zA-HJ-NP-Z2-9]{12}", temp) and not set(temp) & set("0O1lI")
    assert users.verify_password(temp, urow("beto")["password_hash"])
    pages.append(note(client.get("/admin/usuarios")).text)          # recarregar a lista: a senha sumiu
    pages.append(note(client.get("/admin/usuarios?msg=saved")).text)
    pages.append(note(client.get("/")).text)

    note(login(client, "beto", "chute-errado-1"))
    note(login(client, "beto", temp))
    pages.append(note(client.get("/equipe/senha")).text)
    note(client.post("/equipe/senha", data={"current": "chute-errado-2", "new": OWN, "confirm": OWN},
                     follow_redirects=False))
    note(client.post("/equipe/senha", data={"current": temp, "new": "curta", "confirm": "curta"},
                     follow_redirects=False))
    note(client.post("/equipe/senha", data={"current": temp, "new": temp, "confirm": temp}, follow_redirects=False))
    note(client.post("/equipe/senha", data={"current": temp, "new": OWN, "confirm": OTHER}, follow_redirects=False))
    ok = note(client.post("/equipe/senha", data={"current": temp, "new": OWN, "confirm": OWN},
                          follow_redirects=False))
    assert ok.headers["location"] == "/equipe"
    for path in ("/equipe", "/equipe/vendas", "/equipe/senha", "/equipe/senha?msg=saved"):
        pages.append(note(client.get(path)).text)
    note(login(client, "beto", OWN))
    note(login(client, "", STAFF_PW))
    note(login(client, "admin", ADMIN_PW))
    pages.append(note(client.get("/admin/usuarios")).text)

    out = capsys.readouterr()
    haystacks = {"log": caplog.text, "stdout": out.out, "stderr": out.err, "urls": "\n".join(seen_urls),
                 "cookies": "\n".join(seen_cookies), "banco": _whole_database(), "páginas": "\n".join(pages)}
    for secret in (temp, OWN, OTHER, ADMIN_PW, STAFF_PW, SECRET, "chute-errado-1", "chute-errado-2"):
        for where, text in haystacks.items():
            assert secret not in text, (where, secret[:3] + "…")
    for where in ("log", "urls", "cookies", "páginas", "stdout", "stderr"):
        assert "scrypt$" not in haystacks[where] and "pbkdf2" not in haystacks[where], where
    stored = urow("beto")["password_hash"]
    assert stored.startswith("scrypt$") and stored not in haystacks["páginas"]


def test_password_pages_are_never_cached(client):
    shown, _ = _generate(client, "beto")
    assert shown.headers["cache-control"] == "no-store"
    assert client.get("/admin/usuarios").headers["cache-control"] == "no-store"
    assert login(client, "beto", NEWPW_RE.findall(shown.text)[0]).status_code == 303
    assert client.get("/equipe/senha").headers["cache-control"] == "no-store"


def test_login_cookie_is_httponly_lax_and_secure_on_https(client, monkeypatch):
    give_own("beto")
    r = login(client, "beto", OWN)
    flags = r.headers["set-cookie"].lower()
    assert "httponly" in flags and "samesite=lax" in flags and "path=/" in flags and "secure" not in flags
    client.cookies.clear()
    r = client.post("/login", data={"username": "beto", "password": OWN}, headers={"x-forwarded-proto": "https"},
                    follow_redirects=False)
    assert "secure" in r.headers["set-cookie"].lower()


def test_hashes_are_salted_and_slow(conn):
    for name in ("beto", "arthur"):
        temp = users.reset_password(conn, uid(name))
        assert users.change_password(conn, uid(name), temp, OWN) == "saved"
    a, b = urow("beto")["password_hash"], urow("arthur")["password_hash"]
    assert a != b and a.split("$")[4] != b.split("$")[4]            # mesma senha, sal e hash diferentes
    scheme, n, r, p, salt, digest = a.split("$")
    assert (scheme, int(r), int(p)) == ("scrypt", 8, 1) and int(n) >= 2 ** 14
    assert len(bytes.fromhex(salt)) >= 16 and len(bytes.fromhex(digest)) >= 32
    for forged in ("", "scrypt$", "scrypt$1$1$1$$", "plain$" + OWN, OWN, "scrypt$16384$8$1$zz$zz", None):
        assert users.verify_password(OWN, forged) is False


def test_temp_passwords_are_unpredictable():
    made = {users.generate_temp_password() for _ in range(300)}
    assert len(made) == 300
    assert all(len(p) >= 10 and not set(p) & set("0O1lI") for p in made)
    assert users.generate_temp_password.__module__ == "app.services.users"
    import inspect
    source = inspect.getsource(users.generate_temp_password)
    assert "secrets." in source and "random." not in source


# =======================================================================================
# (6) transição da senha única
# =======================================================================================

def test_shared_password_full_cycle(client):
    assert login(client, "equipe", STAFF_PW).status_code == 303     # todos sem senha: vale
    shared = token(client)
    assert shared.startswith("v2.staff.") and is_in(client)
    assert "Olá," not in client.get("/equipe").text                 # sessão sem nome
    for name in SEED[:-1]:
        give_temp(name)
        assert is_in(use(client, shared)), name                     # ainda falta alguém
    give_temp(SEED[-1])
    assert not is_in(use(client, shared))                           # todos têm: sessão antiga cai
    for username in ("", "equipe", "EQUIPE"):
        auth.reset_failures()
        assert login(client, username, STAFF_PW).status_code == 401
    assert not is_in(use(client, old_cookie("staff", STAFF_PW)))    # nem cookie recém-forjado com a senha certa


def test_shared_password_reopens_when_admin_creates_a_user(client):
    """Comportamento do contrato (reportado como risco): funcionário novo sem senha reabre a senha única,
    inclusive para cookies antigos dela, até o admin gerar a provisória."""
    assert login(client, "", STAFF_PW).status_code == 303
    shared = token(client)
    everyone_gets_a_password()
    assert not is_in(use(client, shared))
    use(client, old_cookie("admin", ADMIN_PW))
    assert "senha única antiga ainda funciona" not in client.get("/admin/usuarios").text
    r = client.post("/admin/usuarios", data={"name": "Novo", "username": "novo", "roles": "vendedor"},
                    follow_redirects=False)
    assert r.headers["location"] == "/admin/usuarios?msg=saved"
    assert "senha única antiga ainda funciona" in client.get("/admin/usuarios").text   # a tela avisa o admin
    assert is_in(use(client, shared))                               # cookie de semanas atrás voltou a valer
    assert login(client, "", STAFF_PW).status_code == 303
    _generate(client, "novo")
    assert not is_in(use(client, shared))
    auth.reset_failures()
    assert login(client, "", STAFF_PW).status_code == 401


def test_deactivating_the_last_pending_user_also_closes_it(client):
    for name in SEED[1:]:
        give_temp(name)
    assert login(client, "", STAFF_PW).status_code == 303
    shared = token(client)
    set_active("arthur", False)                                     # o único sem senha saiu da empresa
    assert not is_in(use(client, shared))
    set_active("arthur", True)                                      # reativado ainda sem senha: reabre
    assert is_in(use(client, shared))


def test_deactivated_employee_can_still_use_shared_password_during_transition(client):
    """Limite da transição (reportado): desativar alguém não tira dele a senha única que ele já conhece."""
    cookie = staff_cookie(client)
    set_active("beto", False)
    assert not is_in(use(client, cookie))
    assert login(client, "", STAFF_PW).status_code == 303 and is_in(client)
    assert "Olá, Beto" not in client.get("/equipe").text


def test_without_staff_password_shared_login_never_works(client, monkeypatch):
    stale = old_cookie("staff", STAFF_PW)
    monkeypatch.delenv("STAFF_PASSWORD")
    assert all(r[4] is None for r in all_users())                   # todos pendentes, e mesmo assim não vale
    for username, password in (("", STAFF_PW), ("equipe", STAFF_PW), ("equipe", ""), ("", ""), ("equipe", "None")):
        auth.reset_failures()
        assert login(client, username, password).status_code == 401
    for value in (stale, old_cookie("staff", ""), old_cookie("staff", "None")):
        assert not is_in(use(client, value))
    monkeypatch.setenv("STAFF_PASSWORD", "")
    auth.reset_failures()
    assert login(client, "equipe", "").status_code == 401
    assert not is_in(use(client, old_cookie("staff", "")))


def test_shared_password_is_nobodys_personal_password(client):
    give_own("beto")
    for username in SEED:
        auth.reset_failures()
        assert login(client, username, STAFF_PW).status_code == 401
    auth.reset_failures()
    assert login(client, "equipe", OWN).status_code == 401          # e a senha pessoal não vale como senha única
    assert login(client, "", OWN).status_code == 401


def test_shared_session_cannot_change_anyones_password(client):
    temp = give_temp("beto")
    assert login(client, "", STAFF_PW).status_code == 303
    before = all_users()
    r = client.post("/equipe/senha", data={"current": temp, "new": OWN, "confirm": OWN, "username": "beto",
                                           "user_id": str(uid("beto")), "id": str(uid("beto"))},
                    follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe")
    assert all_users() == before
    r = client.get("/equipe/senha", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe")


# =======================================================================================
# (7) entradas hostis
# =======================================================================================

HOSTILE = ["' OR '1'='1", "beto' --", "beto'; DROP TABLE users; --", "%", "_eto", "b%", "bet?", "beto\x00",
           "\x00beto", "be\x00to", "ｂｅｔｏ", "betο", "béto", "beto​", "‮beto", "beto\nadmin",
           "beto,arthur", "b" * 1000, "b" * 100_000, "../beto", "<script>alert(1)</script>", "{{7*7}}", "beto beto",
           "𝓫𝓮𝓽𝓸", "BETOİ", "1", "1 OR 1=1", "null", "undefined"]


def test_hostile_usernames_never_log_in_and_never_break(client):
    give_own("beto")
    for username in HOSTILE:
        auth.reset_failures()
        for password in (OWN, "", "x"):
            auth.reset_failures()
            r = login(client, username, password)
            assert r.status_code == 401, (ascii(username)[:30], r.status_code)
            assert COOKIE not in client.cookies
            assert "<script>alert(1)</script>" not in r.text and "49" not in r.text.split("login-form")[1][:400]
            assert len(r.text) < 20_000                             # não devolve o texto gigante
    assert len(all_users()) == len(SEED)
    for username in ("beto", "BETO", "  Beto  ", "\tbeto\n"):       # o que o contrato manda aceitar
        auth.reset_failures()
        assert login(client, username, OWN).status_code == 303, ascii(username)


def test_hostile_passwords_never_break(client):
    give_own("beto")
    # (OWN + NUL no FIM entra: o HMAC do scrypt completa a chave com zeros, então é a mesma senha.
    #  Quem acerta isso já sabe a senha: informativo, sem risco.)
    for password in ("", " ", "\x00", OWN + "\x00x", "\x00" + OWN, OWN + " ", " " + OWN, OWN.upper(), OWN[:-1],
                     "x" * 201, "x" * 100_000, "' OR '1'='1", "日本語のパスワード", "🔑" * 50, OWN + "\n"):
        for username in ("beto", "", "admin", "equipe", "fulano"):
            auth.reset_failures()
            assert login(client, username, password).status_code == 401, (username, ascii(password)[:20])
    auth.reset_failures()
    assert login(client, "beto", OWN).status_code == 303


VARIED_200 = ("k7#Qm2 zP9!xW4-vB" * 12)[:200]


@pytest.mark.parametrize("password", ["sênha çom açento", "日本語のパスワード", "🔑🚗🏁🎯🌙⭐🍀🎲", "  com espaços  ",
                                      "aspas'\"<>&;--", VARIED_200])
def test_unusual_but_valid_passwords_work_exactly(client, password):
    temp = give_temp("beto")
    assert login(client, "beto", temp).status_code == 303
    r = client.post("/equipe/senha", data={"current": temp, "new": password, "confirm": password},
                    follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe")
    assert login(client, "beto", password).status_code == 303
    auth.reset_failures()
    assert login(client, "beto", password.strip() + "x").status_code == 401
    if password != password.strip():
        assert login(client, "beto", password.strip()).status_code == 401   # espaço faz parte da senha
    assert password not in _whole_database()


BAD_USERNAMES = ["", "a", "a" * 31, "a" * 1000, "com espaço", "joão", "jo\x00ao", "a/b", "a@b", "a'b", 'a"b',
                 "<b>x</b>", "admin", "ADMIN", " Admin ", "equipe", "Equipe", "a\nb", "ａｂ", "a,b", "a;b", "😀😀",
                 "' OR 1=1 --", "a%b", "a b"]


@pytest.mark.parametrize("username", BAD_USERNAMES)
def test_create_user_rejects_bad_usernames(client, username):
    use(client, old_cookie("admin", ADMIN_PW))
    r = client.post("/admin/usuarios", data={"name": "Pessoa", "username": username, "roles": "vendedor"},
                    follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/admin/usuarios?msg=invalid")
    assert len(all_users()) == len(SEED)


@pytest.mark.parametrize("username", ["beto", "BETO", "Beto", "  beto  ", "bEtO"])
def test_create_user_duplicate_in_any_case(client, username):
    give_own("beto")
    before = all_users()
    use(client, old_cookie("admin", ADMIN_PW))
    r = client.post("/admin/usuarios", data={"name": "Outro Beto", "username": username, "roles": "chefe"},
                    follow_redirects=False)
    assert r.headers["location"] == "/admin/usuarios?msg=exists"
    assert all_users() == before                                    # não renomeou, não zerou a senha


@pytest.mark.parametrize("data", [
    {"name": "", "roles": "vendedor"}, {"name": "   ", "roles": "vendedor"}, {"name": "x" * 61, "roles": "vendedor"},
    {"name": "x" * 5000, "roles": "vendedor"}, {"name": "Ok"}, {"name": "Ok", "roles": ""},
    {"name": "Ok", "roles": "admin"}, {"name": "Ok", "roles": "dono"}, {"name": "Ok", "roles": ["vendedor", "admin"]},
    {"name": "Ok", "roles": "vendedor,admin"}, {"name": "Ok", "roles": "<script>"}, {"name": "Ok", "roles": "x" * 2000},
    {"name": "Ok", "roles": "vendedor\x00"},
])
def test_create_user_rejects_bad_names_and_roles(client, data):
    use(client, old_cookie("admin", ADMIN_PW))
    r = client.post("/admin/usuarios", data={"username": "novo", **data}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/admin/usuarios?msg=invalid")
    assert len(all_users()) == len(SEED)


def test_extra_form_fields_cannot_set_password_or_flags(client):
    """Mass assignment: campos a mais no cadastro não dão senha, perfil nem pulam o must_change."""
    use(client, old_cookie("admin", ADMIN_PW))
    r = client.post("/admin/usuarios", data={
        "name": "Novo", "username": "novo", "roles": "chefe", "password": OWN, "password_hash": "x",
        "must_change": "0", "active": "0", "pw_version": "9", "id": "1", "role": "admin", "is_admin": "1"},
        follow_redirects=False)
    assert r.headers["location"] == "/admin/usuarios?msg=saved"
    row = urow("novo")
    assert (row["password_hash"], row["must_change"], row["pw_version"], row["active"], row["roles"]) == \
        (None, 1, 0, 1, "chefe")
    assert row["id"] == len(SEED) + 1
    assert login(client, "novo", OWN).status_code == 401
    auth.reset_failures()
    assert login(client, "novo", "").status_code == 401


def test_hostile_display_name_is_escaped_everywhere(client):
    evil = '<img src=x onerror=alert(1)>"\'{{7*7}}'
    use(client, old_cookie("admin", ADMIN_PW))
    r = client.post("/admin/usuarios", data={"name": evil, "username": "xss", "roles": ["chefe", "vendedor"]},
                    follow_redirects=False)
    assert r.headers["location"] == "/admin/usuarios?msg=saved"
    listing = client.get("/admin/usuarios").text
    shown, temp = _generate(client, "xss")
    assert login(client, "xss", temp).status_code == 303
    forced = client.get("/equipe/senha").text
    client.post("/equipe/senha", data={"current": temp, "new": OWN, "confirm": OWN})
    pages = [listing, shown.text, forced, client.get("/equipe").text, client.get("/equipe/vendas").text,
             client.get("/equipe/senha").text]
    for html in pages:
        assert "<img src=x" not in html and "onerror=alert(1)>" not in html
        assert "&lt;img src=x onerror=alert(1)&gt;" in html
        assert "{{7*7}}" in html.replace("&#34;", '"').replace("&#39;", "'") and "None" not in html
    assert "Vendedor e chefe" in listing or "Vendedor, chefe" in listing or "vendedor e chefe" in listing.lower()


def test_login_echo_of_username_is_escaped_and_short(client):
    r = login(client, '"><script>alert(1)</script>' + "a" * 500, "x")
    assert r.status_code == 401 and "<script>alert(1)" not in r.text and "a" * 60 not in r.text
    r = login(client, "beto", '"><script>alert(2)</script>')
    assert "alert(2)" not in r.text                                 # a senha digitada nunca volta


@pytest.mark.parametrize("name", ["Jo\x00ao", "\x00", "João\x00", "Jo\x1bao", "Jo​ao", "‮João", "Jo\x7fao",
                                  "Jo﻿ao", "Jo⁠ao"])
def test_control_characters_in_name_are_refused(client, name):
    """NUL derrubava o INSERT no Postgres (500). Controle/invisível no nome agora é 'invalid' nos dois bancos."""
    use(client, old_cookie("admin", ADMIN_PW))
    r = client.post("/admin/usuarios", data={"name": name, "username": "nulo", "roles": "chefe"},
                    follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/admin/usuarios?msg=invalid")
    assert len(all_users()) == len(SEED)


@pytest.mark.parametrize("name", ["João da Silva", "Zé", "D'Ávila", "Ana-Maria", "José  Luís", "Müller Ñandú", "李小龙"])
def test_ordinary_names_are_still_accepted(client, name):
    use(client, old_cookie("admin", ADMIN_PW))
    r = client.post("/admin/usuarios", data={"name": name, "username": "novo", "roles": "chefe"},
                    follow_redirects=False)
    assert r.headers["location"] == "/admin/usuarios?msg=saved"
    assert urow("novo")["name"] == " ".join(name.split())


@pytest.mark.parametrize("bad", ["0", "-1", "999999", str(2 ** 31), str(2 ** 63), "9" * 30])
def test_admin_actions_on_unknown_or_huge_user_ids(client, bad):
    use(client, old_cookie("admin", ADMIN_PW))
    before = all_users()
    assert client.post(f"/admin/usuarios/{bad}/senha").status_code == 404
    assert client.post(f"/admin/usuarios/{bad}/ativo", data={"value": "0"}).status_code == 404
    assert all_users() == before


@pytest.mark.parametrize("value", ["", "2", "-1", "true", "on", "01", " 1", "1 ", "0x1", "１", "1\x00"])
def test_active_toggle_rejects_other_values(client, value):
    use(client, old_cookie("admin", ADMIN_PW))
    before = all_users()
    r = client.post(f"/admin/usuarios/{uid('beto')}/ativo", data={"value": value}, follow_redirects=False)
    assert r.headers["location"] == "/admin/usuarios?msg=invalid"
    assert all_users() == before


# =======================================================================================
# (8) POST /equipe/senha
# =======================================================================================

@pytest.fixture
def beto(client):
    """Beto logado com senha própria; devolve o cookie."""
    return staff_cookie(client)


@pytest.mark.parametrize("data, msg", [
    ({"current": "errada-123", "new": OTHER, "confirm": OTHER}, "wrong"),
    ({"current": "", "new": OTHER, "confirm": OTHER}, "wrong"),
    ({"current": OWN + " ", "new": OTHER, "confirm": OTHER}, "wrong"),
    ({"current": OWN.upper(), "new": OTHER, "confirm": OTHER}, "wrong"),
    ({"current": STAFF_PW, "new": OTHER, "confirm": OTHER}, "wrong"),
    ({"current": ADMIN_PW, "new": OTHER, "confirm": OTHER}, "wrong"),
    ({"current": "x" * 5000, "new": OTHER, "confirm": OTHER}, "wrong"),
    ({"new": OTHER, "confirm": OTHER}, "wrong"),
    ({"current": OWN, "new": "kz7#Qm2", "confirm": "kz7#Qm2"}, "weak"),
    ({"current": OWN, "new": "", "confirm": ""}, "weak"),
    ({"current": OWN}, "weak"),
    ({"current": OWN, "new": " " * 12, "confirm": " " * 12}, "weak"),
    ({"current": OWN, "new": "x" * 201, "confirm": "x" * 201}, "weak"),
    ({"current": OWN, "new": OTHER, "confirm": OTHER + "x"}, "weak"),
    ({"current": OWN, "new": OTHER, "confirm": ""}, "weak"),
    ({"current": OWN, "new": OTHER}, "weak"),
    ({"current": OWN, "new": OTHER, "confirm": OTHER.upper()}, "weak"),
    ({"current": OWN, "new": OWN, "confirm": OWN}, "same"),
    ({}, "wrong"),
])
def test_change_password_refusals_change_nothing(client, beto, data, msg):
    before = urow("beto")
    r = client.post("/equipe/senha", data=data, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, f"/equipe/senha?msg={msg}")
    assert "set-cookie" not in r.headers
    assert urow("beto") == before and is_in(use(client, beto))      # nada mudou e a sessão continua
    page = client.get(r.headers["location"])
    assert page.status_code == 200 and "role=\"alert\"" in page.text
    for value in data.values():
        assert not value.strip() or value not in page.text


def test_wrong_current_with_mismatched_confirm_does_not_tell_which(client, beto):
    r = client.post("/equipe/senha", data={"current": "errada", "new": OTHER, "confirm": "diferente-9"},
                    follow_redirects=False)
    assert r.headers["location"] == "/equipe/senha?msg=weak"


def test_change_password_success_and_only_own_account(client, beto):
    give_own("arthur", OWN)
    arthur_before = urow("arthur")
    r = client.post("/equipe/senha", data={"current": OWN, "new": OTHER, "confirm": OTHER, "username": "arthur",
                                           "user_id": str(arthur_before["id"]), "id": str(arthur_before["id"])},
                    follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe")
    assert urow("arthur") == arthur_before                          # o campo `username` do formulário é ignorado
    assert login(client, "beto", OWN).status_code == 401
    assert login(client, "beto", OTHER).status_code == 303
    assert login(client, "arthur", OWN).status_code == 303
    row = urow("beto")
    assert row["must_change"] == 0 and users.verify_password(OTHER, row["password_hash"])


def test_change_password_msg_is_whitelisted(client, beto):
    for msg in ("<script>alert(1)</script>", "x" * 500, "saved&x=1", "../admin"):
        html = client.get("/equipe/senha", params={"msg": msg}).text
        assert "<script>alert(1)" not in html and "x" * 100 not in html and "pw-ok" not in html.split("<form")[0]


def test_change_password_has_no_attempt_limit(client, beto):
    """Documenta o desvio apontado pela Forja: /equipe/senha não conta tentativas (risco BAIXO: exige sessão
    já aberta, cada chute custa um scrypt, e não dá acesso a nada que a sessão já não tenha)."""
    before = urow("beto")
    for attempt in range(12):
        r = client.post("/equipe/senha", data={"current": f"chute-{attempt}", "new": OTHER, "confirm": OTHER},
                        follow_redirects=False)
        assert (r.status_code, r.headers["location"]) == (303, "/equipe/senha?msg=wrong")
    assert urow("beto") == before
    assert auth.retry_after("testclient") == 0                      # e não gasta o limite do /login


WEAK = [
    "12345678", "aaaaaaaa", "betobeto", "beto1234", "        a", "password",
    "🔑🔑🔑🔑🔑🔑🔑🔑", "x" * 200, "abababab", "a a a a a a a a",           # um ou dois caracteres repetidos
    "BETO1234", "Beto@2026", "Béto1234", "ｂｅｔｏ1234", "xxBeToxx9", "o beto é 10",  # usuário/1º nome: caixa, acento
    "  12345678  ", "１２３４５６７８", "87654321", "23456789", "abcdefgh", "ABCDEFGH",   # sequências e pontas
    "Senha123", "SENHA@2026", "sénha 2026", "Union#2026", "union veiculos 1", "QWERTYUI", "Password1",
    "   1234567   ", "abc def", "1234567\t",                                   # 8 só contando espaço das pontas
]
STRONG_ENOUGH = ["cachorro azul", "gato2024", "minha casa 77", "flamengo1981", "Café com Leite", "sol#lua#mar",
                 "maria e joão", "vasco da gama", "tres3tres", "Rua Sete 150"]


@pytest.mark.parametrize("weak", WEAK)
def test_trivial_passwords_are_refused(client, beto, weak):
    before = urow("beto")
    assert users.is_weak_password(weak, "beto", "Beto")
    r = client.post("/equipe/senha", data={"current": OWN, "new": weak, "confirm": weak}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe/senha?msg=weak")
    assert urow("beto") == before and is_in(use(client, beto))
    assert login(client, "beto", weak).status_code == 401


@pytest.mark.parametrize("password", STRONG_ENOUGH)
def test_simple_honest_passwords_are_accepted(client, beto, password):
    """Sem exigir símbolo nem maiúscula: frase curta ou palavra + ano de um leigo tem de passar."""
    assert not users.is_weak_password(password, "beto", "Beto")
    r = client.post("/equipe/senha", data={"current": OWN, "new": password, "confirm": password},
                    follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe")
    assert login(client, "beto", password).status_code == 303


def test_weak_rule_uses_username_and_first_name_only():
    weak = users.is_weak_password
    assert weak("thiago2026x", "thiago", "Thiago") and weak("THIAGO2026x", "thiago", "Thiago")
    assert weak("joão#2026", "jsilva", "João da Silva") and weak("JOAO#2026", "jsilva", "João da Silva")
    assert weak("jsilva#2026", "jsilva", "João da Silva")
    assert not weak("silva#2026", "jsilva", "João da Silva")       # sobrenome não entra (regra da correção)
    assert not weak("jo#2026xyz", "jo", "Jô Soares")                # usuário/nome com menos de 3 letras não conta
    assert not weak("cachorro azul", "", "") and not weak("cachorro azul")
    assert weak("") and weak(None) and weak("x" * 201) and weak("abcdefg")


def test_weak_password_is_refused_on_first_access_too(client):
    temp = give_temp("beto")
    assert login(client, "beto", temp).status_code == 303
    for weak in ("12345678", "beto2026!", "senha123"):
        r = client.post("/equipe/senha", data={"current": temp, "new": weak, "confirm": weak},
                        follow_redirects=False)
        assert r.headers["location"] == "/equipe/senha?msg=weak"
    assert urow("beto")["must_change"] == 1                         # continua preso na troca
    assert client.get("/equipe", follow_redirects=False).headers["location"] == "/equipe/senha"


# =======================================================================================
# (9) limite de tentativas do login
# =======================================================================================

def test_rate_limit_counts_every_failure_from_the_ip(client):
    give_own("beto")
    tries = [("beto", "x1"), ("fulano", "x2"), ("", "x3"), ("equipe", "x4"), ("admin", "x5")]
    assert len(tries) == auth.MAX_FAILURES
    for username, password in tries:
        assert login(client, username, password).status_code == 401
    for username, password in (("beto", OWN), ("", ADMIN_PW), ("equipe", STAFF_PW), ("fulano", "x")):
        r = login(client, username, password)                       # nem a senha certa passa durante o bloqueio
        assert r.status_code == 429 and 0 < int(r.headers["retry-after"]) <= auth.FAILURE_WINDOW + 1
        assert COOKIE not in client.cookies and auth.LOGIN_ERROR not in r.text


def test_rate_limit_blocked_attempts_do_not_reach_the_database(client, monkeypatch):
    give_own("beto")
    for _ in range(auth.MAX_FAILURES):
        login(client, "beto", "errada")
    calls = []
    real = hashlib.scrypt
    monkeypatch.setattr(hashlib, "scrypt", lambda *a, **kw: (calls.append(1), real(*a, **kw))[1])
    stamp = urow("beto")["last_login"]
    for _ in range(5):
        assert login(client, "beto", OWN).status_code == 429
    assert calls == [] and urow("beto")["last_login"] == stamp      # bloqueado não gasta hash nem grava login


def test_rate_limit_window_expires_and_success_clears(client):
    give_own("beto")
    past = time.time() - auth.FAILURE_WINDOW - 5
    for _ in range(auth.MAX_FAILURES):
        auth.register_failure("testclient", past)
    assert login(client, "beto", OWN).status_code == 303            # janela vencida
    for _ in range(auth.MAX_FAILURES - 1):
        assert login(client, "beto", "errada").status_code == 401
    assert login(client, "beto", OWN).status_code == 303            # 4 erros + acerto
    assert auth.retry_after("testclient") == 0
    auth.register_failure("10.0.0.9")
    assert auth.retry_after("testclient") == 0                      # por IP


def test_rate_limit_ip_is_not_spoofable_outside_vercel(client):
    for i in range(auth.MAX_FAILURES):
        client.cookies.clear()
        client.post("/login", data={"username": "beto", "password": "x"},
                    headers={"x-forwarded-for": f"9.9.9.{i}", "x-real-ip": f"8.8.8.{i}"})
    r = client.post("/login", data={"username": "beto", "password": "x"}, headers={"x-forwarded-for": "7.7.7.7"})
    assert r.status_code == 429


# =======================================================================================
# (11) F5 / reenvio do POST de gerar senha
# =======================================================================================

def test_resubmitting_generate_password_replaces_the_previous_one(client):
    first_page, first = _generate(client, "beto")
    v1 = urow("beto")["pw_version"]
    assert login(client, "beto", first).status_code == 303
    session = token(client)
    second_page, second = _generate(client, "beto")                 # F5 na tela do admin = outro POST
    row = urow("beto")
    assert second != first and first not in second_page.text
    assert (row["pw_version"], row["must_change"]) == (v1 + 1, 1)
    assert not is_in(use(client, session))                          # quem entrou com a 1ª cai
    assert login(client, "beto", first).status_code == 401          # a 1ª deixa de valer (a tela avisa o admin)
    assert login(client, "beto", second).headers["location"] == "/equipe/senha"
    assert "Não atualize esta página" in second_page.text and "não aparece de novo" in second_page.text
    listing = client.get("/admin/usuarios", follow_redirects=False)
    assert listing.status_code == 303                               # (sessão é do beto) e nada de senha em GET
    use(client, old_cookie("admin", ADMIN_PW))
    again = client.get("/admin/usuarios").text
    assert first not in again and second not in again and "newpw-pass" not in again.split("<script>")[0]


def test_generate_password_never_answers_get(client):
    use(client, old_cookie("admin", ADMIN_PW))
    before = all_users()
    assert client.get(f"/admin/usuarios/{uid('beto')}/senha").status_code == 405
    assert client.get(f"/admin/usuarios/{uid('beto')}/ativo").status_code == 405
    assert all_users() == before


def test_generating_for_one_user_touches_only_that_user(client):
    give_own("arthur", OTHER)
    others = [r for r in all_users() if r[1] != "beto"]
    _generate(client, "beto")
    assert [r for r in all_users() if r[1] != "beto"] == others
    assert login(client, "arthur", OTHER).status_code == 303


# =======================================================================================
# (10) migração + seed 2x nos dois bancos
# =======================================================================================

def _seed_rows(conn):
    rows = [tuple(r) for r in conn.execute(
        "SELECT id, username, name, roles, password_hash, must_change, pw_version, active, last_login "
        "FROM users ORDER BY id")]
    conn.commit()
    return rows


EXPECTED_SEED = [
    (1, "arthur", "Arthur", "comprador,vendedor", None, 1, 0, 1, None),
    (2, "marcelo", "Marcelo", "comprador,vendedor", None, 1, 0, 1, None),
    (3, "beto", "Beto", "vendedor", None, 1, 0, 1, None),
    (4, "thiago", "Thiago", "vendedor", None, 1, 0, 1, None),
    (5, "haroldo", "Haroldo", "documentador", None, 1, 0, 1, None),
    (6, "ruan", "Ruan", "chefe", None, 1, 0, 1, None),
]


def _seed_scenario(conn):
    """Depois do 1º init_db: seed certo; mexe nos funcionários; init_db 2x não desfaz nada."""
    assert _seed_rows(conn) == EXPECTED_SEED
    db.init_db()
    assert _seed_rows(conn) == EXPECTED_SEED                        # 2ª vez: nada duplicado
    temp = users.reset_password(conn, 1)
    assert users.change_password(conn, 1, temp, OWN) == "saved"     # arthur com senha própria
    users.reset_password(conn, 2)                                   # marcelo só com provisória
    assert users.set_active(conn, 3, False)                         # beto desativado
    conn.execute("UPDATE users SET name = 'Thiago Silva', roles = 'chefe' WHERE id = 4")
    assert users.create_user(conn, "novo", "Novo", ["vendedor"]) == "saved"
    conn.commit()
    full = [tuple(r) for r in conn.execute("SELECT * FROM users ORDER BY id")]
    conn.commit()
    db.init_db()
    db.init_db()
    assert [tuple(r) for r in conn.execute("SELECT * FROM users ORDER BY id")] == full
    conn.commit()
    assert users.verify_password(OWN, conn.execute("SELECT password_hash FROM users WHERE id = 1").fetchone()[0])
    assert tuple(conn.execute("SELECT active, pw_version FROM users WHERE id = 3").fetchone()) == (0, 1)
    assert conn.execute("SELECT id FROM users WHERE username = 'novo'").fetchone()[0] == 7   # seed não queima id
    conn.commit()
    with pytest.raises(Exception):                                  # UNIQUE de verdade nos dois bancos
        conn.execute("INSERT INTO users (username, name, roles, created_at) VALUES ('arthur', 'X', '', 'x')")
    conn.rollback()
    with pytest.raises(Exception):
        conn.execute("UPDATE users SET must_change = NULL WHERE id = 1")
    conn.rollback()


def test_seed_from_current_schema_twice(conn):
    _seed_scenario(conn)


def test_seed_recreates_a_hand_deleted_username_without_password(conn, client):
    """Desvio da Forja (reportado): apagar um funcionário do seed direto no banco faz o seed recriá-lo
    SEM senha no próximo cold start, o que reabre a senha única. Pela tela só existe desativar."""
    everyone_gets_a_password()
    assert not auth.shared_password_active()
    conn.execute("DELETE FROM users WHERE username = 'ruan'")
    conn.commit()
    db.init_db()
    row = urow("ruan")
    assert (row["password_hash"], row["active"]) == (None, 1) and auth.shared_password_active()


@sqlite_only
def test_sqlite_migration_from_production_schema_with_data(tmp_path, monkeypatch):
    path = tmp_path / "producao.db"
    monkeypatch.setenv("UNION_DB_PATH", str(path))
    text = (FIXTURES / "schema_6a50942.sql").read_text(encoding="utf-8")
    assert "users" not in text
    rawdb = sqlite3.connect(path)
    rawdb.executescript(text)
    _fill_old(rawdb.execute, has_finance=True)
    rawdb.commit()
    rawdb.close()
    conn = db.connect()
    try:
        assert "users" not in _tables(conn)
        before = _dump(conn, ())
        db.init_db()
        assert "users" in _tables(conn) and _dump(conn, ("not_sale",)) == before
        _seed_scenario(conn)
        assert _dump(conn, ("not_sale",)) == before                 # carros, custos e despesas intactos
    finally:
        conn.close()


@requires_pg
def test_postgres_migration_from_production_schema_with_data(empty_pg):  # noqa: F811
    conn = empty_pg
    text = (FIXTURES / "schema_6a50942.sql").read_text(encoding="utf-8")
    conn.executescript(text.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"))
    _fill_old(conn.execute, has_finance=True)
    conn.commit()
    assert "users" not in _tables(conn)
    before = _dump(conn, ())
    db.init_db()
    assert "users" in _tables(conn) and _dump(conn, ("not_sale",)) == before
    _seed_scenario(conn)
    assert _dump(conn, ("not_sale",)) == before
    kinds = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name = 'users'")}
    conn.commit()
    assert kinds == {
        "id": ("integer", "NO"), "username": ("text", "NO"), "name": ("text", "NO"), "roles": ("text", "NO"),
        "password_hash": ("text", "YES"), "must_change": ("integer", "NO"), "pw_version": ("integer", "NO"),
        "active": ("integer", "NO"), "created_at": ("text", "NO"), "last_login": ("text", "YES")}


@requires_pg
def test_postgres_cold_start_on_production_schema_serves_logins(empty_pg, site):  # noqa: F811
    """Deploy da F15 sobre o banco de hoje: o lifespan migra, o admin antigo segue logado, a senha única
    segue valendo e o fluxo completo de um funcionário funciona."""
    from fastapi.testclient import TestClient

    from app.main import app

    conn = empty_pg
    text = (FIXTURES / "schema_6a50942.sql").read_text(encoding="utf-8")
    conn.executescript(text.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"))
    _fill_old(conn.execute, has_finance=True)
    conn.commit()
    with TestClient(app) as client:
        assert is_admin(use(client, old_cookie("admin", ADMIN_PW, int(time.time()) - 86400)))
        assert is_in(use(client, old_cookie("staff", STAFF_PW, int(time.time()) - 86400)))
        page, temp = _generate(client, "haroldo")
        assert "Haroldo" in page.text
        assert login(client, "Haroldo", temp).headers["location"] == "/equipe/senha"
        assert client.get("/equipe", follow_redirects=False).headers["location"] == "/equipe/senha"
        r = client.post("/equipe/senha", data={"current": temp, "new": OWN, "confirm": OWN}, follow_redirects=False)
        assert r.headers["location"] == "/equipe"
        html = client.get("/equipe").text
        assert "Olá, Haroldo" in html and "Documentador" in html
        assert client.get("/equipe/vendas?mes=2026-09").status_code == 200
        assert client.get("/", follow_redirects=False).headers["location"] == "/equipe"


def test_users_table_is_in_every_schema_list():
    from app import pgcompat

    assert "users" in db.TABLES and "users" in pgcompat.ID_TABLES
    statements = pgcompat.split_statements(db.PG_SCHEMA)
    table = next(s for s in statements if s.startswith("CREATE TABLE IF NOT EXISTS users"))
    assert "--" not in table and table.count("(") == table.count(")") and "SERIAL PRIMARY KEY" in table
    assert all(s.upper().startswith("CREATE ") for s in statements)
