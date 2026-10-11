"""F16 (Sentinela): QA independente do cadastro de clientes + aniversários. Tenta quebrar por fora:
permissões, validações, XSS/injeção, link do WhatsApp, fuso e viradas de data, "parabéns enviado",
privacidade (dados pessoais fora de log/URL/cookie), migração a partir do schema da F15 e telas.

TODOS os clientes daqui são fictícios (nomes inventados, telefones de teste)."""
from __future__ import annotations

import html as htmllib
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta
from urllib.parse import unquote

import pytest

from app import auth, db
from app.services import customers, users
from conftest import FIXTURES, PG, requires_pg, sqlite_only
from test_login_qa import (  # noqa: F401  (env é fixture autouse: liga APP_PASSWORD/STAFF_PASSWORD/SESSION_SECRET)
    ADMIN_PW, OTHER, OWN, STAFF_PW, _db, env, give_own, give_temp, login, old_cookie, set_active, staff_cookie,
    token, uid, use,
)
from test_not_sale_qa import _cols, _dump, _fill_old, _tables, empty_pg  # noqa: F401

FAR = date(2100, 1, 1)
BASE = {"name": "Fulano de Teste", "phone": "(21) 99876-5432", "birth_day": "15", "birth_month": "3",
        "birth_year": "1985", "email": "", "car_text": "", "purchase_date": "", "notes": ""}


# ---------- helpers ----------

@pytest.fixture
def clock(monkeypatch):
    """Fixa o relógio do app: clock('2026-03-15T02:30:00+00:00')."""
    def set_(text: str):
        fixed = datetime.fromisoformat(text).astimezone(db.TZ).replace(microsecond=0)
        monkeypatch.setattr(db, "now", lambda: fixed)
        monkeypatch.setattr(customers, "now", lambda: fixed)
        return fixed.date()
    return set_


def form(**over) -> dict:
    return {**BASE, **over}


def rows() -> list[dict]:
    return _db(lambda c: [dict(r) for r in c.execute("SELECT * FROM customers ORDER BY id")])


def crow(customer_id: int) -> dict:
    return _db(lambda c: dict(c.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()))


_counter = iter(range(10_000_000, 99_999_999))


def put(name: str, day: int, month: int, year: int | None = None, by: int | None = None,
        by_name: str = "Administrador", **over) -> int:
    """Grava direto pelo serviço (telefone fictício único) e devolve o id."""
    phone = over.pop("phone", None) or f"219{next(_counter):08d}"
    birthday = f"{day:02d}/{month:02d}" + (f"/{year}" if year else "")

    def run(c):
        assert customers.create_customer(c, {"name": name, "phone": phone, "birthday": birthday, **over},
                                         by, by_name, today=FAR) == "saved"
        return c.execute("SELECT MAX(id) FROM customers").fetchone()[0]
    return _db(run)


def as_admin(client):
    return use(client, old_cookie("admin", ADMIN_PW))


def as_shared(client):
    assert login(client, "", STAFF_PW).status_code == 303
    return client


def as_user(client, username="beto", password=OWN):
    staff_cookie(client, username, password)
    return client


def post(client, path="/equipe/clientes", **over):
    return client.post(path, data=form(**over), follow_redirects=False)


def view(today: date, q: str | None = None, sender: str | None = None) -> dict:
    return _db(lambda c: customers.customers_view(c, today, q, sender))


def names(items) -> list[str]:
    return [i["name"] for i in items]


def hrefs(text: str) -> list[str]:
    return [htmllib.unescape(h) for h in re.findall(r'href="([^"]*)"', text)]


WA_RE = re.compile(r"https://wa\.me/55\d{10,11}(\?text=[A-Za-z0-9%._~-]+)?")


def assert_safe_links(text: str):
    for href in hrefs(text):
        low = href.strip().lower()
        assert not low.startswith(("javascript:", "data:", "vbscript:")), href
        assert low.startswith(("/", "#", "https://")), href
        if "wa.me" in low:
            assert WA_RE.fullmatch(href), href


# =======================================================================================
# (1) permissões
# =======================================================================================

@pytest.mark.parametrize("kind, by_name", [("admin", "Administrador"), ("employee", "Beto"), ("shared", "Equipe")])
def test_admin_employee_and_shared_use_the_whole_module(client, clock, kind, by_name):
    clock("2026-03-15T12:00:00-03:00")                              # dia do aniversário do cliente (15/03)
    {"admin": as_admin, "employee": as_user, "shared": as_shared}[kind](client)
    assert client.get("/equipe/clientes").status_code == 200
    assert client.get("/equipe/clientes/novo").status_code == 200
    r = post(client)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe/clientes?msg=saved")
    row = rows()[0]
    assert (row["created_by"], row["created_by_name"]) == (uid("beto") if kind == "employee" else None, by_name)
    assert (row["name"], row["phone"], row["birth_day"], row["birth_month"], row["birth_year"]) == \
        ("Fulano de Teste", "21998765432", 15, 3, 1985)
    assert client.get(f"/equipe/clientes/{row['id']}").status_code == 200
    r = post(client, f"/equipe/clientes/{row['id']}", name="Fulano Editado")
    assert (r.status_code, r.headers["location"]) == (303, "/equipe/clientes?msg=saved")
    r = client.post(f"/equipe/clientes/{row['id']}/parabens", data={"value": "1"}, follow_redirects=False)
    assert r.status_code == 303
    after = crow(row["id"])
    assert (after["name"], after["greeted_by_name"], after["created_by_name"]) == ("Fulano Editado", by_name, by_name)
    listing = client.get("/equipe/clientes").text
    assert "Fulano Editado" in listing and f"Cadastrado por {by_name}" in listing


