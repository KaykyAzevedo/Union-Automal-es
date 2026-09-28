"""Scraper do estoque da Union Veículos (www.unionrioveiculos.com.br, plataforma AutoCerto).

O estoque inteiro vem renderizado no HTML de /Veiculos (sem API JSON e sem paginação).
"""
from __future__ import annotations

import httpx

from ._http import ScraperError, fetch, log, new_client
from .listed_at import fetch_listed_at, fill_listed_at
from .parser import (
    BASE_URL,
    LISTING_URL,
    ScrapedCar,
    parse_listing,
    parse_price_cents,
    parse_total_count,
)

__all__ = [
    "BASE_URL",
    "LISTING_URL",
    "ScrapedCar",
    "ScraperError",
    "fetch_listed_at",
    "fetch_listing_html",
    "fill_listed_at",
    "parse_listing",
    "parse_price_cents",
    "parse_total_count",
    "scrape_all",
]


def fetch_listing_html(client: httpx.Client, url: str = LISTING_URL) -> str:
    return fetch(client, url).text


def scrape_all(client: httpx.Client | None = None) -> list[ScrapedCar]:
    """Baixa /Veiculos e devolve todos os carros anunciados.

    Levanta ScraperError em falha de rede ou se a página não parecer a listagem
    (evita que um erro vire "todos os carros sumiram").
    """
    owns_client = client is None
    client = client or new_client()
    try:
        html = fetch_listing_html(client)
    finally:
        if owns_client:
            client.close()

    cars = parse_listing(html)
    total = parse_total_count(html)
    if total is None and not cars:
        raise ScraperError("página de estoque sem carros nem contador — layout mudou?")
    if total is not None and total != len(cars):
        log.warning("scraper: site anuncia %d veículos, extraí %d", total, len(cars))
    return cars
