"""F15 — funcionários (Lupa: app/services/users.py). Senhas aqui são sintéticas, só de teste."""
from __future__ import annotations

import logging
from datetime import datetime

import pytest

from app.services import users

AMBIGUOUS = set("0O1lI")


# --- hash ---------------------------------------------------------------------

def test_hash_and_verify_round_trip():
    stored = users.hash_password("correta-cavalo-bateria")
    assert stored.startswith(("scrypt$", "pbkdf2_sha256$"))
    assert "correta-cavalo-bateria" not in stored
    assert users.verify_password("correta-cavalo-bateria", stored) is True
    assert users.verify_password("correta-cavalo-bateriA", stored) is False
    assert users.verify_password("", stored) is False


def test_hash_uses_a_new_salt_each_time():
    assert users.hash_password("mesma-senha-1") != users.hash_password("mesma-senha-1")


@pytest.mark.parametrize("stored", [None, "", "texto-puro", "scrypt$x$8$1$zz$zz", "md5$abc", "scrypt$1$2"])
def test_verify_rejects_missing_or_malformed_hash(stored):
    assert users.verify_password("qualquer-coisa", stored) is False


def test_verify_supports_pbkdf2_fallback_format():
    import hashlib

    salt = bytes.fromhex("00112233445566778899aabbccddeeff")
    digest = hashlib.pbkdf2_hmac("sha256", b"senha-de-teste-9", salt, 1000)
    stored = f"pbkdf2_sha256$1000${salt.hex()}${digest.hex()}"
    assert users.verify_password("senha-de-teste-9", stored) is True
    assert users.verify_password("senha-de-teste-8", stored) is False


def test_verify_rejects_huge_password_without_hashing():
    stored = users.hash_password("senha-normal-1")
    assert users.verify_password("x" * (users.MAX_PASSWORD_LEN + 1), stored) is False


def test_generate_temp_password_is_readable_and_random():
    seen = {users.generate_temp_password() for _ in range(200)}
    assert len(seen) == 200
    for pw in seen:
        assert len(pw) >= 10
        assert pw.isalnum() and pw.isascii()
        assert not set(pw) & AMBIGUOUS
        assert any(c.isdigit() for c in pw) and any(c.isalpha() for c in pw)


# --- cargos -------------------------------------------------------------------

@pytest.mark.parametrize("roles, label", [
    ("comprador,vendedor", "Comprador e vendedor"),
    ("vendedor, comprador", "Comprador e vendedor"),     # ordem de exibição fixa
    (["vendedor"], "Vendedor"),
    ("documentador", "Documentador"),
    ("chefe", "Chefe"),
    ("comprador,vendedor,chefe", "Comprador, vendedor e chefe"),
    ("", ""), (None, ""), ("vendedor,vendedor", "Vendedor"),
])
def test_roles_label(roles, label):
    assert users.roles_label(roles) == label


def test_role_options_follow_role_labels():
    assert users.role_options() == [{"key": k, "label": v} for k, v in users.ROLE_LABELS.items()]
    assert list(users.ROLE_LABELS) == ["comprador", "vendedor", "documentador", "chefe"]


# --- cadastro -----------------------------------------------------------------

def _by_username(conn, username):
    return next(u for u in users.list_users(conn) if u["username"] == username)


def _raw(conn, username):
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def test_seeded_users_have_no_password(conn):
    seeded = {u["username"]: u for u in users.list_users(conn)}
    assert {"arthur", "marcelo", "beto", "thiago", "haroldo", "ruan"} <= set(seeded)
    assert seeded["arthur"]["roles"] == ["comprador", "vendedor"]
    assert seeded["ruan"]["roles_label"] == "Chefe"
    assert not any(u["has_password"] for u in seeded.values())
    assert users.has_pending_users(conn) is True


def test_create_user_ok_and_listed_without_hash(conn):
    assert users.create_user(conn, "  Teste.Um ", "  Teste   Um ", ["vendedor", "comprador"]) == "saved"
    u = _by_username(conn, "teste.um")
    assert u["name"] == "Teste Um" and u["roles"] == ["comprador", "vendedor"]
    assert u["roles_label"] == "Comprador e vendedor"
    assert u["active"] is True and u["has_password"] is False and u["must_change"] is True
    assert u["last_login"] is None and u["pw_version"] == 0
    assert "password_hash" not in u
    assert users.get_user(conn, u["id"]) == u
    assert users.get_user(conn, 999_999) is None


