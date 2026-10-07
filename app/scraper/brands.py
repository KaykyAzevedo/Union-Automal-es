"""Marca de um anúncio: separação do nome e caixa de exibição (funções puras).

Usado pelo detalhe (CarDetail.brand) e pelo backend (cars.brand via brand_from_name).
"""
from __future__ import annotations

# Marcas com mais de uma palavra, para separar "LAND ROVER RANGE ROVER ..." sem o slug.
KNOWN_MULTIWORD_BRANDS = ("LAND ROVER", "MERCEDES-BENZ", "MERCEDES BENZ", "ALFA ROMEO",
                          "ASTON MARTIN", "ROLLS-ROYCE", "CAOA CHERY", "GREAT WALL")
# Siglas que ficam em caixa alta ("BMW", não "Bmw").
_BRAND_ACRONYMS = {"BMW", "BYD", "GWM", "JAC", "RAM", "MG", "KIA", "DS", "GM", "VW", "JMC", "CAOA"}
_BRAND_SPECIAL = {"KIA": "Kia", "RAM": "RAM", "CAOA": "Caoa"}
# Grafias diferentes da mesma marca → uma só (evita duas barras no ranking de vendas).
_BRAND_ALIASES = {"Mercedes Benz": "Mercedes-Benz", "Caoa Chery": "Chery", "VW": "Volkswagen",
                  "GM": "Chevrolet", "Great Wall": "GWM"}
UNKNOWN_BRAND = "Outros"


def brand_display(raw: str) -> str:
    """"CHEVROLET" -> "Chevrolet", "LAND ROVER" -> "Land Rover", "BMW" -> "BMW"."""
    def word(w: str) -> str:
        if "-" in w:
            return "-".join(word(p) for p in w.split("-"))
        up = w.upper()
        if up in _BRAND_SPECIAL:
            return _BRAND_SPECIAL[up]
        if up in _BRAND_ACRONYMS:
            return up
        return w.capitalize()

    return " ".join(word(w) for w in raw.split())


def split_brand(title: str) -> tuple[str, str]:
    """("LAND ROVER", "RANGE ROVER SPORT 3.0 ...") a partir só do texto: marcas compostas
    conhecidas, senão a 1ª palavra. Sem alterar a caixa."""
    words = title.split()
    if not words:
        return "", ""
    upper = " ".join(words).upper()
    for brand in KNOWN_MULTIWORD_BRANDS:
        if upper == brand or upper.startswith(brand + " "):
            n = len(brand.split())
            return " ".join(words[:n]), " ".join(words[n:])
    return words[0], " ".join(words[1:])


def brand_from_name(name: str | None) -> str:
    """Marca em caixa de exibição a partir do nome do estoque.

    "CHEVROLET TRACKER 1.0 TURBO ..." -> "Chevrolet"; "LAND ROVER RANGE ROVER ..." -> "Land Rover";
    "MERCEDES-BENZ CLA 200 ..." -> "Mercedes-Benz"; "BMW 320i ..." -> "BMW".
    Marca desconhecida -> 1ª palavra em Title Case; vazio/None -> "Outros".
    """
    raw, _ = split_brand(name or "")
    if not raw or not any(ch.isalpha() for ch in raw):
        return UNKNOWN_BRAND
    shown = brand_display(raw)
    return _BRAND_ALIASES.get(shown, shown)
