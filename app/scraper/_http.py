"""HTTP compartilhado pelo scraper de listagem e de detalhe."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("app.scraper")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
RETRIES = 3


class ScraperError(RuntimeError):
    """Falha ao obter/interpretar o estoque. Quem chama NÃO deve tratar como estoque vazio."""


def new_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt;q=0.9"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )


def fetch(client: httpx.Client, url: str) -> httpx.Response:
    """GET com RETRIES tentativas; levanta ScraperError se todas falharem."""
    last_exc: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return resp
        except httpx.HTTPError as exc:
            last_exc = exc
            log.warning("scraper: tentativa %d/%d falhou em %s: %s", attempt, RETRIES, url, exc)
    raise ScraperError(f"não consegui baixar {url}: {last_exc}") from last_exc
