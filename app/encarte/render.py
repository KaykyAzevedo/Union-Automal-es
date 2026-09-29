"""Renderização do encarte (Instagram 4:5, 1080x1350) com Pillow.

Coordenadas medidas por pixel em app/assets/encarte/*_base.png comparando com
docs/referencia/*_exemplo.png. Posições de texto são do topo da caixa-alta
(cap height) e da borda da tinta, igual ao que se mede no exemplo.
"""
from __future__ import annotations

import io
import threading
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw

from .fonts import get_font

ASSETS = Path(__file__).resolve().parent.parent / "assets" / "encarte"
SIZE = (1080, 1350)

# ---- capa -------------------------------------------------------------------
# moldura chanfrada (interior escuro do capa_base): retângulo + chanfro 45°
COVER_PHOTO_BOX = (89, 252, 992, 830)   # (x0, y0, x1, y1) exclusivo
COVER_CHAMFER = 43
COVER_ZOOM = 1.08                       # enquadramento calibrado contra capa_exemplo
COVER_CENTERING = (0.45, 0.64)          # corta mais teto que chão (fotos de loja)
# textos: cap = altura da caixa-alta em px; top = topo da caixa-alta
BRAND = dict(style="brand", cap=36, top=881, center_x=253, max_w=330, color=(205, 14, 23))
MODEL = dict(style="model", cap=30, top=944, center_x=242, max_w=330, color=(248, 248, 248))
VERSION = dict(style="version", cap=22, top=1008, center_x=242, max_w=320, line_pitch=41, color=(228, 225, 228))
KM = dict(style="value", cap=23, top=884, left=672, max_w=100, color=(255, 255, 255))
YEAR = dict(style="value", cap=23, top=884, left=965, max_w=95, color=(255, 255, 255))
PRICE = dict(style="price", cap=54, top=1000, right=960, max_w=348, color=(228, 228, 228))
PRICE_CONSULT = dict(style="price", cap=46, top=1004, right=1036, max_w=420, color=(228, 228, 228))
CENTS_BOX = (970, 1020, 1040, 1060)     # ",00" impresso no fundo (coberto quando preço None)
CENTS_PATCH_SRC_X = 895                  # de onde copiar fundo liso da caixa para cobrir o ",00"
ITALIC_SHEAR = 0.21                      # inclinação sintética (~12°)

# selos no canto superior da moldura: bbox medido por diff de docs/referencia/capa_{blindado,0km,blindado_0km}.png
# contra capa_exemplo.png, ajustado ±1 px pelo menor erro; o selo é encaixado na caixa mantendo a proporção
BADGE_ARMORED = (110, 265, 283, 468)        # só blindado
BADGE_ZERO_KM = (108, 257, 280, 426)        # só 0 km
BADGE_PAIR_ARMORED = (110, 267, 259, 443)   # os dois: escudo à esquerda, um pouco menor
BADGE_PAIR_ZERO_KM = (790, 272, 963, 442)   # os dois: 0KM à direita
BADGE_ALPHA_TRIM = 8                        # alfa <= isto é borda vazia do PNG

# ---- demais fotos -----------------------------------------------------------
PHOTO_BOX = (0, 253, 1080, 1059)         # entre a linha vermelha do cabeçalho e a do rodapé


@lru_cache(maxsize=2)
def _base(name: str) -> Image.Image:
    return Image.open(ASSETS / name).convert("RGB")


