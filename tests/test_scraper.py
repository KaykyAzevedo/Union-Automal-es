"""Scraper (Lupa): parsing puro com fixtures salvas + scrape_all com httpx mockado."""
from __future__ import annotations

import httpx
import pytest

from app.scraper import LISTING_URL, ScraperError, scrape_all
from app.scraper.parser import parse_listing, parse_price_cents, parse_total_count


@pytest.mark.parametrize("text, expected", [
    ("R$ 89.900,00", 8_990_000),
    ("R$ 779.900,00", 77_990_000),
    ("R$\xa01.234.567,89", 123_456_789),
    ("89.900", 8_990_000),
    ("R$ 89900,5", 8_990_050),
    ("R$ 950,00", 95_000),
    ("  R$ 45.000,00  ", 4_500_000),
    ("Sob Consulta", None),
    ("Consulte", None),
    ("", None),
    (None, None),
    ("R$ 0,00", None),
])
def test_parse_price_cents(text, expected):
    assert parse_price_cents(text) == expected


def test_parse_price_cents_returns_int():
    assert isinstance(parse_price_cents("R$ 10.000,00"), int)


def test_listing_fixture_count_matches_header(listing_html):
    cars = parse_listing(listing_html)
    assert parse_total_count(listing_html) == 47
    assert len(cars) == 47
    assert len({c.external_id for c in cars}) == 47


def test_listing_fields(listing_html):
    cars = {c.external_id: c for c in parse_listing(listing_html)}
    q3 = cars["4746561"]
    assert q3.name.startswith("AUDI Q3 2.0")
    assert q3.name.endswith("2024")
    assert q3.price_cents == 31_990_000
    assert q3.photo_url == "https://www.autocerto.com/fotos/339/4746561/1_044812.jpg"
    assert q3.url.startswith("https://www.unionrioveiculos.com.br/Veiculo/")
    assert q3.url.endswith("/4746561/detalhes")
    assert all(c.external_id.isdigit() for c in cars.values())
    assert all(c.name and "  " not in c.name for c in cars.values())


def test_listing_accents_decoded(listing_html):
    names = [c.name for c in parse_listing(listing_html)]
    assert any("AUTOMÁTICO" in n for n in names)
    assert not any("�" in n or "Ã" in n for n in names)


def test_listing_sob_consulta_and_placeholder_photo(listing_html):
    cars = {c.external_id: c for c in parse_listing(listing_html)}
    assert cars["5555997"].price_cents is None  # "Sob Consulta"
    assert cars["5555997"].photo_url is None     # emBreve.jpg
    assert not any("embreve" in (c.photo_url or "").lower() for c in cars.values())


def test_listing_homonyms_keep_distinct_ids(listing_html):
    songs = [c for c in parse_listing(listing_html)
             if c.name == "BYD SONG PLUS 1.5 DM-I TURBO HÍBRIDO AUTOMÁTICO 2027"]
    assert len(songs) == 3
    assert len({s.external_id for s in songs}) == 3


def test_parse_listing_ignores_cards_without_detail_link():
    html = """
    <div class="result-item"><div class="result-item-title"><a href="/x">Sem link</a></div></div>
    <div class="result-item">
      <div class="result-item-title"><a href="Veiculo/onix/123/detalhes">CHEVROLET ONIX<br/>
        <span class="versaoVeiculo">1.0 LT</span></a></div>
      <ul class="listaEspecificacoes"><li><span>Ano</span><p>2020</p></li></ul>
      <div class="result-item-pricing"><span class="price">R$ 69.900,00</span></div>
      <div class="fotoVeiculo"><img src="https://www.autocerto.com/fotos/339/123/1_a.jpg"></div>
    </div>"""
    cars = parse_listing(html)
    assert len(cars) == 1
    c = cars[0]
    assert (c.external_id, c.name, c.price_cents) == ("123", "CHEVROLET ONIX 1.0 LT 2020", 6_990_000)
    assert c.url == "https://www.unionrioveiculos.com.br/Veiculo/onix/123/detalhes"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_scrape_all_ok(listing_html):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=listing_html)

    cars = scrape_all(_client(handler))
    assert len(cars) == 47
    assert seen == [LISTING_URL]


def test_scrape_all_http_error_raises():
    with pytest.raises(ScraperError):
        scrape_all(_client(lambda r: httpx.Response(503, text="down")))


def test_scrape_all_network_error_raises():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(ScraperError):
        scrape_all(_client(handler))


def test_scrape_all_retries_then_succeeds(listing_html):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500) if calls["n"] == 1 else httpx.Response(200, text=listing_html)

    assert len(scrape_all(_client(handler))) == 47
    assert calls["n"] == 2


def test_scrape_all_layout_changed_is_error_not_empty_stock():
    # ex.: página de desafio do Cloudflare com status 200
    with pytest.raises(ScraperError):
        scrape_all(_client(lambda r: httpx.Response(200, text="<html><body>Just a moment...</body></html>")))
