"""Legenda do post (Forja: app/encarte/caption.py): blindagem, limite de 2200 e compactação."""
from __future__ import annotations

from dataclasses import replace

import pytest

from app.encarte.caption import CONTACT, MAX_CHARS, build_caption
from app.scraper.detail import parse_detail
from conftest import FIXTURES

BASE = "https://www.unionrioveiculos.com.br/Veiculo"
DETAILS = {
    "detail_tracker_5575766.html": f"{BASE}/tracker-1.0-turbo-flex-ltz-automatico-flex-2026/5575766/detalhes",
    "detail_bmw320i_sem_foto_5555997.html": f"{BASE}/320i-2.0-16v-turbo-flex-m-sport-automatico-flex-2022/5555997/detalhes",
    "detail_range_rover_sport_5509677.html": (f"{BASE}/range-rover-sport-3.0-d350-turbo-diesel-mhev-first-edition-awd-"
                                              "automatico-diesel-e-eletrico-2023/5509677/detalhes"),
    "detail_blindado_compass_gr_5462415.html": f"{BASE}/compass-2.0-td350-turbo-diesel-longitude-at9-diesel-2023/5462415/detalhes",
    "detail_blindado_x2_security_5395911.html": f"{BASE}/x2-2.0-turbo-gasolina-xdrive20i-m-sport-steptronic-gasolina-2025/5395911/detalhes",
    "detail_blindado_sem_blindadora_120i_5530290.html": f"{BASE}/120i-2.0-16v-gasolina-sport-4p-automatico-gasolina-2019/5530290/detalhes",
}


def load(name):
    return parse_detail((FIXTURES / name).read_text(encoding="utf-8"), DETAILS[name])


def armor_lines(text):
    return [ln for ln in text.splitlines() if ln.startswith("🛡️ BLINDADO")]


@pytest.mark.parametrize("name", list(DETAILS))
def test_every_fixture_fits_and_is_complete(name):
    d = load(name)
    text = build_caption(d)
    assert text.strip() and len(text) <= MAX_CHARS
    assert CONTACT in text
    assert "None" not in text
    assert len(armor_lines(text)) == (1 if d.armored else 0)


@pytest.mark.parametrize("name, line", [
    ("detail_blindado_compass_gr_5462415.html", "🛡️ BLINDADO — Blindadora GR Vidros Eternity"),
    ("detail_blindado_x2_security_5395911.html", "🛡️ BLINDADO — Blindadora Security"),
    ("detail_bmw320i_sem_foto_5555997.html", "🛡️ BLINDADO — Blindadora Master Vidros Protector Evo"),
    ("detail_blindado_sem_blindadora_120i_5530290.html", "🛡️ BLINDADO"),
])
def test_armor_line(name, line):
    text = build_caption(load(name))
    assert armor_lines(text) == [line]
    assert "#Blindado" in text


def test_not_armored():
    text = build_caption(load("detail_tracker_5575766.html"))
    assert "🛡️" not in text and "#Blindado" not in text


def test_price_and_km_formats():
    d = load("detail_tracker_5575766.html")
    assert "💰 R$ 129.900,00" in build_caption(d)
    assert "Preço sob consulta" in build_caption(replace(d, price_cents=None))
    assert "0 km" in build_caption(replace(d, km=0))
    no_years = build_caption(replace(d, year_fab=None, year_model=None, km=None, fuel=None, transmission=None))
    assert "📅" not in no_years and "🛣️" not in no_years and "None" not in no_years


# --- compactação ---------------------------------------------------------------

def _huge(d, *, n_options=120, info_sentences=60, armor_sentence="Blindadora GR Vidros Eternity nível III-A."):
    options = list(d.options) + [f"Opcional extra número {i} com nome comprido" for i in range(n_options)]
    info = " ".join(f"Frase informativa {i} sobre o carro, revisões e histórico completo." for i in range(info_sentences))
    return replace(d, options=options, info_text=f"{armor_sentence} {info}")


@pytest.mark.parametrize("name", list(DETAILS))
def test_compaction_never_exceeds_limit(name):
    d = _huge(load(name))
    text = build_caption(d)
    assert len(text) <= MAX_CHARS
    assert len(armor_lines(text)) == (1 if d.armored else 0)
    assert CONTACT in text


def test_compaction_keeps_armor_sentence_of_info():
    d = replace(_huge(load("detail_blindado_compass_gr_5462415.html")), armored=True)
    text = build_caption(d)
    assert "Blindadora GR Vidros Eternity nível III-A." in text
    assert "Frase informativa 59" not in text  # cortou do fim
    assert armor_lines(text) == ["🛡️ BLINDADO — Blindadora GR Vidros Eternity"]


def test_moderate_overflow_uses_bullets_first():
    d = load("detail_blindado_compass_gr_5462415.html")
    d = replace(d, options=list(d.options) + [f"Extra {i}" for i in range(60)])
    text = build_caption(d)
    assert len(text) <= MAX_CHARS
    assert " • " in text


def test_short_caption_is_not_compacted():
    d = load("detail_tracker_5575766.html")
    text = build_caption(d)
    assert " • " not in text and "…" not in text


@pytest.mark.parametrize("armored, company", [(True, "GR Vidros Eternity"), (True, None), (False, None)])
def test_worst_case_unsplittable_info(armored, company):
    """info_text gigante sem pontuação e citando blindagem: não dá para cortar por frase."""
    d = replace(load("detail_tracker_5575766.html"), armored=armored, armor_company=company,
                info_text="BLINDADO " + "palavra " * 600,
                options=[f"Opcional {i}" for i in range(200)])
    text = build_caption(d)
    assert len(text) <= MAX_CHARS
    assert len(armor_lines(text)) == (1 if armored else 0)
    assert CONTACT in text, "contato (WhatsApp/endereço) sumiu da legenda"


def test_no_broken_emoji_at_cut():
    d = replace(load("detail_tracker_5575766.html"), info_text="x" * 5000, options=[])
    text = build_caption(d)
    assert len(text) <= MAX_CHARS
    assert not text.endswith("️") and not text[-1:].isspace()
