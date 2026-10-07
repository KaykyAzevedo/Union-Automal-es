"""F13 — marca a partir do nome do estoque (Lupa: app/scraper/brands.py)."""
from __future__ import annotations

import pytest

from app.scraper import brand_from_name, parse_listing
from app.scraper.brands import UNKNOWN_BRAND, split_brand
from app.scraper.detail import brand_display, split_brand_model
from conftest import FIXTURES

# Todas as marcas do estoque (fixture real da listagem + coleta ao vivo de 07/10/2026).
STOCK_BRANDS = {
    "Audi", "BMW", "BYD", "Chery", "Chevrolet", "Fiat", "Ford", "GWM", "Hyundai", "Jaguar", "Jeep",
    "Land Rover", "Mercedes-Benz", "Mitsubishi", "Porsche", "RAM", "Renault", "Toyota", "Volkswagen",
}


@pytest.mark.parametrize("name, brand", [
    ("AUDI Q3 2.0 40 TFSI GASOLINA PERFORMANCE BLACK QUATTRO TIPTRONIC 2024", "Audi"),
    ("BMW 320i 2.0 16V TURBO FLEX M SPORT AUTOMÁTICO 2022", "BMW"),
    ("BYD SONG PLUS 1.5 DM-I TURBO HÍBRIDO AUTOMÁTICO 2027", "BYD"),
    ("CHERY TIGGO 5X PRO 1.5 VVT TURBO iFLEX CVT 2027", "Chery"),
    ("CHEVROLET TRACKER 1.0 TURBO FLEX LTZ AUTOMÁTICO 2026", "Chevrolet"),
    ("FIAT MOBI 1.0 EVO FLEX LIKE. MANUAL 2024", "Fiat"),
    ("FORD RANGER 3.2 FX4 4X4 CD 20V DIESEL 4P AUTOMÁTICO 2023", "Ford"),
    ("GWM HAVAL H6 GT 1.5 PHEV AWD E-TRACTION 2025", "GWM"),
    ("HYUNDAI HB20S 1.0 TGDI FLEX LIMITED AUTOMÁTICO 2026", "Hyundai"),
    ("JAGUAR F-PACE 3.0 P340 MHEV R-DYNAMIC SE AWD AUTOMÁTICO 2023", "Jaguar"),
    ("JEEP COMPASS 1.3 T270 TURBO FLEX S AT6 2024", "Jeep"),
    ("LAND ROVER RANGE ROVER SPORT 3.0 HSE 4X4 V6 24V TURBO DIESEL 4P AUTOMÁTICO 2019", "Land Rover"),
    ("MERCEDES-BENZ CLA 200 1.3 MHEV AMG LINE 7G-DCT 2025", "Mercedes-Benz"),
    ("MITSUBISHI ECLIPSE CROSS 1.5 MIVEC TURBO GASOLINA HPE-S BLACK S-AWC CVT 2026", "Mitsubishi"),
    ("PORSCHE MACAN 2.0 TURBO GASOLINA T PDK 2024", "Porsche"),
    ("RAM RAMPAGE 2.0 HURRICANE 4 TURBO GASOLINA R/T 4X4 AUTOMÁTICO 2025", "RAM"),
    ("RENAULT DUSTER 1.3 TCE FLEX ICONIC PLUS X-TRONIC 2026", "Renault"),
    ("TOYOTA HILUX SW4 2.8 D-4D TURBO DIESEL SRX PLATINUM 7L 4X4 AUTOMÁTICO 2025", "Toyota"),
    ("VOLKSWAGEN T-CROSS 1.0 200 TSI TOTAL FLEX SENSE AUTOMÁTICO 2020", "Volkswagen"),
])
def test_brand_from_name_stock_brands(name, brand):
    assert brand_from_name(name) == brand


def test_every_name_in_listing_fixture_maps_to_a_known_brand():
    cars = parse_listing((FIXTURES / "union_listing.html").read_text(encoding="utf-8"))
    brands = {brand_from_name(c.name) for c in cars}
    assert len(cars) == 47
    assert brands <= STOCK_BRANDS, brands - STOCK_BRANDS
    assert UNKNOWN_BRAND not in brands
    assert {"Land Rover", "Mercedes-Benz", "BMW", "Chevrolet"} <= brands


@pytest.mark.parametrize("name, brand", [
    ("lexus nx 350h", "Lexus"),                 # desconhecida → 1ª palavra em Title Case
    ("HONDA CIVIC 2.0", "Honda"),
    ("Honda", "Honda"),
    ("  chevrolet   onix  ", "Chevrolet"),      # espaços e caixa
    ("land rover discovery", "Land Rover"),
    ("LAND ROVER", "Land Rover"),               # só a marca
    ("KIA SPORTAGE", "Kia"),
    ("JAC T40", "JAC"),
    ("ALFA ROMEO GIULIA", "Alfa Romeo"),
    ("ROLLS-ROYCE GHOST", "Rolls-Royce"),
])
def test_brand_from_name_other_brands(name, brand):
    assert brand_from_name(name) == brand


@pytest.mark.parametrize("name, brand", [
    ("MERCEDES BENZ C180", "Mercedes-Benz"),    # grafias diferentes → uma marca só
    ("CAOA CHERY TIGGO 7", "Chery"),
    ("VW GOL 1.0", "Volkswagen"),
    ("GM ONIX", "Chevrolet"),
    ("GREAT WALL HAVAL H6", "GWM"),
])
def test_brand_from_name_aliases(name, brand):
    assert brand_from_name(name) == brand


@pytest.mark.parametrize("name", ["", None, "   ", "2024", "1.0 16V"])
def test_brand_from_name_empty_is_outros(name):
    assert brand_from_name(name) == "Outros" == UNKNOWN_BRAND


def test_split_brand_keeps_case_and_rest():
    assert split_brand("LAND ROVER RANGE ROVER SPORT 3.0") == ("LAND ROVER", "RANGE ROVER SPORT 3.0")
    assert split_brand("BMW 320i 2.0") == ("BMW", "320i 2.0")
    assert split_brand("") == ("", "")


def test_detail_still_uses_the_same_brand_logic():
    """detail.py reusa brands.py: sem slug cai na mesma separação; com slug, o slug manda."""
    assert split_brand_model("LAND ROVER RANGE ROVER SPORT", None) == ("LAND ROVER", "RANGE ROVER SPORT")
    assert split_brand_model("GWM HAVAL H6 GT", "haval-h6-gt-1.5-phev") == ("GWM", "HAVAL H6 GT")
    assert brand_display("MERCEDES-BENZ") == "Mercedes-Benz"
    assert brand_from_name("MERCEDES-BENZ CLA 200") == brand_display("MERCEDES-BENZ")
