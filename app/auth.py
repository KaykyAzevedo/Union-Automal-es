"""Login do painel (cookie assinado com HMAC-SHA256).

Quem entra — /login tem os campos usuário e senha:
- admin: usuário vazio, 'admin' ou 'union' (o que a tela antiga gravou nos gerenciadores de
  senha) + APP_PASSWORD → acesso total (perfil 'admin').
- funcionário: usuário próprio (tabela `users`, app/services/users.py) + senha própria →
  perfil 'staff' (só STAFF_PATHS, /equipe*). A sessão leva id e pw_version do funcionário
  e é conferida no banco a cada requisição: desativar ou gerar senha nova derruba na hora.
  Com must_change=1 (senha provisória) tudo redireciona para /equipe/senha.
- senha única da equipe (STAFF_PASSWORD), TRANSIÇÃO: usuário vazio, 'equipe' ou 'union'. Só vale
  enquanto existir funcionário ativo sem senha; depois para sozinha e as sessões dela caem.

O bloqueio é central (middleware, por allowlist): rota nova já nasce proibida para a equipe.

Variáveis (lidas a cada requisição):
- APP_PASSWORD: senha de admin. Ausente → local sem login (tudo aberto como admin);
  na Vercel (VERCEL=1) → 503.
- STAFF_PASSWORD: senha única da equipe (opcional; ver transição acima).
  Igual à APP_PASSWORD → o login entra como admin.
- SESSION_SECRET: chave que assina o cookie. Obrigatória na Vercel; local, se ausente,
  usa uma chave aleatória por processo (sessões caem ao reiniciar).

Integração (main.py): `install_auth(app, templates)`. Nenhuma senha é escrita em log.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import db
from .services import users

log = logging.getLogger(__name__)

COOKIE_NAME = "union_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 dias
MAX_FAILURES = 5  # tentativas erradas por IP (qualquer usuário/senha)...
FAILURE_WINDOW = 5 * 60  # ...dentro desta janela (s) → 429 até a janela passar
LOGIN_ERROR = "Usuário ou senha incorretos"  # única: não revela se o usuário existe

ADMIN, STAFF = "admin", "staff"
ROLES = (ADMIN, STAFF)
# 'union' = apelido de usuário vazio: a tela antiga de login tinha um campo oculto
# username='union', e os gerenciadores de senha dos celulares continuam preenchendo isso.
LEGACY_USERNAME = "union"
ADMIN_USERNAMES = ("", "admin", LEGACY_USERNAME)
SHARED_USERNAMES = ("", "equipe", LEGACY_USERNAME)

# Caminhos que não exigem login (/cron/* tem o próprio Bearer CRON_SECRET).
PUBLIC_PREFIXES = ("/static/", "/cron/")
PUBLIC_PATHS = {"/login", "/logout", "/health", "/favicon.ico"}
# Únicos caminhos (além dos públicos) liberados para a equipe.
STAFF_HOME = "/equipe"
STAFF_PATHS = {"/equipe"}
STAFF_PREFIXES = ("/equipe/",)
# Único caminho da equipe liberado enquanto a senha é provisória (must_change).
CHANGE_PASSWORD_PATH = "/equipe/senha"

_LOCAL_SECRET = secrets.token_hex(32)
_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()
_templates: Jinja2Templates | None = None


# ---------- configuração ----------

def on_vercel() -> bool:
    return os.environ.get("VERCEL") == "1"


def app_password() -> str | None:
    return os.environ.get("APP_PASSWORD") or None


def staff_password() -> str | None:
    """Senha única da equipe; None se não configurada ou se o login está desligado (sem APP_PASSWORD)."""
    if app_password() is None:
        return None
    return os.environ.get("STAFF_PASSWORD") or None


def auth_enabled() -> bool:
    return app_password() is not None


def _role_password(role: str) -> str | None:
    if role == ADMIN:
        return app_password()
    if role == STAFF:
        return staff_password()
    return None


def _session_secret() -> str | None:
    return os.environ.get("SESSION_SECRET") or (None if on_vercel() else _LOCAL_SECRET)


def config_error() -> str | None:
    """Mensagem de configuração faltando na nuvem (→ 503), ou None se está ok."""
    if not on_vercel():
        return None
    missing = [name for name, value in (("APP_PASSWORD", app_password()),
                                        ("SESSION_SECRET", os.environ.get("SESSION_SECRET")))
               if not value]
    if missing:
        return ("Painel não configurado: defina " + " e ".join(missing)
                + " nas variáveis de ambiente da Vercel e faça um novo deploy.")
    return None


class _AuthEnabledFlag:
    """Global do Jinja `auth_enabled`: reavaliada a cada uso, não congelada no import."""

    def __bool__(self) -> bool:
        return auth_enabled()

    def __repr__(self) -> str:
        return repr(auth_enabled())


# ---------- banco (funcionários) ----------

def _is_missing_table(exc: Exception) -> bool:
    text = str(exc).lower()
    return "users" in text and ("no such table" in text or "does not exist" in text)


def _with_conn(conn, fn):
    if conn is not None:
        return fn(conn)
    own = db.connect()
    try:
        return fn(own)
    finally:
        own.close()


def shared_password_active(conn=None) -> bool:
    """A senha única da equipe ainda vale? STAFF_PASSWORD configurada E existe funcionário
    ativo sem senha. Banco antigo sem a tabela `users` → vale (legado). Outro erro → não vale."""
    if staff_password() is None:
        return False
    try:
        return bool(_with_conn(conn, users.has_pending_users))
    except Exception as exc:
        if _is_missing_table(exc):
            return True
        log.warning("auth: falha ao consultar funcionários (%s); senha única recusada", type(exc).__name__)
        return False


def _load_user(user_id: int) -> dict | None:
    try:
        return _with_conn(None, lambda conn: users.get_user(conn, user_id))
    except Exception as exc:  # sem banco não dá para conferir a sessão: recusa
        log.warning("auth: falha ao conferir a sessão do funcionário (%s)", type(exc).__name__)
        return None


# ---------- senhas e cookie ----------

def _matches(candidate: str, expected: str | None) -> bool:
    # digests de tamanho fixo: compare_digest não vaza nem o tamanho da senha
    same = hmac.compare_digest(hashlib.sha256(candidate.encode()).digest(),
                               hashlib.sha256((expected or "").encode()).digest())
    return same and expected is not None


def password_role(candidate: str) -> str | None:
    """Perfil de uma senha GLOBAL (admin tem prioridade) ou None. Sempre compara as duas.
    Não olha a transição da senha única nem os funcionários (isso é do login)."""
    is_admin = _matches(candidate, app_password())
    is_staff = _matches(candidate, staff_password())
    if is_admin:
        return ADMIN
    return STAFF if is_staff else None


def check_password(candidate: str) -> bool:
    return password_role(candidate) is not None


def _sign(payload: str, secret: str, role: str, password: str) -> str:
    # Perfil + senha dele entram na chave: trocar STAFF_PASSWORD derruba só a senha única,
    # trocar APP_PASSWORD derruba só os admins.
    key = hashlib.sha256(f"{secret}\x00{role}\x00{password}".encode()).digest()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def _sign_user(payload: str, secret: str) -> str:
    key = hashlib.sha256(f"{secret}\x00user".encode()).digest()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def make_session_token(role: str = ADMIN, now: float | None = None) -> str:
    """Cookie de admin ou da senha única: v2.<role>.<ts>.<sig>."""
    secret, password = _session_secret(), _role_password(role)
    if not secret or not password:
        raise RuntimeError(f"auth não configurada para o perfil {role!r}")
    payload = f"v2.{role}.{int(now if now is not None else time.time())}"
    return f"{payload}.{_sign(payload, secret, role, password)}"


def make_user_token(user: dict, now: float | None = None) -> str:
    """Cookie de funcionário: v3.<uid>.<pw_version>.<ts>.<sig>."""
    secret = _session_secret()
    if not secret:
        raise RuntimeError("auth não configurada (SESSION_SECRET)")
    payload = f"v3.{int(user['id'])}.{int(user['pw_version'])}.{int(now if now is not None else time.time())}"
    return f"{payload}.{_sign_user(payload, secret)}"


def _fresh(issued_at: int, now: float | None) -> bool:
    age = (now if now is not None else time.time()) - issued_at
    return -60 <= age <= SESSION_MAX_AGE


def session_role(token: str | None, now: float | None = None) -> str | None:
    """Perfil de um cookie v2 com assinatura válida ('admin' | 'staff') ou None.
    Só criptografia: a transição da senha única é conferida em resolve_token."""
    secret = _session_secret()
    if not token or not secret:
        return None
    try:
        version, role, issued, signature = token.split(".")
        issued_at = int(issued)
    except ValueError:
        return None  # inclui v1 antigo (3 partes) e v3 (5 partes)
    password = _role_password(role)
    if version != "v2" or role not in ROLES or not password:
        return None
    expected = _sign(f"{version}.{role}.{issued}", secret, role, password)
    if not hmac.compare_digest(signature, expected):
        return None
    return role if _fresh(issued_at, now) else None


def user_token_claims(token: str | None, now: float | None = None) -> tuple[int, int] | None:
    """(user_id, pw_version) de um cookie v3 com assinatura válida, ou None. Sem banco."""
    secret = _session_secret()
    if not token or not secret:
        return None
    try:
        version, uid, pwv, issued, signature = token.split(".")
        claims = int(uid), int(pwv)
        issued_at = int(issued)
    except ValueError:
        return None
    if version != "v3":
        return None
    if not hmac.compare_digest(signature, _sign_user(f"{version}.{uid}.{pwv}.{issued}", secret)):
        return None
    return claims if _fresh(issued_at, now) else None


@dataclass(frozen=True)
class Session:
    role: str | None = None      # 'admin' | 'staff' | None (sem login)
    user: dict | None = None     # funcionário logado; None = admin ou senha única
    shared: bool = False         # entrou com a senha única da equipe

    @property
    def must_change(self) -> bool:
        return bool(self.user and self.user["must_change"])


_ANON = Session()


def resolve_token(token: str | None, now: float | None = None) -> Session:
    """Sessão de um cookie, já com as regras de banco (funcionário ativo e pw_version atual;
    senha única só na transição)."""
    if not token:
        return _ANON
    if token.startswith("v3."):
        claims = user_token_claims(token, now)
        if claims is None:
            return _ANON
        user = _load_user(claims[0])
        if user is None or not user["active"] or not user["has_password"] or user["pw_version"] != claims[1]:
            return _ANON
        return Session(STAFF, user)
    role = session_role(token, now)
    if role == ADMIN:
        return Session(ADMIN)
    if role == STAFF and shared_password_active():
        return Session(STAFF, shared=True)
    return _ANON


def verify_session_token(token: str | None, now: float | None = None) -> bool:
    return resolve_token(token, now).role is not None


def current_session(request: Request) -> Session:
    """Sessão da requisição (uma consulta ao banco por requisição, guardada em request.state)."""
    cached = getattr(request.state, "union_session", None)
    if cached is not None:
        return cached
    session = Session(ADMIN) if not auth_enabled() else resolve_token(request.cookies.get(COOKIE_NAME))
    request.state.union_session = session
    return session


def current_role(request: Request) -> str | None:
    """'admin' | 'staff' | None. Sem login configurado (local) → 'admin'."""
    return current_session(request).role


def current_user(request: Request) -> dict | None:
    """Funcionário logado ({id, username, name, roles, roles_label, must_change, ...}).
    None para admin e para quem entrou com a senha única."""
    return current_session(request).user


def is_authenticated(request: Request) -> bool:
    return current_role(request) is not None


def require_role(*roles: str):
    """Dependência FastAPI (defesa extra; o bloqueio principal é o middleware)."""
    def dependency(request: Request) -> str:
        role = current_role(request)
        if role is None:
            raise HTTPException(status_code=401, detail="login necessário")
        if role not in roles:
            raise HTTPException(status_code=403, detail="acesso negado para este perfil")
        return role
    return dependency


require_admin = require_role(ADMIN)


# ---------- limite de tentativas (memória do processo) ----------

def client_ip(request: Request) -> str:
    if on_vercel():  # a Vercel sobrescreve estes headers; localmente seriam forjáveis
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        if request.headers.get("x-real-ip"):
            return request.headers["x-real-ip"]
    return request.client.host if request.client else "?"


def _recent_failures(ip: str, now: float) -> list[float]:
    recent = [t for t in _failures.get(ip, []) if now - t < FAILURE_WINDOW]
    if recent:
        _failures[ip] = recent
    else:
        _failures.pop(ip, None)
    return recent


def retry_after(ip: str, now: float | None = None) -> int:
    """Segundos até poder tentar de novo (0 = liberado)."""
    now = time.time() if now is None else now
    with _failures_lock:
        recent = _recent_failures(ip, now)
        if len(recent) < MAX_FAILURES:
            return 0
        return max(1, int(FAILURE_WINDOW - (now - recent[0])) + 1)


def register_failure(ip: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with _failures_lock:
        _recent_failures(ip, now)
        _failures.setdefault(ip, []).append(now)


def reset_failures(ip: str | None = None) -> None:
    with _failures_lock:
        if ip is None:
            _failures.clear()
        else:
            _failures.pop(ip, None)


# ---------- caminhos ----------

def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _staff_allowed(path: str) -> bool:
    return path in STAFF_PATHS or path.startswith(STAFF_PREFIXES) or _is_public(path)


def _safe_next(target: str | None) -> str:
    # só caminhos locais: evita redirect aberto (//evil.com, /\evil.com, http://...)
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")):
        return "/"
    return target


def home_for(role: str | None, next_url: str | None = None, must_change: bool = False) -> str:
    """Destino depois do login: equipe só vai para caminhos permitidos (senão /equipe);
    senha provisória vai direto para a troca."""
    if role == STAFF and must_change:
        return CHANGE_PASSWORD_PATH
    target = _safe_next(next_url)
    if role == STAFF and not _staff_allowed(target.split("?", 1)[0]):
        return STAFF_HOME
    return target


# ---------- rotas ----------

router = APIRouter()


def _render_login(request: Request, error: str | None, next_url: str | None, status: int,
                  username: str = "") -> Response:
    ctx = {"error": error, "next": next_url, "username": username}
    if _templates is not None:
        return _templates.TemplateResponse(request, "login.html", ctx, status_code=status)
    return PlainTextResponse(error or "login", status_code=status)


def _is_secure(request: Request) -> bool:
    return on_vercel() or request.url.scheme == "https" or \
        request.headers.get("x-forwarded-proto") == "https"


def _set_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_MAX_AGE, httponly=True,
                        samesite="lax", secure=_is_secure(request), path="/")


def login_response(request: Request, user: dict, url: str | None = None) -> RedirectResponse:
    """303 com um cookie NOVO do funcionário. Use depois de trocar a senha (pw_version subiu),
    passando o usuário relido do banco (users.get_user)."""
    target = url or home_for(STAFF, None, must_change=bool(user["must_change"]))
    response = RedirectResponse(target, status_code=303)
    _set_cookie(response, request, make_user_token(user))
    return response


def _login(username: str, password: str) -> Session:
    """Decide quem entrou. Session vazia = recusado."""
    name = users.normalize_username(username)
    if name in ADMIN_USERNAMES and _matches(password, app_password()):
        return Session(ADMIN)
    if name in SHARED_USERNAMES and _matches(password, staff_password()) and shared_password_active():
        return Session(STAFF, shared=True)
    if name in ADMIN_USERNAMES or name in SHARED_USERNAMES:
        return _ANON
    try:
        conn = db.connect()
        try:
            user = users.authenticate(conn, name, password)
        finally:
            conn.close()
    except Exception as exc:
        log.warning("auth: falha ao autenticar funcionário (%s)", type(exc).__name__)
        return _ANON
    return Session(STAFF, user) if user else _ANON


@router.get("/login")
def login_form(request: Request, next: str | None = None):
    session = current_session(request)
    if session.role is not None:
        return RedirectResponse(home_for(session.role, next, session.must_change), status_code=303)
    return _render_login(request, None, next, 200)


@router.post("/login")
def login_submit(request: Request, username: str = Form(""), password: str = Form(""),
                 next: str | None = Form(None)):
    if not auth_enabled():
        return RedirectResponse(home_for(ADMIN, next), status_code=303)
    shown = users.normalize_username(username)[:40]
    ip = client_ip(request)
    wait = retry_after(ip)
    if wait:
        resp = _render_login(request, f"Muitas tentativas. Tente de novo em {max(1, round(wait / 60))} min.",
                             next, 429, shown)
        resp.headers["Retry-After"] = str(wait)
        return resp
    session = _login(username, password)
    if session.role is None:
        register_failure(ip)
        return _render_login(request, LOGIN_ERROR, next, 401, shown)
    reset_failures(ip)
    resp = RedirectResponse(home_for(session.role, next, session.must_change), status_code=303)
    token = make_user_token(session.user) if session.user else make_session_token(session.role)
    _set_cookie(resp, request, token)
    return resp


@router.api_route("/logout", methods=["GET", "POST"])
def logout(request: Request):
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=_is_secure(request))
    return resp


# ---------- middleware ----------

def _send_staff_to(request: Request, target: str, detail: str) -> Response:
    if request.headers.get("hx-request"):
        return Response(status_code=403, headers={"HX-Redirect": target})
    if request.method in ("GET", "HEAD"):
        return RedirectResponse(target, status_code=303)
    return JSONResponse({"detail": detail}, status_code=403)


def _forbidden_for_staff(request: Request) -> Response:
    return _send_staff_to(request, STAFF_HOME, "acesso restrito ao painel da equipe")


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    problem = config_error()
    if problem and not path.startswith("/static/"):
        return PlainTextResponse(problem, status_code=503)
    if path.startswith("/static/"):  # arquivos estáticos: sem consulta de sessão
        return await call_next(request)
    session = current_session(request)
    role = session.role
    request.state.role = role
    request.state.user = session.user
    if _is_public(path) or role == ADMIN:
        return await call_next(request)
    if role == STAFF:
        if not _staff_allowed(path):
            return _forbidden_for_staff(request)
        if session.must_change and path != CHANGE_PASSWORD_PATH:
            return _send_staff_to(request, CHANGE_PASSWORD_PATH, "troque a senha provisória antes de continuar")
        return await call_next(request)

    if request.headers.get("hx-request"):
        # HTMX: 401 + HX-Redirect faz a página inteira ir para o login
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    if request.method in ("GET", "HEAD"):
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='/')}", status_code=303)
    return JSONResponse({"detail": "login necessário"}, status_code=401)


def _role_context(request: Request) -> dict:
    session = current_session(request)
    user = session.user
    return {
        "role": session.role,
        "current_user": {"name": user["name"], "username": user["username"],
                         "roles_label": user["roles_label"]} if user else None,
    }


def install_auth(app: FastAPI, templates: Jinja2Templates) -> None:
    """Liga login + middleware no app e expõe `auth_enabled`, `role` e `current_user` aos templates."""
    global _templates
    _templates = templates
    templates.env.globals["auth_enabled"] = _AuthEnabledFlag()
    templates.env.globals.setdefault("role", None)
    templates.env.globals.setdefault("current_user", None)
    if _role_context not in templates.context_processors:
        templates.context_processors.append(_role_context)
    app.include_router(router)
    app.middleware("http")(auth_middleware)
