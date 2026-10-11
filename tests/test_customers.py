"""F16 — clientes e aniversários (Lupa: app/services/customers.py). Só dados fictícios."""
from __future__ import annotations

import logging
from datetime import date, datetime
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from app import db
from app.services import customers as cs

TODAY = date(2026, 10, 12)  # segunda-feira


def form(**over) -> dict:
    base = {"name": "Maria Fictícia da Silva", "phone": "(21) 99999-0001", "birthday": "12/10/1985",
            "email": "maria.ficticia@example.com", "car_text": "Chevrolet Tracker 2026",
            "purchase_date": "2026-09-01", "notes": "Prefere contato à tarde."}
    base.update(over)
    return base


def add(conn, name, phone, birthday, today=TODAY, **over) -> int:
    assert cs.create_customer(conn, form(name=name, phone=phone, birthday=birthday, **over),
                              created_by=None, created_by_name="Administrador", today=today) == "saved"
    return conn.execute("SELECT id FROM customers WHERE phone = ?", (cs.normalize_phone(phone),)).fetchone()[0]


def names(items) -> list[str]:
    return [i["name"] for i in items]


# --- telefone -----------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("(21) 99647-9014", "21996479014"),
    ("+55 21 99647-9014", "21996479014"),
    ("5521996479014", "21996479014"),
    ("021996479014", "21996479014"),
    ("21 99647 9014", "21996479014"),
    ("21.99647.9014", "21996479014"),
    ("(21) 2568-9348", "2125689348"),            # fixo, 10 dígitos
    ("+55 (21) 2568-9348", "2125689348"),
    ("02125689348", "2125689348"),
    ("55996479014", "55996479014"),              # DDD 55 (RS) não é o código do país
    ("5555996479014", "55996479014"),
    (21996479014, "21996479014"),
    ("01996479014", "1996479014"),               # 0 + DDD 19 + fixo de 8 dígitos
])
def test_normalize_phone_ok(text, expected):
    assert cs.normalize_phone(text) == expected


@pytest.mark.parametrize("text", [
    "", None, "   ", "abc", "99647-9014",        # sem DDD
    "219964790",                                 # curto
    "219964790141",                              # longo
    "20996479014", "00996479014",                # DDD com zero
    "21896479014",                               # 9 dígitos sem começar em 9
    "2105689348", "2115689348",                  # número começando em 0 ou 1
    "21999999999", "2122222222",                 # todos iguais
    "+1 415 555 0100", "5521996479014000",
])
def test_normalize_phone_invalid(text):
    assert cs.normalize_phone(text) is None


def test_phone_display():
    assert cs.phone_display("21996479014") == "(21) 99647-9014"
    assert cs.phone_display("2125689348") == "(21) 2568-9348"
    assert cs.phone_display("") == "" and cs.phone_display(None) == ""
    assert cs.phone_display("123") == "123"


# --- aniversário: leitura -------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("12/10", (12, 10, None)),
    ("1/2", (1, 2, None)),
    ("12/10/1985", (12, 10, 1985)),
    ("12-10-1985", (12, 10, 1985)),
    ("12.10.1985", (12, 10, 1985)),
    (" 12 / 10 / 1985 ", (12, 10, 1985)),
    ("1985-10-12", (12, 10, 1985)),              # input date
    ("29/02", (29, 2, None)),                    # sem ano: aceito
    ("29/02/2000", (29, 2, 2000)),
    ("2024-02-29", (29, 2, 2024)),
    ("12/10/2026", (12, 10, 2026)),              # hoje
    ("01/01/1900", (1, 1, 1900)),
])
def test_parse_birthday_ok(text, expected):
    assert cs.parse_birthday(text, TODAY) == expected


@pytest.mark.parametrize("text", [
    "", None, "abc", "12", "12/10/85", "12/10/985", "12/10/19850",   # ano de 2, 3 ou 5 dígitos
    "32/01", "00/01", "12/13", "12/00", "31/04", "30/02",            # impossíveis
    "29/02/2023", "2023-02-29",                                      # não bissexto
    "13/10/2026", "01/01/2027", "2030-01-01",                        # futuras
    "31/12/1899", "1899-12-31", "12/10/0000",                        # antes de 1900
    "1985-13-01", "10/12/1985 x", "12/10/", "/10",
])
def test_parse_birthday_invalid(text):
    assert cs.parse_birthday(text, TODAY) is None


