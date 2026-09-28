"""Login por senha única do painel (cookie assinado com HMAC-SHA256).

Variáveis (lidas a cada requisição):
- APP_PASSWORD: senha do painel. Ausente → local sem login; na Vercel (VERCEL=1) → 503.
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

from fastapi import APIRouter, FastAPI, Form, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

COOKIE_NAME = "union_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 dias
MAX_FAILURES = 5  # tentativas erradas por IP...
FAILURE_WINDOW = 5 * 60  # ...dentro desta janela (s) → 429 até a janela passar

# Caminhos que não exigem login (/cron/* tem o próprio Bearer CRON_SECRET).
PUBLIC_PREFIXES = ("/static/", "/cron/")
PUBLIC_PATHS = {"/login", "/logout", "/health", "/favicon.ico"}

_LOCAL_SECRET = secrets.token_hex(32)
_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()
_templates: Jinja2Templates | None = None


# ---------- configuração ----------

def on_vercel() -> bool:
    return os.environ.get("VERCEL") == "1"


def app_password() -> str | None:
    return os.environ.get("APP_PASSWORD") or None


def auth_enabled() -> bool:
    return app_password() is not None


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


# ---------- senha e cookie ----------

def check_password(candidate: str) -> bool:
    expected = app_password()
    if expected is None:
        return False
    # digests de tamanho fixo: compare_digest não vaza nem o tamanho da senha
    return hmac.compare_digest(hashlib.sha256(candidate.encode()).digest(),
                               hashlib.sha256(expected.encode()).digest())


def _sign(payload: str, secret: str, password: str) -> str:
    # A senha entra na chave: trocar APP_PASSWORD invalida todas as sessões.
    key = hashlib.sha256(f"{secret}\x00{password}".encode()).digest()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def make_session_token(now: float | None = None) -> str:
    secret, password = _session_secret(), app_password()
    if not secret or not password:
        raise RuntimeError("auth não configurada")
    payload = f"v1.{int(now if now is not None else time.time())}"
    return f"{payload}.{_sign(payload, secret, password)}"


def verify_session_token(token: str | None, now: float | None = None) -> bool:
    secret, password = _session_secret(), app_password()
    if not token or not secret or not password:
        return False
    try:
        version, issued, signature = token.split(".")
        issued_at = int(issued)
    except ValueError:
        return False
    if version != "v1":
        return False
    expected = _sign(f"{version}.{issued}", secret, password)
    if not hmac.compare_digest(signature, expected):
        return False
    age = (now if now is not None else time.time()) - issued_at
    return -60 <= age <= SESSION_MAX_AGE


def is_authenticated(request: Request) -> bool:
    return not auth_enabled() or verify_session_token(request.cookies.get(COOKIE_NAME))


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


# ---------- rotas ----------

router = APIRouter()


def _safe_next(target: str | None) -> str:
    # só caminhos locais: evita redirect aberto (//evil.com, /\evil.com, http://...)
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")):
        return "/"
    return target


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
    if not auth_enabled() or is_authenticated(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return _render_login(request, None, next, 200)


@router.post("/login")
def login_submit(request: Request, password: str = Form(""), next: str | None = Form(None)):
    if not auth_enabled():
        return RedirectResponse(_safe_next(next), status_code=303)
    ip = client_ip(request)
    wait = retry_after(ip)
    if wait:
        resp = _render_login(request, f"Muitas tentativas. Tente de novo em {max(1, round(wait / 60))} min.",
                             next, 429)
        resp.headers["Retry-After"] = str(wait)
        return resp
    if not check_password(password):
        register_failure(ip)
        return _render_login(request, "Senha incorreta", next, 401)
    reset_failures(ip)
    resp = RedirectResponse(_safe_next(next), status_code=303)
    resp.set_cookie(COOKIE_NAME, make_session_token(), max_age=SESSION_MAX_AGE, httponly=True,
                    samesite="lax", secure=_is_secure(request), path="/")
    return resp


@router.api_route("/logout", methods=["GET", "POST"])
def logout(request: Request):
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=_is_secure(request))
    return resp


# ---------- middleware ----------

def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    problem = config_error()
    if problem and not path.startswith("/static/"):
        return PlainTextResponse(problem, status_code=503)
    if _is_public(path) or is_authenticated(request):
        return await call_next(request)

    if request.headers.get("hx-request"):
        # HTMX: 401 + HX-Redirect faz a página inteira ir para o login
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    if request.method in ("GET", "HEAD"):
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='/')}", status_code=303)
    return JSONResponse({"detail": "login necessário"}, status_code=401)


def install_auth(app: FastAPI, templates: Jinja2Templates) -> None:
    """Liga login + middleware no app e expõe `auth_enabled` para os templates."""
    global _templates
    _templates = templates
    templates.env.globals["auth_enabled"] = _AuthEnabledFlag()
    app.include_router(router)
    app.middleware("http")(auth_middleware)
