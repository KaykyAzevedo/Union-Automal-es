"""Detalhe do anúncio para o encarte, com cache em memória (~10 min).

Usa app.scraper.detail.scrape_detail (Lupa). FakeCarDetail/TRACKER_SAMPLE
servem só para amostras e testes offline.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from ..models import Car
from .photos import SITE_SEMAPHORE, key_lock

TTL_SECONDS = 600
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


def get_detail(car: Car):
    """CarDetail do carro, cacheado por TTL_SECONDS e single-flight por carro:
    11 slides pedidos juntos geram UM scrape. Propaga ScraperError."""
    key = car.external_id
    detail = _cached(key)
    if detail is not None:
        return detail
    with key_lock(_car_locks, _lock, key):
        detail = _cached(key)
        if detail is None:
            detail = _scrape(car)
            with _lock:
                _cache[key] = (time.monotonic(), detail)
        return detail


def clear_cache() -> None:
    with _lock:
        _cache.clear()