def test_birthday_from_form_accepts_separate_fields():
    assert cs.birthday_from_form({"birth_day": "5", "birth_month": "3", "birth_year": ""}, TODAY) == (5, 3, None)
    assert cs.birthday_from_form({"birth_day": "05", "birth_month": "03", "birth_year": "1990"}, TODAY) == (5, 3, 1990)
    assert cs.birthday_from_form({"birthday": "05/03/1990", "birth_day": "9"}, TODAY) == (5, 3, 1990)
    assert cs.birthday_from_form({"birth_day": "5"}, TODAY) is None
    assert cs.birthday_from_form({"birth_day": "31", "birth_month": "2"}, TODAY) is None
    assert cs.birthday_from_form({}, TODAY) is None


@pytest.mark.parametrize("text, expected", [
    ("2026-09-01", date(2026, 9, 1)), ("01/09/2026", date(2026, 9, 1)), ("2026-10-12", TODAY),
])
def test_parse_purchase_date_ok(text, expected):
    assert cs.parse_purchase_date(text, TODAY) == expected


@pytest.mark.parametrize("text", ["", "ontem", "2026-10-13", "31/02/2026", "01/09/26", "1899-01-01"])
def test_parse_purchase_date_invalid(text):
    assert cs.parse_purchase_date(text, TODAY) is None


# --- aniversário: contas ------------------------------------------------------------------

def c(day, month, year=None):
    return {"birth_day": day, "birth_month": month, "birth_year": year}


def test_next_birthday_today_counts_and_past_goes_to_next_year():
    assert cs.next_birthday(c(12, 10), TODAY) == TODAY and cs.days_until(c(12, 10), TODAY) == 0
    assert cs.next_birthday(c(13, 10), TODAY) == date(2026, 10, 13) and cs.days_until(c(13, 10), TODAY) == 1
    assert cs.next_birthday(c(11, 10), TODAY) == date(2027, 10, 11) and cs.days_until(c(11, 10), TODAY) == 364


def test_feb_29_counts_on_feb_28_outside_leap_years():
    leap = c(29, 2, 2000)
    assert cs.birthday_in_year(29, 2, 2027) == date(2027, 2, 28)
    assert cs.birthday_in_year(29, 2, 2028) == date(2028, 2, 29)
    assert cs.days_until(leap, date(2027, 2, 28)) == 0                # não bissexto: comemora dia 28
    assert cs.next_birthday(leap, date(2027, 3, 1)) == date(2028, 2, 29)
    assert cs.days_until(leap, date(2028, 2, 28)) == 1                # bissexto: só no dia 29
    assert cs.days_until(leap, date(2028, 2, 29)) == 0
    assert cs.age_turning(leap, date(2027, 2, 28)) == 27


def test_year_turn():
    jan2 = c(2, 1, 1990)
    assert cs.next_birthday(jan2, date(2026, 12, 30)) == date(2027, 1, 2)
    assert cs.days_until(jan2, date(2026, 12, 30)) == 3
    assert cs.age_turning(jan2, date(2026, 12, 30)) == 37
    assert cs.days_until(c(31, 12), date(2026, 12, 31)) == 0
    assert cs.days_until(c(31, 12), date(2027, 1, 1)) == 364


def test_age_turning_none_without_year():
    assert cs.age_turning(c(12, 10), TODAY) is None
    assert cs.age_turning(c(12, 10, 1985), TODAY) == 41
    assert cs.age_turning(c(11, 10, 1985), TODAY) == 42               # já fez este ano: próximo é 2027


def test_today_sp_uses_sao_paulo_at_day_turn(monkeypatch):
    """02:30 UTC de 13/10 ainda é 23:30 de 12/10 em São Paulo."""
    from datetime import timezone

    utc = datetime(2026, 10, 13, 2, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(cs, "now", lambda: utc.astimezone(db.TZ))
    assert cs.today_sp() == date(2026, 10, 12)
    monkeypatch.setattr(cs, "now", lambda: datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc).astimezone(db.TZ))
    assert cs.today_sp() == date(2026, 10, 13)
    assert db.now().tzinfo is not None and str(db.TZ) == "America/Sao_Paulo"


# --- WhatsApp --------------------------------------------------------------------------

def test_birthday_message_uses_first_names_and_team_signature():
    msg = cs.birthday_message("MARIA FICTÍCIA DA SILVA", "arthur exemplo")
    assert msg.startswith("Olá, Maria!")
    assert "feliz aniversário" in msg.lower()
    assert msg.rstrip().endswith("Arthur — Equipe Union Veículos")
    assert "Fictícia" not in msg and "Silva" not in msg and "exemplo" not in msg
    assert "R$" not in msg and "preço" not in msg.lower() and "oferta" not in msg.lower()
    assert len(msg) < 400


