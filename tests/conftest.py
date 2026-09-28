"""Fixtures compartilhadas. Nada aqui acessa a rede nem o banco real (data/union.db / Neon).

Banco: SQLite temporário por teste (padrão). Com TEST_DATABASE_URL=postgresql://...@localhost:55432/...
a suíte inteira roda no Postgres local (Docker), zerado antes de cada teste. Ver tests/run_both.ps1.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

# Antes de importar app.*: sem scheduler, sem toast do Windows, sem mock embutido,
# e NUNCA herdar credenciais de nuvem do ambiente (Neon, WhatsApp, cron, Vercel).
os.environ["UNION_DISABLE_SCHEDULER"] = "1"
os.environ["UNION_DISABLE_NOTIFY"] = "1"
for _var in ("UNION_MOCK_SCRAPER", "DATABASE_URL", "POSTGRES_URL", "DATABASE_URL_UNPOOLED",
             "POSTGRES_URL_NON_POOLING", "VERCEL", "APP_PASSWORD", "SESSION_SECRET",
             "CRON_SECRET", "WHATSAPP_PHONE", "CALLMEBOT_APIKEY", "APP_URL"):
    os.environ.pop(_var, None)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
if TEST_DATABASE_URL and urlsplit(TEST_DATABASE_URL).hostname not in ("localhost", "127.0.0.1", "::1"):
    raise SystemExit(f"TEST_DATABASE_URL precisa ser local (os testes apagam tudo): {TEST_DATABASE_URL}")
PG = bool(TEST_DATABASE_URL)

from app import db  # noqa: E402
from app.scraper.parser import ScrapedCar  # noqa: E402


def make_car(ext: str, name: str | None = None, price: int | None = 5_000_000,
             photo: str | None = "https://img.test/{ext}.jpg") -> ScrapedCar:
    return ScrapedCar(
        external_id=str(ext),
        name=name or f"Carro {ext} 1.0 2020",
        price_cents=price,
        photo_url=photo.format(ext=ext) if photo else None,
        url=f"https://www.unionrioveiculos.com.br/Veiculo/carro-{ext}/{ext}/detalhes",
    )


@pytest.fixture
def car():
    return make_car


@pytest.fixture(scope="session")
def listing_html() -> str:
    return (FIXTURES / "union_listing.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch) -> Path:
    """Cada teste usa um SQLite novo (app lê UNION_DB_PATH a cada connect())."""
    path = tmp_path / "union.db"
    monkeypatch.setenv("UNION_DB_PATH", str(path))
    monkeypatch.setenv("UNION_DATA_DIR", str(tmp_path / "data"))
    from app.encarte import photos  # cache de fotos fora de data/ real

    monkeypatch.setattr(photos, "CACHE_DIR", tmp_path / "data" / "cache" / "photos")
    if PG:
        monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
        db.init_db()
        c = db.connect()
        try:
            db.truncate_all(c)
            c.commit()
        finally:
            c.close()
    else:
        db.init_db()
    try:
        from app.encarte import source

        source.clear_cache()
    except ImportError:
        pass
    return path


def pytest_report_header(config):
    return f"banco: {'Postgres ' + TEST_DATABASE_URL if PG else 'SQLite temporário'}"


requires_pg = pytest.mark.skipif(not PG, reason="só com TEST_DATABASE_URL (Postgres)")
sqlite_only = pytest.mark.skipif(PG, reason="só no SQLite")


@pytest.fixture
def conn(tmp_db):
    c = db.connect()
    yield c
    c.close()


class FakeSite:
    """Estoque controlado pelo teste; substitui app.services.scraping.scrape_all."""

    def __init__(self):
        self.cars: list[ScrapedCar] = []
        self.error: Exception | None = None
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.cars)


@pytest.fixture
def site(monkeypatch) -> FakeSite:
    from app.services import scraping

    fake = FakeSite()
    monkeypatch.setattr(scraping, "scrape_all", fake)
    return fake


@pytest.fixture
def client(site):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
