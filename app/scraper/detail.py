"""Página de detalhe de um anúncio (Veiculo/<slug>/<id>/detalhes) → CarDetail, para o encarte.

As fotos do AutoCerto (autocerto.com/fotos/<loja>/<id>/N_hhmmss.jpg) já são o arquivo
original (~1440x1080); não existe variante maior, então usamos a URL como está.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from ._http import ScraperError, fetch, new_client
from .parser import BASE_URL, PLACEHOLDER_PHOTO_MARKERS, parse_price_cents

# Marcas com mais de uma palavra, para quando o slug não resolver a separação.
KNOWN_MULTIWORD_BRANDS = ("LAND ROVER", "MERCEDES-BENZ", "ALFA ROMEO", "ASTON MARTIN", "ROLLS-ROYCE")
# Siglas que ficam em caixa alta ("BMW", não "Bmw").
_BRAND_ACRONYMS = {"BMW", "BYD", "GWM", "JAC", "RAM", "MG", "KIA", "DS", "GM", "VW", "JMC", "CAOA"}
_BRAND_SPECIAL = {"KIA": "Kia", "RAM": "RAM", "CAOA": "Caoa"}

_DETAIL_PATH_RE = re.compile(r"/Veiculo/([^/]+)/(\d+)/detalhes", re.IGNORECASE)
_YEAR_SUFFIX_RE = re.compile(r"\s+(19|20)\d{2}$")
_YEARS_RE = re.compile(r"((?:19|20)\d{2})(?:\s*/\s*((?:19|20)\d{2}))?")
# "BLINDADORA SECURITY." / "BLINDADO FORCE CAR" / "BLINDADA ..." e erros vistos no site
# ("BLIDADO", "BLINADAORA"). Não casa "BLINDAGEM".
_ARMOR_RE = re.compile(
    r"\bBLI[NDAOR]*D[NDAOR]*[OA]\b[\s:\-]+(.+?)\s*(?:\.(?=\s|$)|$)",
    re.IGNORECASE | re.MULTILINE,
)
_GLASS_AFTER_RE = re.compile(r"[ \t]*(VIDROS?\b[^.\n]*)", re.IGNORECASE)
_ARMOR_KEEP_UPPER = {"AGP"}
_ARMOR_LOWER = {"com", "e", "de", "da", "do", "das", "dos"}


@dataclass
class CarDetail:
    external_id: str
    url: str
    brand: str
    model: str
    version: str
    name_no_year: str
    year_fab: int | None
    year_model: int | None
    km: int | None
    price_cents: int | None
    photos: list[str]  # URLs absolutas na maior resolução, ordem do site, 1ª = principal
    fuel: str | None = None
    transmission: str | None = None
    color: str | None = None
    options: list[str] = field(default_factory=list)  # Características + Opcionais, completas, ordem do site
    info_text: str | None = None  # bloco "Informações do Veículo", linhas separadas por \n
    armored: bool = False
    armor_company: str | None = None  # "Security", "GR Vidros Eternity"


def _clean(text: str) -> str:
    return " ".join(text.split())


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-")


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


def split_brand_model(title: str, slug: str | None) -> tuple[str, str]:
    """Separa "LAND ROVER RANGE ROVER SPORT" em ("LAND ROVER", "RANGE ROVER SPORT").

    O slug do anúncio começa pelo modelo (range-rover-sport-3.0-...), então a menor
    marca cujo resto bate com o início do slug vence. Sem slug: marcas compostas
    conhecidas, senão a 1ª palavra.
    """
    words = title.split()
    if len(words) < 2:
        return title, ""
    if slug:
        slug = slug.lower()
        for i in range(1, len(words)):
            model_slug = _slugify(" ".join(words[i:]))
            if model_slug and (slug == model_slug or slug.startswith(model_slug + "-")):
                return " ".join(words[:i]), " ".join(words[i:])
    upper = title.upper()
    for brand in KNOWN_MULTIWORD_BRANDS:
        if upper.startswith(brand + " "):
            n = len(brand.split())
            return " ".join(words[:n]), " ".join(words[n:])
    return words[0], " ".join(words[1:])


def _parse_int(text: str | None) -> int | None:
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


def _is_real_photo(src: str) -> bool:
    path = urlsplit(src).path
    if not path or path.endswith("/"):
        return False  # anúncio sem foto: src="https://www.autocerto.com/fotos/"
    return not any(marker in src.lower() for marker in PLACEHOLDER_PHOTO_MARKERS)


def _parse_photos(soup: BeautifulSoup, base_url: str) -> list[str]:
    candidates = [img.get("src") for img in soup.select(".additional-images img.fotoSlide")]
    featured = soup.select_one(".featured-image img")
    if featured is not None:
        candidates.insert(0, featured.get("src"))  # principal primeiro; dedupe abaixo
    og = soup.find("meta", attrs={"property": "og:image"})
    if og is not None:
        candidates.append(og.get("content"))
    photos: list[str] = []
    for src in candidates:
        if not src or not src.strip():
            continue
        absolute = urljoin(base_url + "/", src.strip())
        if _is_real_photo(absolute) and absolute not in photos:
            photos.append(absolute)
    return photos


def _parse_specs(soup: BeautifulSoup) -> dict[str, str]:
    specs: dict[str, str] = {}
    for li in soup.select(".dados_anuncio .listadados li, ul.listadados li"):
        label = li.select_one(".info")
        value = li.select_one(".info_destaque")
        if label and value:
            specs[_clean(label.get_text()).lower()] = _clean(value.get_text(" "))
    return specs


def _parse_options(soup: BeautifulSoup) -> list[str]:
    options: list[str] = []
    for li in soup.select("#vehicle-add-features ul.add-features-list li"):
        text = _clean(li.get_text(" "))
        if text and text not in options:
            options.append(text)
    return options


def _parse_info_text(soup: BeautifulSoup) -> str | None:
    """Texto de "Informações do Veículo", sem o título e sem linhas vazias ou só com '.'."""
    block = soup.select_one("#vehicle-overview")
    if block is None:
        return None
    block = BeautifulSoup(str(block), "html.parser")  # cópia: não mexer no soup original
    for strong in block.find_all("strong"):
        if _clean(strong.get_text()).lower().startswith("informações do veículo"):
            strong.decompose()
    for br in block.find_all("br"):
        br.replace_with("\n")
    lines = [_clean(line) for line in block.get_text().split("\n")]
    lines = [line for line in lines if line and set(line) - set(". ")]
    return "\n".join(lines) or None


def armor_company_display(raw: str) -> str:
    """"GR VIDROS ETERNITY" -> "GR Vidros Eternity"; "MG3" fica; "com"/"e" em minúsculas."""
    words = []
    for i, w in enumerate(raw.split()):
        up = w.upper()
        if any(ch.isdigit() for ch in w) or up in _ARMOR_KEEP_UPPER or (len(w) <= 2 and w.isalpha() and w.lower() not in _ARMOR_LOWER):
            words.append(up)
        elif i > 0 and w.lower() in _ARMOR_LOWER:
            words.append(w.lower())
        else:
            words.append("-".join(p.capitalize() for p in w.split("-")))
    return " ".join(words)


def parse_armor(info_text: str | None, options: list[str]) -> tuple[bool, str | None]:
    """(blindado?, blindadora). Blindado se houver o opcional 'Blindado' ou menção a blindadora."""
    company = None
    if info_text:
        match = _ARMOR_RE.search(info_text)
        if match:
            raw = match.group(1).strip(" .:-")
            # "BLINDADORA SOLUTION PLACE. VIDROS PROTECTHOR EVO" → junta os vidros
            glass = _GLASS_AFTER_RE.match(info_text, match.end())
            if glass:
                raw = f"{raw} {glass.group(1).strip(' .')}"
            company = armor_company_display(raw) or None
    has_option = any(opt.strip().lower().startswith("blindad") for opt in options)
    return (has_option or company is not None), company


def parse_detail(html: str, url: str) -> CarDetail:
    """Função pura: HTML da página de detalhe + URL do anúncio → CarDetail.

    Levanta ScraperError se o HTML não for uma página de detalhe (ex.: anúncio removido,
    que redireciona para /Veiculos).
    """
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.select_one("h2.post-title") or soup.select_one(".title-Veiculo h2")
    if title_tag is None:
        raise ScraperError(f"página de detalhe sem título — anúncio removido ou layout mudou: {url}")

    path_match = _DETAIL_PATH_RE.search(urlsplit(url).path)
    slug = path_match.group(1) if path_match else None
    external_id = path_match.group(2) if path_match else None
    if external_id is None:
        hidden = soup.find("input", attrs={"name": "idVeiculo"})
        external_id = hidden.get("value") if hidden else None
    if not external_id:
        raise ScraperError(f"não achei o id do anúncio em {url}")

    # <h2><strong>CHEVROLET TRACKER </strong><br/>1.0 TURBO FLEX LTZ AUTOMÁTICO 2026</h2>
    strong = title_tag.find("strong")
    brand_model = _clean(strong.get_text(" ")) if strong else ""
    rest = _clean(" ".join(t for t in title_tag.find_all(string=True, recursive=False)))
    if not brand_model:
        brand_model, rest = rest, ""
    version = _YEAR_SUFFIX_RE.sub("", rest)
    title_year = _YEAR_SUFFIX_RE.search(rest)

    brand_raw, model = split_brand_model(brand_model, slug)
    brand = brand_display(brand_raw)

    specs = _parse_specs(soup)
    year_fab = year_model = None
    years = _YEARS_RE.search(specs.get("ano", ""))
    if years:
        year_fab = int(years.group(1))
        year_model = int(years.group(2) or years.group(1))
    elif title_year:
        year_model = int(title_year.group(0))

    options = _parse_options(soup)
    info_text = _parse_info_text(soup)
    armored, armor_company = parse_armor(info_text, options)

    price_tag = soup.select_one(".precoVeiculo strong") or soup.select_one(".precoVeiculo")
    base = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}" if urlsplit(url).netloc else BASE_URL

    return CarDetail(
        external_id=external_id,
        url=urljoin(base + "/", url),
        brand=brand,
        model=model,
        version=version,
        name_no_year=" ".join(p for p in (brand, model, version) if p),
        year_fab=year_fab,
        year_model=year_model,
        km=_parse_int(specs.get("km")),
        price_cents=parse_price_cents(price_tag.get_text(" ") if price_tag else None),
        photos=_parse_photos(soup, base),
        fuel=specs.get("combustível") or specs.get("combustivel") or None,
        transmission=specs.get("câmbio") or specs.get("cambio") or None,
        color=specs.get("cor") or None,  # hoje o AutoCerto não mostra cor no detalhe
        options=options,
        info_text=info_text,
        armored=armored,
        armor_company=armor_company,
    )


def scrape_detail(url: str, client: httpx.Client | None = None) -> CarDetail:
    """Baixa e interpreta a página de detalhe. Levanta ScraperError em falha
    ou se o anúncio não existe mais (o site redireciona para /Veiculos)."""
    url = urljoin(BASE_URL + "/", url)
    owns_client = client is None
    client = client or new_client()
    try:
        resp = fetch(client, url)
    finally:
        if owns_client:
            client.close()
    final_path = urlsplit(str(resp.url)).path
    if not _DETAIL_PATH_RE.search(final_path):
        raise ScraperError(f"anúncio não encontrado (redirecionou para {final_path}): {url}")
    return parse_detail(resp.text, url)