@pytest.mark.parametrize("sender", [None, "", "   "])
def test_birthday_message_without_sender_signs_only_as_team(sender):
    msg = cs.birthday_message("José Teste", sender)
    assert msg.rstrip().endswith("\nEquipe Union Veículos")
    assert "—" not in msg and "None" not in msg


def test_birthday_message_without_customer_name_does_not_break():
    assert cs.birthday_message("", "Beto").startswith("Olá! ")
    assert cs.first_name("McQueen Relâmpago") == "McQueen" and cs.first_name("ana") == "Ana"


def test_whatsapp_link_is_fully_encoded():
    text = "Olá, João! 🎉\nParabéns & saúde? 100% #1 + a/b = c"
    link = cs.whatsapp_link("21996479014", text)
    parts = urlsplit(link)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "wa.me", "/5521996479014")
    assert link.isascii() and " " not in link and "\n" not in link
    query = link.split("?", 1)[1]
    assert query.startswith("text=") and query.count("=") == 1 and "&" not in query and "#" not in link
    assert "%0A" in query and "%F0%9F%8E%89" in query and "%26" in query and "%2B" in query
    assert parse_qs(parts.query)["text"] == [text]
    assert unquote(query[len("text="):]) == text


def test_whatsapp_link_without_text_is_just_the_chat():
    assert cs.whatsapp_link("21996479014") == "https://wa.me/5521996479014"
    assert cs.whatsapp_link("21996479014", "") == "https://wa.me/5521996479014"
    assert cs.whatsapp_link("(21) 99647-9014?x=1", None) == "https://wa.me/55219964790141"  # só dígitos


# --- cadastro -----------------------------------------------------------------------------

def test_create_and_get_customer(conn):
    assert cs.create_customer(conn, form(name="  maria   fictícia "), created_by=None,
                              created_by_name="Administrador", today=TODAY) == "saved"
    cid = conn.execute("SELECT id FROM customers").fetchone()[0]
    got = cs.get_customer(conn, cid)
    assert got["name"] == "maria fictícia" and got["phone"] == "21999990001"
    assert got["phone_display"] == "(21) 99999-0001" and got["email"] == "maria.ficticia@example.com"
    assert got["birthday"] == "12/10/1985" and (got["birth_day"], got["birth_month"], got["birth_year"]) == (12, 10, 1985)
    assert got["car_text"] == "Chevrolet Tracker 2026" and got["purchase_date"] == "2026-09-01"
    assert got["notes"] == "Prefere contato à tarde."
    assert got["created_by"] is None and got["created_by_name"] == "Administrador"
    assert got["created_at"] and got["updated_at"] and got["greeted_year"] is None
    assert cs.get_customer(conn, 999_999) is None


def test_create_with_only_required_fields_and_employee_author(conn):
    uid = conn.execute("SELECT id FROM users WHERE username = 'arthur'").fetchone()[0]
    minimal = {"name": "Zé", "phone": "21988880002", "birthday": "05/03"}
    assert cs.create_customer(conn, minimal, created_by=uid, created_by_name="Arthur", today=TODAY) == "saved"
    got = cs.get_customer(conn, conn.execute("SELECT id FROM customers").fetchone()[0])
    assert got["birthday"] == "05/03" and got["birth_year"] is None
    assert got["email"] is None and got["car_text"] is None and got["purchase_date"] is None and got["notes"] is None
    assert got["created_by"] == uid and got["created_by_name"] == "Arthur"


def test_author_name_defaults_to_administrador(conn):
    assert cs.create_customer(conn, form(), today=TODAY) == "saved"
    assert cs.get_customer(conn, 1)["created_by_name"] == "Administrador"


@pytest.mark.parametrize("over", [
    {"name": ""}, {"name": "A"}, {"name": "N" * 81}, {"name": "Jo\x00ao Teste"}, {"name": "Jo​ao Teste"},
    {"phone": ""}, {"phone": "99999-0001"}, {"phone": "abc"},
    {"birthday": ""}, {"birthday": "31/02"}, {"birthday": "12/10/85"}, {"birthday": "13/10/2026"},
    {"email": "sem-arroba"}, {"email": "a@b"}, {"email": "a b@c.com"}, {"email": "a@" + "b" * 116 + ".com"},
    {"car_text": "C" * 121}, {"notes": "N" * 1001},
    {"purchase_date": "ontem"}, {"purchase_date": "2026-10-13"}, {"purchase_date": "31/02/2026"},
])
def test_create_invalid(conn, over):
    assert cs.create_customer(conn, form(**over), today=TODAY) == "invalid"
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0


