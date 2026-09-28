"""Detalhe do anúncio para o encarte, com cache em memória + banco (~10 min).

Usa app.scraper.detail.scrape_detail (Lupa). FakeCarDetail/TRACKER_SAMPLE
servem só para amostras e testes offline.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field, fields

from .. import db
from ..models import Car
from .photos import SITE_SEMAPHORE, key_lock

log = logging.getLogger(__name__)
TTL_SECONDS = 600
DETAIL_LOCK_WAIT = 8.0
_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()
_car_locks: dict[str, threading.Lock] = {}


@dataclass
class FakeCarDetail:
    """Espelha o contrato CarDetail do Lupa."""
    external_id: str
    url: str
    brand: str
    model: str
    version: str
    name_no_year: str
    year_fab: int | None
    year_model: int | None
    km: int | None
    price_cents: int | None
    photos: list[str] = field(default_factory=list)
    fuel: str | None = None
    transmission: str | None = None
    color: str | None = None
    options: list[str] = field(default_factory=list)


TRACKER_SAMPLE = FakeCarDetail(
    external_id="5575766",
    url="https://www.unionrioveiculos.com.br/Veiculo/tracker-1.0-turbo-flex-ltz-automatico-flex-2026/5575766/detalhes",
    brand="CHEVROLET", model="TRACKER", version="1.0 TURBO FLEX LTZ AUTOMÁTICO",
    name_no_year="CHEVROLET TRACKER 1.0 TURBO FLEX LTZ AUTOMÁTICO",
    year_fab=2025, year_model=2026, km=5500, price_cents=12990000,
    photos=[f"https://www.autocerto.com/fotos/339/5575766/{p}.jpg" for p in (
        "1_042346", "1_042347", "1_042349", "1_042350", "2_042350", "1_042351",
        "1_042352", "1_042353", "1_042354", "2_042354", "1_042355")],
    fuel="Flex", transmission="Automático", color="Vermelho",
)


def _scrape(car: Car):
    from ..scraper.detail import scrape_detail

    with SITE_SEMAPHORE:
        return scrape_detail(car.url)


def _cached(key: str):
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < TTL_SECONDS:
            return hit[1]
    return None


def _decode(data: str):
    try:
        from ..scraper.detail import CarDetail as cls
    except ImportError:  # pragma: no cover
        cls = FakeCarDetail
    raw = json.loads(data)
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in names})


def _db_get(conn, key: str):
    row = conn.execute("SELECT data, fetched_at FROM detail_cache WHERE external_id = ?", (key,)).fetchone()
    if row is None:
        return None
    fetched = db.from_iso(row["fetched_at"])
    if fetched is None or (db.now() - fetched).total_seconds() >= TTL_SECONDS:
        return None
    try:
        return _decode(row["data"])
    except (ValueError, TypeError):  # linha corrompida/formato antigo: trata como ausente
        log.warning("detail_cache inválido para %s; raspando de novo", key)
        return None


def _db_put(conn, key: str, detail) -> None:
    conn.execute(
        """INSERT INTO detail_cache (external_id, data, fetched_at) VALUES (?, ?, ?)
           ON CONFLICT (external_id) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at""",
        (key, json.dumps(asdict(detail), ensure_ascii=False), db.to_iso(db.now())),
    )
    conn.commit()


def get_detail(car: Car, conn=None):
    """CarDetail do carro. Camadas: memória → tabela detail_cache (TTL_SECONDS,
    compartilhada entre instâncias da Vercel) → scrape. Single-flight por carro no
    processo (lock) e entre instâncias (pg_advisory_lock no Postgres): 11 slides
    pedidos juntos geram UM scrape. Propaga ScraperError."""
    key = car.external_id
    detail = _cached(key)
    if detail is not None:
        return detail
    with key_lock(_car_locks, _lock, key):
        detail = _cached(key)
        if detail is not None:
            return detail
        own = conn is None
        conn = conn or db.connect()
        try:
            # espera no máx. DETAIL_LOCK_WAIT; lock vazado → segue sem lock (no pior caso, 1 scrape a mais)
            with db.advisory_lock(conn, f"union:detail:{key}", wait=DETAIL_LOCK_WAIT, required=False):
                detail = _db_get(conn, key)
                if detail is None:
                    detail = _scrape(car)
                    _db_put(conn, key, detail)
        finally:
            if own:
                conn.close()
        with _lock:
            _cache[key] = (time.monotonic(), detail)
        return detail


def clear_cache(conn=None) -> None:
    """Limpa a memória; com `conn`, também a tabela detail_cache."""
    with _lock:
        _cache.clear()
    if conn is not None:
        conn.execute("DELETE FROM detail_cache")
        conn.commit()
