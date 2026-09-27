"""Funções puras de parsing do estoque da Union Veículos (plataforma AutoCerto).

Nada aqui faz I/O: recebem HTML/texto e devolvem dados, para serem testadas
com fixtures salvas em tests/fixtures/.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

BASE_URL = "https://www.unionrioveiculos.com.br"
LISTING_URL = f"{BASE_URL}/Veiculos"

# Imagem genérica do AutoCerto para anúncio ainda sem foto.
PLACEHOLDER_PHOTO_MARKERS = ("/images/embreve",)

# href dos anúncios: Veiculo/<slug>/<id>/detalhes
_DETAIL_HREF_RE = re.compile(r"Veiculo/[^/]+/(\d+)/detalhes", re.IGNORECASE)
_TOTAL_RE = re.compile(r"de\s+(\d+)\s+ve[ií]culos?", re.IGNORECASE)
_PRICE_RE = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:,\d{1,2})?")


@dataclass
class ScrapedCar:
    external_id: str
    name: str
    price_cents: int | None
    photo_url: str | None
    url: str


def _clean(text: str) -> str:
    return " ".join(text.split())


def parse_price_cents(text: str | None) -> int | None:
    """"R$ 89.900,00" -> 8990000; "89.900" -> 8990000; "Consulte" / "" -> None."""
    if not text:
        return None
    match = _PRICE_RE.search(text.replace("\xa0", " "))
    if not match:
        return None
    raw = match.group(0)
    reais, _, cents = raw.replace(".", "").partition(",")
    value = int(reais) * 100 + (int(cents.ljust(2, "0")) if cents else 0)
    return value or None


def parse_total_count(html: str) -> int | None:
    """Total anunciado pela página ("mostrando 1 - 47 de 47 veículos.")."""
    soup = BeautifulSoup(html, "html.parser")
    for h4 in soup.select(".search-actions h4, h4"):
        match = _TOTAL_RE.search(h4.get_text(" "))
        if match:
            return int(match.group(1))
    return None


def _parse_photo(item: Tag, base_url: str) -> str | None:
    img = item.select_one(".fotoVeiculo img") or item.select_one("img")
    if img is None:
        return None
    src = (img.get("data-src") or img.get("src") or "").strip()
    if not src:
        return None
    if any(marker in src.lower() for marker in PLACEHOLDER_PHOTO_MARKERS):
        return None
    return urljoin(base_url + "/", src)


def _parse_name(item: Tag) -> str | None:
    title = item.select_one(".result-item-title a") or item.select_one(".result-item-title")
    if title is None:
        return None
    # <a>BMW X6<br/><span class="versaoVeiculo">3.0 ...</span></a>
    model = _clean(" ".join(title.find_all(string=True, recursive=False)))
    version_tag = title.select_one(".versaoVeiculo")
    version = _clean(version_tag.get_text(" ")) if version_tag else ""
    if not model and not version:
        model = _clean(title.get_text(" "))
    year = None
    for li in item.select(".listaEspecificacoes li"):
        label = li.find("span")
        value = li.find("p")
        if label and value and _clean(label.get_text()).lower() == "ano":
            year = _clean(value.get_text())
            break
    return " ".join(part for part in (model, version, year) if part) or None


def parse_listing(html: str, base_url: str = BASE_URL) -> list[ScrapedCar]:
    """Extrai os carros da página /Veiculos. Ignora cards sem link de detalhe."""
    soup = BeautifulSoup(html, "html.parser")
    cars: list[ScrapedCar] = []
    seen: set[str] = set()
    for item in soup.select("div.result-item"):
        link = item.find("a", href=_DETAIL_HREF_RE)
        if link is None:
            continue
        href = link["href"]
        external_id = _DETAIL_HREF_RE.search(href).group(1)
        if external_id in seen:
            continue
        name = _parse_name(item)
        if not name:
            continue
        price_tag = item.select_one(".result-item-pricing .price") or item.select_one(".price")
        seen.add(external_id)
        cars.append(
            ScrapedCar(
                external_id=external_id,
                name=name,
                price_cents=parse_price_cents(price_tag.get_text(" ") if price_tag else None),
                photo_url=_parse_photo(item, base_url),
                url=urljoin(base_url + "/", href.lstrip("/")),
            )
        )
    return cars