@pytest.mark.parametrize("over", [
    {"name": "Zé"}, {"name": "N" * 80}, {"email": ""}, {"email": "A.B@Example.COM"},
    {"car_text": "C" * 120}, {"notes": "N" * 1000}, {"notes": "linha 1\r\nlinha 2"},
    {"purchase_date": ""}, {"purchase_date": "01/09/2026"},
    {"birthday": "", "birth_day": "12", "birth_month": "10", "birth_year": ""},
])
def test_create_at_the_limits_is_accepted(conn, over):
    assert cs.create_customer(conn, form(**over), today=TODAY) == "saved"
    got = cs.get_customer(conn, 1)
    if over.get("email"):
        assert got["email"] == "a.b@example.com"
    if "\r\n" in over.get("notes", ""):
        assert got["notes"] == "linha 1\nlinha 2"


def test_control_characters_are_removed_from_free_text(conn):
    assert cs.create_customer(conn, form(car_text="Onix\x00 \x1b2020", notes="ok\x00 fim\x07"), today=TODAY) == "saved"
    got = cs.get_customer(conn, 1)
    assert got["car_text"] == "Onix 2020" and got["notes"] == "ok fim"


def test_duplicate_phone_in_any_format(conn):
    first = add(conn, "Ana Um", "(21) 99999-0001", "01/01")
    assert cs.create_customer(conn, form(name="Outra Pessoa", phone="+55 21 99999 0001"), today=TODAY) == "duplicate"
    assert cs.create_customer(conn, form(name="Outra Pessoa", phone="021999990001"), today=TODAY) == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1
    second = add(conn, "Bia Dois", "(21) 99999-0002", "02/02")
    assert cs.update_customer(conn, second, form(name="Bia Dois", phone="21999990001"), today=TODAY) == "duplicate"
    assert cs.get_customer(conn, second)["phone"] == "21999990002"
    assert cs.update_customer(conn, first, form(name="Ana Um Editada", phone="(21) 99999-0001"), today=TODAY) == "saved"


def test_update_customer(conn):
    uid = conn.execute("SELECT id FROM users WHERE username = 'beto'").fetchone()[0]
    assert cs.create_customer(conn, form(), created_by=uid, created_by_name="Beto", today=TODAY) == "saved"
    cid = conn.execute("SELECT id FROM customers").fetchone()[0]
    new = form(name="Maria Editada", phone="21988887777", birthday="29/02", email="", car_text="",
               purchase_date="", notes="")
    assert cs.update_customer(conn, cid, new, today=TODAY) == "saved"
    got = cs.get_customer(conn, cid)
    assert got["name"] == "Maria Editada" and got["phone"] == "21988887777" and got["birthday"] == "29/02"
    assert got["email"] is None and got["car_text"] is None and got["purchase_date"] is None and got["notes"] is None
    assert got["created_by"] == uid and got["created_by_name"] == "Beto"        # autor não muda
    assert cs.update_customer(conn, cid, form(name="X"), today=TODAY) == "invalid"
    assert cs.get_customer(conn, cid)["name"] == "Maria Editada"
    assert cs.update_customer(conn, 999_999, form(), today=TODAY) == "invalid"


def test_delete_customer(conn):
    cid = add(conn, "Ana Um", "21999990001", "01/01")
    assert cs.delete_customer(conn, cid) is True
    assert cs.get_customer(conn, cid) is None
    assert cs.delete_customer(conn, cid) is False


def test_draft_keeps_what_was_typed_in_get_customer_shape(conn):
    cid = add(conn, "Ana Um", "21999990001", "01/01")
    typed = {"name": " A ", "phone": "telefone errado", "birthday": "31/02", "email": "x", "car_text": "Onix",
             "purchase_date": "ontem", "notes": "obs"}
    d = cs.draft(typed, cid)
    assert set(d) == set(cs.get_customer(conn, cid))
    assert d["id"] == cid and d["name"] == " A " and d["phone"] == "telefone errado" and d["birthday"] == "31/02"
    assert d["purchase_date"] == "ontem" and d["notes"] == "obs"
    split = cs.draft({"birth_day": "31", "birth_month": "2", "birth_year": "1990"})
    assert split["id"] is None and split["birthday"] == "31/2/1990" and split["birth_day"] == "31"
    assert cs.draft({})["name"] == ""


