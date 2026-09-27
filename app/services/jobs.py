"""Jobs do scheduler e dos botões de admin (cada execução fica registrada em `runs`)."""
from __future__ import annotations

import json
import logging
import os
import threading

from ..db import connect, now, to_iso
from . import scraping
from .notify import notify
from .prices import run_price_check
from .sync import run_poll

log = logging.getLogger(__name__)
_lock = threading.Lock()  # poll e price-check nunca rodam ao mesmo tempo
PANEL_URL = os.environ.get("UNION_PANEL_URL", "http://127.0.0.1:8000/")


def _run(kind: str, fn) -> dict | None:
    with _lock:
        conn = connect()
        run_id = conn.execute("INSERT INTO runs (kind, started_at) VALUES (?, ?)", (kind, to_iso(now()))).lastrowid
        conn.commit()
        try:
            result = fn(conn, scraping.scrape_all())
            conn.execute("UPDATE runs SET finished_at = ?, ok = 1, summary = ? WHERE id = ?",
                         (to_iso(now()), json.dumps(result), run_id))
            conn.commit()
            log.info("%s ok: %s", kind, result)
            return result
        except Exception as exc:
            conn.rollback()
            conn.execute("UPDATE runs SET finished_at = ?, ok = 0, error = ? WHERE id = ?",
                         (to_iso(now()), repr(exc), run_id))
            conn.commit()
            log.exception("%s falhou", kind)
            return None
        finally:
            conn.close()


def _car_names(ids: list[int]) -> list[str]:
    conn = connect()
    try:
        return [r[0] for r in conn.execute(
            f"SELECT name FROM cars WHERE id IN ({','.join('?' * len(ids))})", ids)]
    finally:
        conn.close()


def _notify_sold(result: dict) -> None:
    if result.get("sold_car_ids"):
        names = _car_names(result["sold_car_ids"])
        title = "Carro vendido" if len(names) == 1 else f"{len(names)} carros vendidos"
        notify(title, ", ".join(names)[:200], PANEL_URL)


def poll_job() -> dict | None:
    result = _run("poll", run_poll)
    if result and result["new"]:
        names = _car_names(result["new_car_ids"])
        title = "Carro novo no site" if len(names) == 1 else f"{len(names)} carros novos no site"
        notify(title, ", ".join(names)[:200], PANEL_URL)
    if result:
        _notify_sold(result)
    return result


def price_check_job() -> dict | None:
    result = _run("price_check", run_price_check)
    if result and result["drops"]:
        notify("Queda de preço", f"{result['drops']} carro(s) baixaram de preço", PANEL_URL)
    if result:
        _notify_sold(result)
    return result
