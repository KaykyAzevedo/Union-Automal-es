"""F6 — enrich_listed_at no poll: HEAD das fotos (mockado), limite por execução e orçamento de tempo."""
from __future__ import annotations

import time
from datetime import date

import pytest

from app.services import scraping
from app.services.sync import run_poll


@pytest.fixture
def heads(monkeypatch):
    import app.scraper.listed_at as la

    state = {"urls": [], "delay": 0.0, "value": date(2026, 9, 1)}

    def fake(photo_url, client=None):
        state["urls"].append(photo_url)
        time.sleep(state["delay"])
        return state["value"] if photo_url else None

    monkeypatch.setattr(la, "fetch_listed_at", fake)
    monkeypatch.delenv("UNION_DISABLE_LISTED_AT")
    return state


def test_only_new_or_missing_and_batch_limit(conn, car, heads):
    stock = [car(str(i)) for i in range(40)] + [car("sem-foto", photo=None)]
    n = scraping.enrich_listed_at(conn, stock)
    assert n == scraping.LISTED_AT_BATCH and len(heads["urls"]) == scraping.LISTED_AT_BATCH
    run_poll(conn, stock)
    filled = conn.execute("SELECT COUNT(*) FROM cars WHERE listed_at IS NOT NULL").fetchone()[0]
    assert filled == scraping.LISTED_AT_BATCH
    heads["urls"].clear()
    stock = [car(str(i)) for i in range(40)] + [car("sem-foto", photo=None)]
    scraping.enrich_listed_at(conn, stock)
    assert len(heads["urls"]) == scraping.LISTED_AT_BATCH  # só os que faltam
    assert all(u for u in heads["urls"])                    # carro sem foto nunca vira HEAD


def test_poll_job_stores_listed_at(client, site, car, heads):
    site.cars = [car("1"), car("2")]
    client.post("/admin/check-now")
    from app import db

    c = db.connect()
    try:
        vals = {r[0] for r in c.execute("SELECT listed_at FROM cars")}
    finally:
        c.close()
    assert len(vals) == 1 and str(date(2026, 9, 1)) in str(vals.pop())


def test_failure_never_breaks_poll(client, site, car, monkeypatch):
    import app.scraper.listed_at as la

    monkeypatch.delenv("UNION_DISABLE_LISTED_AT")
    monkeypatch.setattr(la, "fetch_listed_at", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cdn")))
    site.cars = [car("1")]
    assert client.post("/admin/check-now", follow_redirects=False).headers["location"] == "/?msg=checked"


def test_slow_cdn_does_not_eat_the_function_budget(conn, car, heads):
    """Na Vercel a função tem maxDuration=60 e o poll ainda precisa raspar o site depois.
    CDN lento (2 s por HEAD x 15) não pode consumir mais que ~10 s da execução."""
    heads["delay"] = 2.0
    t0 = time.monotonic()
    scraping.enrich_listed_at(conn, [car(str(i)) for i in range(30)])
    assert time.monotonic() - t0 <= 10.5


def test_photo_without_last_modified_retried_once_per_day(conn, car, heads, monkeypatch):
    from datetime import timedelta

    from app import db

    heads["value"] = None  # CDN não devolve Last-Modified
    real_now = db.now

    def cycle():
        stock = [car("1"), car("2")]
        scraping.enrich_listed_at(conn, stock)
        run_poll(conn, stock)

    cycle()                                   # novos: tentados no mesmo poll
    assert len(heads["urls"]) == 2
    cycle()
    cycle()                                   # mesmo dia: nenhum HEAD
    assert len(heads["urls"]) == 2
    monkeypatch.setattr(db, "now", lambda: real_now() + timedelta(days=1))
    cycle()                                   # dia seguinte: 1 tentativa por carro
    assert len(heads["urls"]) == 4
    cycle()
    assert len(heads["urls"]) == 4


def test_deadline_keeps_what_arrived(conn, car, heads):
    """Com prazo estourado, os que já responderam ficam gravados; os outros tentam no próximo poll."""
    heads["delay"] = 1.0
    stock = [car(str(i)) for i in range(15)]
    scraping.enrich_listed_at(conn, stock)
    run_poll(conn, stock)
    got = conn.execute("SELECT COUNT(*) FROM cars WHERE listed_at IS NOT NULL").fetchone()[0]
    assert 1 <= got < 15
    assert conn.execute("SELECT COUNT(*) FROM cars WHERE listed_at IS NULL AND listed_at_checked IS NULL"
                        ).fetchone()[0] == 15 - got