def test_can_delete():
    mine = {"created_by": 7}
    assert cs.can_delete(mine, "admin", None) is True
    assert cs.can_delete(mine, "staff", {"id": 7}) is True
    assert cs.can_delete(mine, "staff", {"id": 8}) is False
    assert cs.can_delete(mine, "staff", None) is False                 # senha única
    assert cs.can_delete({"created_by": None}, "staff", {"id": 7}) is False
    assert cs.can_delete({"created_by": None}, "staff", None) is False
    assert cs.can_delete(None, "admin", None) is False
    assert cs.can_delete(mine, None, {"id": 7}) is True               # dono, qualquer que seja o rótulo


# --- tela: customers_view --------------------------------------------------------------------

@pytest.fixture
def crowd(conn):
    add(conn, "Zuleica Hoje", "21999990001", "12/10/1985", car_text="Fiat Mobi")
    add(conn, "Álvaro Hoje", "21999990002", "12/10", car_text="")
    add(conn, "Bruno Amanhã", "21999990003", "13/10/2000")
    add(conn, "Carla Semana", "21999990004", "19/10")           # +7: ainda na semana
    add(conn, "Dênis Oito Dias", "21999990005", "20/10")        # +8: resto do mês
    add(conn, "Érica Fim Do Mês", "21999990006", "31/10")
    add(conn, "Fábio Novembro", "21999990007", "01/11")         # fora do mês
    add(conn, "Gil Já Passou", "21999990008", "11/10")          # ontem → só em 2027
    add(conn, "joão minúsculo", "21988887777", "05/05", car_text="Jeep Compass Série S")
    return conn


def test_view_sections(crowd):
    view = cs.customers_view(crowd, TODAY)
    assert names(view["today"]) == ["Álvaro Hoje", "Zuleica Hoje"]              # alfabética sem acento
    assert names(view["week"]) == ["Bruno Amanhã", "Carla Semana"]              # por proximidade, sem hoje
    assert names(view["month"]) == ["Dênis Oito Dias", "Érica Fim Do Mês"]      # resto do mês corrente
    assert names(view["all"]) == ["Álvaro Hoje", "Bruno Amanhã", "Carla Semana", "Dênis Oito Dias",
                                  "Érica Fim Do Mês", "Fábio Novembro", "Gil Já Passou", "joão minúsculo",
                                  "Zuleica Hoje"]
    assert view["counts"] == {"today": 2, "week": 2, "total": 9}
    assert [i["days_until"] for i in view["week"]] == [1, 7]
    assert cs.birthdays_today(crowd, TODAY) == 2


def test_view_item_fields(crowd):
    view = cs.customers_view(crowd, TODAY, sender_name="Arthur Exemplo")
    item = next(i for i in view["today"] if i["name"] == "Zuleica Hoje")
    assert item["phone"] == "21999990001" and item["phone_display"] == "(21) 99999-0001"
    assert item["birthday_label"] == "12/10" and item["age_turning"] == 41 and item["days_until"] == 0
    assert item["car_text"] == "Fiat Mobi" and item["purchase_date"] == date(2026, 9, 1)
    assert item["email"] == "maria.ficticia@example.com" and item["notes"] == "Prefere contato à tarde."
    assert item["created_by_name"] == "Administrador"
    assert item["greeted"] is False and item["greeted_by_name"] is None
    assert item["wa_chat_link"] == "https://wa.me/5521999990001"
    text = parse_qs(urlsplit(item["wa_link"]).query)["text"][0]
    assert text == cs.birthday_message("Zuleica Hoje", "Arthur Exemplo")
    assert text.startswith("Olá, Zuleica!") and text.rstrip().endswith("Arthur — Equipe Union Veículos")
    other = next(i for i in view["today"] if i["name"] == "Álvaro Hoje")
    assert other["age_turning"] is None and other["car_text"] is None
    anon = cs.customers_view(crowd, TODAY)["today"][0]["wa_link"]
    assert parse_qs(urlsplit(anon).query)["text"][0].rstrip().endswith("\nEquipe Union Veículos")
    assert {"id", "name", "phone", "phone_display", "email", "birthday_label", "age_turning", "days_until",
            "car_text", "purchase_date", "notes", "created_by_name", "greeted", "greeted_by_name", "wa_link",
            "wa_chat_link"} <= set(item)


