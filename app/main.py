"""App FastAPI: painel da social media da Union Veículos."""
from __future__ import annotations

import hmac
import io
import logging
import os
import re
import unicodedata
import zipfile
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .services import queries
from .services.formatting import brl, dt
from .services import aging, customers, finance, jobs, users
from .services.jobs import daily_job, poll_job, price_check_job
from .encarte import source as encarte_source
from .encarte.caption import build_caption
from .encarte.render import slide_png

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("union")

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.filters["brl"] = brl
templates.env.filters["dt"] = dt

FLASH = {
    "checked": "Verificação concluída: carros novos, preços e vendidos.",
    "price_checked": "Revisão de preços concluída.",
    "check_failed": "Falha ao consultar o site — veja o log.",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if db.is_vercel() and not db.database_url():
        log.error("VERCEL sem DATABASE_URL/POSTGRES_URL: usando SQLite em /tmp (dados se perdem a cada instância!)")
    db.init_db()
    scheduler = None
    # Na Vercel não há processo contínuo: jobs vêm de /cron/* (Vercel Cron + agendador externo)
    if os.environ.get("UNION_DISABLE_SCHEDULER") != "1" and not db.is_vercel():
        from .scheduler import create_scheduler

        scheduler = create_scheduler()
        scheduler.start()
        for job in scheduler.get_jobs():
            log.info("job registrado: %s (%s) próxima: %s", job.id, job.trigger, job.next_run_time)
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


app = FastAPI(title="Union Veículos — Painel", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static"), check_dir=False), name="static")

from . import auth  # noqa: E402
from .auth import install_auth, require_role  # noqa: E402  (perfis admin/staff, sessão, globals Jinja)

install_auth(app, templates)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, msg: str | None = None, conn=Depends(db.get_conn)):
    return templates.TemplateResponse(request, "dashboard.html", {
        "tickets": queries.pending_tickets(conn),
        "alerts": queries.open_alerts(conn),
        "sold": queries.open_sold(conn),
        "stats": queries.stats(conn),
        "flash": FLASH.get(msg) if msg else None,
    })


@app.get("/equipe", response_class=HTMLResponse,
         dependencies=[Depends(require_role(auth.ADMIN, auth.STAFF))])
def equipe(request: Request, conn=Depends(db.get_conn)):
    """Painel da Equipe (admin e staff): tempo em estoque e sugestões de redução. Só leitura."""
    return templates.TemplateResponse(request, "equipe.html", _with_birthdays(conn, aging.team_view(conn)))


def _with_birthdays(conn, ctx: dict) -> dict:
    """F16: `birthdays_today` (int) para o selo da aba Clientes na área da equipe."""
    ctx["birthdays_today"] = customers.birthdays_today(conn, db.now().date())
    return ctx


# ---- F13: vendas e financeiro (admin e equipe; tudo sob /equipe/*) ----------------

TEAM = [Depends(require_role(auth.ADMIN, auth.STAFF))]
FINANCE_MSGS = {"saved", "invalid"}


@app.get("/equipe/vendas", response_class=HTMLResponse, dependencies=TEAM)
def equipe_vendas(request: Request, mes: str | None = None, conn=Depends(db.get_conn)):
    """Vendas do mês (?mes=YYYY-MM, default mês atual): faturamento, gasto, lucro e marcas."""
    return templates.TemplateResponse(request, "vendas.html", _with_birthdays(conn, finance.sales_view(conn, mes)))


@app.get("/equipe/carro/{car_id}", response_class=HTMLResponse, dependencies=TEAM)
def equipe_carro(request: Request, car_id: int, msg: str | None = None, conn=Depends(db.get_conn)):
    ctx = finance.car_view(conn, car_id)
    if ctx is None:
        raise HTTPException(404, "Carro não encontrado")
    ctx["msg"] = msg if msg in FINANCE_MSGS else None
    return templates.TemplateResponse(request, "carro_financeiro.html", _with_birthdays(conn, ctx))


def _finance_saved(conn, car_id: int, save) -> RedirectResponse:
    """Aplica `save(conn, car_id)` e volta para a página do carro com ?msg=saved|invalid."""
    _car_or_404(conn, car_id)
    return RedirectResponse(f"/equipe/carro/{car_id}?msg={'saved' if save(conn, car_id) else 'invalid'}",
                            status_code=303)


@app.post("/equipe/carro/{car_id}/custo", dependencies=TEAM)
def equipe_carro_custo(car_id: int, cost: str = Form(""), conn=Depends(db.get_conn)):
    return _finance_saved(conn, car_id, lambda c, i: finance.set_cost(c, i, cost))


@app.post("/equipe/carro/{car_id}/venda", dependencies=TEAM)
def equipe_carro_venda(car_id: int, sale_price: str = Form(""), conn=Depends(db.get_conn)):
    return _finance_saved(conn, car_id, lambda c, i: finance.set_sale_price(c, i, sale_price))


@app.post("/equipe/carro/{car_id}/nao-venda", dependencies=TEAM)
def equipe_carro_nao_venda(car_id: int, value: str = Form(""), conn=Depends(db.get_conn)):
    """F14: value='1' marca "não foi venda" (anúncio retirado); '0' desfaz. Só carro que saiu do site."""
    return _finance_saved(conn, car_id, lambda c, i: finance.set_not_sale(c, i, value))


@app.post("/equipe/carro/{car_id}/despesas", dependencies=TEAM)
def equipe_carro_despesa(car_id: int, description: str = Form(""), amount: str = Form(""),
                         conn=Depends(db.get_conn)):
    return _finance_saved(conn, car_id, lambda c, i: finance.add_expense(c, i, description, amount))


@app.post("/equipe/carro/{car_id}/despesas/{expense_id}/excluir", dependencies=TEAM)
def equipe_carro_despesa_excluir(car_id: int, expense_id: int, conn=Depends(db.get_conn)):
    return _finance_saved(conn, car_id, lambda c, i: finance.delete_expense(c, i, expense_id))


# ---- F16: clientes e aniversários (admin e equipe; tudo sob /equipe/clientes*) ------
# Regras em app/services/customers.py. Dados de cliente são pessoais: NUNCA em log, em URL
# (por isso o erro do formulário é re-renderizado, sem redirect) nem em mensagem de erro.

CUSTOMER_LIST_MSGS = {"saved", "deleted", "invalid"}
CUSTOMER_FORM_MSGS = {"invalid", "duplicate"}
CUSTOMER_FIELDS = ("name", "phone", "birthday", "birth_day", "birth_month", "birth_year", "email", "car_text",
                   "purchase_date", "notes")
MAX_SEARCH = 80
NO_STORE = {"Cache-Control": "no-store"}


def _actor(request: Request) -> tuple[int | None, str, str | None]:
    """(users.id, nome para "cadastrado por"/"enviado por", nome que assina a mensagem) de quem está logado."""
    user = auth.current_user(request)
    if user is not None:
        return user["id"], user["name"], user["name"]
    return None, "Administrador" if auth.current_role(request) == auth.ADMIN else "Equipe", None


def _customer_or_404(conn, customer_id: int) -> dict:
    customer = customers.get_customer(conn, customer_id) if finance.valid_id(customer_id) else None
    if customer is None:
        raise HTTPException(404, "Cliente não encontrado")
    return customer


def _customer_form(request: Request, conn, customer: dict | None, msg: str | None = None,
                   form: dict | None = None, status: int = 200):
    """`customer` = cadastro gravado (None no novo). POST recusado: `form` traz o texto digitado, e
    `customer` vira esse mesmo texto nas chaves do cadastro (customers.draft), para a tela não perder nada."""
    return templates.TemplateResponse(request, "cliente_form.html", {
        "customer": customer if form is None else customers.draft(form, customer["id"] if customer else None),
        "form": form,
        "msg": msg if msg in CUSTOMER_FORM_MSGS else None,
        "can_delete": customer is not None and customers.can_delete(
            customer, auth.current_role(request), auth.current_user(request)),
        "birthdays_today": customers.birthdays_today(conn, db.now().date()),
    }, status_code=status, headers=NO_STORE)


async def _customer_fields(request: Request) -> dict:
    data = await request.form()
    return {field: value if isinstance(value := data.get(field, ""), str) else "" for field in CUSTOMER_FIELDS}


@app.get("/equipe/clientes", response_class=HTMLResponse, dependencies=TEAM)
def equipe_clientes(request: Request, q: str | None = None, msg: str | None = None, conn=Depends(db.get_conn)):
    """Aniversariantes (hoje, próximos 7 dias, resto do mês) e a lista de clientes, com busca ?q=."""
    q = (q or "").strip()[:MAX_SEARCH] or None
    today = db.now().date()
    view = customers.customers_view(conn, today, q, _actor(request)[2])
    return templates.TemplateResponse(request, "clientes.html", {
        "view": view,
        "q": q or "",
        "msg": msg if msg in CUSTOMER_LIST_MSGS else None,
        "birthdays_today": customers.birthdays_today(conn, today),
    }, headers=NO_STORE)


@app.get("/equipe/clientes/novo", response_class=HTMLResponse, dependencies=TEAM)
def equipe_cliente_novo(request: Request, conn=Depends(db.get_conn)):
    return _customer_form(request, conn, None)


@app.get("/equipe/clientes/{customer_id}", response_class=HTMLResponse, dependencies=TEAM)
def equipe_cliente(request: Request, customer_id: int, conn=Depends(db.get_conn)):
    return _customer_form(request, conn, _customer_or_404(conn, customer_id))


@app.post("/equipe/clientes", dependencies=TEAM)
async def equipe_cliente_criar(request: Request, conn=Depends(db.get_conn)):
    form = await _customer_fields(request)
    created_by, created_by_name, _ = _actor(request)
    result = customers.create_customer(conn, form, created_by, created_by_name)
    if result == "saved":
        return RedirectResponse("/equipe/clientes?msg=saved", status_code=303)
    return _customer_form(request, conn, None, "duplicate" if result == "duplicate" else "invalid", form, 422)


@app.post("/equipe/clientes/{customer_id}", dependencies=TEAM)
async def equipe_cliente_editar(request: Request, customer_id: int, conn=Depends(db.get_conn)):
    customer = _customer_or_404(conn, customer_id)
    form = await _customer_fields(request)
    result = customers.update_customer(conn, customer_id, form)
    if result == "saved":
        return RedirectResponse("/equipe/clientes?msg=saved", status_code=303)
    return _customer_form(request, conn, customer, "duplicate" if result == "duplicate" else "invalid", form, 422)


@app.post("/equipe/clientes/{customer_id}/parabens", dependencies=TEAM)
def equipe_cliente_parabens(request: Request, customer_id: int, value: str = Form(""), conn=Depends(db.get_conn)):
    """value='1' registra "parabéns enviado" neste aniversário (com o nome de quem enviou); '0' desfaz.
    Só dá para marcar no dia do aniversário (página aberta ontem e clicada hoje → msg=invalid, nada gravado)."""
    _customer_or_404(conn, customer_id)
    if value not in ("0", "1"):
        return RedirectResponse("/equipe/clientes?msg=invalid", status_code=303)
    ok = customers.mark_greeted(conn, customer_id, _actor(request)[1], db.now().date(), value == "1")
    return RedirectResponse(f"/equipe/clientes?msg={'saved' if ok else 'invalid'}", status_code=303)


@app.post("/equipe/clientes/{customer_id}/excluir", dependencies=TEAM)
def equipe_cliente_excluir(request: Request, customer_id: int, conn=Depends(db.get_conn)):
    """Só o admin ou quem cadastrou o cliente pode excluir."""
    customer = _customer_or_404(conn, customer_id)
    if not customers.can_delete(customer, auth.current_role(request), auth.current_user(request)):
        raise HTTPException(403, "Só o administrador ou quem cadastrou pode excluir")
    customers.delete_customer(conn, customer_id)
    return RedirectResponse("/equipe/clientes?msg=deleted", status_code=303)


# ---- F15: login individual por funcionário ----------------------------------------
# /login e /logout, sessão, must_change e o ctx `current_user` ficam em app/auth.py.
# Senhas: app/services/users.py. Aqui só as telas. NUNCA registrar senha em log nem em URL.

PASSWORD_MSGS = {"saved", "wrong", "weak", "same"}
USER_MSGS = {"saved", "invalid", "exists"}
ADMIN_ONLY = [Depends(require_role(auth.ADMIN))]


@app.get("/equipe/senha", response_class=HTMLResponse, dependencies=TEAM)
def equipe_senha(request: Request, msg: str | None = None):
    """Troca de senha do funcionário logado. Admin e senha única não têm senha própria: voltam ao início."""
    user = auth.current_user(request)
    if user is None:
        return RedirectResponse(auth.home_for(auth.current_role(request)), status_code=303)
    return templates.TemplateResponse(request, "senha.html", {
        "user": {"name": user["name"], "username": user["username"], "roles_label": user["roles_label"]},
        "forced": bool(user["must_change"]),
        "msg": msg if msg in PASSWORD_MSGS else None,
    }, headers={"Cache-Control": "no-store"})


@app.post("/equipe/senha", dependencies=TEAM)
def equipe_senha_salvar(request: Request, current: str = Form(""), new: str = Form(""), confirm: str = Form(""),
                        conn=Depends(db.get_conn)):
    user = auth.current_user(request)
    if user is None:
        return RedirectResponse(auth.home_for(auth.current_role(request)), status_code=303)
    result = "weak" if new != confirm else users.change_password(conn, user["id"], current, new)
    if result != "saved":
        return RedirectResponse(f"/equipe/senha?msg={result if result in PASSWORD_MSGS else 'weak'}", status_code=303)
    # a troca sobe o pw_version: o cookie antigo morreu, este entra no lugar
    return auth.login_response(request, users.get_user(conn, user["id"]), "/equipe")


def _users_page(request: Request, conn, msg: str | None = None, new_password: dict | None = None):
    return templates.TemplateResponse(request, "usuarios.html", {
        "users": users.list_users(conn),
        "new_password": new_password,
        "msg": msg if msg in USER_MSGS else None,
        "shared_password_active": auth.shared_password_active(conn),
        "role_options": users.role_options(),
    }, headers={"Cache-Control": "no-store"})


@app.get("/admin/usuarios", response_class=HTMLResponse, dependencies=ADMIN_ONLY)
def admin_usuarios(request: Request, msg: str | None = None, conn=Depends(db.get_conn)):
    return _users_page(request, conn, msg)


@app.post("/admin/usuarios", dependencies=ADMIN_ONLY)
def admin_usuarios_criar(name: str = Form(""), username: str = Form(""), roles: list[str] = Form([]),
                         conn=Depends(db.get_conn)):
    result = users.create_user(conn, username, name, roles)
    return RedirectResponse(f"/admin/usuarios?msg={result if result in USER_MSGS else 'invalid'}", status_code=303)


@app.post("/admin/usuarios/{user_id}/senha", response_class=HTMLResponse, dependencies=ADMIN_ONLY)
def admin_usuarios_senha(request: Request, user_id: int, conn=Depends(db.get_conn)):
    """Gera a senha provisória e mostra UMA vez, na própria resposta (200, no-store).
    Nada de redirect: a senha não pode passar por URL, cookie, log nem ficar em texto no banco."""
    password = users.reset_password(conn, user_id) if finance.valid_id(user_id) else None
    if password is None:
        raise HTTPException(404, "Funcionário não encontrado")
    user = users.get_user(conn, user_id)
    return _users_page(request, conn, new_password={
        "name": user["name"], "username": user["username"], "password": password})


@app.post("/admin/usuarios/{user_id}/ativo", dependencies=ADMIN_ONLY)
def admin_usuarios_ativo(user_id: int, value: str = Form(""), conn=Depends(db.get_conn)):
    """value='1' reativa, '0' desativa (derruba a sessão do funcionário na hora)."""
    if value not in ("0", "1"):
        return RedirectResponse("/admin/usuarios?msg=invalid", status_code=303)
    if not finance.valid_id(user_id) or not users.set_active(conn, user_id, value == "1"):
        raise HTTPException(404, "Funcionário não encontrado")
    return RedirectResponse("/admin/usuarios?msg=saved", status_code=303)


@app.get("/cars", response_class=HTMLResponse)
def cars(request: Request, conn=Depends(db.get_conn)):
    return templates.TemplateResponse(request, "cars.html", {"cars": queries.all_cars(conn)})


@app.post("/tickets/{ticket_id}/done", response_class=HTMLResponse)
def ticket_done(request: Request, ticket_id: int, conn=Depends(db.get_conn)):
    ticket = queries.mark_ticket_done(conn, ticket_id)
    if ticket is None:
        raise HTTPException(404, "Chamado não encontrado")
    return templates.TemplateResponse(request, "partials/ticket_done.html", {"ticket": ticket})


@app.post("/alerts/{alert_id}/dismiss", response_class=HTMLResponse)
def alert_dismiss(alert_id: int, conn=Depends(db.get_conn)):
    if not queries.dismiss_alert(conn, alert_id):
        raise HTTPException(404, "Alerta não encontrado")
    return HTMLResponse("")


@app.post("/sold/{sold_id}/dismiss", response_class=HTMLResponse)
def sold_dismiss(sold_id: int, conn=Depends(db.get_conn)):
    if not queries.dismiss_sold(conn, sold_id):
        raise HTTPException(404, "Aviso de vendido não encontrado")
    return HTMLResponse("")


@app.post("/admin/check-now")
def check_now():
    msg = "checked" if daily_job() is not None else "check_failed"
    return RedirectResponse(f"/?msg={msg}", status_code=303)


@app.post("/admin/price-check-now")
def price_check_now():
    # compatibilidade: o botão antigo "Verificar preços" também roda a verificação completa
    msg = "checked" if daily_job() is not None else "check_failed"
    return RedirectResponse(f"/?msg={msg}", status_code=303)


@app.get("/health")
def health(request: Request, conn=Depends(db.get_conn)):
    scheduler = request.app.state.scheduler
    jobs = [] if scheduler is None else [
        {"id": j.id, "name": j.name, "next_run": j.next_run_time.isoformat() if j.next_run_time else None}
        for j in scheduler.get_jobs()
    ]
    stats = queries.stats(conn)
    for key in ("last_check", "next_check"):
        stats[key] = stats[key].isoformat() if stats[key] else None
    return {"ok": True, "jobs": jobs, "stats": stats}


@app.get("/api/cars/{car_id}/prices")
def car_prices(car_id: int, conn=Depends(db.get_conn)):
    return [{"price_cents": p["price_cents"], "recorded_at": p["recorded_at"].isoformat()}
            for p in queries.price_history(conn, car_id)]


# ---- F4: editor de encarte ----------------------------------------------------

# Sem limite de fotos: por padrão o post usa TODAS as fotos do anúncio, na ordem do site
# (1ª = capa). MAX_SLIDES é só um teto de segurança contra pedidos absurdos.
MAX_SLIDES = 60


def _car_or_404(conn, car_id: int):
    car = queries.get_car(conn, car_id) if finance.valid_id(car_id) else None
    if car is None:
        raise HTTPException(404, "Carro não encontrado")
    return car


def _detail_or_502(car, conn):
    from .scraper import ScraperError

    try:
        return encarte_source.get_detail(car, conn)
    except ScraperError as exc:
        log.warning("502 encarte car=%s: scrape_detail falhou: %s", car.id, exc)
        raise HTTPException(502, f"Falha ao ler o anúncio: {exc}") from exc


def _chosen_urls(detail, photos: str | None) -> list[str]:
    """`photos` = índices em detail.photos na ordem escolhida, sem repetição
    (default: todas as fotos, na ordem do site)."""
    if not detail.photos:
        raise HTTPException(422, "Anúncio sem fotos")
    if photos is None or not photos.strip():
        return detail.photos[:MAX_SLIDES]
    try:
        idx = [int(p) for p in photos.split(",") if p.strip()]
    except ValueError as exc:
        raise HTTPException(400, "photos deve ser uma lista de índices, ex.: 0,3,4") from exc
    idx = list(dict.fromkeys(idx))
    if not idx or any(i < 0 or i >= len(detail.photos) for i in idx) or len(idx) > MAX_SLIDES:
        raise HTTPException(400, f"índices de foto inválidos (0..{len(detail.photos) - 1}, máx. {MAX_SLIDES})")
    return [detail.photos[i] for i in idx]


MAX_MANUAL_PRICE = 100_000_000  # R$
_PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d+)(?:,0{1,2})?")


