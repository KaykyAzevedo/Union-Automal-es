"""Jobs do scheduler, dos botões de admin e das rotas /cron/* (cada execução fica em `runs`)."""
from __future__ import annotations

import json
import logging
import threading

from .. import db
from ..db import connect, now, to_iso
from . import scraping
from .notify import notify_new_cars, notify_price_drops, notify_sold
from .prices import run_price_check
from .sync import run_poll

log = logging.getLogger(__name__)
_lock = threading.Lock()  # poll e price-check nunca rodam ao mesmo tempo (no processo)
last_error: dict[str, str] = {}  # motivo da última falha por tipo de job (rotas /cron/*)
busy: dict[str, bool] = {}  # True se a última tentativa desistiu por outra instância estar rodando
JOB_LOCK_WAIT = 20.0  # s (maxDuration da função é 60)


def _run(kind: str, fn) -> dict | None:
    """Roda um job registrando em `runs`. Retorna o resultado, ou None se falhou
    (motivo em last_error[kind]; busy[kind] se desistiu por lock ocupado). No
    Postgres, um lock entre instâncias (db.advisory_lock, com prazo) impede duas
    execuções simultâneas (cron duplicado da Vercel, agendador externo)."""
    busy[kind] = False
    with _lock:
        conn = connect()
        try:
            with db.advisory_lock(conn, "union:jobs", wait=JOB_LOCK_WAIT):
                run_id = conn.execute("INSERT INTO runs (kind, started_at) VALUES (?, ?)",
                                      (kind, to_iso(now()))).lastrowid
                conn.commit()
                try:
                    scraped = scraping.scrape_all()
                    scraping.enrich_listed_at(conn, scraped)
                    result = fn(conn, scraped)
                except Exception as exc:
                    conn.rollback()
                    last_error[kind] = f"{type(exc).__name__}: {exc}"
                    conn.execute("UPDATE runs SET finished_at = ?, ok = 0, error = ? WHERE id = ?",
                                 (to_iso(now()), repr(exc), run_id))
                    conn.commit()
                    log.exception("%s falhou", kind)
                    return None
                conn.execute("UPDATE runs SET finished_at = ?, ok = 1, summary = ? WHERE id = ?",
                             (to_iso(now()), json.dumps(result), run_id))
                conn.commit()
                last_error.pop(kind, None)
                log.info("%s ok: %s", kind, result)
                return result
        except db.LockTimeout:
            busy[kind] = True
            last_error[kind] = "outro job está em andamento (lock ocupado); tente de novo"
            log.warning("%s: %s", kind, last_error[kind])
            return None
        finally:
            conn.close()


def _cars(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT id, name, price_cents FROM cars WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id", ids)
        return [{"id": r["id"], "name": r["name"], "price_cents": r["price_cents"]} for r in rows]
    finally:
        conn.close()


def poll_job() -> dict | None:
    result = _run("poll", run_poll)
    if result:
        notify_new_cars(_cars(result["new_car_ids"]))
        notify_sold([c["name"] for c in _cars(result.get("sold_car_ids", []))])
    return result


def price_check_job() -> dict | None:
    result = _run("price_check", run_price_check)
    if result:
        notify_price_drops(result["drops"])
        notify_sold([c["name"] for c in _cars(result.get("sold_car_ids", []))])
    return result
