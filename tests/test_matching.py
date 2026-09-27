"""Normalização de nomes e casamento anúncio ↔ carro (Forja: app/services/matching.py)."""
from __future__ import annotations

import pytest

from app.services.matching import find_match, normalize_name
from app.services.sync import run_poll


@pytest.mark.parametrize("a, b", [
    ("BYD SONG PLUS 1.5 DM-I HÍBRIDO AUTOMÁTICO 2025", "byd song plus 1.5 dm i hibrido automatico 2025"),
    ("  Chevrolet   Onix  1.0 ", "chevrolet onix 1.0"),
    ("Onix 1,0 LT", "onix 1.0 lt"),
    ("Jeep Renegade — Longitude!", "jeep renegade longitude"),
    ("Citroën C4 Cactus", "citroen c4 cactus"),
    ("Fim de frase. 2020", "fim de frase 2020"),
])
def test_normalize_name(a, b):
    assert normalize_name(a) == b


def test_normalize_keeps_decimal_point():
    assert normalize_name("Onix 1.0") != normalize_name("Onix 10")


def test_normalize_empty():
    assert normalize_name("") == ""
    assert normalize_name(None) == ""


def test_match_by_external_id_wins_over_name(conn, car):
    run_poll(conn, [car("1", "Onix 1.0 2020"), car("2", "HB20 1.6 2019")])
    # mesmo id, nome mudou (site editou o anúncio)
    m = find_match(conn, car("1", "HB20 1.6 2019"))
    assert m is not None and m.external_id == "1"


def test_match_by_name_fallback(conn, car):
    run_poll(conn, [car("1", "Chevrolet Onix 1.0 LT 2020")])
    m = find_match(conn, car("99", "CHEVROLET ONIX 1.0 LT 2020"))
    assert m is not None and m.external_id == "1"


def test_no_match(conn, car):
    run_poll(conn, [car("1", "Onix 1.0 2020")])
    assert find_match(conn, car("2", "Onix 10 2020")) is None


def test_name_fallback_prefers_closest_price(conn, car):
    run_poll(conn, [car("1", "Song Plus 2027", 32_490_000), car("2", "Song Plus 2027", 17_990_000)])
    m = find_match(conn, car("3", "Song Plus 2027", 18_000_000))
    assert m.external_id == "2"


def test_name_fallback_respects_exclude(conn, car):
    run_poll(conn, [car("1", "Song Plus 2027")])
    m = find_match(conn, car("3", "Song Plus 2027"), exclude_ids=frozenset({1}))
    assert m is None
