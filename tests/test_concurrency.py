"""Concorrência do encarte: muitas requisições de slide ao mesmo tempo (como a prévia do editor).

Rede toda mockada no nível do httpx: o scrape de detalhe devolve a fixture e o CDN de fotos
devolve JPEGs, com latência para forçar sobreposição. Nada de porta/servidor.
"""
from __future__ import annotations

import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
from PIL import Image

from app.encarte import photos as encarte_photos
from app.encarte import render, source
from app.scraper import ScraperError
from app.scraper.detail import parse_detail
from conftest import FIXTURES, make_car

URL = ("https://www.unionrioveiculos.com.br/Veiculo/range-rover-sport-3.0-d350-turbo-diesel-mhev-first-edition-"
       "awd-automatico-diesel-e-eletrico-2023/5509677/detalhes")
DETAIL = parse_detail((FIXTURES / "detail_range_rover_sport_5509677.html").read_text(encoding="utf-8"), URL)


def jpeg(seed: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1440, 1080), (seed % 255, 80, 120)).save(buf, "JPEG", quality=90)
    return buf.getvalue()


class FakeCDN:
    """Transport do CDN de fotos: latência, contagem de concorrência e falhas programadas."""

    def __init__(self, delay=0.05):
        self.delay = delay
        self.lock = threading.Lock()
        self.inflight = self.max_inflight = 0
        self.hits: dict[str, int] = {}
        self.script: dict[str, list[httpx.Response]] = {}  # respostas pré-definidas por URL (consumidas)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        with self.lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            self.hits[url] = self.hits.get(url, 0) + 1
            scripted = self.script.get(url)
            resp = scripted.pop(0) if scripted else None
        try:
            time.sleep(self.delay)
            return resp or httpx.Response(200, content=jpeg(len(url)), headers={"content-type": "image/jpeg"})
        finally:
            with self.lock:
                self.inflight -= 1


@pytest.fixture
def cdn(monkeypatch):
    fake = FakeCDN()
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(encarte_photos.httpx, "Client", client_factory)
    monkeypatch.setattr(encarte_photos, "BACKOFF_BASE", 0.01)
    return fake


@pytest.fixture
def scrape(monkeypatch):
    import app.scraper.detail as detail_mod

    state = {"calls": 0, "error": None, "delay": 0.2}
    lock = threading.Lock()

    def fake(url, client=None):
        with lock:
            state["calls"] += 1
        time.sleep(state["delay"])
        if state["error"]:
            raise state["error"]
        return DETAIL

    monkeypatch.setattr(detail_mod, "scrape_detail", fake)
    return state


@pytest.fixture
def car_id(conn):
    from app.services.sync import run_poll

    run_poll(conn, [replace(make_car("5509677"), url=URL)])
    return conn.execute("SELECT id FROM cars").fetchone()[0]


@pytest.fixture(autouse=True)
def _clean_caches():
    source.clear_cache()
    render._SLIDE_CACHE.clear()
    yield
    source.clear_cache()
    render._SLIDE_CACHE.clear()


def _parallel(client, paths, workers=24, timeout=60):
    with ThreadPoolExecutor(workers) as pool:
        futures = [pool.submit(client.get, p) for p in paths]
        return [f.result(timeout=timeout) for f in futures]


def test_burst_of_slides_no_5xx_single_flight(client, car_id, scrape, cdn):
    """Prévia fria: todos os slides (um por foto) x2 + zip + legenda + editor ao mesmo tempo."""
    n_photos = len(DETAIL.photos)  # Range Rover: 12 (F10: sem teto de 11)
    paths = [f"/encarte/{car_id}/slide/{n}.png" for n in range(n_photos)] * 2
    paths += [f"/encarte/{car_id}.zip", f"/encarte/{car_id}/caption.txt", f"/editor/{car_id}"]
    t0 = time.monotonic()
    responses = _parallel(client, paths)
    assert [r.status_code for r in responses] == [200] * len(paths), [r.text[:200] for r in responses if r.status_code != 200]
    assert scrape["calls"] == 1                       # single-flight do detalhe
    assert set(cdn.hits.values()) == {1}              # single-flight por foto (+ cache em disco)
    assert len(cdn.hits) == n_photos
    assert cdn.max_inflight <= encarte_photos.SITE_CONCURRENCY
    for r in responses[:2 * n_photos]:
        assert Image.open(io.BytesIO(r.content)).size == (1080, 1350)
    assert time.monotonic() - t0 < 30


def test_burst_with_reordered_photos(client, car_id, scrape, cdn):
    q = "?photos=11,3,0,7"
    responses = _parallel(client, [f"/encarte/{car_id}/slide/{n}.png{q}" for n in range(4)] * 4)
    assert all(r.status_code == 200 for r in responses)


