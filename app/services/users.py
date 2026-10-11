"""Funcionários com login individual (tabela `users`).

Senhas: só o hash vai para o banco (scrypt da stdlib, salt por usuário). A senha provisória
existe em texto apenas no retorno de reset_password, para a tela do admin mostrar UMA vez.
Nada aqui escreve senha em log.

Todas as funções recebem a conexão (sqlite3 ou PgConnection) e fazem commit do que gravam.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import unicodedata
from collections.abc import Iterable

from ..db import from_iso, now, to_iso

ROLE_LABELS = {"comprador": "Comprador", "vendedor": "Vendedor",
               "documentador": "Documentador", "chefe": "Chefe"}
VALID_ROLES = tuple(ROLE_LABELS)  # ordem de exibição
USERNAME_RE = re.compile(r"[a-z0-9._-]{2,30}")
# Nomes com significado próprio na tela de login (admin / senha única da equipe).
# 'union': a tela antiga gravava esse usuário nos gerenciadores de senha (vale como vazio).
RESERVED_USERNAMES = {"admin", "equipe", "union"}
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 200  # evita gastar CPU do scrypt com entradas gigantes
MAX_NAME_LEN = 60

# Sem caracteres ambíguos: 0/O, 1/l/I.
_TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
TEMP_PASSWORD_LEN = 12

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_DKLEN = 2 ** 14, 8, 1, 32
_PBKDF2_ITERATIONS = 600_000

# Retornos em texto = os mesmos valores de `msg` das telas.
SAVED, INVALID, EXISTS = "saved", "invalid", "exists"
CHANGE_SAVED, CHANGE_WRONG, CHANGE_WEAK, CHANGE_SAME = "saved", "wrong", "weak", "same"

# Senhas triviais (comparadas em minúsculas). Sem exigir símbolo nem maiúscula: o usuário é leigo.
TRIVIAL_PASSWORDS = {
    "12345678", "123456789", "1234567890", "87654321", "987654321", "12341234", "123123123",
    "password", "password1", "passw0rd", "qwertyui", "qwertyuiop", "asdfghjk", "abcdefgh", "abcd1234",
    "a1b2c3d4", "1q2w3e4r", "senha123", "union123",
}
# Palavra óbvia + números/símbolos ("senha2026", "union@123", "unionveiculos1").
TRIVIAL_WORDS = {"senha", "senhas", "password", "qwerty", "union", "unionveiculos", "unionrio",
                 "veiculos", "carro", "carros", "admin", "equipe", "teste", "mudar", "trocar", "abc", "abcd"}
MIN_DISTINCT_CHARS = 3


# ---------- senha ----------

def hash_password(password: str) -> str:
    """'scrypt$n$r$p$salt_hex$hash_hex' (ou 'pbkdf2_sha256$iter$salt$hash' onde não há scrypt)."""
    salt = secrets.token_bytes(16)
    data = password.encode("utf-8")
    if hasattr(hashlib, "scrypt"):
        digest = hashlib.scrypt(data, salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN)
        return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"
    digest = hashlib.pbkdf2_hmac("sha256", data, salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Compara em tempo constante. Hash ausente/malformado → False."""
    if not stored or not isinstance(password, str) or len(password) > MAX_PASSWORD_LEN:
        return False
    try:
        scheme, *parts = stored.split("$")
        data = password.encode("utf-8")
        if scheme == "scrypt":
            n, r, p, salt_hex, hash_hex = parts
            expected = bytes.fromhex(hash_hex)
            digest = hashlib.scrypt(data, salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p),
                                    dklen=len(expected))
        elif scheme == "pbkdf2_sha256":
            iterations, salt_hex, hash_hex = parts
            expected = bytes.fromhex(hash_hex)
            digest = hashlib.pbkdf2_hmac("sha256", data, bytes.fromhex(salt_hex), int(iterations))
        else:
            return False
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


_dummy_hash: str | None = None