@pytest.mark.parametrize("username, name, roles", [
    ("a", "Nome", "vendedor"),                 # curto
    ("x" * 31, "Nome", "vendedor"),            # longo
    ("joão", "Nome", "vendedor"),              # acento
    ("jo ao", "Nome", "vendedor"),             # espaço
    ("jo@ao", "Nome", "vendedor"),
    ("admin", "Nome", "vendedor"),             # reservados da tela de login
    ("Equipe", "Nome", "vendedor"),
    ("valido", "", "vendedor"),                # sem nome
    ("valido", "n" * 61, "vendedor"),
    ("valido", "Nome", ""),                    # sem cargo
    ("valido", "Nome", ["vendedor", "gerente"]),  # cargo fora da lista
])
def test_create_user_invalid(conn, username, name, roles):
    before = len(users.list_users(conn))
    assert users.create_user(conn, username, name, roles) == "invalid"
    assert len(users.list_users(conn)) == before


def test_create_user_exists(conn):
    assert users.create_user(conn, "ARTHUR", "Outro Arthur", "vendedor") == "exists"
    assert users.create_user(conn, "novo.user", "Novo", "chefe") == "saved"
    assert users.create_user(conn, "novo.user", "Novo 2", "chefe") == "exists"
    assert _by_username(conn, "novo.user")["name"] == "Novo"


# --- senha provisória, troca, ativação ---------------------------------------------

def test_reset_password_stores_only_hash_and_bumps_version(conn):
    uid = _by_username(conn, "beto")["id"]
    temp = users.reset_password(conn, uid)
    row = _raw(conn, "beto")
    assert isinstance(temp, str) and len(temp) >= 10
    assert row["password_hash"] and temp not in row["password_hash"]
    assert temp not in " ".join(str(v) for v in tuple(row))        # nada em texto no banco
    assert row["must_change"] == 1 and row["pw_version"] == 1
    assert users.verify_password(temp, row["password_hash"])
    again = users.reset_password(conn, uid)
    assert again != temp and _raw(conn, "beto")["pw_version"] == 2
    assert users.authenticate(conn, "beto", temp) is None            # a anterior morreu
    assert users.reset_password(conn, 999_999) is None


def test_change_password_outcomes(conn):
    uid = _by_username(conn, "thiago")["id"]
    assert users.change_password(conn, uid, "qualquer", "nova-senha-123") == "wrong"   # ainda sem senha
    temp = users.reset_password(conn, uid)
    v = _raw(conn, "thiago")["pw_version"]
    assert users.change_password(conn, uid, "errada", "nova-senha-123") == "wrong"
    assert users.change_password(conn, uid, temp, "curta12") == "weak"                  # < 8
    assert users.change_password(conn, uid, temp, " " * 9) == "weak"
    assert users.change_password(conn, uid, temp, temp) == "same"
    assert _raw(conn, "thiago")["pw_version"] == v and _raw(conn, "thiago")["must_change"] == 1
    assert users.change_password(conn, uid, temp, "nova-senha-123") == "saved"
    row = _raw(conn, "thiago")
    assert row["must_change"] == 0 and row["pw_version"] == v + 1
    assert "nova-senha-123" not in row["password_hash"]
    assert users.authenticate(conn, "thiago", temp) is None
    assert users.authenticate(conn, "thiago", "nova-senha-123")["must_change"] is False
    assert users.change_password(conn, 999_999, "a", "nova-senha-123") == "wrong"


def test_set_active_bumps_version_only_when_deactivating(conn):
    uid = _by_username(conn, "haroldo")["id"]
    temp = users.reset_password(conn, uid)
    v = _raw(conn, "haroldo")["pw_version"]
    assert users.set_active(conn, uid, False) is True
    assert _raw(conn, "haroldo")["active"] == 0 and _raw(conn, "haroldo")["pw_version"] == v + 1
    assert users.authenticate(conn, "haroldo", temp) is None
    assert users.change_password(conn, uid, temp, "nova-senha-123") == "wrong"   # desativado não troca
    assert users.set_active(conn, uid, True) is True
    assert _raw(conn, "haroldo")["active"] == 1 and _raw(conn, "haroldo")["pw_version"] == v + 1
    assert users.authenticate(conn, "haroldo", temp) is not None
    assert users.set_active(conn, 999_999, False) is False


# --- authenticate -------------------------------------------------------------------

def test_authenticate(conn):
    uid = _by_username(conn, "marcelo")["id"]
    assert users.authenticate(conn, "marcelo", "") is None                 # sem senha não entra
    assert users.authenticate(conn, "marcelo", "qualquer-coisa") is None
    temp = users.reset_password(conn, uid)
    assert users.authenticate(conn, "marcelo", temp + "x") is None
    assert _by_username(conn, "marcelo")["last_login"] is None
    user = users.authenticate(conn, "  MARCELO ", temp)                    # caixa e espaços nas pontas
    assert user["id"] == uid and user["username"] == "marcelo" and user["must_change"] is True
    assert user["roles_label"] == "Comprador e vendedor" and "password_hash" not in user
    assert isinstance(user["last_login"], datetime)
    assert isinstance(_by_username(conn, "marcelo")["last_login"], datetime)
    assert users.authenticate(conn, "naoexiste", temp) is None
    assert users.authenticate(conn, "mar celo", temp) is None
    assert users.authenticate(conn, "", temp) is None
    assert users.authenticate(conn, None, None) is None