def parse_manual_price(price: str | None) -> int | None:
    """`price` em reais inteiros ('125900', '125.900', '125.900,00') → centavos; vazio → None."""
    if price is None or not price.strip():
        return None
    m = _PRICE_RE.fullmatch(price.strip())
    reais = int(m.group(1).replace(".", "")) if m else 0
    if not 0 < reais <= MAX_MANUAL_PRICE:
        raise HTTPException(400, f"price inválido: use reais inteiros entre 1 e {MAX_MANUAL_PRICE}, ex.: 125900")
    return reais * 100


def _with_price(detail, price: str | None):
    """Cópia do detalhe com o preço manual (só para renderizar/legenda; banco e cache intactos)."""
    cents = parse_manual_price(price)
    return detail if cents is None else replace(detail, price_cents=cents)


def _png(detail, url: str, cover: bool) -> bytes:
    import httpx

    try:
        return slide_png(detail, url, cover)
    except (httpx.HTTPError, OSError) as exc:
        log.warning("502 encarte foto %s (capa=%s): %s: %s", url, cover, type(exc).__name__, exc)
        raise HTTPException(502, f"Falha ao baixar/abrir a foto: {exc}") from exc


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def zip_filename(detail) -> str:
    parts = [detail.brand, detail.model, str(detail.year_model or detail.year_fab or "")]
    return (_slug("-".join(p for p in parts if p)) or "encarte") + ".zip"


