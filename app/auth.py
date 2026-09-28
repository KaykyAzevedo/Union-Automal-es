"""Login do painel com dois perfis (cookie assinado com HMAC-SHA256).

Perfis — a senha digitada em /login decide:
- admin: APP_PASSWORD → acesso total.
- staff (equipe): STAFF_PASSWORD → só STAFF_PATHS (/equipe) + rotas públicas.
  O bloqueio é central (middleware, por allowlist): rota nova já nasce proibida para a equipe.

Variáveis (lidas a cada requisição):
- APP_PASSWORD: senha de admin. Ausente → local sem login (tudo aberto como admin,
  STAFF_PASSWORD ignorada); na Vercel (VERCEL=1) → 503.
- STAFF_PASSWORD: senha da equipe (opcional; sem ela o perfil equipe fica desligado).
  Igual à APP_PASSWORD → o login entra como admin.
- SESSION_SECRET: chave que assina o cookie. Obrigatória na Vercel; local, se ausente,
  usa uma chave aleatória por processo (sessões caem ao reiniciar).

Integração (main.py): `install_auth(app, templates)`.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

COOKIE_NAME = "union_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 dias
MAX_FAILURES = 5  # tentativas erradas por IP (qualquer senha)...
FAILURE_WINDOW = 5 * 60  # ...dentro desta janela (s) → 429 até a janela passar

ADMIN, STAFF = "admin", "staff"
ROLES = (ADMIN, STAFF)

# Caminhos que não exigem login (/cron/* tem o próprio Bearer CRON_SECRET).
PUBLIC_PREFIXES = ("/static/", "/cron/")
PUBLIC_PATHS = {"/login", "/logout", "/health", "/favicon.ico"}
# Únicos caminhos (além dos públicos) liberados para a equipe.
STAFF_HOME = "/equipe"
STAFF_PATHS = {"/equipe"}
STAFF_PREFIXES = ("/equipe/",)

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
    """Senha da equipe; None se não configurada ou se o login está desligado (sem APP_PASSWORD)."""
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


# ---------- senhas e cookie ----------

def _matches(candidate: str, expected: str | None) -> bool:
    # digests de tamanho fixo: compare_digest não vaza nem o tamanho da senha
    same = hmac.compare_digest(hashlib.sha256(candidate.encode()).digest(),
                               hashlib.sha256((expected or "").encode()).digest())
    return same and expected is not None


def password_role(candidate: str) -> str | None:
    """Perfil da senha digitada (admin tem prioridade) ou None. Sempre compara as duas."""
    is_admin = _matches(candidate, app_password())
    is_staff = _matches(candidate, staff_password())
    if is_admin:
        return ADMIN
    return STAFF if is_staff else None


def check_password(candidate: str) -> bool:
    return password_role(candidate) is not None


def _sign(payload: str, secret: str, role: str, password: str) -> str:
    # Perfil + senha dele entram na chave: trocar STAFF_PASSWORD derruba só a equipe,
    # trocar APP_PASSWORD derruba só os admins.
    key = hashlib.sha256(f"{secret}\x00{role}\x00{password}".encode()).digest()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def make_session_token(role: str = ADMIN, now: float | None = None) -> str:
    secret, password = _session_secret(), _role_password(role)
    if not secret or not password:
        raise RuntimeError(f"auth não configurada para o perfil {role!r}")
    payload = f"v2.{role}.{int(now if now is not None else time.time())}"
    return f"{payload}.{_sign(payload, secret, role, password)}"


def session_role(token: str | None, now: float | None = None) -> str | None:
    """Perfil de um cookie válido ('admin' | 'staff') ou None."""
    secret = _session_secret()
    if not token or not secret:
        return None
    try:
        version, role, issued, signature = token.split(".")
        issued_at = int(issued)
    except ValueError:
        return None  # inclui cookies v1 antigos (3 partes): pedem login de novo
    password = _role_password(role)
    if version != "v2" or role not in ROLES or not password:
        return None
    expected = _sign(f"{version}.{role}.{issued}", secret, role, password)
    if not hmac.compare_digest(signature, expected):
        return None
    age = (now if now is not None else time.time()) - issued_at
    return role if -60 <= age <= SESSION_MAX_AGE else None


def verify_session_token(token: str | None, now: float | None = None) -> bool:
    return session_role(token, now) is not None


def current_role(request: Request) -> str | None:
    """'admin' | 'staff' | None. Sem login configurado (local) → 'admin'."""
    if not auth_enabled():
        return ADMIN
    return session_role(request.cookies.get(COOKIE_NAME))


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


def home_for(role: str | None, next_url: str | None = None) -> str:
    """Destino depois do login: equipe só vai para caminhos permitidos (senão /equipe)."""
    target = _safe_next(next_url)
    if role == STAFF and not _staff_allowed(target.split("?", 1)[0]):
        return STAFF_HOME
    return target


# ---------- rotas ----------

router = APIRouter()


def _render_login(request: Request, error: str | None, next_url: str | None, status: int) -> Response:
    ctx = {"error": error, "next": next_url}
    if _templates is not None:
        return _templates.TemplateResponse(request, "login.html", ctx, status_code=status)
    return PlainTextResponse(error or "login", status_code=status)


def _is_secure(request: Request) -> bool:
    return on_vercel() or request.url.scheme == "https" or \
        request.headers.get("x-forwarded-proto") == "https"


@router.get("/login")
def login_form(request: Request, next: str | None = None):
    role = current_role(request)
    if role is not None:
        return RedirectResponse(home_for(role, next), status_code=303)
    return _render_login(request, None, next, 200)


@router.post("/login")
def login_submit(request: Request, password: str = Form(""), next: str | None = Form(None)):
    if not auth_enabled():
        return RedirectResponse(home_for(ADMIN, next), status_code=303)
    ip = client_ip(request)
    wait = retry_after(ip)
    if wait:
        resp = _render_login(request, f"Muitas tentativas. Tente de novo em {max(1, round(wait / 60))} min.",
                             next, 429)
        resp.headers["Retry-After"] = str(wait)
        return resp
    role = password_role(password)
    if role is None:
        register_failure(ip)
        return _render_login(request, "Senha incorreta", next, 401)
    reset_failures(ip)
    resp = RedirectResponse(home_for(role, next), status_code=303)
    resp.set_cookie(COOKIE_NAME, make_session_token(role), max_age=SESSION_MAX_AGE, httponly=True,
                    samesite="lax", secure=_is_secure(request), path="/")
    return resp


@router.api_route("/logout", methods=["GET", "POST"])
def logout(request: Request):
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=_is_secure(request))
    return resp


# ---------- middleware ----------

def _forbidden_for_staff(request: Request) -> Response:
    if request.headers.get("hx-request"):
        return Response(status_code=403, headers={"HX-Redirect": STAFF_HOME})
    if request.method in ("GET", "HEAD"):
        return RedirectResponse(STAFF_HOME, status_code=303)
    return JSONResponse({"detail": "acesso restrito ao painel da equipe"}, status_code=403)


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    problem = config_error()
    if problem and not path.startswith("/static/"):
        return PlainTextResponse(problem, status_code=503)
    role = current_role(request)
    request.state.role = role
    if _is_public(path) or role == ADMIN:
        return await call_next(request)
    if role == STAFF:
        if _staff_allowed(path):
            return await call_next(request)
        return _forbidden_for_staff(request)

    if request.headers.get("hx-request"):
        # HTMX: 401 + HX-Redirect faz a página inteira ir para o login
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    if request.method in ("GET", "HEAD"):
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='/')}", status_code=303)
    return JSONResponse({"detail": "login necessário"}, status_code=401)


def _role_context(request: Request) -> dict:
    role = getattr(request.state, "role", None)
    return {"role": role if role is not None else current_role(request)}


def install_auth(app: FastAPI, templates: Jinja2Templates) -> None:
    """Liga login + middleware no app e expõe `auth_enabled` e `role` para os templates."""
    global _templates
    _templates = templates
    templates.env.globals["auth_enabled"] = _AuthEnabledFlag()
    templates.env.globals.setdefault("role", None)
    if _role_context not in templates.context_processors:
        templates.context_processors.append(_role_context)
    app.include_router(router)
    app.middleware("http")(auth_middleware)