def test_has_pending_users_ends_when_all_active_have_password(conn):
    for u in users.list_users(conn):
        if u["username"] != "ruan":
            users.reset_password(conn, u["id"])
    assert users.has_pending_users(conn) is True          # falta o ruan
    users.set_active(conn, _by_username(conn, "ruan")["id"], False)
    assert users.has_pending_users(conn) is False         # desativado sem senha não conta
    users.create_user(conn, "novato", "Novato", "vendedor")
    assert users.has_pending_users(conn) is True


def test_no_password_in_logs(conn, caplog):
    caplog.set_level(logging.DEBUG)
    uid = _by_username(conn, "arthur")["id"]
    temp = users.reset_password(conn, uid)
    users.authenticate(conn, "arthur", temp)
    users.authenticate(conn, "arthur", "senha-errada-xyz")
    users.change_password(conn, uid, temp, "definitiva-456")
    text = caplog.text
    assert temp not in text and "definitiva-456" not in text and "senha-errada-xyz" not in text


# --- pós-QA F15 ----------------------------------------------------------------------

@pytest.mark.parametrize("weak", [
    "12345678", "123456789", "87654321", "23456789", "abcdefgh",     # sequências
    "senha123", "Senha@2026", "password", "PASSWORD1", "qwertyui",    # óbvias
    "union123", "UNION2026!", "unionveiculos1",                       # nome da loja
    "aaaaaaaa", "abababab", "11111111",                               # poucos caracteres diferentes
    "thiago12", "THIAGO2026", "xxthiagoxx", "thiagothiago",           # contém usuário / primeiro nome
    "        a", "  curta1 ", "         ",                            # espaços nas pontas não contam
    "curta12", "", "x" * 201,
])
def test_change_password_refuses_weak_passwords(conn, weak):
    uid = _by_username(conn, "thiago")["id"]
    temp = users.reset_password(conn, uid)
    before = _raw(conn, "thiago")
    assert users.change_password(conn, uid, temp, weak) == "weak"
    after = _raw(conn, "thiago")
    assert after["password_hash"] == before["password_hash"] and after["must_change"] == 1
    assert after["pw_version"] == before["pw_version"]


@pytest.mark.parametrize("fine", ["cachorro azul", "gato2024", "19870523", "minha-senha-nova-789",
                                  "tudo minusculo sem simbolo", "  abcdxyz12  "])
def test_change_password_accepts_simple_but_not_trivial_passwords(conn, fine):
    """Usuário leigo: sem exigir símbolo, número ou maiúscula."""
    uid = _by_username(conn, "thiago")["id"]
    temp = users.reset_password(conn, uid)
    assert users.change_password(conn, uid, temp, fine) == "saved"
    assert users.authenticate(conn, "thiago", fine) is not None


def test_weak_check_uses_first_name_without_accents_or_case(conn):
    assert users.create_user(conn, "jsilva", "João da Silva", "vendedor") == "saved"
    uid = _by_username(conn, "jsilva")["id"]
    temp = users.reset_password(conn, uid)
    for weak in ("joao2024x", "JOÃO#2024", "meujsilva9"):
        assert users.change_password(conn, uid, temp, weak) == "weak", weak
    assert users.change_password(conn, uid, temp, "silva-carro-88") == "saved"   # sobrenome pode


def test_is_weak_password_ignores_too_short_names():
    assert users.is_weak_password("jo-cachorro-9", "jo", "Jo") is False   # nome de 2 letras não bloqueia
    assert users.is_weak_password("cachorro-9", "", "") is False


@pytest.mark.parametrize("name", ["Jo\x00ao", "\x00", "Jo\x07ao", "Jo\x1bao", "Jo​ao", "Jo\x7fao", "Jo‮ao"])
def test_create_user_rejects_control_characters_in_name(conn, name):
    before = len(users.list_users(conn))
    assert users.create_user(conn, "nulo", name, "chefe") == "invalid"
    assert len(users.list_users(conn)) == before


def test_create_user_still_accepts_accents_and_inner_whitespace(conn):
    assert users.create_user(conn, "ze.maria", "José\tMaria  d'Ávila\n", "vendedor") == "saved"
    assert _by_username(conn, "ze.maria")["name"] == "José Maria d'Ávila"


@pytest.mark.parametrize("username", ["union", "UNION", " Union "])
def test_union_is_a_reserved_username(conn, username):
    assert users.create_user(conn, username, "Union", "chefe") == "invalid"
    assert "union" in users.RESERVED_USERNAMES