@app.get("/editor", response_class=HTMLResponse)
def editor(request: Request, conn=Depends(db.get_conn)):
    return templates.TemplateResponse(request, "editor.html", {"cars": queries.active_cars(conn), "selected": None})


@app.get("/editor/{car_id}", response_class=HTMLResponse)
def editor_car(request: Request, car_id: int, conn=Depends(db.get_conn)):
    from .scraper import ScraperError

    car = _car_or_404(conn, car_id)
    ctx = {"cars": queries.active_cars(conn), "selected": None, "car": car}
    try:
        detail = encarte_source.get_detail(car, conn)
    except ScraperError as exc:
        log.warning("editor car=%s: scrape_detail falhou: %s", car.id, exc)
        ctx["error"] = f"Não foi possível ler o anúncio no site: {exc}"
    else:
        ctx["selected"] = {
            "car": car,
            "detail": detail,
            "photos": [{"index": i, "url": u} for i, u in enumerate(detail.photos)],
            "caption": build_caption(detail),
        }
    return templates.TemplateResponse(request, "editor.html", ctx)


@app.get("/encarte/{car_id}/slide/{n}.png")
def encarte_slide(car_id: int, n: int, photos: str | None = None, price: str | None = None,
                  conn=Depends(db.get_conn)):
    parse_manual_price(price)  # 400 antes de raspar
    detail = _with_price(_detail_or_502(_car_or_404(conn, car_id), conn), price)
    urls = _chosen_urls(detail, photos)
    if not 0 <= n < len(urls):
        raise HTTPException(404, "Slide inexistente")
    return Response(_png(detail, urls[n], n == 0), media_type="image/png",
                    headers={"Cache-Control": "private, max-age=600"})