@pytest.mark.parametrize("q, expected", [
    ("joao", ["joão minúsculo"]), ("JOÃO", ["joão minúsculo"]), ("  minusculo ", ["joão minúsculo"]),
    ("serie s", ["joão minúsculo"]), ("COMPASS", ["joão minúsculo"]), ("mobi", ["Zuleica Hoje"]),
    ("hoje", ["Álvaro Hoje", "Zuleica Hoje"]), ("alvaro", ["Álvaro Hoje"]),
    ("8888", ["joão minúsculo"]), ("(21) 98888-7777", ["joão minúsculo"]), ("21 9 8888", ["joão minúsculo"]),
    ("naoexiste", []), ("%", []), ("_", []),
])
def test_view_search_ignores_case_and_accents(crowd, q, expected):
    view = cs.customers_view(crowd, TODAY, q=q)
    assert names(view["all"]) == expected
    assert view["counts"] == {"today": 2, "week": 2, "total": 9}     # busca não mexe nos aniversariantes
    assert names(view["today"]) == ["Álvaro Hoje", "Zuleica Hoje"]


@pytest.mark.parametrize("q", [None, "", "   "])
def test_view_empty_search_lists_everyone(crowd, q):
    assert len(cs.customers_view(crowd, TODAY, q=q)["all"]) == 9


def test_view_empty_database(conn):
    assert cs.customers_view(conn, TODAY) == {"today": [], "week": [], "month": [], "all": [],
                                              "counts": {"today": 0, "week": 0, "total": 0}}
    assert cs.birthdays_today(conn, TODAY) == 0


def test_view_year_turn_shows_january_birthday_in_december(conn):
    add(conn, "Jana Janeiro", "21999990001", "02/01/1990")
    add(conn, "Dedé Dezembro", "21999990002", "31/12")
    dec30 = date(2026, 12, 30)
    view = cs.customers_view(conn, dec30)
    assert names(view["week"]) == ["Dedé Dezembro", "Jana Janeiro"]
    jana = view["week"][1]
    assert jana["days_until"] == 3 and jana["age_turning"] == 37 and jana["next_birthday"] == date(2027, 1, 2)
    assert view["month"] == []                                        # 02/01 não é "ainda este mês"
    assert names(cs.customers_view(conn, date(2027, 1, 2))["today"]) == ["Jana Janeiro"]


def test_view_feb_29_shows_on_feb_28_in_common_year(conn):
    add(conn, "Bia Bissexta", "21999990001", "29/02/2000")
    assert names(cs.customers_view(conn, date(2027, 2, 28))["today"]) == ["Bia Bissexta"]
    assert cs.birthdays_today(conn, date(2027, 2, 28)) == 1
    assert cs.customers_view(conn, date(2027, 2, 28))["today"][0]["birthday_label"] == "29/02"
    assert cs.customers_view(conn, date(2028, 2, 28))["today"] == []          # bissexto: só dia 29
    assert cs.birthdays_today(conn, date(2028, 2, 28)) == 0
    assert cs.birthdays_today(conn, date(2028, 2, 29)) == 1
    assert names(cs.customers_view(conn, date(2028, 2, 28))["week"]) == ["Bia Bissexta"]


# --- parabéns enviado ---------------------------------------------------------------------------

def _item(conn, cid, today):
    return next(i for i in cs.customers_view(conn, today)["all"] if i["id"] == cid)


def test_mark_greeted_and_undo(conn):
    cid = add(conn, "Zuleica Hoje", "21999990001", "12/10/1985")
    assert cs.mark_greeted(conn, cid, "Arthur", TODAY, True) is True
    item = _item(conn, cid, TODAY)
    assert item["greeted"] is True and item["greeted_by_name"] == "Arthur"
    row = conn.execute("SELECT greeted_year, greeted_at FROM customers WHERE id = ?", (cid,)).fetchone()
    assert row["greeted_year"] == 2026 and row["greeted_at"]
    assert cs.mark_greeted(conn, cid, "Beto", TODAY, False) is True
    item = _item(conn, cid, TODAY)
    assert item["greeted"] is False and item["greeted_by_name"] is None
    assert cs.mark_greeted(conn, 999_999, "Arthur", TODAY, True) is False