def test_anonymous_is_locked_out(client):
    target = put("Cliente Secreto", 15, 3, 1985, notes="observacao sigilosa")
    before = rows()
    for path in ("/equipe/clientes", "/equipe/clientes/novo", f"/equipe/clientes/{target}", "/equipe/clientes?q=Secreto"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next="), path
        assert "Secreto" not in r.text and "sigilosa" not in r.text
        hx = client.get(path, headers={"HX-Request": "true"}, follow_redirects=False)
        assert (hx.status_code, hx.text) == (401, "")
    for path, data in (("/equipe/clientes", form()), (f"/equipe/clientes/{target}", form(name="Invadido")),
                       (f"/equipe/clientes/{target}/parabens", {"value": "1"}),
                       (f"/equipe/clientes/{target}/excluir", {})):
        r = client.post(path, data=data, follow_redirects=False)
        assert r.status_code == 401 and "Secreto" not in r.text, path
    assert rows() == before
    for forged in ("v3.1.0.9999999999.abc", old_cookie("admin", "chute"), old_cookie("staff", "chute")):
        use(client, forged)
        assert client.get("/equipe/clientes", follow_redirects=False).status_code == 303
        assert client.post(f"/equipe/clientes/{target}/excluir", follow_redirects=False).status_code == 401
    assert rows() == before


def test_must_change_blocks_customers(client):
    target = put("Cliente Secreto", 15, 3, 1985)
    assert login(client, "beto", give_temp("beto")).headers["location"] == "/equipe/senha"
    before = rows()
    for path in ("/equipe/clientes", "/equipe/clientes/novo", f"/equipe/clientes/{target}", "/equipe/clientes/"):
        r = client.get(path, follow_redirects=False)
        assert (r.status_code, r.headers["location"]) == (303, "/equipe/senha"), path
    for path, data in (("/equipe/clientes", form()), (f"/equipe/clientes/{target}", form(name="X Y")),
                       (f"/equipe/clientes/{target}/parabens", {"value": "1"}),
                       (f"/equipe/clientes/{target}/excluir", {})):
        r = client.post(path, data=data, follow_redirects=False)
        assert r.status_code == 403 and "Secreto" not in r.text, path
    assert rows() == before
    assert "Secreto" not in client.get("/equipe/senha").text and "tab-badge" not in client.get("/equipe/senha").text


def test_delete_only_admin_or_whoever_registered(client):
    give_own("arthur", OTHER)
    ids = {
        "beto": put("Do Beto", 1, 1, by=uid("beto"), by_name="Beto"),
        "arthur": put("Do Arthur", 2, 1, by=uid("arthur"), by_name="Arthur"),
        "admin": put("Do Admin", 3, 1),
        "shared": put("Da Equipe", 4, 1, by_name="Equipe"),
    }
    as_user(client, "beto")
    for owner in ("arthur", "admin", "shared"):
        page = client.get(f"/equipe/clientes/{ids[owner]}").text
        assert "Excluir cliente" not in page and "/excluir" not in page
        r = client.post(f"/equipe/clientes/{ids[owner]}/excluir", follow_redirects=False)
        assert r.status_code == 403 and "Do " not in r.text and "Da Equipe" not in r.text
    assert len(rows()) == 4
    assert "Excluir cliente" in client.get(f"/equipe/clientes/{ids['beto']}").text
    r = client.post(f"/equipe/clientes/{ids['beto']}/excluir", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe/clientes?msg=deleted")
    assert client.post(f"/equipe/clientes/{ids['beto']}/excluir", follow_redirects=False).status_code == 404

    as_shared(client)                                               # senha única não exclui nem o que ela cadastrou
    for owner in ("arthur", "admin", "shared"):
        assert "/excluir" not in client.get(f"/equipe/clientes/{ids[owner]}").text
        assert client.post(f"/equipe/clientes/{ids[owner]}/excluir", follow_redirects=False).status_code == 403
    assert len(rows()) == 3

    as_admin(client)
    for owner in ("arthur", "admin", "shared"):
        assert "Excluir cliente" in client.get(f"/equipe/clientes/{ids[owner]}").text
        r = client.post(f"/equipe/clientes/{ids[owner]}/excluir", follow_redirects=False)
        assert r.headers["location"] == "/equipe/clientes?msg=deleted"
    assert rows() == []
    assert "Cliente excluído" in client.get("/equipe/clientes?msg=deleted").text


def test_delete_cannot_be_redirected_to_another_id(client):
    mine = put("Do Beto", 1, 1, by=uid("beto"), by_name="Beto")
    other = put("Do Admin", 3, 1)
    as_user(client, "beto")
    for data in ({"customer_id": str(mine), "id": str(mine)}, {"created_by": str(uid("beto"))}):
        r = client.post(f"/equipe/clientes/{other}/excluir", data=data, follow_redirects=False)
        assert r.status_code == 403
    r = client.post(f"/equipe/clientes/{other}/excluir?customer_id={mine}&created_by={uid('beto')}",
                    follow_redirects=False)
    assert r.status_code == 403
    # editar o cliente alheio (permitido) não transfere a autoria
    assert post(client, f"/equipe/clientes/{other}", name="Do Admin", created_by=str(uid("beto")),
                created_by_name="Beto").status_code == 303
    assert (crow(other)["created_by"], crow(other)["created_by_name"]) == (None, "Administrador")
    assert client.post(f"/equipe/clientes/{other}/excluir", follow_redirects=False).status_code == 403
    assert len(rows()) == 2
    for bad in ("0", "-1", "999999", str(2 ** 31), str(2 ** 63), "9" * 30):
        assert client.post(f"/equipe/clientes/{bad}/excluir", follow_redirects=False).status_code == 404, bad
        assert client.get(f"/equipe/clientes/{bad}", follow_redirects=False).status_code == 404, bad
        assert post(client, f"/equipe/clientes/{bad}").status_code == 404, bad
        assert client.post(f"/equipe/clientes/{bad}/parabens", data={"value": "1"}).status_code == 404, bad
    for bad in ("abc", "1e3", "NOVO", "1;2", "0x1", "1,0"):
        assert client.get(f"/equipe/clientes/{bad}", follow_redirects=False).status_code in (404, 422), bad
        assert client.post(f"/equipe/clientes/{bad}/excluir", follow_redirects=False).status_code in (404, 422), bad
    assert len(rows()) == 2


def test_deactivated_employee_loses_everything_and_gets_it_back(client):
    mine = put("Do Beto", 1, 1, by=uid("beto"), by_name="Beto")
    cookie = staff_cookie(client, "beto")
    set_active("beto", False)
    use(client, cookie)
    assert client.get("/equipe/clientes", follow_redirects=False).status_code == 303
    assert client.post(f"/equipe/clientes/{mine}/excluir", follow_redirects=False).status_code == 401
    assert post(client).status_code == 401 and len(rows()) == 1
    set_active("beto", True)
    assert client.post(f"/equipe/clientes/{mine}/excluir", follow_redirects=False).status_code == 401   # cookie antigo
    assert login(client, "beto", OWN).status_code == 303
    assert client.post(f"/equipe/clientes/{mine}/excluir", follow_redirects=False).status_code == 303
    assert rows() == []


def test_employee_still_reaches_nothing_outside_equipe(client):
    put("Cliente Qualquer", 1, 1)
    as_user(client, "beto")
    for path in ("/", "/cars", "/admin/usuarios", "/clientes", "/customers", "/equipe/../admin/usuarios"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (303, 404), path
        if r.status_code == 303:
            assert r.headers["location"] == "/equipe", path
    assert client.post("/admin/usuarios", data={"name": "X", "username": "xx", "roles": "chefe"},
                       follow_redirects=False).status_code == 403
    nav = client.get("/equipe/clientes").text
    assert 'href="/admin/usuarios"' not in nav and 'href="/cars"' not in nav


def test_local_mode_without_login_is_admin(client, monkeypatch):
    monkeypatch.delenv("APP_PASSWORD")
    client.cookies.clear()
    assert post(client).status_code == 303
    row = rows()[0]
    assert (row["created_by"], row["created_by_name"]) == (None, "Administrador")
    assert client.post(f"/equipe/clientes/{row['id']}/excluir", follow_redirects=False).status_code == 303


# =======================================================================================
# (2) validações
# =======================================================================================

@pytest.mark.parametrize("typed, stored", [
    ("(21) 99876-5432", "21998765432"), ("21998765432", "21998765432"), ("+55 21 99876-5432", "21998765432"),
    ("+55(21)99876.5432", "21998765432"), ("021 99876 5432", "21998765432"), ("5521998765432", "21998765432"),
    ("  21 9 9876 5432  ", "21998765432"), ("(21) 2568-9348", "2125689348"), ("11 3456-7890", "1134567890"),
    ("055 21 2568-9348", None), ("+55 021 99876-5432", None),
])
def test_phone_formats(client, typed, stored):
    as_admin(client)
    r = post(client, phone=typed)
    if stored is None:
        assert r.status_code == 422 and rows() == []
    else:
        assert r.status_code == 303 and rows()[0]["phone"] == stored
        assert re.fullmatch(r"[0-9]{10,11}", rows()[0]["phone"])


@pytest.mark.parametrize("typed", [
    "", " ", "abc", "99876-5432", "998765432", "219987654321", "00998765432", "20998765432",
    "21098765432", "21198765432", "21899998888", "21999999999", "2111111111", "+1 21 99876-5432",
    "21 99876-5432 ramal 5", "21998765432;21998765433", "21998765432\n21998765433",
    "5" * 50, "-21998765432e3", "<script>21998765432</script>9",
])
def test_phone_invalid(client, typed):
    as_admin(client)
    r = post(client, phone=typed)
    assert r.status_code == 422 and "Não foi salvo" in r.text and rows() == []


UNICODE_PHONES = [
    "２１２５６８９３４８", "٢١٢٥٦٨٩٣٤٨", "२१२५६८९३४८", "2125689３４８", "219987654３", "2199876543２", "21998765４32",
    "(21) 99876-543²", "(21) 99876-543③", "(21) 99876-543½", "(21) 99876-543Ⅷ", "𝟐𝟏𝟗𝟗𝟖𝟕𝟔𝟓𝟒𝟑𝟐", "+５５ 21 99876-5432",
    "21 99876-5432 ٣", "２1998765432", "21998765432０",
]


@pytest.mark.parametrize("typed", UNICODE_PHONES)
def test_phone_with_unicode_digits_is_refused(client, typed):
    """Era bug: dígito fora de 0-9 era gravado ou descartado, e o link ia para OUTRO número."""
    as_admin(client)
    assert customers.normalize_phone(typed) is None
    r = post(client, phone=typed)
    assert r.status_code == 422 and "Não foi salvo" in r.text and rows() == []
    target = put("Cliente Antigo", 1, 1, phone="(21) 99876-5432")
    r = post(client, f"/equipe/clientes/{target}", phone=typed)
    assert r.status_code == 422 and crow(target)["phone"] == "21998765432"


@pytest.mark.parametrize("typed", [
    "\u202a(21) 99876-5432\u202c",          # marcas de direção que o WhatsApp põe ao copiar um contato
    "21\u200b99876\u200d5432", "\ufeff21998765432", "21\u00a099876\u20095432", "(21)\t99876–5432", "21 99876—5432",
    "tel:+55-21-99876-5432", "📱 21 99876 5432", "Zap: (21) 99876.5432", "+55 (21) 99876-5432\n",
])
def test_phone_with_invisible_marks_and_signs_keeps_the_same_number(client, typed):
    """Sinais e caracteres invisíveis são descartados, mas o número gravado é exatamente o digitado."""
    as_admin(client)
    assert post(client, phone=typed).status_code == 303
    assert [r["phone"] for r in rows()] == ["21998765432"]
    item = view(date(2026, 10, 11))["all"][0]
    assert item["wa_chat_link"] == "https://wa.me/5521998765432" and item["phone_display"] == "(21) 99876-5432"
    assert post(client, name="Outra Pessoa", phone="21998765432").status_code == 422       # e conta como duplicado


def test_every_stored_phone_is_ascii_digits_and_matches_its_link(client):
    as_admin(client)
    tries = UNICODE_PHONES + ["(21) 99876-5432", "+55 11 3456-7890", "021 99111 2222", "5531993334444", "21 9987",
                              "(21) 9987O-5432", "2I998765432", "21998765432x", "x", ""]
    for n, typed in enumerate(tries):
        post(client, name=f"Cliente Numero {n}", phone=typed)
    stored = rows()
    assert len(stored) >= 4
    for row in stored:
        assert re.fullmatch(r"[0-9]{10,11}", row["phone"]) and row["phone"].isascii(), ascii(row["phone"])
    for item in view(date(2026, 10, 11))["all"]:
        assert item["wa_chat_link"] == "https://wa.me/55" + item["phone"]
        assert item["wa_link"].startswith("https://wa.me/55" + item["phone"] + "?text=")


def test_unicode_digits_are_refused_in_dates_and_search_too(client, clock):
    clock(TODAY)
    as_admin(client)
    for over in ({"birth_day": "１５"}, {"birth_day": "١٥"}, {"birth_month": "３"}, {"birth_year": "１９８５"},
                 {"birth_year": "198５"}, {"birthday": "١٥/٠٣/١٩٨٥"}, {"birthday": "15/03/１９８５"},
                 {"purchase_date": "２０２０-05-17"}, {"purchase_date": "17/05/２０２０"}):
        assert post(client, **over).status_code == 422, ascii(over)
    assert rows() == []
    put("Cliente Real", 1, 1, phone="(21) 99876-5432")
    assert names(view(date(2026, 1, 1), "99876")["all"]) == ["Cliente Real"]
    assert names(view(date(2026, 1, 1), "９９８７６")["all"]) == []
    assert client.get("/equipe/clientes", params={"q": "９９８７６"}).status_code == 200


TODAY = "2026-10-11T12:00:00-03:00"


@pytest.mark.parametrize("day, month, year, stored", [
    ("15", "3", "1985", (15, 3, 1985)), ("15", "3", "", (15, 3, None)), ("05", "03", "1985", (5, 3, 1985)),
    ("29", "2", "", (29, 2, None)), ("29", "2", "2024", (29, 2, 2024)), ("29", "2", "2000", (29, 2, 2000)),
    ("31", "12", "1900", (31, 12, 1900)), ("11", "10", "2026", (11, 10, 2026)), ("31", "1", " ", (31, 1, None)),
    (" 7 ", " 9 ", " 1970 ", (7, 9, 1970)),
])
def test_birthday_valid(client, clock, day, month, year, stored):
    clock(TODAY)
    as_admin(client)
    assert post(client, birth_day=day, birth_month=month, birth_year=year).status_code == 303
    row = rows()[0]
    assert (row["birth_day"], row["birth_month"], row["birth_year"]) == stored


@pytest.mark.parametrize("day, month, year", [
    ("", "", ""), ("15", "", ""), ("", "3", ""), ("0", "3", ""), ("32", "1", ""), ("31", "4", ""), ("30", "2", ""),
    ("31", "6", "1990"), ("29", "2", "2023"), ("29", "2", "1900"), ("29", "2", "2025"), ("15", "0", ""),
    ("15", "13", ""), ("15", "3", "85"), ("15", "3", "985"), ("15", "3", "01985"), ("15", "3", "1899"),
    ("15", "3", "0000"), ("12", "10", "2026"), ("1", "1", "2027"), ("15", "3", "9999"), ("-1", "3", ""),
    ("1.5", "3", ""), ("15", "março", ""), ("quinze", "3", ""), ("15", "3", "mil"), ("1e1", "3", ""),
    ("15", "3", "1985 "*3), ("9" * 500, "3", ""), ("15", "3", "9" * 5000), ("15\x00", "3", ""),
    ("<script>", "3", ""), ("15", "3; DROP TABLE customers", ""),
])
def test_birthday_invalid(client, clock, day, month, year):
    clock(TODAY)
    as_admin(client)
    r = post(client, birth_day=day, birth_month=month, birth_year=year)
    assert r.status_code == 422 and rows() == []


def test_birthday_text_field_wins_and_is_validated(client, clock):
    """A chave `birthday` (texto) também é aceita pela rota: mesmas regras."""
    clock(TODAY)
    as_admin(client)
    for text, ok in (("29/02", True), ("31/04", False), ("12/10/2026", False), ("11/10/2026", True),
                     ("1985-03-15", True), ("15/03/85", False), ("2027-01-01", False), ("1985-02-30", False)):
        r = post(client, phone=f"2199{len(rows()):07d}", birthday=text, birth_day="1", birth_month="1", birth_year="")
        assert (r.status_code == 303) is ok, text


def test_birthday_in_the_future_follows_sao_paulo_clock(client, clock):
    as_admin(client)
    clock("2026-10-12T02:59:00+00:00")                              # ainda 11/10 23:59 em São Paulo
    assert post(client, birth_day="12", birth_month="10", birth_year="2026").status_code == 422
    assert post(client, purchase_date="2026-10-12").status_code == 422
    clock("2026-10-12T03:00:00+00:00")                              # 12/10 00:00 em São Paulo
    assert post(client, birth_day="12", birth_month="10", birth_year="2026", purchase_date="2026-10-12").status_code == 303


@pytest.mark.parametrize("field, value, ok", [
    ("name", "A", False), ("name", "Zé", True), ("name", "x" * 80, True), ("name", "x" * 81, False),
    ("name", "   ", False), ("name", " A ", False), ("name", "Jo\x00ão", False), ("name", "Jo‮ão", False),
    ("name", "x" * 100_000, False), ("name", "  Maria   da   Silva  ", True),
    ("email", "a@b.co", True), ("email", "A.B+tag@Exemplo.COM.br", True), ("email", "x" * 114 + "@b.com", True),
    ("email", "x" * 115 + "@b.com", False), ("email", "sem-arroba", False), ("email", "a@b", False),
    ("email", "a@@b.com", False), ("email", "a b@c.com", False), ("email", "a@b.com\nbcc:x@y.com", False),
    ("email", "a@b.com\x00", False), ("email", "@b.com", False), ("email", "a@.com", False),
    ("car_text", "x" * 120, True), ("car_text", "x" * 121, False), ("car_text", "Corolla XEi 2020 prata", True),
    ("notes", "x" * 1000, True), ("notes", "x" * 1001, False), ("notes", "linha 1\r\nlinha 2\n\n  linha 3  ", True),
    ("purchase_date", "2020-05-17", True), ("purchase_date", "17/05/2020", True), ("purchase_date", "2020-02-30", False),
    ("purchase_date", "1899-12-31", False), ("purchase_date", "2026-10-12", False), ("purchase_date", "ontem", False),
    ("purchase_date", "2020-05-17T10:00", False), ("purchase_date", "20-05-17", False),
], ids=lambda v: None if isinstance(v, bool) else ascii(v)[:24] if isinstance(v, str) else None)
def test_field_limits(client, clock, field, value, ok):
    clock(TODAY)
    as_admin(client)
    r = post(client, **{field: value})
    assert r.status_code == (303 if ok else 422), (field, ascii(value)[:30])
    assert len(rows()) == (1 if ok else 0)
    if ok and field in ("name", "car_text"):
        assert rows()[0][field] == " ".join(value.split())
    if ok and field == "email":
        assert rows()[0]["email"] == value.lower()


def test_optional_fields_are_stored_as_null_and_never_render_none(client, clock):
    clock(TODAY)
    as_admin(client)
    assert post(client, birth_day="11", birth_month="10", birth_year="").status_code == 303
    row = rows()[0]
    assert [row[k] for k in ("email", "car_text", "purchase_date", "notes", "birth_year", "greeted_year")] == [None] * 6
    for path in ("/equipe/clientes", f"/equipe/clientes/{row['id']}", "/equipe/clientes?q=fulano"):
        text = client.get(path).text
        assert "None" not in text and "Undefined" not in text and "null" not in text.split("<script>")[0], path
    assert "aniversário hoje" in client.get("/equipe/clientes").text


def test_notes_keep_line_breaks_but_drop_control_characters(client):
    as_admin(client)
    assert post(client, notes="prefere tarde\r\n\x00indicou\x1b um amigo​\n\n\nfim   ", car_text="Onix\x00 \t LT").status_code == 303
    row = rows()[0]
    assert row["notes"] == "prefere tarde\nindicou um amigo\n\n\nfim" and row["car_text"] == "Onix LT"


@pytest.mark.parametrize("again", ["21998765432", "(21) 99876-5432", "+55 21 99876-5432", "021998765432",
                                   "5521998765432", "21 9 9876 5432", " 21.99876.5432 "])
def test_duplicate_phone_in_any_format(client, again):
    as_admin(client)
    assert post(client).status_code == 303
    r = post(client, name="Outra Pessoa", phone=again)
    assert r.status_code == 422 and "já existe um cliente com esse WhatsApp" in r.text
    assert "Fulano de Teste" not in r.text                         # não revela de quem é o telefone
    assert len(rows()) == 1 and "Outra Pessoa" in r.text


def test_edit_keeping_or_changing_the_phone(client):
    as_admin(client)
    assert post(client).status_code == 303
    assert post(client, name="Segunda Pessoa", phone="(21) 99111-2222").status_code == 303
    first, second = [r["id"] for r in rows()]
    for same in ("(21) 99876-5432", "+55 21 99876-5432", "21998765432"):
        r = post(client, f"/equipe/clientes/{first}", name="Fulano Renomeado", phone=same)
        assert r.status_code == 303, same                           # o próprio telefone não é duplicado
    before = crow(second)
    r = post(client, f"/equipe/clientes/{second}", name="Tentativa", phone="021 99876-5432")
    assert r.status_code == 422 and "já existe um cliente" in r.text and crow(second) == before
    assert 'action="/equipe/clientes/%d"' % second in r.text and "Tentativa" in r.text
    assert post(client, f"/equipe/clientes/{second}", name="Segunda Pessoa", phone="(21) 99333-4444").status_code == 303
    assert post(client, name="Terceira", phone="(21) 99111-2222").status_code == 303   # o número antigo ficou livre
    assert [r["phone"] for r in rows()] == ["21998765432", "21993334444", "21991112222"]


def test_mass_assignment_fields_are_ignored(client, clock):
    clock(TODAY)
    as_user(client, "beto")
    hostile = {"id": "77", "created_by": "1", "created_by_name": "Hacker", "created_at": "2000-01-01T00:00:00-03:00",
               "updated_at": "2000-01-01T00:00:00-03:00", "greeted_year": "2026", "greeted_by_name": "Hacker",
               "greeted_at": "2026-10-11T00:00:00-03:00", "customer_id": "77", "role": "admin"}
    assert post(client, birth_day="11", birth_month="10", **hostile).status_code == 303
    row = rows()[0]
    assert (row["id"], row["created_by"], row["created_by_name"]) == (1, uid("beto"), "Beto")
    assert (row["greeted_year"], row["greeted_by_name"], row["greeted_at"]) == (None, None, None)
    assert row["created_at"].startswith("2026-10-11") and row["updated_at"] == row["created_at"]
    assert "Enviado" not in client.get("/equipe/clientes").text
    clock("2026-10-11T15:00:00-03:00")
    assert post(client, "/equipe/clientes/1", birth_day="11", birth_month="10", **hostile).status_code == 303
    after = crow(1)
    assert {k: after[k] for k in row if k != "updated_at"} == {k: row[k] for k in row if k != "updated_at"}
    assert after["updated_at"].startswith("2026-10-11T15:00")
    assert len(rows()) == 1


def test_editing_keeps_the_greeting_and_the_author(client, clock):
    clock(TODAY)
    target = put("Aniversariante", 11, 10, 1990, by=uid("arthur"), by_name="Arthur")
    as_user(client, "beto")
    client.post(f"/equipe/clientes/{target}/parabens", data={"value": "1"})
    assert post(client, f"/equipe/clientes/{target}", name="Aniversariante Editado", birth_day="11",
                birth_month="10", birth_year="1990").status_code == 303
    row = crow(target)
    assert (row["greeted_year"], row["greeted_by_name"], row["created_by"], row["created_by_name"]) == \
        (2026, "Beto", uid("arthur"), "Arthur")


def test_form_error_keeps_everything_typed(client):
    as_admin(client)
    typed = form(name="Maria Teste da Silva", phone="21 9987", birth_day="31", birth_month="4", birth_year="1990",
                 email="maria@exemplo", car_text="Corolla XEi 2020", purchase_date="2020-05-17",
                 notes="prefere contato à tarde")
    r = client.post("/equipe/clientes", data=typed, follow_redirects=False)
    assert r.status_code == 422 and "location" not in r.headers and r.headers["cache-control"] == "no-store"
    text = r.text
    for value in ('value="Maria Teste da Silva"', 'value="21 9987"', 'name="birth_day" value="31"',
                  '<option value="4" selected>', 'value="1990"', 'value="maria@exemplo"', 'value="Corolla XEi 2020"',
                  'value="2020-05-17"', ">prefere contato à tarde</textarea>"):
        assert value in text, value
    assert 'action="/equipe/clientes"' in text and "Não foi salvo" in text and rows() == []
    target = put("Cliente Antigo", 1, 1, 1980)
    r = client.post(f"/equipe/clientes/{target}", data=typed, follow_redirects=False)
    assert r.status_code == 422 and f'action="/equipe/clientes/{target}"' in r.text
    assert 'value="Maria Teste da Silva"' in r.text and "Cliente Antigo" not in r.text
    assert "Excluir cliente" in r.text and crow(target)["name"] == "Cliente Antigo"


# =======================================================================================
# (3) XSS, injeção e links
# =======================================================================================

EVIL = '<img src=x onerror=alert(1)>"\'><script>alert(2)</script>{{7*7}}'
EVIL_MARKS = ("<img src=x", "<script>alert(2)", "onerror=alert(1)>")


def _assert_escaped(text: str, where: str):
    for mark in EVIL_MARKS:
        assert mark not in text, (where, mark)
    assert "&lt;img src=x onerror=alert(1)&gt;" in text, where
    assert "{{7*7}}" in htmllib.unescape(text) and ">49<" not in text, where
    assert_safe_links(text)


def test_xss_in_every_customer_field_and_in_who_registered(client, clock):
    clock(TODAY)
    _db(lambda c: users.create_user(c, "malvado", EVIL[:60], ["vendedor"]))
    evil_name = _db(lambda c: c.execute("SELECT name FROM users WHERE username = 'malvado'").fetchone()[0])
    as_user(client, "malvado")
    r = post(client, name=EVIL, car_text=EVIL, notes=EVIL + "\n</textarea><script>alert(3)</script>",
             birth_day="11", birth_month="10", birth_year="1990", email="evil@exemplo.com")
    assert r.status_code == 303
    row = rows()[0]
    assert row["created_by_name"] == evil_name
    client.post(f"/equipe/clientes/{row['id']}/parabens", data={"value": "1"})
    assert crow(row["id"])["greeted_by_name"] == evil_name
    pages = {
        "lista (aniversariante, enviado)": client.get("/equipe/clientes").text,
        "edição": client.get(f"/equipe/clientes/{row['id']}").text,
        "busca": client.get("/equipe/clientes", params={"q": "img"}).text,
        "estoque": client.get("/equipe").text,
    }
    client.post(f"/equipe/clientes/{row['id']}/parabens", data={"value": "0"})
    pages["lista (botão do WhatsApp)"] = client.get("/equipe/clientes").text
    as_admin(client)
    pages["admin lista"] = client.get("/equipe/clientes").text
    pages["admin edição"] = client.get(f"/equipe/clientes/{row['id']}").text
    for where, text in pages.items():
        if where == "estoque":
            assert "alert(2)" not in text.replace("&lt;script&gt;alert(2)", "") and "<img src=x" not in text
            continue
        _assert_escaped(text, where)
        assert "</textarea><script>alert(3)" not in text, where


def test_xss_in_rejected_form_and_search(client):
    as_admin(client)
    r = client.post("/equipe/clientes", data={
        "name": EVIL, "phone": EVIL, "birth_day": EVIL, "birth_month": EVIL, "birth_year": EVIL, "email": EVIL,
        "car_text": EVIL, "purchase_date": EVIL, "notes": "</textarea>" + EVIL}, follow_redirects=False)
    assert r.status_code == 422
    _assert_escaped(r.text, "422 campos separados")
    r = client.post("/equipe/clientes", data={"name": "Ok Nome", "phone": "21998765432",
                                              "birthday": EVIL + "/" + EVIL + "/" + EVIL}, follow_redirects=False)
    assert r.status_code == 422
    for mark in EVIL_MARKS:
        assert mark not in r.text
    put("Cliente Normal", 1, 1)
    for q in (EVIL, '"><svg onload=alert(1)>', "' OR '1'='1", "javascript:alert(1)", "</script><script>alert(1)</script>",
              "{{7*7}}", "%", "_", "\x00", "a" * 5000, "\\", "😀" * 40):
        text = client.get("/equipe/clientes", params={"q": q}).text
        assert "<svg onload" not in text and "<script>alert(1)" not in text and "<img src=x" not in text, q
        assert_safe_links(text)
        assert "a" * 100 not in text                               # a busca é cortada em 80
    assert names(view(date(2026, 1, 1), "' OR '1'='1")["all"]) == []
    assert names(view(date(2026, 1, 1), "%")["all"]) == [] and names(view(date(2026, 1, 1), "_")["all"]) == []
    assert len(rows()) == 1


def test_sql_injection_in_fields_is_just_text(client):
    as_admin(client)
    bomb = "Robert'); DROP TABLE customers; --"
    assert post(client, name=bomb, car_text=bomb, notes=bomb).status_code == 303
    row = rows()[0]
    assert (row["name"], row["car_text"], row["notes"]) == (bomb, bomb, bomb)
    assert names(view(date(2026, 1, 1), "drop table")["all"]) == [bomb]
    assert post(client, name="Segundo Cliente", phone="21 99111-2222").status_code == 303 and len(rows()) == 2


def test_whatsapp_link_is_encoded_and_cannot_change_destination(client, clock):
    clock(TODAY)
    tricky = "Ana&text=golpe#x?y=1/../ Çélia 😀 da Silva"
    target = put(tricky, 11, 10, 1990, phone="(21) 99876-5432")
    as_user(client, "beto")
    text = client.get("/equipe/clientes").text
    links = [h for h in hrefs(text) if "wa.me" in h]
    assert links and all(WA_RE.fullmatch(h) for h in links)
    assert {h.split("?")[0] for h in links} == {"https://wa.me/5521998765432"}
    with_text = [h for h in links if "?text=" in h]
    assert len(with_text) == 1 and with_text[0].count("?") == 1 and "&" not in with_text[0] and "#" not in with_text[0]
    message = unquote(with_text[0].split("?text=", 1)[1])
    assert message == customers.birthday_message(tricky, "Beto")
    assert message.startswith("Olá, Ana&text=golpe#x?y=1/../! 🎉\n") and message.endswith("Beto — Equipe Union Veículos")
    assert "%0A" in with_text[0] and "%F0%9F%8E%82" in with_text[0] and "R$" not in message
    for anchor in re.findall(r"<a [^>]*wa\.me[^>]*>", text):
        assert 'target="_blank"' in anchor and 'rel="noopener"' in anchor
    as_admin(client)                                                # admin e senha única: assinatura só da equipe
    admin_link = next(h for h in hrefs(client.get("/equipe/clientes").text) if "?text=" in h)
    assert unquote(admin_link).endswith("Um abraço,\nEquipe Union Veículos")
    assert crow(target)["phone"] == "21998765432"


@pytest.mark.parametrize("phone", ["21998765432&text=x", "21998765432/../../evil", "21998765432?x=1", "21998765432#a",
                                   "21998765432@evil.com", "2199876 5432 javascript:alert(1)", "//evil.com/21998765432"])
def test_whatsapp_link_builder_keeps_only_digits(phone):
    link = customers.whatsapp_link(phone, "oi & tchau?\n#")
    assert re.fullmatch(r"https://wa\.me/55[0-9]*\?text=oi%20%26%20tchau%3F%0A%23", link), link
    assert customers.normalize_phone(phone) is None or re.fullmatch(r"[0-9]{10,11}", customers.normalize_phone(phone))


# =======================================================================================
# (4) aniversários: fuso, 29/02, virada de ano, listas, selo, idade
# =======================================================================================

def test_day_turns_at_midnight_in_sao_paulo(client, clock):
    put("Faz Dia Quatorze", 14, 3, 1980)
    put("Faz Dia Quinze", 15, 3, 1990)
    as_admin(client)
    clock("2026-03-15T02:59:59+00:00")                              # 14/03 23:59:59 em São Paulo (já é 15 em UTC)
    text = client.get("/equipe/clientes").text
    today_block = text.split("Próximos 7 dias")[0]
    assert "Faz Dia Quatorze" in today_block and "Faz Dia Quinze" not in today_block
    assert "faz 46 anos hoje" in today_block and "amanhã" in text
    assert "🎂 1" in client.get("/equipe").text
    clock("2026-03-15T03:00:00+00:00")                              # 15/03 00:00 em São Paulo
    text = client.get("/equipe/clientes").text
    today_block = text.split("Próximos 7 dias")[0]
    assert "Faz Dia Quinze" in today_block and "Faz Dia Quatorze" not in today_block
    assert "faz 36 anos hoje" in today_block
    v = view(date(2026, 3, 15))
    assert names(v["today"]) == ["Faz Dia Quinze"] and v["week"] == [] and v["month"] == []
    assert [i["days_until"] for i in v["all"]] == [364, 0]


def test_feb_29_birthdays(client, clock):
    leap = put("Nasceu em Vinte e Nove", 29, 2, 2000)
    put("Nasceu em Vinte e Oito", 28, 2, 1990)
    put("Nasceu em Primeiro de Março", 1, 3)
    v = view(date(2027, 2, 28))                                     # ano não bissexto: 29/02 conta em 28/02
    assert names(v["today"]) == ["Nasceu em Vinte e Nove", "Nasceu em Vinte e Oito"]   # ordem alfabética
    assert next(i for i in v["today"] if i["id"] == leap)["age_turning"] == 27
    assert names(v["week"]) == ["Nasceu em Primeiro de Março"] and v["week"][0]["days_until"] == 1
    assert _db(lambda c: customers.birthdays_today(c, date(2027, 2, 28))) == 2
    assert _db(lambda c: customers.birthdays_today(c, date(2027, 3, 1))) == 1
    v = view(date(2027, 3, 1))                                      # no dia seguinte não repete
    assert names(v["today"]) == ["Nasceu em Primeiro de Março"]
    assert next(i for i in v["all"] if i["id"] == leap)["days_until"] == 365   # 29/02/2028

    v = view(date(2028, 2, 28))                                     # ano bissexto: só no dia 29
    assert names(v["today"]) == ["Nasceu em Vinte e Oito"]
    assert names(v["week"]) == ["Nasceu em Vinte e Nove", "Nasceu em Primeiro de Março"]
    assert _db(lambda c: customers.birthdays_today(c, date(2028, 2, 28))) == 1
    v = view(date(2028, 2, 29))
    assert names(v["today"]) == ["Nasceu em Vinte e Nove"] and v["today"][0]["age_turning"] == 28
    assert _db(lambda c: customers.birthdays_today(c, date(2028, 2, 29))) == 1
    clock("2027-02-28T12:00:00-03:00")
    as_admin(client)
    text = client.get("/equipe/clientes").text
    assert "faz 27 anos hoje" in text and "🎂 2" in text and "29/02" in text


def test_year_turn(client, clock):
    jan2 = put("Faz Dois de Janeiro", 2, 1, 1990)
    put("Faz Trinta e Um de Dezembro", 31, 12)
    put("Fez Vinte e Nove de Dezembro", 29, 12, 1980)
    put("Faz Seis de Janeiro", 6, 1)
    put("Faz Sete de Janeiro", 7, 1)                                # 8 dias: fora da semana e fora de "este mês"
    v = view(date(2026, 12, 30))
    assert v["today"] == [] and v["month"] == []
    assert [(i["name"], i["days_until"]) for i in v["week"]] == [
        ("Faz Trinta e Um de Dezembro", 1), ("Faz Dois de Janeiro", 3), ("Faz Seis de Janeiro", 7)]
    item = next(i for i in v["week"] if i["id"] == jan2)
    assert (item["age_turning"], item["next_birthday"]) == (37, date(2027, 1, 2))
    last = next(i for i in v["all"] if i["name"].startswith("Fez"))
    assert (last["days_until"], last["age_turning"]) == (364, 47)   # 29/12/2027
    clock("2026-12-30T12:00:00-03:00")
    as_admin(client)
    text = client.get("/equipe/clientes").text
    assert "Ninguém faz aniversário hoje" in text and "em 3 dias · faz 37 anos" in text and "Ainda este mês" not in text
    assert "tab-badge" not in text
    clock("2027-01-02T00:00:00-03:00")
    assert "faz 37 anos hoje" in client.get("/equipe/clientes").text


def test_lists_never_repeat_a_person(conn):
    """Um cliente por dia do ano (366): em qualquer data, hoje / 7 dias / mês são disjuntos e exatos."""
    start = date(2024, 1, 1)
    for offset in range(366):
        d = start + timedelta(days=offset)
        assert customers.create_customer(conn, {"name": f"Cliente {d:%d-%m}", "phone": f"2199{offset:07d}",
                                                "birthday": f"{d.day}/{d.month}"}, None, None, today=FAR) == "saved"
    for today in (date(2026, 1, 1), date(2026, 1, 25), date(2026, 2, 21), date(2026, 2, 28), date(2026, 3, 1),
                  date(2026, 10, 11), date(2026, 12, 24), date(2026, 12, 31), date(2028, 2, 22), date(2028, 2, 29)):
        v = customers.customers_view(conn, today)
        groups = [[i["id"] for i in v[k]] for k in ("today", "week", "month")]
        flat = sum(groups, [])
        assert len(flat) == len(set(flat)), today
        leap = today.year % 4 == 0
        expected_today = 2 if (today.month, today.day) == (2, 28) and not leap else 1
        assert len(v["today"]) == expected_today == customers.birthdays_today(conn, today) == v["counts"]["today"], today
        assert [i["days_until"] for i in v["week"]] == sorted(i["days_until"] for i in v["week"])
        assert all(1 <= i["days_until"] <= 7 for i in v["week"]) and len(v["week"]) in (7, 8), today
        assert all(i["days_until"] > 7 and i["next_birthday"].month == today.month
                   and i["next_birthday"].year == today.year for i in v["month"]), today
        assert len(v["all"]) == 366 == v["counts"]["total"] and v["counts"]["week"] == len(v["week"])
        assert all(i["next_birthday"] >= today and (i["next_birthday"] - today).days == i["days_until"] for i in v["all"])
    conn.commit()


def test_tab_badge_counts_todays_birthdays_on_every_team_page(client, clock):
    clock(TODAY)
    as_user(client, "beto")
    pages = ("/equipe", "/equipe/vendas", "/equipe/clientes", "/equipe/clientes/novo", "/equipe/senha")
    for path in pages:
        assert "tab-badge" not in client.get(path).text, path
    first = put("Zenobio Um", 11, 10, 1990)
    put("Zenobio Dois", 11, 10)
    put("Outro Dia", 12, 10)
    for path in pages[:4] + (f"/equipe/clientes/{first}",):
        text = client.get(path).text
        assert "🎂 2" in text and "2 aniversariantes hoje" in text, path
    for path in ("/equipe", "/equipe/vendas"):                      # só o número: nenhum dado do cliente fora da aba
        assert "Zenobio" not in client.get(path).text, path
    r = client.post("/equipe/clientes", data=form(phone="x"), follow_redirects=False)
    assert r.status_code == 422 and "🎂 2" in r.text


def test_age(conn):
    for name, year in (("Com Ano", 1985), ("Sem Ano", None), ("Nasceu Hoje", 2026), ("Centenario", 1926)):
        put(name, 11, 10, year)
    v = {i["name"]: i for i in customers.customers_view(conn, date(2026, 10, 11))["today"]}
    assert [v[n]["age_turning"] for n in ("Com Ano", "Sem Ano", "Nasceu Hoje", "Centenario")] == [41, None, 0, 100]
    v = {i["name"]: i for i in customers.customers_view(conn, date(2026, 10, 12))["all"]}
    assert [v[n]["age_turning"] for n in ("Com Ano", "Sem Ano", "Nasceu Hoje")] == [42, None, 1]
    conn.commit()


def test_search_by_name_phone_and_car(conn):
    put("João Conceição", 1, 1, phone="(21) 99876-5432", car_text="Corolla XEi 2020")
    put("Maria Souza", 2, 2, phone="(11) 3456-7890", car_text="HB20 Prata")
    put("ANA LÚCIA", 3, 3, phone="(31) 99111-2222")
    today = date(2026, 6, 1)

    def found(q):
        return names(customers.customers_view(conn, today, q)["all"])
    assert found("joao") == found("JOÃO CONC") == found("  conceicao ") == ["João Conceição"]
    assert found("corolla") == ["João Conceição"] and found("hb20 prata") == ["Maria Souza"]
    assert found("99876") == found("(21) 99876-5432") == found("21 9 9876") == ["João Conceição"]
    assert found("3456-7890") == ["Maria Souza"] and found("ana lucia") == ["ANA LÚCIA"]
    assert found("2020") == ["João Conceição"]                      # número que está no carro
    assert found("") == found(None) == ["ANA LÚCIA", "João Conceição", "Maria Souza"]
    assert found("zzz") == found("99876 joao") == []
    v = customers.customers_view(conn, today, "zzz")
    assert v["counts"]["total"] == 3                                # a busca não mexe nos aniversariantes
    conn.commit()


# =======================================================================================
# (5) parabéns enviado
# =======================================================================================

MARK_BUTTON = 'bday-mark">Marcar como enviado<'
GREET_FORM = re.compile(r'action="/equipe/clientes/\d+/parabens"')


def _mark(client, customer_id, value="1"):
    return client.post(f"/equipe/clientes/{customer_id}/parabens", data={"value": value}, follow_redirects=False)


def test_greeting_mark_undo_and_who_sent(client, clock):
    clock(TODAY)
    target = put("Aniversariante", 11, 10, 1990)
    as_user(client, "beto")
    text = client.get("/equipe/clientes").text
    assert "Dar parabéns no WhatsApp" in text and MARK_BUTTON in text and "✅ Enviado" not in text
    r = _mark(client, target)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe/clientes?msg=saved")
    row = crow(target)
    assert (row["greeted_year"], row["greeted_by_name"]) == (2026, "Beto") and row["greeted_at"].startswith("2026-10-11")
    text = client.get("/equipe/clientes").text
    assert "✅ Enviado por Beto" in text and "Desfazer" in text and MARK_BUTTON not in text
    as_admin(client)                                                # todo mundo vê quem enviou
    assert "✅ Enviado por Beto" in client.get("/equipe/clientes").text
    assert _mark(client, target).status_code == 303                 # marcar de novo troca o autor
    assert crow(target)["greeted_by_name"] == "Administrador"
    as_shared(client)
    assert _mark(client, target, "0").status_code == 303
    row = crow(target)
    assert (row["greeted_year"], row["greeted_by_name"], row["greeted_at"]) == (None, None, None)
    assert MARK_BUTTON in client.get("/equipe/clientes").text
    assert _mark(client, target).status_code == 303 and crow(target)["greeted_by_name"] == "Equipe"


@pytest.mark.parametrize("value", ["", "2", "-1", "true", "on", "sim", " 1", "1 ", "01", "１", "1\x00", "x" * 5000])
def test_greeting_rejects_other_values(client, clock, value):
    clock(TODAY)
    target = put("Aniversariante", 11, 10, 1990)
    as_admin(client)
    _mark(client, target)
    before = crow(target)
    r = _mark(client, target, value)
    assert (r.status_code, r.headers["location"]) == (303, "/equipe/clientes?msg=invalid")
    assert crow(target) == before
    r = client.post(f"/equipe/clientes/{target}/parabens", follow_redirects=False)
    assert r.headers["location"] == "/equipe/clientes?msg=invalid" and crow(target) == before


def test_greeting_is_only_for_this_years_birthday(client, clock):
    clock(TODAY)
    target = put("Aniversariante", 11, 10, 1990)
    as_admin(client)
    _mark(client, target)
    clock("2026-10-12T00:00:00-03:00")                              # no dia seguinte já saiu da lista de hoje
    text = client.get("/equipe/clientes").text
    assert "✅ Enviado" not in text and not GREET_FORM.search(text)
    assert view(date(2026, 10, 12))["all"][0]["greeted"] is False
    clock("2027-10-11T09:00:00-03:00")                              # no ano seguinte volta a pedir o parabéns
    text = client.get("/equipe/clientes").text
    assert "faz 37 anos hoje" in text and MARK_BUTTON in text and "✅ Enviado" not in text
    item = view(date(2027, 10, 11))["today"][0]
    assert (item["greeted"], item["greeted_by_name"]) == (False, None)
    _mark(client, target)
    assert crow(target)["greeted_year"] == 2027


def test_day_after_the_birthday_the_screen_offers_no_greeting_button(client, clock):
    """Caso apontado pela Lupa: marcar no dia SEGUINTE gravava o ano que vem. A tela não oferece o botão
    (só aparece na lista de hoje) e, depois da correção, o POST direto também é recusado."""
    target = put("Fez Ontem", 10, 10, 1990)
    put("Faz Amanha", 12, 10, 1990)
    put("Faz Daqui a Vinte Dias", 31, 10)
    clock(TODAY)
    as_user(client, "beto")
    text = client.get("/equipe/clientes").text
    assert not GREET_FORM.search(text) and MARK_BUTTON not in text and "Dar parabéns no WhatsApp<" not in text
    assert f'action="/equipe/clientes/{target}/parabens"' not in client.get(f"/equipe/clientes/{target}").text
    before = rows()
    for row in before:                                              # ontem, amanhã e daqui a 20 dias: nada grava
        r = _mark(client, row["id"])
        assert (r.status_code, r.headers["location"]) == (303, "/equipe/clientes?msg=invalid"), row["name"]
    assert rows() == before
    clock("2027-10-10T09:00:00-03:00")                              # um ano depois ninguém aparece como já enviado
    text = client.get("/equipe/clientes").text
    assert "✅ Enviado" not in text and MARK_BUTTON in text


def test_greeting_page_opened_before_midnight_and_clicked_after(client, clock):
    """O cenário real do achado: a tela de hoje fica aberta, vira o dia, e a pessoa toca em 'Marcar como enviado'."""
    target = put("Aniversariante", 11, 10, 1990)
    clock("2026-10-11T23:59:00-03:00")
    as_user(client, "beto")
    assert f'action="/equipe/clientes/{target}/parabens"' in client.get("/equipe/clientes").text
    clock("2026-10-12T00:00:00-03:00")
    r = _mark(client, target)
    assert r.headers["location"] == "/equipe/clientes?msg=invalid"
    row = crow(target)
    assert (row["greeted_year"], row["greeted_by_name"], row["greeted_at"]) == (None, None, None)
    assert "Não deu para concluir" in client.get(r.headers["location"]).text
    clock("2026-10-11T23:59:59-03:00")                              # ainda no dia: grava o ano de hoje
    assert _mark(client, target).headers["location"] == "/equipe/clientes?msg=saved"
    assert (crow(target)["greeted_year"], crow(target)["greeted_by_name"]) == (2026, "Beto")
    clock("2026-10-12T00:00:00-03:00")                              # marcar de novo fora do dia não mexe no que já existe
    done = crow(target)
    assert _mark(client, target).headers["location"].endswith("msg=invalid") and crow(target) == done
    clock("2026-11-20T10:00:00-03:00")                              # desfazer vale sempre
    assert _mark(client, target, "0").headers["location"] == "/equipe/clientes?msg=saved"
    assert crow(target)["greeted_year"] is None
    assert _mark(client, target, "0").headers["location"] == "/equipe/clientes?msg=saved"   # desfazer 2x: sem erro


@pytest.mark.parametrize("day, month, when, ok", [
    (2, 1, "2026-12-30T10:00:00-03:00", False),      # 02/01 nos próximos 7 dias: ainda não
    (2, 1, "2027-01-02T00:00:00-03:00", True),
    (2, 1, "2027-01-03T02:59:00+00:00", True),       # ainda dia 02 em São Paulo
    (2, 1, "2027-01-03T03:00:00+00:00", False),      # virou dia 03 em São Paulo
    (29, 2, "2027-02-28T10:00:00-03:00", True),      # 29/02 comemorado em 28/02 fora de ano bissexto
    (29, 2, "2027-03-01T10:00:00-03:00", False),
    (29, 2, "2028-02-28T10:00:00-03:00", False),     # ano bissexto: só no dia 29
    (29, 2, "2028-02-29T10:00:00-03:00", True),
    (28, 2, "2028-02-29T10:00:00-03:00", False),
    (31, 12, "2026-12-31T23:59:59-03:00", True),
    (31, 12, "2027-01-01T00:00:00-03:00", False),
    (1, 1, "2026-12-31T23:59:59-03:00", False),
])
def test_greeting_is_recorded_only_on_the_birthday(client, clock, day, month, when, ok):
    target = put("Cliente do Dia", day, month, 1988)
    today = clock(when)
    as_admin(client)
    r = _mark(client, target)
    assert r.headers["location"] == f"/equipe/clientes?msg={'saved' if ok else 'invalid'}"
    assert crow(target)["greeted_year"] == (today.year if ok else None)
    assert _db(lambda c: customers.mark_greeted(c, target, "Alguém", today, True)) is ok
    shown = "✅ Enviado" in client.get("/equipe/clientes").text
    assert shown is ok
    assert bool(view(today)["today"]) is ok                         # o botão existe exatamente quando a rota aceita


def test_greeting_across_the_year_turn_and_feb_29(client, clock):
    jan2 = put("Faz Dois de Janeiro", 2, 1, 1990)
    leap = put("Vinte e Nove", 29, 2, 2000)
    as_admin(client)
    clock("2027-01-02T10:00:00-03:00")
    _mark(client, jan2)
    assert crow(jan2)["greeted_year"] == 2027 and "✅ Enviado" in client.get("/equipe/clientes").text
    clock("2027-12-30T10:00:00-03:00")                              # 02/01/2028 nos próximos 7 dias: ainda não enviado
    assert view(date(2027, 12, 30))["week"][0]["greeted"] is False
    clock("2027-02-28T10:00:00-03:00")                              # 29/02 comemorado em 28/02
    _mark(client, leap)
    assert crow(leap)["greeted_year"] == 2027
    assert "✅ Enviado" in client.get("/equipe/clientes").text
    clock("2028-02-28T10:00:00-03:00")
    assert view(date(2028, 2, 28))["week"][0]["greeted"] is False
    clock("2028-02-29T10:00:00-03:00")
    assert MARK_BUTTON in client.get("/equipe/clientes").text


# =======================================================================================
# (6) privacidade
# =======================================================================================

PII = {"name": "Zuleica Pessoa Ficticia", "phone": "(21) 99555-0142", "email": "zuleica.ficticia@exemplo.com",
       "car_text": "Fusca Azul Calcinha 1972", "notes": "detalhe sigiloso do cliente"}
PII_MARKS = ("Zuleica", "Pessoa Ficticia", "99555-0142", "995550142", "21995550142", "zuleica.ficticia",
             "Fusca Azul", "detalhe sigiloso")


def test_personal_data_stays_out_of_logs_cookies_and_redirects(client, clock, caplog, capsys):
    clock(TODAY)
    caplog.set_level(logging.DEBUG)
    seen = []

    def note(r):
        assert r.status_code < 500
        seen.append("\n".join(f"{k}: {v}" for k, v in r.headers.items() if k.lower() in ("location", "set-cookie")))
        return r

    as_user(client, "beto")
    data = form(birth_day="11", birth_month="10", birth_year="1990", **PII)
    assert note(client.post("/equipe/clientes", data=data, follow_redirects=False)).status_code == 303
    target = rows()[0]["id"]
    note(client.post("/equipe/clientes", data=data, follow_redirects=False))                          # duplicate
    note(client.post("/equipe/clientes", data={**data, "phone": "x" + PII["phone"][:8]}, follow_redirects=False))
    note(client.post(f"/equipe/clientes/{target}", data={**data, "email": PII["email"] + " x"}, follow_redirects=False))
    note(client.post(f"/equipe/clientes/{target}", data=data, follow_redirects=False))
    note(_mark(client, target))
    note(_mark(client, target, "zuleica"))
    for path in ("/equipe/clientes", f"/equipe/clientes/{target}", "/equipe", "/equipe/vendas"):
        note(client.get(path))
    not_found = note(client.get("/equipe/clientes/999999"))
    assert not_found.status_code == 404
    give_own("arthur", OTHER)
    login(client, "arthur", OTHER)
    forbidden = note(client.post(f"/equipe/clientes/{target}/excluir", follow_redirects=False))
    assert forbidden.status_code == 403
    note(login(client, "beto", OWN))
    note(client.post(f"/equipe/clientes/{target}/excluir", follow_redirects=False))
    client.cookies.clear()
    anon = note(client.get(f"/equipe/clientes/{target}", follow_redirects=False))
    out = capsys.readouterr()
    haystacks = {"log": caplog.text, "stdout": out.out, "stderr": out.err, "location/cookie": "\n".join(seen),
                 "404": not_found.text, "403": forbidden.text, "anônimo": anon.text}
    for where, text in haystacks.items():
        for mark in PII_MARKS:
            assert mark not in text, (where, mark)


def test_customer_pages_are_never_cached(client, clock):
    clock(TODAY)
    target = put("Cliente Qualquer", 11, 10, 1990)
    as_admin(client)
    for path in ("/equipe/clientes", "/equipe/clientes/novo", f"/equipe/clientes/{target}", "/equipe/clientes?q=qualquer",
                 "/equipe/clientes?msg=saved"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store", path
    assert post(client, phone="x").headers["cache-control"] == "no-store"
    assert post(client, f"/equipe/clientes/{target}", phone="x").headers["cache-control"] == "no-store"


def test_search_fallback_is_the_only_personal_data_in_a_url(client):
    """?q= só existe como reserva sem JavaScript (com JS a busca filtra na tela e não envia nada)."""
    put("Zuleica Pessoa Ficticia", 1, 1, phone="(21) 99555-0142")
    as_admin(client)
    plain = client.get("/equipe/clientes").text
    assert 'method="get" action="/equipe/clientes"' in plain and "e.preventDefault()" in plain
    assert "form.addEventListener('submit'" in plain              # sem ?q= o JS segura o envio
    r = client.get("/equipe/clientes", params={"q": "Zuleica"}, follow_redirects=False)
    assert r.status_code == 200 and "location" not in r.headers and "Ver todos" in r.text
    for anchor in re.findall(r"<a [^>]*https://[^>]*>", r.text):  # link externo não leva a URL com ?q= (referrer)
        assert 'rel="noopener"' in anchor or "fonts" in anchor
    for msg in ("Zuleica", "<b>x</b>", "saved&q=Zuleica", "x" * 300):
        text = client.get("/equipe/clientes", params={"msg": msg}).text
        assert "<b>x</b>" not in text and "x" * 100 not in text


def test_other_areas_do_not_expose_customers(client, clock):
    clock(TODAY)
    put("Zuleica Pessoa Ficticia", 11, 10, 1990, phone="(21) 99555-0142", notes="detalhe sigiloso do cliente")
    as_admin(client)
    for path in ("/", "/cars", "/equipe", "/equipe/vendas", "/admin/usuarios", "/health"):
        text = client.get(path).text
        for mark in PII_MARKS:
            assert mark not in text, (path, mark)
    openapi = client.get("/openapi.json")
    if openapi.status_code == 200:
        assert "Zuleica" not in openapi.text


# =======================================================================================
# (8) telas e "nada de 500"
# =======================================================================================

@pytest.mark.parametrize("kind", ["admin", "employee", "shared"])
def test_screens_in_every_state(client, clock, kind):
    clock(TODAY)
    {"admin": as_admin, "employee": as_user, "shared": as_shared}[kind](client)

    def check(text, *must):
        assert "None" not in text and "Undefined" not in text and "Traceback" not in text
        for piece in must:
            assert piece in text, piece
        assert_safe_links(text)

    check(client.get("/equipe/clientes").text, "Nenhum cliente cadastrado ainda", "Ninguém faz aniversário hoje",
          "Nenhum aniversário nos próximos 7 dias", "+ Novo cliente")
    assert 'role="search"' not in client.get("/equipe/clientes").text          # sem cliente, sem busca
    check(client.get("/equipe/clientes?q=ninguem").text, "Nenhum cliente combina com a busca")
    check(client.get("/equipe/clientes/novo").text, "Novo cliente", 'action="/equipe/clientes"', "Salvar cliente")
    assert "Excluir cliente" not in client.get("/equipe/clientes/novo").text
    for msg, piece in (("saved", "✓ Salvo."), ("deleted", "Cliente excluído"), ("invalid", "Não deu para concluir")):
        check(client.get(f"/equipe/clientes?msg={msg}").text, piece)
    other = client.get("/equipe/clientes?msg=outra").text
    assert 'class="flash' not in other and "outra" not in other        # msg fora da lista é ignorada

    today_id = put("Aniversariante de Hoje", 11, 10, 1985, car_text="Corolla XEi 2020", notes="gosta de café",
                   email="hoje@exemplo.com", purchase_date="2020-05-17")
    put("Faz Amanha", 12, 10)
    put("Faz em Vinte Dias", 31, 10, 2001)
    put("Faz Mes que Vem", 5, 11)
    text = client.get("/equipe/clientes").text
    check(text, "Aniversariante de Hoje", "faz 41 anos hoje", "🚗 Corolla XEi 2020", "gosta de café",
          "Dar parabéns no WhatsApp", MARK_BUTTON, "Próximos 7 dias", "amanhã", "Ainda este mês",
          "em 20 dias · faz 25 anos", "Todos os clientes", "Cadastrado por Administrador", 'role="search"')
    assert text.count('class="card customer-card"') == 4 and "hoje@exemplo.com" not in text
    edit = client.get(f"/equipe/clientes/{today_id}").text
    check(edit, "Editar cliente", 'value="Aniversariante de Hoje"', 'value="(21) 9', 'name="birth_day" value="11"',
          '<option value="10" selected>', 'value="1985"', 'value="2020-05-17"', 'value="hoje@exemplo.com"',
          ">gosta de café</textarea>", "Salvar alterações", "Cadastrado por Administrador")
    assert ("Excluir cliente" in edit) is (kind == "admin")
    check(client.get("/equipe/clientes?q=amanha").text, "Faz Amanha", "Mostrando só o que combina")
    bad = post(client, phone="123")
    assert bad.status_code == 422
    check(bad.text, "Não foi salvo. Confira os campos", 'value="123"')
    dup = post(client, phone="(21) 99876-5432")
    assert dup.status_code == 303
    dup = post(client, phone="(21) 99876-5432")
    assert dup.status_code == 422
    check(dup.text, "já existe um cliente com esse WhatsApp")


WEIRD_FORMS = [
    {}, {"name": ""}, {k: "" for k in BASE}, {k: "\x00" for k in BASE}, {k: "x" * 20000 for k in BASE},
    {k: "😀" * 300 for k in BASE}, {k: "'; DROP TABLE customers; --" for k in BASE}, {k: "%s %d {0} ${x}" for k in BASE},
    {**BASE, "birth_day": "٣", "birth_month": "٤"}, {**BASE, "birth_month": "-0"}, {**BASE, "birth_year": "١٩٨٥"},
    {**BASE, "phone": "９" * 11}, {**BASE, "purchase_date": "0000-00-00"}, {**BASE, "purchase_date": "9999-12-31"},
    {**BASE, "birthday": "/"}, {**BASE, "birthday": "//"}, {**BASE, "birthday": "1/"}, {**BASE, "birthday": "a/b/c/d/e"},
    {**BASE, "birthday": "99999999999999999999/1"}, {**BASE, "birth_year": "-1985"}, {**BASE, "value": "1"},
]


@pytest.mark.parametrize("data", WEIRD_FORMS)
def test_weird_forms_never_cause_a_500(client, data):
    target = put("Cliente Alvo", 1, 1, 1980)
    as_admin(client)
    for path in ("/equipe/clientes", f"/equipe/clientes/{target}", f"/equipe/clientes/{target}/parabens"):
        r = client.post(path, data=data, follow_redirects=False)
        assert r.status_code in (303, 422), (path, r.status_code)
        if r.status_code == 422 and "html" in r.headers.get("content-type", ""):
            assert "None" not in r.text and "Undefined" not in r.text
    assert client.get("/equipe/clientes").status_code == 200       # e a lista continua abrindo com o que foi gravado
    for row in rows():
        assert client.get(f"/equipe/clientes/{row['id']}").status_code == 200


def test_weird_requests_never_cause_a_500(client):
    target = put("Cliente Alvo", 1, 1, 1980)
    as_admin(client)
    bodies = [
        ({"content-type": "application/json"}, b'{"name": "x"}'),
        ({"content-type": "application/x-www-form-urlencoded"}, b"name=%ZZ&phone=%&=&&&=x"),
        ({"content-type": "application/x-www-form-urlencoded"}, b"\xff\xfe\x00name=\xff"),
        ({"content-type": "application/x-www-form-urlencoded"}, b"name=a&name=b&phone=1&phone=2"),
        ({"content-type": "multipart/form-data; boundary=x"}, b"--x\r\ncontent-disposition: form-data; name=\"name\"; "
         b"filename=\"a.txt\"\r\n\r\narquivo\r\n--x--\r\n"),
        ({"content-type": "multipart/form-data; boundary=x"}, b"lixo sem fronteira"),
        ({"content-type": "multipart/form-data"}, b"--x--"),
        ({"content-type": "text/plain"}, b"name=x"),
        ({}, b""),
    ]
    for headers, body in bodies:
        target = put("Cliente Alvo", 1, 1, 1980)                    # o excluir do fim apaga: um alvo novo por rodada
        for path in ("/equipe/clientes", f"/equipe/clientes/{target}", f"/equipe/clientes/{target}/parabens",
                     f"/equipe/clientes/{target}/excluir"):
            r = client.post(path, content=body, headers=headers, follow_redirects=False)
            assert r.status_code in (303, 400, 422), (path, headers, r.status_code)
    target = put("Cliente Alvo", 1, 1, 1980)
    for query in ("q=%ZZ", "q=%00", "q=" + "a" * 10000, "q=a&q=b", "msg=%ff", "q[]=x", "q=%F0%9F%98%80", "mes=x"):
        assert client.get(f"/equipe/clientes?{query}").status_code == 200, query
    for method in ("PUT", "DELETE", "PATCH"):
        assert client.request(method, f"/equipe/clientes/{target}").status_code == 405
    assert client.get(f"/equipe/clientes/{target}/excluir").status_code in (404, 405)
    assert client.get(f"/equipe/clientes/{target}/parabens").status_code in (404, 405)


def test_many_customers_page_still_opens(client, conn, clock):
    clock(TODAY)
    for n in range(300):
        assert customers.create_customer(conn, {"name": f"Cliente Numero {n:03d}", "phone": f"2199{n:07d}",
                                                "birthday": f"{n % 28 + 1}/{n % 12 + 1}"}, None, None, today=FAR) == "saved"
    conn.commit()
    as_admin(client)
    text = client.get("/equipe/clientes").text
    assert text.count('class="card customer-card"') == 300
    assert client.get("/equipe/clientes?q=numero 29").text.count('class="card customer-card"') == 10


# =======================================================================================
# (7) migração a partir do schema da F15 (0e2a5ef) e schema novo
# =======================================================================================

F15_SCHEMA = (FIXTURES / "schema_0e2a5ef.sql").read_text(encoding="utf-8")
CUSTOMER_COLUMNS = ["id", "name", "phone", "email", "birth_day", "birth_month", "birth_year", "car_text",
                    "purchase_date", "notes", "created_by", "created_by_name", "created_at", "updated_at",
                    "greeted_year", "greeted_by_name", "greeted_at"]


def test_f15_fixture_is_really_the_previous_schema():
    assert "CREATE TABLE IF NOT EXISTS users" in F15_SCHEMA and "customers" not in F15_SCHEMA
    assert "not_sale" in F15_SCHEMA and "car_expenses" in F15_SCHEMA


def test_schema_has_no_semicolon_in_comments_and_splits_cleanly():
    from app import pgcompat

    for line in db.SCHEMA.splitlines():
        if "--" in line:
            assert ";" not in line.split("--", 1)[1], line
    statements = pgcompat.split_statements(db.PG_SCHEMA)
    assert all(s.upper().startswith("CREATE ") and "--" not in s for s in statements)
    table = next(s for s in statements if s.startswith("CREATE TABLE IF NOT EXISTS customers"))
    assert table.count("(") == table.count(")") and "SERIAL PRIMARY KEY" in table
    assert any(s.startswith("CREATE INDEX IF NOT EXISTS ix_customers_birthday") for s in statements)
    assert "customers" in db.TABLES and "customers" in pgcompat.ID_TABLES


def _fill_f15(execute):
    _fill_old(execute, has_finance=True)
    stamp = "2026-10-11T10:00:00-03:00"
    for username, name, roles, pw, must, version, active in (
            ("arthur", "Arthur", "comprador,vendedor", "scrypt$16384$8$1$aa$bb", 0, 2, 1),
            ("beto", "Beto", "vendedor", None, 1, 0, 1), ("ruan", "Ruan", "chefe", "scrypt$16384$8$1$cc$dd", 1, 1, 0)):
        execute("INSERT INTO users (username, name, roles, password_hash, must_change, pw_version, active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (username, name, roles, pw, must, version, active, stamp))


def _users_dump(conn):
    out = [tuple(r) for r in conn.execute("SELECT * FROM users WHERE username IN ('arthur', 'beto', 'ruan') ORDER BY id")]
    conn.commit()
    return out


def _migration_scenario(conn):
    """Banco no schema da F15 com dados → init_db 2x → usa a F16 → init_db 2x de novo."""
    assert "customers" not in _tables(conn)
    before, staff = _dump(conn, ()), _users_dump(conn)
    db.init_db()
    db.init_db()
    assert "customers" in _tables(conn) and _cols(conn, "customers") == CUSTOMER_COLUMNS
    assert _dump(conn, ()) == before and _users_dump(conn) == staff          # nada da F15 mudou
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 6     # o seed completou os que faltavam
    conn.commit()
    today = date(2026, 10, 11)
    assert customers.create_customer(conn, {"name": "Cliente Migrado", "phone": "(21) 99876-5432",
                                            "birthday": "11/10/1990", "notes": "linha 1\nlinha 2"}, 1, "Arthur",
                                     today=today) == "saved"
    assert customers.create_customer(conn, {"name": "Outro", "phone": "21 99876 5432", "birthday": "1/1"},
                                     today=today) == "duplicate"
    first = conn.execute("SELECT id FROM customers").fetchone()[0]
    assert first == 1 and customers.mark_greeted(conn, first, "Arthur", today, True)
    full = [tuple(r) for r in conn.execute("SELECT * FROM customers ORDER BY id")]
    conn.commit()
    db.init_db()
    db.init_db()
    assert [tuple(r) for r in conn.execute("SELECT * FROM customers ORDER BY id")] == full
    conn.commit()
    assert _dump(conn, ()) == before and _users_dump(conn) == staff
    v = customers.customers_view(conn, today)
    assert names(v["today"]) == ["Cliente Migrado"] and v["today"][0]["greeted"] and customers.birthdays_today(conn, today) == 1
    conn.commit()
    for bad in ("name", "phone", "birth_day", "birth_month", "created_by_name"):   # NOT NULL de verdade
        with pytest.raises(Exception):
            conn.execute(f"UPDATE customers SET {bad} = NULL WHERE id = ?", (first,))
        conn.rollback()
    assert customers.create_customer(conn, {"name": "Segundo", "phone": "21 99111-2222", "birthday": "29/02"},
                                     today=today) == "saved"
    assert [r[0] for r in conn.execute("SELECT id FROM customers ORDER BY id")] == [1, 2]
    conn.commit()


@sqlite_only
def test_sqlite_migration_from_f15_schema_with_data(tmp_path, monkeypatch):
    path = tmp_path / "f15.db"
    monkeypatch.setenv("UNION_DB_PATH", str(path))
    raw = sqlite3.connect(path)
    raw.executescript(F15_SCHEMA)
    _fill_f15(raw.execute)
    raw.commit()
    raw.close()
    conn = db.connect()
    try:
        _migration_scenario(conn)
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert "ix_customers_birthday" in indexes
    finally:
        conn.close()


@requires_pg
def test_postgres_migration_from_f15_schema_with_data(empty_pg):  # noqa: F811
    conn = empty_pg
    conn.executescript(F15_SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"))
    _fill_f15(conn.execute)
    conn.commit()
    _migration_scenario(conn)
    kinds = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name = 'customers'")}
    indexes = {r[0] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
    conn.commit()
    assert "ix_customers_birthday" in indexes
    not_null = {"id", "name", "phone", "birth_day", "birth_month", "created_by_name", "created_at", "updated_at"}
    integers = {"id", "birth_day", "birth_month", "birth_year", "created_by", "greeted_year"}
    assert kinds == {c: ("integer" if c in integers else "text", "NO" if c in not_null else "YES")
                     for c in CUSTOMER_COLUMNS}


@requires_pg
def test_postgres_cold_start_on_f15_schema_serves_customers(empty_pg, site, clock):  # noqa: F811
    """Deploy da F16 sobre o banco de hoje (F15): o lifespan migra e o fluxo inteiro funciona."""
    from fastapi.testclient import TestClient

    from app.main import app

    clock(TODAY)
    conn = empty_pg
    conn.executescript(F15_SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"))
    _fill_f15(conn.execute)
    conn.commit()
    with TestClient(app) as client:
        as_admin(client)
        for path in ("/equipe", "/equipe/vendas", "/equipe/clientes", "/equipe/clientes/novo"):
            assert client.get(path).status_code == 200, path
        assert post(client, birth_day="11", birth_month="10", birth_year="1990").status_code == 303
        assert post(client, name="Duplicado").status_code == 422
        assert _mark(client, 1).status_code == 303
        text = client.get("/equipe/clientes").text
        assert "Fulano de Teste" in text and "✅ Enviado por Administrador" in text and "🎂 1" in client.get("/equipe").text
        assert client.post("/equipe/clientes/1/excluir", follow_redirects=False).status_code == 303
        assert client.get("/equipe/clientes/1").status_code == 404


def test_truncate_and_ids_on_current_schema(conn):
    assert _cols(conn, "customers") == CUSTOMER_COLUMNS
    cur = conn.execute(
        "INSERT INTO customers (name, phone, birth_day, birth_month, created_by_name, created_at, updated_at) "
        "VALUES ('Teste', '21998765432', 1, 1, 'Administrador', 'x', 'x')")
    assert cur.lastrowid == 1                                       # RETURNING id no Postgres (ID_TABLES)
    conn.commit()
    db.truncate_all(conn)
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0
    conn.commit()
    assert put("De Novo", 1, 1) == 1                                # sequência reiniciada
