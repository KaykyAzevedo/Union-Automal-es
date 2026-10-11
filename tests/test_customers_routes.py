"""F16 (Forja): rotas /equipe/clientes* (app/main.py). Regras de cliente/aniversário são da Lupa
(app/services/customers.py) e têm testes próprios. Só dados fictícios."""
from __future__ import annotations

import logging

import pytest

from app import auth, db
from app.services import users

ADMIN_PW = "senha-admin-123"
STAFF_PW = "senha-equipe-456"
OWN_PW = "senha-propria-9"
BIG = [str(2**31), str(2**63)]


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
    r = client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    assert r.status_code == 303
    return client


def as_admin(client):
    return login(client, "", ADMIN_PW)


def as_shared(client):
    return login(client, "", STAFF_PW)


def as_user(client, username):
    """Funcionário com senha definitiva (gravada direto no banco: o fluxo de senha é testado na F15)."""
    c = db.connect()
    try:
        c.execute("UPDATE users SET password_hash = ?, must_change = 0 WHERE username = ?",
                  (users.hash_password(OWN_PW), username))
        c.commit()
    finally:
        c.close()
    return login(client, username, OWN_PW)


def today():
    return db.now().date()


def form(**over) -> dict:
    t = today()
    data = {"name": "Cliente Fictício", "phone": "(21) 90000-0001", "birth_day": str(t.day),
            "birth_month": str(t.month), "birth_year": "1990", "email": "cliente@exemplo.test",
            "car_text": "Fiat Uno 2015", "purchase_date": "", "notes": "Prefere contato à tarde"}
    data.update(over)
    return data


def create(client, **over):
    return client.post("/equipe/clientes", data=form(**over), follow_redirects=False)


def rows():
    c = db.connect()
    try:
        return [dict(r) for r in c.execute("SELECT * FROM customers ORDER BY id")]
    finally:
        c.close()


def spy_ctx(monkeypatch) -> dict:
    from app import main

    seen = {}
    original = main.templates.TemplateResponse

    def spy(request, name, ctx=None, **kw):
        seen[name] = ctx
        return original(request, name, ctx, **kw)

    monkeypatch.setattr(main.templates, "TemplateResponse", spy)
    return seen


# ---------- criar ----------

def test_create_as_named_user(client):
    r = create(as_user(client, "arthur"))
    assert (r.status_code, r.headers["location"]) == (303, "/equipe/clientes?msg=saved")
    (c,) = rows()
    t = today()
    assert (c["name"], c["phone"], c["email"]) == ("Cliente Fictício", "21900000001", "cliente@exemplo.test")
    assert (c["birth_day"], c["birth_month"], c["birth_year"]) == (t.day, t.month, 1990)
    assert (c["car_text"], c["notes"], c["purchase_date"]) == ("Fiat Uno 2015", "Prefere contato à tarde", None)
    assert c["created_by_name"] == "Arthur"
    assert c["created_by"] == [u for u in users.list_users(db.connect()) if u["username"] == "arthur"][0]["id"]
    assert c["greeted_year"] is None


def test_create_as_admin_and_shared_password(client):
    create(as_admin(client))
    create(as_shared(client), phone="21900000002")
    first, second = rows()
    assert (first["created_by"], first["created_by_name"]) == (None, "Administrador")
    assert (second["created_by"], second["created_by_name"]) == (None, "Equipe")


def test_birthday_field_or_three_fields(client):
    as_admin(client)
    create(client, birth_day="5", birth_month="3", birth_year="")
    create(client, phone="21900000002", birthday="29/02/1992", birth_day="", birth_month="", birth_year="")
    first, second = rows()
    assert (first["birth_day"], first["birth_month"], first["birth_year"]) == (5, 3, None)
    assert (second["birth_day"], second["birth_month"], second["birth_year"]) == (29, 2, 1992)