def test_greeted_is_only_for_that_years_birthday(conn):
    cid = add(conn, "Zuleica Hoje", "21999990001", "12/10/1985")
    cs.mark_greeted(conn, cid, "Arthur", TODAY, True)
    assert _item(conn, cid, TODAY)["greeted"] is True
    assert _item(conn, cid, date(2026, 10, 13))["greeted"] is False       # dia seguinte: próximo é 2027
    assert _item(conn, cid, date(2027, 10, 12))["greeted"] is False       # ano seguinte começa do zero
    assert _item(conn, cid, date(2027, 10, 12))["greeted_by_name"] is None


def test_greeted_across_year_turn_uses_the_birthday_year(conn):
    """Pós-QA: marcar só vale NO DIA. Adiantado em dezembro é recusado e nada é gravado."""
    cid = add(conn, "Jana Janeiro", "21999990001", "02/01/1990")
    dec30 = date(2026, 12, 30)
    assert cs.mark_greeted(conn, cid, "Equipe", dec30, True) is False
    assert conn.execute("SELECT greeted_year FROM customers WHERE id = ?", (cid,)).fetchone()[0] is None
    assert _item(conn, cid, dec30)["greeted"] is False
    jan2 = date(2027, 1, 2)
    assert cs.mark_greeted(conn, cid, "Equipe", jan2, True) is True       # no dia: ano do aniversário
    assert conn.execute("SELECT greeted_year FROM customers WHERE id = ?", (cid,)).fetchone()[0] == 2027
    assert _item(conn, cid, jan2)["greeted"] is True
    assert _item(conn, cid, date(2027, 1, 3))["greeted"] is False


@pytest.mark.parametrize("when", [date(2026, 10, 11), date(2026, 10, 13), date(2026, 10, 5), date(2027, 1, 1),
                                  date(2025, 10, 12).replace(day=13)])
def test_mark_greeted_is_refused_when_birthday_is_not_today(conn, when):
    cid = add(conn, "Zuleica Hoje", "21999990001", "12/10/1985")
    assert cs.mark_greeted(conn, cid, "Arthur", when, True) is False
    row = conn.execute("SELECT greeted_year, greeted_by_name, greeted_at FROM customers WHERE id = ?", (cid,)).fetchone()
    assert tuple(row) == (None, None, None)


def test_mark_greeted_after_midnight_does_not_record_next_year(conn):
    """Página aberta em 12/10 às 23:59 e botão clicado em 13/10 00:01: não grava 2027."""
    cid = add(conn, "Zuleica Hoje", "21999990001", "12/10/1985")
    assert cs.mark_greeted(conn, cid, "Arthur", date(2026, 10, 13), True) is False
    assert conn.execute("SELECT greeted_year FROM customers WHERE id = ?", (cid,)).fetchone()[0] is None
    assert _item(conn, cid, date(2027, 10, 12))["greeted"] is False


def test_undo_greeted_is_always_allowed(conn):
    cid = add(conn, "Zuleica Hoje", "21999990001", "12/10/1985")
    assert cs.mark_greeted(conn, cid, "Arthur", TODAY, True) is True
    assert cs.mark_greeted(conn, cid, "Beto", date(2026, 10, 13), False) is True     # dia seguinte: pode desfazer
    assert conn.execute("SELECT greeted_year FROM customers WHERE id = ?", (cid,)).fetchone()[0] is None
    assert cs.mark_greeted(conn, cid, "Beto", date(2026, 3, 1), False) is True       # sem nada marcado: também
    assert cs.mark_greeted(conn, 999_999, "Beto", TODAY, False) is False


def test_mark_greeted_feb_29_customer_on_feb_28_of_common_year(conn):
    cid = add(conn, "Bia Bissexta", "21999990001", "29/02/2000")
    assert cs.mark_greeted(conn, cid, "Arthur", date(2027, 2, 28), True) is True      # 2027: comemora dia 28
    assert conn.execute("SELECT greeted_year FROM customers WHERE id = ?", (cid,)).fetchone()[0] == 2027
    assert cs.mark_greeted(conn, cid, "Arthur", date(2028, 2, 28), True) is False     # 2028 bissexto: só dia 29
    assert cs.mark_greeted(conn, cid, "Arthur", date(2028, 2, 29), True) is True


def test_greeted_sender_defaults_to_administrador(conn):
    cid = add(conn, "Zuleica Hoje", "21999990001", "12/10")
    cs.mark_greeted(conn, cid, None, TODAY, True)
    assert _item(conn, cid, TODAY)["greeted_by_name"] == "Administrador"


