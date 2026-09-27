"""F4 — página de detalhe (Lupa: app/scraper/detail.py) com fixtures salvas."""
from __future__ import annotations

import httpx
import pytest

from app.scraper import ScraperError
from app.scraper.detail import (
    armor_company_display,
    brand_display,
    parse_armor,
    parse_detail,
    scrape_detail,
    split_brand_model,
)
from conftest import FIXTURES

BASE = "https://www.unionrioveiculos.com.br/Veiculo"
TRACKER = f"{BASE}/tracker-1.0-turbo-flex-ltz-automatico-flex-2026/5575766/detalhes"
BMW = f"{BASE}/320i-2.0-16v-turbo-flex-m-sport-automatico-flex-2022/5555997/detalhes"
RANGE = (f"{BASE}/range-rover-sport-3.0-d350-turbo-diesel-mhev-first-edition-awd-automatico-"
         "diesel-e-eletrico-2023/5509677/detalhes")


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_tracker():
    d = parse_detail(_html("detail_tracker_5575766.html"), TRACKER)
    assert (d.external_id, d.url) == ("5575766", TRACKER)
    assert (d.brand, d.model, d.version) == ("Chevrolet", "TRACKER", "1.0 TURBO FLEX LTZ AUTOMÁTICO")
    assert (d.year_fab, d.year_model, d.km, d.price_cents) == (2025, 2026, 5500, 12_990_000)
    assert len(d.photos) == 11 and d.photos[0].endswith("/1_042346.jpg")
    assert all(p.startswith("https://") for p in d.photos)
    assert len(set(d.photos)) == len(d.photos)
    assert (d.fuel, d.transmission, d.color) == ("Flex", "Automático", None)
    assert len(d.options) == 38 and all(o and o == o.strip() for o in d.options)


def test_bmw_without_photo_and_price():
    d = parse_detail(_html("detail_bmw320i_sem_foto_5555997.html"), BMW)
    assert (d.brand, d.model) == ("BMW", "320i")
    assert d.price_cents is None and d.photos == []
    assert (d.km, d.year_fab, d.year_model) == (45_000, 2022, 2022)


def test_range_rover_multiword_brand():
    d = parse_detail(_html("detail_range_rover_sport_5509677.html"), RANGE)
    assert (d.brand, d.model) == ("Land Rover", "RANGE ROVER SPORT")
    assert len(d.photos) == 12
    assert (d.year_fab, d.year_model, d.km, d.price_cents) == (2022, 2023, 14_500, 77_990_000)


def test_parse_detail_not_a_detail_page():
    with pytest.raises(ScraperError):
        parse_detail(_html("union_listing.html"), TRACKER)
    with pytest.raises(ScraperError):
        parse_detail("<html><body>Just a moment...</body></html>", TRACKER)


@pytest.mark.parametrize("title, slug, expected", [
    ("LAND ROVER RANGE ROVER SPORT", "range-rover-sport-3.0-d350", ("LAND ROVER", "RANGE ROVER SPORT")),
    ("CHEVROLET TRACKER", "tracker-1.0-turbo", ("CHEVROLET", "TRACKER")),
    ("BMW 320i", "320i-2.0-16v", ("BMW", "320i")),
    ("LAND ROVER DEFENDER", None, ("LAND ROVER", "DEFENDER")),
    ("FIAT UNO", None, ("FIAT", "UNO")),
    ("FIAT", "uno", ("FIAT", "")),
])
def test_split_brand_model(title, slug, expected):
    assert split_brand_model(title, slug) == expected


@pytest.mark.parametrize("raw, expected", [
    ("CHEVROLET", "Chevrolet"),
    ("LAND ROVER", "Land Rover"),
    ("BMW", "BMW"),
])
def test_brand_display(raw, expected):
    assert brand_display(raw) == expected


@pytest.mark.parametrize("follow", [True, False])
def test_scrape_detail_ok(follow):
    html = _html("detail_tracker_5575766.html")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)),
                          follow_redirects=follow)
    assert scrape_detail(TRACKER, client).external_id == "5575766"


