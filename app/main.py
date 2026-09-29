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
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .services import queries
from .services.formatting import brl, dt
from .services import aging, jobs
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
    return templates.TemplateResponse(request, "equipe.html", aging.team_view(conn))


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
    car = queries.get_car(conn, car_id)
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
def encarte_slide(car_id: int, n: int, photos: str | None = None, conn=Depends(db.get_conn)):
    detail = _detail_or_502(_car_or_404(conn, car_id), conn)
    urls = _chosen_urls(detail, photos)
    if not 0 <= n < len(urls):
        raise HTTPException(404, "Slide inexistente")
    return Response(_png(detail, urls[n], n == 0), media_type="image/png",
                    headers={"Cache-Control": "private, max-age=600"})


@app.get("/encarte/{car_id:int}.zip")
def encarte_zip(car_id: int, photos: str | None = None, conn=Depends(db.get_conn)):
    detail = _detail_or_502(_car_or_404(conn, car_id), conn)
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
def encarte_caption(car_id: int, conn=Depends(db.get_conn)):
    return PlainTextResponse(build_caption(_detail_or_502(_car_or_404(conn, car_id), conn)),
                             media_type="text/plain; charset=utf-8")


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