@lru_cache(maxsize=4)
def _badge(name: str, box: tuple[int, int, int, int]) -> tuple[Image.Image, tuple[int, int]]:
    """Selo RGBA sem a borda transparente, redimensionado (LANCZOS) para caber na caixa."""
    img = Image.open(ASSETS / name).convert("RGBA")
    img = img.crop(img.getchannel("A").point(lambda a: 255 if a > BADGE_ALPHA_TRIM else 0).getbbox())
    x0, y0, x1, y1 = box
    scale = min((x1 - x0) / img.width, (y1 - y0) / img.height)
    w, h = round(img.width * scale), round(img.height * scale)
    return img.resize((w, h), Image.Resampling.LANCZOS), (x0 + (x1 - x0 - w) // 2, y0 + (y1 - y0 - h) // 2)


def is_zero_km(detail) -> bool:
    return detail.km == 0


def cover_badges(detail) -> list[tuple[str, tuple[int, int, int, int]]]:
    """(arquivo, caixa) dos selos da capa: blindado = detail.armored, 0 km = km == 0 (None não conta)."""
    armored, zero = bool(getattr(detail, "armored", False)), is_zero_km(detail)
    if armored and zero:
        return [("selo_blindado.png", BADGE_PAIR_ARMORED), ("selo_0km.png", BADGE_PAIR_ZERO_KM)]
    if armored:
        return [("selo_blindado.png", BADGE_ARMORED)]
    if zero:
        return [("selo_0km.png", BADGE_ZERO_KM)]
    return []


def _paste_badges(img: Image.Image, detail) -> Image.Image:
    badges = cover_badges(detail)
    if not badges:
        return img
    out = img.convert("RGBA")
    for name, box in badges:
        badge, pos = _badge(name, box)
        out.alpha_composite(badge, pos)
    return out.convert("RGB")


def cover_fit(photo: Image.Image, size: tuple[int, int], zoom: float = 1.0,
              centering: tuple[float, float] = (0.5, 0.5)) -> Image.Image:
    """Recorte tipo CSS object-fit: cover, com zoom extra e ponto de centro opcionais."""
    w, h = size
    scale = max(w / photo.width, h / photo.height) * zoom
    sw, sh = max(w, round(photo.width * scale)), max(h, round(photo.height * scale))
    scaled = photo.convert("RGB").resize((sw, sh), Image.Resampling.LANCZOS)
    ox, oy = round((sw - w) * centering[0]), round((sh - h) * centering[1])
    return scaled.crop((ox, oy, ox + w, oy + h))


@lru_cache(maxsize=1)
def _chamfer_mask() -> Image.Image:
    """Máscara poligonal com cantos chanfrados, antialias por supersampling 4x."""
    x0, y0, x1, y1 = COVER_PHOTO_BOX
    w, h, c, s = x1 - x0, y1 - y0, COVER_CHAMFER, 4
    big = Image.new("L", (w * s, h * s), 0)
    W, H, C = w * s, h * s, c * s
    ImageDraw.Draw(big).polygon([(C, 0), (W - C, 0), (W, C), (W, H - C), (W - C, H), (C, H), (0, H - C), (0, C)],
                                fill=255)
    return big.resize((w, h), Image.Resampling.LANCZOS)


# ---- texto ------------------------------------------------------------------
# Objetos de fonte FreeType em cache são compartilhados entre threads: todo uso de
# fonte passa por este lock (o resize/recorte da foto fica fora dele).
_font_lock = threading.RLock()

@lru_cache(maxsize=16)
def _cap_ratio(style: str) -> float:
    font = get_font(style, 200)[0]
    with _font_lock:
        top, bottom = font.getbbox("H")[1::2]
    return (bottom - top) / 200


def _text_mask(text: str, style: str, size: int) -> tuple[Image.Image, int]:
    """Máscara L do texto com o topo da caixa-alta em y=pad. Retorna (máscara, pad)."""
    font, slant, stretch = get_font(style, size)
    with _font_lock:
        l, t, r, b = font.getbbox(text)
        cap_top, cap_bottom = font.getbbox("H")[1::2]
        pad = max(4, size // 4)
        height = (b - min(t, cap_top)) + 2 * pad
        extra = int(ITALIC_SHEAR * height) + 2 if slant else 0
        mask = Image.new("L", (r - l + 2 * pad + extra, height), 0)
        ImageDraw.Draw(mask).text((pad - l, pad - cap_top), text, font=font, fill=255)
    if slant:
        baseline = pad + (cap_bottom - cap_top)
        # saída (x, y) lê entrada (x - k*(baseline - y), y): topo desloca para a direita
        mask = mask.transform(mask.size, Image.Transform.AFFINE,
                              (1, ITALIC_SHEAR, -ITALIC_SHEAR * baseline, 0, 1, 0),
                              resample=Image.Resampling.BICUBIC)
    if stretch != 1.0:
        mask = mask.resize((round(mask.width * stretch), mask.height), Image.Resampling.LANCZOS)
    return mask, pad


def _fit_size(text: str, style: str, cap: int, max_w: int | None, start: int | None = None) -> int:
    """Tamanho que dá a caixa-alta `cap` (ou `start`), reduzido até caber em `max_w`."""
    size = start or max(8, round(cap / _cap_ratio(style)))
    while max_w and size > 8:
        mask, _ = _text_mask(text, style, size)
        box = mask.getbbox()
        if not box or box[2] - box[0] <= max_w:
            break
        size -= 1
    return size


def _draw(img: Image.Image, text: str, *, style: str, cap: int, top: int, color, max_w: int | None = None,
          left: int | None = None, right: int | None = None, center_x: int | None = None,
          size: int | None = None, **_) -> None:
    if not text:
        return
    size = _fit_size(text, style, cap, max_w, start=size)
    mask, pad = _text_mask(text, style, size)
    box = mask.getbbox()
    if not box:
        return
    ink_w = box[2] - box[0]
    if left is not None:
        x = left
    elif right is not None:
        x = right - ink_w
    else:
        x = round(center_x - ink_w / 2)
    ink = mask.crop((box[0], 0, box[2], mask.height))
    img.paste(Image.new("RGB", ink.size, color), (x, top - pad), ink)


def _wrap(text: str, style: str, size: int, max_w: int) -> list[str]:
    font, _, stretch = get_font(style, size)
    max_w = max_w / stretch
    lines: list[str] = []
    for word in text.split():
        trial = f"{lines[-1]} {word}" if lines else word
        with _font_lock:
            fits = font.getlength(trial) <= max_w
        if lines and fits:
            lines[-1] = trial
        else:
            lines.append(word)
    return lines


# ---- formatação dos dados ----------------------------------------------------

def fmt_brand(brand: str) -> str:
    brand = (brand or "").strip()
    if brand.isupper() and len(brand) > 3:  # CHEVROLET -> Chevrolet; BYD, BMW, GWM ficam
        return " ".join(w.capitalize() if len(w) > 3 else w for w in brand.split())
    return brand


def fmt_km(km: int | None) -> str:
    return "—" if km is None else f"{km:,}".replace(",", ".")


def fmt_years(year_fab: int | None, year_model: int | None) -> str:
    years = [y for y in (year_fab, year_model) if y]
    if not years:
        return "—"
    if len(years) == 1:
        return f"{years[0] % 100:02d}"
    return f"{year_fab % 100:02d}/{year_model % 100:02d}"


def fmt_price(price_cents: int | None) -> str | None:
    if price_cents is None:
        return None
    return f"{price_cents // 100:,}".replace(",", ".")


# ---- slides -----------------------------------------------------------------

def render_cover(detail, photo: Image.Image) -> Image.Image:
    img = _base("capa_base.png").copy()
    x0, y0, x1, y1 = COVER_PHOTO_BOX
    img.paste(cover_fit(photo, (x1 - x0, y1 - y0), COVER_ZOOM, COVER_CENTERING), (x0, y0), _chamfer_mask())

    _draw(img, fmt_brand(detail.brand), **BRAND)
    _draw(img, (detail.model or "").upper(), **MODEL)

    version = (detail.version or "").upper()
    if version:
        size = _fit_size("H", VERSION["style"], VERSION["cap"], None)
        lines = _wrap(version, VERSION["style"], size, VERSION["max_w"])
        while len(lines) > 2 and size > 12:
            size -= 1
            lines = _wrap(version, VERSION["style"], size, VERSION["max_w"])
        for i, line in enumerate(lines[:2]):
            _draw(img, line, **{**VERSION, "top": VERSION["top"] + i * VERSION["line_pitch"], "size": size})

    _draw(img, fmt_km(detail.km), **KM)
    _draw(img, fmt_years(detail.year_fab, detail.year_model), **YEAR)

    price = fmt_price(detail.price_cents)
    if price is None:
        cx0, cy0, cx1, cy1 = CENTS_BOX
        patch = img.crop((CENTS_PATCH_SRC_X, cy0, CENTS_PATCH_SRC_X + cx1 - cx0, cy1))
        img.paste(patch, (cx0, cy0))
        _draw(img, "CONSULTE", **PRICE_CONSULT)
    else:
        _draw(img, price, **PRICE)
    return _paste_badges(img, detail)


def render_photo(photo: Image.Image) -> Image.Image:
    img = _base("foto_base.png").copy()
    x0, y0, x1, y1 = PHOTO_BOX
    img.paste(cover_fit(photo, (x1 - x0, y1 - y0)), (x0, y0))
    return img


def to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=6)
    return buf.getvalue()


_SLIDE_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
# ~1,6 MB por PNG (~64 MB no total). Cabe um post inteiro de anúncio grande (todas as fotos;
# hoje o maior tem 14) para prévia + ZIP não re-renderizarem.
_SLIDE_CACHE_MAX = 40
_slide_lock = threading.Lock()


def slide_png(detail, url: str, cover: bool) -> bytes:
    """PNG de um slide (capa se `cover`), com cache LRU em memória."""
    from .photos import load_photo

    key = (url, cover) + ((detail.brand, detail.model, detail.version, detail.year_fab, detail.year_model,
                           detail.km, detail.price_cents, bool(getattr(detail, "armored", False)))
                          if cover else ())
    with _slide_lock:
        if key in _SLIDE_CACHE:
            _SLIDE_CACHE.move_to_end(key)
            return _SLIDE_CACHE[key]
    photo = load_photo(url)
    data = to_png(render_cover(detail, photo) if cover else render_photo(photo))
    with _slide_lock:
        _SLIDE_CACHE[key] = data
        while len(_SLIDE_CACHE) > _SLIDE_CACHE_MAX:
            _SLIDE_CACHE.popitem(last=False)
    return data


def build_post(detail, photo_urls: list[str]) -> list[bytes]:
    """PNGs do post: capa (1ª foto) + uma imagem por foto seguinte."""
    return [slide_png(detail, url, i == 0) for i, url in enumerate(photo_urls)]