@pytest.mark.parametrize("follow", [True, False])
def test_scrape_detail_removed_ad_redirects_to_listing(follow, listing_html):
    def handler(request):
        if request.url.path.endswith("/detalhes"):
            return httpx.Response(302, headers={"Location": "/Veiculos"})
        return httpx.Response(200, text=listing_html)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=follow)
    with pytest.raises(ScraperError):
        scrape_detail(TRACKER, client)


def test_scrape_detail_http_error():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(ScraperError):
        scrape_detail(TRACKER, client)


# --- F4.1: blindagem ---------------------------------------------------------

COMPASS = f"{BASE}/compass-2.0-td350-turbo-diesel-longitude-at9-diesel-2023/5462415/detalhes"
X2 = f"{BASE}/x2-2.0-turbo-gasolina-xdrive20i-m-sport-steptronic-gasolina-2025/5395911/detalhes"
BMW120 = f"{BASE}/120i-2.0-16v-gasolina-sport-4p-automatico-gasolina-2019/5530290/detalhes"


def test_armored_compass_gr():
    d = parse_detail(_html("detail_blindado_compass_gr_5462415.html"), COMPASS)
    assert (d.armored, d.armor_company) == (True, "GR Vidros Eternity")
    assert len(d.options) == 46
    assert d.options[0] == "IPVA Pago" and d.options[1] == "Blindado" and d.options[-1] == "Freio de mão elétrico"
    assert d.info_text.rstrip().endswith("BLINDADORA GR VIDROS ETERNITY.")


def test_armored_x2_security_info_text_clean():
    d = parse_detail(_html("detail_blindado_x2_security_5395911.html"), X2)
    assert (d.armored, d.armor_company) == (True, "Security")
    assert d.info_text.rstrip().endswith("BLINDADORA SECURITY.")
    lines = [ln.strip() for ln in d.info_text.splitlines()]
    assert "." not in lines  # "<br>.<br>" do HTML não vira linha solta
    assert "<br" not in d.info_text.lower()


def test_armored_without_company():
    d = parse_detail(_html("detail_blindado_sem_blindadora_120i_5530290.html"), BMW120)
    assert (d.armored, d.armor_company) == (True, None)


def test_not_armored_and_old_fixture_armored():
    t = parse_detail(_html("detail_tracker_5575766.html"), TRACKER)
    assert (t.armored, t.armor_company) == (False, None)
    b = parse_detail(_html("detail_bmw320i_sem_foto_5555997.html"), BMW)
    assert (b.armored, b.armor_company) == (True, "Master Vidros Protector Evo")


@pytest.mark.parametrize("text, expected", [
    ("BLIDADO RJ PRO VIDROS SECFORCE LEV", (True, "RJ Pro Vidros Secforce Lev")),
    ("BLINADAORA SHELLTER COM VIDROS PROTECHTOR EVO", (True, "Shellter com Vidros Protechtor Evo")),
    ("BLINDADORA SOLUTION PLACE. VIDROS PROTECTHOR EVO", (True, "Solution Place Vidros Protecthor Evo")),
    ("BLINDADO FORCE CAR", (True, "Force Car")),
    ("BLINDADORA MG3 VIDROS PROTECHTOR EVO.", (True, "MG3 Vidros Protechtor Evo")),
    ("BLINDAGEM NIVEL III-A.", (False, None)),
    ("", (False, None)),
    (None, (False, None)),
])
def test_parse_armor_inline(text, expected):
    assert parse_armor(text, []) == expected


def test_parse_armor_option_only():
    assert parse_armor("Carro revisado.", ["IPVA Pago", "Blindado"]) == (True, None)
    assert parse_armor("Carro revisado.", ["IPVA Pago"]) == (False, None)


@pytest.mark.parametrize("raw, expected", [
    ("GR VIDROS ETERNITY", "GR Vidros Eternity"),
    ("MG3 VIDROS PROTECHTOR EVO", "MG3 Vidros Protechtor Evo"),
    ("SHELLTER COM VIDROS", "Shellter com Vidros"),
])
def test_armor_company_display(raw, expected):
    assert armor_company_display(raw) == expected
