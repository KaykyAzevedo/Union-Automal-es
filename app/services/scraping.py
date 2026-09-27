"""Obtém anúncios: scraper real (app.scraper, do Lupa) ou mock."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class MockScrapedCar:
    external_id: str
    name: str
    price_cents: int | None
    photo_url: str | None
    url: str


def mock_scrape_all() -> list[MockScrapedCar]:
    base = "https://www.unionrioveiculos.com.br"
    return [
        MockScrapedCar("mock-1", "Chevrolet Onix 1.0 LT 2020", 6990000, "https://placehold.co/600x400?text=Onix", f"{base}/mock-1"),
        MockScrapedCar("mock-2", "Hyundai HB20 1.6 Comfort 2019", 5990000, "https://placehold.co/600x400?text=HB20", f"{base}/mock-2"),
        MockScrapedCar("mock-3", "Jeep Renegade Longitude 2021", 10990000, "https://placehold.co/600x400?text=Renegade", f"{base}/mock-3"),
    ]


def scrape_all() -> list:
    """app.scraper.scrape_all; mock se UNION_MOCK_SCRAPER=1 ou se o scraper ainda não existir."""
    if os.environ.get("UNION_MOCK_SCRAPER") == "1":
        return mock_scrape_all()
    try:
        from ..scraper import scrape_all as real_scrape_all
    except ImportError:
        log.warning("app.scraper indisponível — usando dados mock")
        return mock_scrape_all()
    return real_scrape_all()
