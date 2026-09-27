"""App FastAPI: painel da social media da Union Veículos."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .services import queries
from .services.formatting import brl, dt
from .services.jobs import poll_job, price_check_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("union")

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.filters["brl"] = brl
templates.env.filters["dt"] = dt

FLASH = {
    "checked": "Verificação de carros novos concluída.",
    "price_checked": "Revisão de preços concluída.",
    "check_failed": "Falha ao consultar o site — veja o log.",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler = None
    if os.environ.get("UNION_DISABLE_SCHEDULER") != "1":
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


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, msg: str | None = None, conn=Depends(db.get_conn)):
    return templates.TemplateResponse(request, "dashboard.html", {
        "tickets": queries.pending_tickets(conn),
        "alerts": queries.open_alerts(conn),
        "sold": queries.open_sold(conn),
        "stats": queries.stats(conn),
        "flash": FLASH.get(msg) if msg else None,
    })


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
    msg = "checked" if poll_job() is not None else "check_failed"
    return RedirectResponse(f"/?msg={msg}", status_code=303)


@app.post("/admin/price-check-now")
def price_check_now():
    msg = "price_checked" if price_check_job() is not None else "check_failed"
    return RedirectResponse(f"/?msg={msg}", status_code=303)


@app.get("/health")
def health(request: Request, conn=Depends(db.get_conn)):
    scheduler = request.app.state.scheduler
    jobs = [] if scheduler is None else [
        {"id": j.id, "name": j.name, "next_run": j.next_run_time.isoformat() if j.next_run_time else None}
        for j in scheduler.get_jobs()
    ]
    stats = queries.stats(conn)
    stats["last_check"] = stats["last_check"].isoformat() if stats["last_check"] else None
    return {"ok": True, "jobs": jobs, "stats": stats}


@app.get("/api/cars/{car_id}/prices")
def car_prices(car_id: int, conn=Depends(db.get_conn)):
    return [{"price_cents": p["price_cents"], "recorded_at": p["recorded_at"].isoformat()}
            for p in queries.price_history(conn, car_id)]