@app.get("/encarte/{car_id:int}.zip")
def encarte_zip(car_id: int, photos: str | None = None, price: str | None = None, conn=Depends(db.get_conn)):
    parse_manual_price(price)
    detail = _with_price(_detail_or_502(_car_or_404(conn, car_id), conn), price)
    urls = _chosen_urls(detail, photos)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:  # PNG já é comprimido
        for i, url in enumerate(urls):
            zf.writestr(f"{i + 1:02d}.png", _png(detail, url, i == 0))
        zf.writestr("legenda.txt", build_caption(detail).encode("utf-8"))
    name = zip_filename(detail)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/encarte/{car_id}/caption.txt", response_class=PlainTextResponse)
def encarte_caption(car_id: int, price: str | None = None, conn=Depends(db.get_conn)):
    parse_manual_price(price)
    detail = _with_price(_detail_or_502(_car_or_404(conn, car_id), conn), price)
    return PlainTextResponse(build_caption(detail), media_type="text/plain; charset=utf-8")


# ---- F5: jobs via HTTP (Vercel Cron / agendador externo) -------------------------

def _cron(request: Request, kind: str, job) -> JSONResponse:
    secret = os.environ.get("CRON_SECRET")
    if not secret:
        return JSONResponse({"ok": False, "job": kind, "error": "CRON_SECRET não configurado"}, status_code=503)
    given = request.headers.get("authorization", "")
    if not hmac.compare_digest(given.encode(), f"Bearer {secret}".encode()):
        return JSONResponse({"ok": False, "job": kind, "error": "não autorizado"}, status_code=401)
    result = job()
    if result is None and jobs.busy.get(kind):
        return JSONResponse({"ok": False, "job": kind, "error": jobs.last_error.get(kind)}, status_code=409)
    if result is None:
        error = jobs.last_error.get(kind, "falha desconhecida")
        log.warning("cron %s falhou: %s", kind, error)
        return JSONResponse({"ok": False, "job": kind, "error": error}, status_code=502)
    return JSONResponse({"ok": True, "job": kind, "result": result})


@app.get("/cron/poll")
def cron_poll(request: Request):
    """Só carros novos/vendidos (compatibilidade; o agendamento usa /cron/daily)."""
    return _cron(request, "poll", poll_job)


@app.get("/cron/price-check")
def cron_price_check(request: Request):
    """Só revisão de preços (compatibilidade; o agendamento usa /cron/daily)."""
    return _cron(request, "price_check", price_check_job)


@app.get("/cron/daily")
def cron_daily(request: Request):
    """Verificação diária completa (novos, preços, vendidos). Idempotente.
    Vercel Cron "0 21 * * *" (18:00 BRT; no Hobby dispara entre 18:00 e 18:59)."""
    return _cron(request, "daily", daily_job)