@pytest.mark.parametrize("over", [
    {"name": ""}, {"name": "A"}, {"phone": "123"}, {"phone": ""}, {"birth_day": "31", "birth_month": "2"},
    {"birth_day": "", "birth_month": "", "birth_year": ""}, {"email": "sem-arroba"}, {"car_text": "x" * 121},
    {"notes": "x" * 1001}, {"purchase_date": "2999-01-01"}, {"birth_year": "1800"},
])
def test_invalid_rerenders_form_with_typed_values(client, over, monkeypatch):
    seen = spy_ctx(monkeypatch)
    r = create(as_admin(client), **over)
    assert r.status_code == 422 and "location" not in r.headers
    assert r.headers["cache-control"] == "no-store"
    ctx = seen["cliente_form.html"]
    assert ctx["msg"] == "invalid" and ctx["can_delete"] is False
    assert ctx["form"]["notes"] == form(**over)["notes"] and ctx["form"]["name"] == form(**over)["name"]
    assert ctx["customer"]["name"] == form(**over)["name"] and ctx["customer"].get("id") is None
    assert rows() == []
    if over.get("name") != "":
        assert "Fiat Uno 2015" in r.text or "car_text" in over  # o que foi digitado volta para a tela


def test_duplicate_phone(client, monkeypatch):
    as_admin(client)
    create(client)
    seen = spy_ctx(monkeypatch)
    r = create(client, name="Outra Pessoa", phone="+55 21 90000-0001")
    assert r.status_code == 422 and seen["cliente_form.html"]["msg"] == "duplicate"
    assert seen["cliente_form.html"]["form"]["name"] == "Outra Pessoa"
    assert len(rows()) == 1


def test_missing_fields_do_not_crash(client):
    r = as_admin(client).post("/equipe/clientes", data={}, follow_redirects=False)
    assert r.status_code == 422 and rows() == []


# ---------- editar ----------