def _burn(password: str) -> None:
    """Gasta o mesmo tempo de uma verificação real (usuário inexistente/sem senha),
    para o tempo de resposta não revelar se o usuário existe."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16))
    verify_password(password, _dummy_hash)


def generate_temp_password() -> str:
    """Senha provisória legível (letras e números, sem 0/O/1/l/I)."""
    while True:
        candidate = "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(TEMP_PASSWORD_LEN))
        if any(c.isdigit() for c in candidate) and any(c.islower() for c in candidate) \
                and any(c.isupper() for c in candidate):
            return candidate


# ---------- cargos ----------

def parse_roles(roles: str | Iterable[str] | None) -> list[str]:
    """'comprador, vendedor' ou ['vendedor', 'comprador'] → lista na ordem de exibição, sem repetir."""
    if roles is None:
        return []
    items = roles.split(",") if isinstance(roles, str) else list(roles)
    found = {str(item).strip().lower() for item in items if str(item).strip()}
    return [role for role in VALID_ROLES if role in found] + sorted(found - set(VALID_ROLES))


def roles_label(roles: str | Iterable[str] | None) -> str:
    """'comprador,vendedor' → 'Comprador e vendedor'; 'chefe' → 'Chefe'; vazio → ''."""
    labels = [ROLE_LABELS.get(role, role.capitalize()) for role in parse_roles(roles)]
    if not labels:
        return ""
    labels = [labels[0]] + [label.lower() for label in labels[1:]]
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " e " + labels[-1]


def role_options() -> list[dict]:
    return [{"key": key, "label": label} for key, label in ROLE_LABELS.items()]


# ---------- leitura ----------

def normalize_username(username: str | None) -> str:
    return (username or "").strip().lower()


def _public(row) -> dict:
    """Linha do banco → dict sem o hash."""
    roles = parse_roles(row["roles"])
    return {
        "id": row["id"],
        "username": row["username"],
        "name": row["name"],
        "roles": roles,
        "roles_label": roles_label(roles),
        "active": bool(row["active"]),
        "has_password": row["password_hash"] is not None,
        "must_change": bool(row["must_change"]),
        "pw_version": int(row["pw_version"] or 0),
        "last_login": from_iso(row["last_login"]),
    }


_COLUMNS = "id, username, name, roles, password_hash, must_change, pw_version, active, last_login"


def _row(conn, user_id: int):
    return conn.execute(f"SELECT {_COLUMNS} FROM users WHERE id = ?", (user_id,)).fetchone()


def list_users(conn) -> list[dict]:
    """Todos os funcionários (ativos primeiro, por nome), sem hash."""
    rows = conn.execute(f"SELECT {_COLUMNS} FROM users ORDER BY active DESC, name, id").fetchall()
    return [_public(row) for row in rows]


def get_user(conn, user_id: int) -> dict | None:
    row = _row(conn, user_id)
    return _public(row) if row is not None else None


def has_pending_users(conn) -> bool:
    """Existe funcionário ATIVO ainda sem senha? (enquanto existir, a senha única antiga vale)"""
    row = conn.execute(
        "SELECT 1 FROM users WHERE active = 1 AND password_hash IS NULL LIMIT 1").fetchone()
    return row is not None


# ---------- escrita ----------

def create_user(conn, username: str, name: str, roles: str | Iterable[str]) -> str:
    """Cadastra sem senha (não entra até o admin gerar a provisória).
    Retorna 'saved' | 'invalid' (usuário, nome ou cargo inválido) | 'exists'."""
    username = normalize_username(username)
    name = " ".join((name or "").split())
    role_list = parse_roles(roles)
    if not USERNAME_RE.fullmatch(username) or username in RESERVED_USERNAMES:
        return INVALID
    if not name or len(name) > MAX_NAME_LEN:
        return INVALID
    if any(unicodedata.category(ch).startswith("C") for ch in name):
        return INVALID  # caracteres de controle/invisíveis (NUL derruba o INSERT no Postgres)
    if not role_list or not set(role_list) <= set(VALID_ROLES):
        return INVALID
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        return EXISTS
    try:
        conn.execute(
            "INSERT INTO users (username, name, roles, password_hash, must_change, pw_version, active, created_at) "
            "VALUES (?, ?, ?, NULL, 1, 0, 1, ?)",
            (username, name, ",".join(role_list), to_iso(now())))
        conn.commit()
    except Exception:  # corrida no UNIQUE: outro request criou o mesmo username
        conn.rollback()
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            return EXISTS
        raise
    return SAVED


def reset_password(conn, user_id: int) -> str | None:
    """Gera a senha provisória, grava só o hash e devolve o texto (mostrar UMA vez).
    must_change=1 e pw_version+1 (derruba as sessões abertas do funcionário). Id inexistente → None."""
    if _row(conn, user_id) is None:
        return None
    temp = generate_temp_password()
    conn.execute(
        "UPDATE users SET password_hash = ?, must_change = 1, pw_version = pw_version + 1 WHERE id = ?",
        (hash_password(temp), user_id))
    conn.commit()
    return temp


def set_active(conn, user_id: int, active: bool) -> bool:
    """Ativa/desativa. Desativar também sobe pw_version (sessões caem na hora).
    Retorna False se o id não existe."""
    if _row(conn, user_id) is None:
        return False
    if active:
        conn.execute("UPDATE users SET active = 1 WHERE id = ?", (user_id,))
    else:
        conn.execute("UPDATE users SET active = 0, pw_version = pw_version + 1 WHERE id = ?", (user_id,))
    conn.commit()
    return True


def _fold(text: str) -> str:
    """Minúsculas e sem acento, para comparar senha com nome/usuário."""
    plain = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in plain if not unicodedata.combining(ch)).casefold()


def _is_sequence(text: str) -> bool:
    """'23456789', '98765432', 'abcdefghi': todos os caracteres em passo +1 ou -1."""
    steps = {ord(b) - ord(a) for a, b in zip(text, text[1:])}
    return steps in ({1}, {-1})


def is_weak_password(password: str, username: str = "", name: str = "") -> bool:
    """Senha fraca demais para aceitar. Regras (sem exigir símbolo nem maiúscula):
    - menos de 8 caracteres, sem contar espaços nas pontas (ou mais de MAX_PASSWORD_LEN);
    - menos de 3 caracteres diferentes ("aaaaaaaa", "abababab");
    - contém o próprio usuário ou o primeiro nome ("beto1234");
    - sequência ou senha/palavra óbvia ("12345678", "senha123", "union2026")."""
    password = password or ""
    core = password.strip()
    if len(core) < MIN_PASSWORD_LEN or len(password) > MAX_PASSWORD_LEN:
        return True
    folded = _fold(core)
    if len(set(folded)) < MIN_DISTINCT_CHARS:
        return True
    first_name = (name or "").split()[0] if (name or "").split() else ""
    for own in (_fold(normalize_username(username)), _fold(first_name)):
        if len(own) >= 3 and own in folded:
            return True
    if folded in TRIVIAL_PASSWORDS or _is_sequence(folded):
        return True
    letters = "".join(ch for ch in folded if ch.isalpha())
    return letters in TRIVIAL_WORDS


def change_password(conn, user_id: int, current: str, new: str) -> str:
    """Troca feita pelo próprio funcionário. Retorna 'saved' | 'wrong' | 'weak' | 'same'
    (os mesmos valores de `msg` da tela). Em 'saved': must_change=0 e pw_version+1.
    'weak': ver is_weak_password."""
    row = _row(conn, user_id)
    if row is None or not row["active"] or not verify_password(current or "", row["password_hash"]):
        return CHANGE_WRONG
    new = new or ""
    if is_weak_password(new, row["username"], row["name"]):
        return CHANGE_WEAK
    if hmac.compare_digest(new.encode("utf-8"), (current or "").encode("utf-8")):
        return CHANGE_SAME
    conn.execute(
        "UPDATE users SET password_hash = ?, must_change = 0, pw_version = pw_version + 1 WHERE id = ?",
        (hash_password(new), user_id))
    conn.commit()
    return CHANGE_SAVED


def authenticate(conn, username: str, password: str) -> dict | None:
    """Funcionário ativo, com senha, e senha correta → dict (sem hash); senão None.
    Não diferencia maiúsculas no usuário e ignora espaços nas pontas. Atualiza last_login."""
    username = normalize_username(username)
    password = password or ""
    row = None
    if USERNAME_RE.fullmatch(username):
        row = conn.execute(f"SELECT {_COLUMNS} FROM users WHERE username = ?", (username,)).fetchone()
    if row is None or not row["active"] or row["password_hash"] is None:
        _burn(password)
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    stamp = to_iso(now())
    conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (stamp, row["id"]))
    conn.commit()
    user = _public(row)
    user["last_login"] = from_iso(stamp)
    return user