def test_transient_cdn_errors_are_retried(client, car_id, scrape, cdn):
    first = DETAIL.photos[0]
    cdn.script[first] = [httpx.Response(503), httpx.Response(429, headers={"retry-after": "0"})]
    responses = _parallel(client, [f"/encarte/{car_id}/slide/{n}.png" for n in range(11)])
    assert all(r.status_code == 200 for r in responses)
    assert cdn.hits[first] == 3


def test_cdn_down_gives_502_not_hang(client, car_id, scrape, cdn):
    first = DETAIL.photos[0]
    cdn.script[first] = [httpx.Response(503)] * encarte_photos.MAX_ATTEMPTS
    responses = _parallel(client, [f"/encarte/{car_id}/slide/0.png"] * 5 + [f"/encarte/{car_id}/slide/1.png"])
    codes = [r.status_code for r in responses]
    assert codes[-1] == 200
    assert all(c in (200, 502) for c in codes[:5]) and 502 in codes[:5]


def test_cloudflare_html_never_cached(client, car_id, scrape, cdn):
    first = DETAIL.photos[0]
    cdn.script[first] = [httpx.Response(200, text="<html>Just a moment</html>", headers={"content-type": "text/html"})]
    assert client.get(f"/encarte/{car_id}/slide/0.png").status_code == 502
    assert client.get(f"/encarte/{car_id}/slide/0.png").status_code == 200


def test_scraper_error_burst_all_502_and_recovers(client, car_id, scrape, cdn):
    scrape["error"] = ScraperError("site fora")
    scrape["delay"] = 0.05
    responses = _parallel(client, [f"/encarte/{car_id}/slide/{n}.png" for n in range(11)])
    assert [r.status_code for r in responses] == [502] * 11
    scrape["error"] = None
    responses = _parallel(client, [f"/encarte/{car_id}/slide/{n}.png" for n in range(11)])
    assert [r.status_code for r in responses] == [200] * 11


def test_panel_keeps_working_during_render_burst(client, car_id, scrape, cdn):
    """Rotas do painel (SQLite) em paralelo com a geração de slides."""
    paths = [f"/encarte/{car_id}/slide/{n}.png" for n in range(11)]
    paths += ["/", "/cars", "/health", "/editor"] * 5
    responses = _parallel(client, paths, workers=32)
    assert all(r.status_code == 200 for r in responses)


def test_many_cars_scraped_concurrently_respect_site_limit(conn, monkeypatch):
    """Detalhes de carros diferentes ao mesmo tempo: no máx. SITE_CONCURRENCY no site."""
    import app.scraper.detail as detail_mod
    from app.models import Car

    inflight = {"now": 0, "max": 0}
    lock = threading.Lock()

    def fake(url, client=None):
        with lock:
            inflight["now"] += 1
            inflight["max"] = max(inflight["max"], inflight["now"])
        time.sleep(0.05)
        with lock:
            inflight["now"] -= 1
        return DETAIL

    monkeypatch.setattr(detail_mod, "scrape_detail", fake)
    cars = [Car(id=i, external_id=str(i), name="x", photo_url=None, price_cents=None, url=URL,
                posted=True, active=True) for i in range(12)]
    with ThreadPoolExecutor(12) as pool:
        results = list(pool.map(source.get_detail, cars, timeout=30))
    assert len(results) == 12
    assert inflight["max"] <= encarte_photos.SITE_CONCURRENCY


def test_parallel_covers_identical_to_sequential():
    """Com o lock só nas chamadas de fonte, capas em paralelo têm de sair iguais às em série."""
    variants = [replace(DETAIL, model=m, version=v, price_cents=p, km=k)
                for m, v, p, k in [("RANGE ROVER SPORT", "3.0 D350 FIRST EDITION", 77_990_000, 14_500),
                                   ("TRACKER", "1.0 TURBO FLEX LTZ AUTOMÁTICO", None, 0),
                                   ("320I", "2.0 16V TURBO FLEX M SPORT AUTOMÁTICO", 18_990_000, 45_000),
                                   ("COMPASS", "2.0 TD350 TURBO DIESEL LONGITUDE AT9", 21_990_000, 61_000)]]
    photo = Image.new("RGB", (1440, 1080), (90, 120, 150))
    expected = [render.render_cover(d, photo).tobytes() for d in variants]
    with ThreadPoolExecutor(16) as pool:
        got = list(pool.map(lambda d: render.render_cover(d, photo).tobytes(), variants * 6, timeout=120))
    assert got == expected * 6