def test_edit_page_and_update(client, monkeypatch):
    as_user(client, "arthur")
    create(client)
    seen = spy_ctx(monkeypatch)
    page = client.get("/equipe/clientes/1")
    assert page.status_code == 200 and "Cliente Fictício" in page.text
    assert page.headers["cache-control"] == "no-store"
    ctx = seen["cliente_form.html"]
    assert ctx["customer"]["id"] == 1 and ctx["form"] is None and ctx["msg"] is None and ctx["can_delete"] is True
    r = client.post("/equipe/clientes/1", data=form(name="Cliente Renomeado", phone="21 90000-0009"),
                    follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe/clientes?msg=saved")
    (c,) = rows()
    assert (c["name"], c["phone"], c["created_by_name"]) == ("Cliente Renomeado", "21900000009", "Arthur")


def test_edit_by_another_user_keeps_creator(client):
    create(as_user(client, "arthur"))
    r = as_user(client, "beto").post("/equipe/clientes/1", data=form(notes="Atualizado"), follow_redirects=False)
    assert r.status_code == 303
    (c,) = rows()
    assert (c["notes"], c["created_by_name"]) == ("Atualizado", "Arthur")


def test_edit_invalid_and_duplicate_keep_record(client, monkeypatch):
    as_admin(client)
    create(client)
    create(client, name="Segundo Fictício", phone="21900000002")
    seen = spy_ctx(monkeypatch)
    r = client.post("/equipe/clientes/2", data=form(name="Digitado Agora", phone="21900000001"),
                    follow_redirects=False)
    assert r.status_code == 422
    ctx = seen["cliente_form.html"]
    assert ctx["msg"] == "duplicate" and ctx["customer"]["id"] == 2 and ctx["customer"]["name"] == "Digitado Agora"
    assert ctx["can_delete"] is True
    r = client.post("/equipe/clientes/2", data=form(phone="1"), follow_redirects=False)
    assert r.status_code == 422 and seen["cliente_form.html"]["msg"] == "invalid"
    assert [c["name"] for c in rows()] == ["Cliente Fictício", "Segundo Fictício"]
    # salvar o próprio cadastro sem mudar o telefone não é duplicado
    assert client.post("/equipe/clientes/2", data=form(name="Segundo Fictício", phone="21900000002"),
                       follow_redirects=False).status_code == 303


# ---------- lista ----------

def test_list_page_context(client, monkeypatch):
    as_user(client, "arthur")
    create(client)
    seen = spy_ctx(monkeypatch)
    r = client.get("/equipe/clientes?msg=saved")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    ctx = seen["clientes.html"]
    assert ctx["msg"] == "saved" and ctx["q"] == "" and ctx["birthdays_today"] == 1
    view = ctx["view"]
    assert {"today", "week", "month", "all", "counts"} <= set(view)
    assert view["counts"]["today"] == 1 and view["counts"]["total"] == 1
    item = view["today"][0]
    assert item["name"] == "Cliente Fictício" and item["greeted"] is False
    assert item["wa_link"].startswith("https://wa.me/5521900000001?text=") and "Arthur" in item["wa_link"]
    assert "Cliente Fictício" in r.text
    client.get("/equipe/clientes?msg=<script>alert(1)</script>")
    assert seen["clientes.html"]["msg"] is None


def test_search(client, monkeypatch):
    as_admin(client)
    create(client)
    create(client, name="José Açaí", phone="21900000002", car_text="Honda Civic")
    seen = spy_ctx(monkeypatch)
    client.get("/equipe/clientes", params={"q": "  jose acai "})
    ctx = seen["clientes.html"]
    assert ctx["q"] == "jose acai" and [c["name"] for c in ctx["view"]["all"]] == ["José Açaí"]
    client.get("/equipe/clientes", params={"q": "civic"})
    assert [c["name"] for c in seen["clientes.html"]["view"]["all"]] == ["José Açaí"]
    client.get("/equipe/clientes", params={"q": "x" * 500})
    assert len(seen["clientes.html"]["q"]) == 80
    client.get("/equipe/clientes", params={"q": "   "})
    assert seen["clientes.html"]["q"] == "" and len(seen["clientes.html"]["view"]["all"]) == 2


def test_new_page(client, monkeypatch):
    seen = spy_ctx(monkeypatch)
    r = as_shared(client).get("/equipe/clientes/novo")
    assert r.status_code == 200
    ctx = seen["cliente_form.html"]
    assert ctx["customer"] is None and ctx["form"] is None and ctx["can_delete"] is False and ctx["msg"] is None


# ---------- parabéns ----------

def test_greet_and_undo(client):
    as_user(client, "beto")
    create(client)
    r = client.post("/equipe/clientes/1/parabens", data={"value": "1"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe/clientes?msg=saved")
    (c,) = rows()
    assert (c["greeted_year"], c["greeted_by_name"]) == (today().year, "Beto") and c["greeted_at"]
    assert "Beto" in client.get("/equipe/clientes").text
    client.post("/equipe/clientes/1/parabens", data={"value": "0"})
    assert rows()[0]["greeted_year"] is None


def test_greet_names_for_admin_and_shared(client):
    create(as_admin(client))
    client.post("/equipe/clientes/1/parabens", data={"value": "1"})
    assert rows()[0]["greeted_by_name"] == "Administrador"
    as_shared(client).post("/equipe/clientes/1/parabens", data={"value": "1"})
    assert rows()[0]["greeted_by_name"] == "Equipe"


def test_greet_only_on_the_birthday(client):
    """Aniversário não é hoje: marcar é recusado (msg=invalid, nada gravado); desfazer continua valendo."""
    as_admin(client)
    other = 1 if today().day != 1 else 2
    create(client, birth_day=str(other))
    r = client.post("/equipe/clientes/1/parabens", data={"value": "1"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe/clientes?msg=invalid")
    assert rows()[0]["greeted_year"] is None and rows()[0]["greeted_by_name"] is None
    r = client.post("/equipe/clientes/1/parabens", data={"value": "0"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe/clientes?msg=saved")


@pytest.mark.parametrize("value", ["", "2", "sim"])
def test_greet_bad_value_changes_nothing(client, value):
    create(as_admin(client))
    r = client.post("/equipe/clientes/1/parabens", data={"value": value}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe/clientes?msg=invalid")
    assert rows()[0]["greeted_year"] is None


# ---------- excluir ----------

def test_delete_only_creator_or_admin(client):
    create(as_user(client, "arthur"))
    for other in (lambda: as_user(client, "beto"), lambda: as_shared(client)):
        r = other().post("/equipe/clientes/1/excluir", follow_redirects=False)
        assert r.status_code == 403 and "Cliente Fictício" not in r.text
        assert len(rows()) == 1
    r = as_user(client, "arthur").post("/equipe/clientes/1/excluir", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe/clientes?msg=deleted")
    assert rows() == []
    create(as_user(client, "arthur"))
    assert as_admin(client).post("/equipe/clientes/2/excluir", follow_redirects=False).status_code == 303
    assert rows() == []


def test_can_delete_flag(client, monkeypatch):
    create(as_user(client, "arthur"))
    create(as_shared(client), phone="21900000002")
    seen = spy_ctx(monkeypatch)
    expected = [(lambda: as_user(client, "arthur"), True, False), (lambda: as_user(client, "beto"), False, False),
                (lambda: as_shared(client), False, False), (lambda: as_admin(client), True, True)]
    for who, first, second in expected:
        who().get("/equipe/clientes/1")
        assert seen["cliente_form.html"]["can_delete"] is first
        client.get("/equipe/clientes/2")
        assert seen["cliente_form.html"]["can_delete"] is second


# ---------- 404, métodos e permissões ----------

@pytest.mark.parametrize("bad", ["999", "0", "-1", *BIG])
def test_unknown_customer_404(client, bad):
    as_admin(client)
    create(client)
    assert client.get(f"/equipe/clientes/{bad}").status_code == 404
    assert client.post(f"/equipe/clientes/{bad}", data=form(phone="21900000007")).status_code == 404
    assert client.post(f"/equipe/clientes/{bad}/parabens", data={"value": "1"}).status_code == 404
    assert client.post(f"/equipe/clientes/{bad}/excluir").status_code == 404
    assert len(rows()) == 1 and rows()[0]["greeted_year"] is None


def test_non_numeric_id_is_not_a_customer(client):
    as_admin(client)
    assert client.get("/equipe/clientes/abc").status_code == 422
    assert client.get("/equipe/clientes/novo").status_code == 200


ROUTES = [("GET", "/equipe/clientes"), ("GET", "/equipe/clientes/novo"), ("GET", "/equipe/clientes/1"),
          ("POST", "/equipe/clientes"), ("POST", "/equipe/clientes/1"), ("POST", "/equipe/clientes/1/parabens"),
          ("POST", "/equipe/clientes/1/excluir")]


@pytest.mark.parametrize("method,path", ROUTES)
def test_anonymous_is_refused(client, method, path):
    create(as_admin(client))
    client.cookies.clear()
    r = client.request(method, path, data=form(phone="21900000005", value="1"), follow_redirects=False)
    if method == "GET":
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
    else:
        assert r.status_code == 401
    assert "Cliente Fictício" not in r.text
    assert len(rows()) == 1 and rows()[0]["greeted_year"] is None


@pytest.mark.parametrize("method,path", ROUTES)
def test_forced_password_change_blocks_customers(client, method, path):
    create(as_admin(client))
    temp = users.reset_password(db.connect(), 3)
    login(client, "beto", temp)
    r = client.request(method, path, data=form(phone="21900000005", value="1"), follow_redirects=False)
    assert r.status_code in (303, 403) and "Cliente Fictício" not in r.text
    assert len(rows()) == 1 and rows()[0]["greeted_year"] is None


# ---------- selo de aniversariantes ----------

def test_birthdays_today_in_team_pages(client, monkeypatch):
    as_admin(client)
    seen = spy_ctx(monkeypatch)
    client.get("/equipe")
    client.get("/equipe/vendas")
    assert seen["equipe.html"]["birthdays_today"] == 0 and seen["vendas.html"]["birthdays_today"] == 0
    create(client)
    other = 1 if today().day != 1 else 2
    create(client, phone="21900000002", birth_day=str(other))
    client.get("/equipe")
    client.get("/equipe/vendas")
    client.get("/equipe/clientes/novo")
    assert seen["equipe.html"]["birthdays_today"] == 1 and seen["vendas.html"]["birthdays_today"] == 1
    assert seen["cliente_form.html"]["birthdays_today"] == 1


# ---------- dados pessoais fora de log e de URL ----------

def test_personal_data_never_in_logs_or_urls(client, caplog):
    caplog.set_level(logging.DEBUG)
    as_admin(client)
    responses = [create(client), create(client, name="Outro Fictício"), create(client, phone="1"),
                 client.post("/equipe/clientes/1", data=form(phone="1"), follow_redirects=False),
                 client.post("/equipe/clientes/1/parabens", data={"value": "1"}, follow_redirects=False),
                 client.get("/equipe/clientes"), client.get("/equipe/clientes/1"),
                 client.post("/equipe/clientes/1/excluir", follow_redirects=False)]
    for secret in ("Cliente Fictício", "Outro Fictício", "21900000001", "90000-0001", "cliente@exemplo.test",
                   "Prefere contato"):
        assert secret not in caplog.text
        for r in responses:
            assert secret not in r.headers.get("location", "")
            assert secret not in str(r.headers.get("set-cookie", ""))