# --- dados pessoais fora de log -------------------------------------------------------------------

def test_nothing_personal_is_logged(conn, caplog):
    caplog.set_level(logging.DEBUG)
    cid = add(conn, "Pessoa Sigilosa", "21977776666", "12/10/1985", email="sigilo@example.com")
    cs.create_customer(conn, form(name="Pessoa Sigilosa", phone="21977776666"), today=TODAY)     # duplicate
    cs.create_customer(conn, form(name="Pessoa Sigilosa", phone="telefone-ruim"), today=TODAY)   # invalid
    cs.update_customer(conn, cid, form(name="Pessoa Sigilosa 2", phone="21977776666"), today=TODAY)
    cs.customers_view(conn, TODAY, q="sigilosa", sender_name="Arthur")
    cs.mark_greeted(conn, cid, "Arthur", TODAY, True)
    cs.delete_customer(conn, cid)
    for secret in ("Sigilosa", "21977776666", "sigilo@example.com", "telefone-ruim"):
        assert secret not in caplog.text


def test_status_values_never_carry_what_was_typed(conn):
    assert cs.create_customer(conn, form(phone="telefone-ruim"), today=TODAY) in {"saved", "invalid", "duplicate"}
    assert {cs.SAVED, cs.INVALID, cs.DUPLICATE} == {"saved", "invalid", "duplicate"}


# --- pós-QA F16: só dígitos ASCII ------------------------------------------------------------

UNICODE_PHONES = [
    "２１２５６８９３４８",      # largura cheia
    "٢١٢٥٦٨٩٣٤٨",      # arábico-índico
    "2125689３４８",                                                 # misturado: viraria outro número
    "219987654３",
    "२१९९६४७९०१४",  # devanágari
    "²1996479014", "21996479014½", "(21) 99647-901④",              # sobrescrito, fração, circulado
]


@pytest.mark.parametrize("typed", UNICODE_PHONES, ids=[ascii(t) for t in UNICODE_PHONES])
def test_normalize_phone_refuses_non_ascii_digits(typed):
    assert cs.normalize_phone(typed) is None


@pytest.mark.parametrize("typed", UNICODE_PHONES, ids=[ascii(t) for t in UNICODE_PHONES])
def test_customer_with_non_ascii_digit_phone_is_not_saved(conn, typed):
    assert cs.create_customer(conn, form(phone=typed), today=TODAY) == "invalid"
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0


def test_stored_phones_and_links_are_ascii_digits_only(conn):
    add(conn, "Ana Um", "+55 (21) 99999-0001", "01/01")
    add(conn, "Bia Dois", "021 2568-9348", "02/02")
    for item in cs.customers_view(conn, TODAY)["all"]:
        assert item["phone"].isascii() and item["phone"].isdigit() and len(item["phone"]) in (10, 11)
        assert item["wa_chat_link"] == "https://wa.me/55" + item["phone"]
        assert item["wa_link"].startswith("https://wa.me/55" + item["phone"] + "?text=")


def test_non_ascii_digits_do_not_bypass_duplicate_check(conn):
    add(conn, "Ana Um", "2125689348", "01/01")
    assert cs.create_customer(conn, form(name="Outra Pessoa", phone="2125689３４８"),
                              today=TODAY) == "invalid"                     # nem salva, nem vira outro número
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1


@pytest.mark.parametrize("text", ["１２/１０", "12/10/１９８５",
                                  "١٢/١٠", "1985-１０-12", "１２/10/1985"])
def test_parse_birthday_refuses_non_ascii_digits(text):
    assert cs.parse_birthday(text, TODAY) is None


def test_other_form_numbers_refuse_non_ascii_digits():
    assert cs.parse_purchase_date("０１/09/2026", TODAY) is None
    assert cs.parse_purchase_date("2026-０９-01", TODAY) is None
    assert cs.birthday_from_form({"birth_day": "１２", "birth_month": "10"}, TODAY) is None
    assert cs.birthday_from_form({"birth_day": "12", "birth_month": "10", "birth_year": "１９８５"},
                                 TODAY) is None
    assert cs.phone_display("２１２５６８９３４８") != "(２１) ２５６８-９３４８"


def test_search_by_phone_only_uses_ascii_digits(conn):
    add(conn, "Ana Um", "21988887777", "01/01")
    assert names(cs.customers_view(conn, TODAY, q="8888")["all"]) == ["Ana Um"]
    assert cs.customers_view(conn, TODAY, q="８８８８")["all"] == []
