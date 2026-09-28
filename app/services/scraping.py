"""Obtém anúncios: scraper real (app.scraper, do Lupa) ou mock."""
from __future__ import annotations

import logging
import os
import threading
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


LISTED_AT_BATCH = 15      # HEADs por execução (novos primeiro); os antigos completam em poucas rodadas
LISTED_AT_DEADLINE = 8.0  # s: prazo TOTAL da etapa (a função da Vercel tem 60 s e o poll vem depois)
# job diário: cobre o estoque todo numa rodada (HEAD de 3 s; o prazo total corta o resto)
DAILY_LISTED_AT_BATCH = 60
DAILY_LISTED_AT_DEADLINE = 25.0


def enrich_listed_at(conn, scraped: list, batch: int = LISTED_AT_BATCH,
                     deadline: float = LISTED_AT_DEADLINE) -> int:
    """Preenche ScrapedCar.listed_at (data da foto principal, via HEAD) dos carros novos ou
    ainda sem data no banco (no máx. 1 tentativa por dia por carro), até LISTED_AT_BATCH por
    execução, em sequência (gentil com o CDN/Cloudflare) e com prazo total
    LISTED_AT_DEADLINE: os HEADs rodam numa thread; estourou o prazo, seguimos só com o que
    já chegou. Marca ScrapedCar.listed_at_checked (hoje) nos tentados. Nunca levanta.
    UNION_DISABLE_LISTED_AT=1 desliga (testes). Retorna quantos carros foram escolhidos."""
    if os.environ.get("UNION_DISABLE_LISTED_AT") == "1" or os.environ.get("UNION_MOCK_SCRAPER") == "1":
        return 0
    try:
        import app.scraper.listed_at as listed_at_mod

        from ..db import now

        today = now().date().isoformat()
        known = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT external_id, listed_at, listed_at_checked FROM cars")}
        new = [c for c in scraped if c.external_id not in known and c.photo_url]
        missing = [c for c in scraped if c.external_id in known and c.photo_url
                   and known[c.external_id][0] is None and (known[c.external_id][1] or "") < today]
        targets = (new + missing)[:batch]
        if not targets:
            return 0

        results: dict[str, object] = {}
        stop = threading.Event()

        def worker():
            try:
                for car in targets:
                    if stop.is_set():
                        break
                    results[car.external_id] = listed_at_mod.fetch_listed_at(car.photo_url)
            except Exception:
                log.exception("falha ao obter listed_at")

        thread = threading.Thread(target=worker, name="listed_at", daemon=True)
        thread.start()
        thread.join(deadline)
        stop.set()
        done = dict(results)  # a thread pode seguir no último HEAD; não mexe nos objetos do scrape
        if thread.is_alive():
            log.warning("listed_at: prazo de %.0fs estourou (%d de %d consultados)",
                        deadline, len(done), len(targets))
        for car in targets:
            if car.external_id in done:
                car.listed_at_checked = today
                if done[car.external_id] is not None:
                    car.listed_at = done[car.external_id]
        return len(targets)
    except Exception:  # data de cadastro é enfeite: nunca derruba o job
        log.exception("falha ao obter listed_at")
        return 0
