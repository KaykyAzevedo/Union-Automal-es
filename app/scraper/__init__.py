"""Scraper do estoque da Union Veículos (www.unionrioveiculos.com.br, plataforma AutoCerto).

O estoque inteiro vem renderizado no HTML de /Veiculos (sem API JSON e sem paginação).
"""
from __future__ import annotations

import logging

import httpx

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
    "fetch_listing_html",
    "parse_listing",
    "parse_price_cents",
    "parse_total_count",
    "scrape_all",
]

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
RETRIES = 3


class ScraperError(RuntimeError):
    """Falha ao obter/interpretar o estoque. Quem chama NÃO deve tratar como estoque vazio."""


def _new_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt;q=0.9"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )


def fetch_listing_html(client: httpx.Client, url: str = LISTING_URL) -> str:
    last_exc: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPError as exc:
            last_exc = exc
            log.warning("scraper: tentativa %d/%d falhou em %s: %s", attempt, RETRIES, url, exc)
    raise ScraperError(f"não consegui baixar {url}: {last_exc}") from last_exc


def scrape_all(client: httpx.Client | None = None) -> list[ScrapedCar]:
    """Baixa /Veiculos e devolve todos os carros anunciados.

    Levanta ScraperError em falha de rede ou se a página não parecer a listagem
    (evita que um erro vire "todos os carros sumiram").
    """
    owns_client = client is None
    client = client or _new_client()
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
