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


@lru_cache(maxsize=8)
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


# Ajuste manual de enquadramento: (zoom, cx, cy) com a semântica de cover_fit(zoom, centering).
Adj = tuple  # (z, cx, cy)
ADJ_ZOOM_RANGE = (1.0, 3.0)
COVER_DEFAULT_ADJ = (COVER_ZOOM, *COVER_CENTERING)
STORY_DEFAULT_ADJ = (1.0, 0.5, 0.5)
# Fotos de blindados do site trazem a marca d'água "BLINDADO" no canto sup. direito. Medida em 140
# fotos de 12 blindados: x >= 0,864 e y <= 0,130 da foto (1440x1080) e, no pior caso (1024x768),
# x >= 0,809 e y <= 0,182. O padrão abaixo tira a marca cortando o topo (base alinhada, cy=1): na
# foto de cima, o menor zoom que a deixa toda atrás do cabeçalho; no interior, sem zoom (a caixa
# já é mais larga que a foto). Ajuste manual continua valendo por cima.
STORY_ARMORED_TOP_ADJ = (1.16, 0.5, 1.0)
STORY_ARMORED_INT_ADJ = (1.0, 0.5, 1.0)


def story_default_adj(armored: bool) -> tuple[tuple, tuple]:
    """(padrão da foto de cima, padrão do interior) do Stories para o carro."""
    return (STORY_ARMORED_TOP_ADJ, STORY_ARMORED_INT_ADJ) if armored else (STORY_DEFAULT_ADJ, STORY_DEFAULT_ADJ)


def norm_adj(adj, default: tuple) -> tuple:
    """Ajuste normalizado (floats arredondados) para usar no render e na chave de cache; None -> padrão."""
    if adj is None:
        return tuple(default)
    z, cx, cy = adj
    return (round(float(z), 3), round(float(cx), 3), round(float(cy), 3))


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

def render_cover(detail, photo: Image.Image, adj=None) -> Image.Image:
    img = _base("capa_base.png").copy()
    x0, y0, x1, y1 = COVER_PHOTO_BOX
    z, cx, cy = norm_adj(adj, COVER_DEFAULT_ADJ)
    img.paste(cover_fit(photo, (x1 - x0, y1 - y0), z, (cx, cy)), (x0, y0), _chamfer_mask())

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


def slide_png(detail, url: str, cover: bool, adj=None) -> bytes:
    """PNG de um slide (capa se `cover`), com cache LRU em memória. `adj` = (z, cx, cy) só na capa."""
    from .photos import load_photo

    adj = norm_adj(adj, COVER_DEFAULT_ADJ)
    key = (url, cover) + ((detail.brand, detail.model, detail.version, detail.year_fab, detail.year_model,
                           detail.km, detail.price_cents, bool(getattr(detail, "armored", False)), adj)
                          if cover else ())
    with _slide_lock:
        if key in _SLIDE_CACHE:
            _SLIDE_CACHE.move_to_end(key)
            return _SLIDE_CACHE[key]
    photo = load_photo(url)
    data = to_png(render_cover(detail, photo, adj) if cover else render_photo(photo))
    with _slide_lock:
        _SLIDE_CACHE[key] = data
        while len(_SLIDE_CACHE) > _SLIDE_CACHE_MAX:
            _SLIDE_CACHE.popitem(last=False)
    return data


def build_post(detail, photo_urls: list[str]) -> list[bytes]:
    """PNGs do post: capa (1ª foto) + uma imagem por foto seguinte."""
    return [slide_png(detail, url, i == 0) for i, url in enumerate(photo_urls)]


# ---- Stories (Instagram 9:16, 1080x1920) ---------------------------------------
# Layout v2 (F20). Camadas geradas por docs/referencia/stories/build_assets.py; posições medidas
# por pixel em docs/referencia/stories/story_exemplo_v2.jpg escalada para 1080x1920. Textos
# ancorados na LINHA DE BASE (cap = altura da caixa-alta em px), para encolher sem subir/descer.
STORY_SIZE = (1080, 1920)
STORY_MIN_SHRINK = 0.45  # encolhe até esta fração do tamanho do molde; depois abrevia com "…"
STORY_MIN_SHRINK_BY_STYLE = {"story_brand": 0.2}  # marca nunca é abreviada: piso bem menor

STORY_BRAND = dict(style="story_brand", cap=58, baseline=1560, left=90, max_w=472,
                   gradient=((232, 10, 16), (105, 0, 2)))
# modelo: 1 linha grande se for curto; nomes compostos vão em 2 linhas (como "COROLLA / CROSS")
STORY_MODEL = dict(style="story_model", cap=82, baseline=1663, left=118, max_w=442, color=(20, 20, 23))
STORY_MODEL_2L = dict(cap=40, gap=9, min_shrink=0.5)
STORY_KM = dict(style="story_value", cap=26, baseline=1589, left=780, max_w=270, color=(18, 18, 20))
STORY_YEAR = dict(style="story_value", cap=26, baseline=1652, left=772, max_w=278, color=(18, 18, 20))
STORY_VERSION = dict(style="story_version", cap=25, baseline=1857, center_x=540, max_w=850, color=(30, 28, 33))
STORY_VERSION_LINE = dict(y=1843, thickness=4, x0=51, x1=1029, gap=20, color=(60, 59, 64), min_len=36)
STORY_PRICE = dict(style="story_price", cap=89, baseline=1793, center_x=638, max_w=680, color=(255, 255, 255))
STORY_CENTS = dict(style="story_cents", cap_ratio=46 / 89, raise_ratio=13 / 89, gap_ratio=13 / 89)
STORY_CONSULT = dict(style="story_price", cap=78, baseline=1788, center_x=638, max_w=665, color=(255, 255, 255))
STORY_BADGE_TOP = 166  # origem vertical dos selos (referência: escudo começa em y~182)


@lru_cache(maxsize=1)
def _story_layers() -> tuple:
    fundo = Image.open(ASSETS / "story_fundo.png").convert("RGB")
    frente = Image.open(ASSETS / "story_frente.png").convert("RGBA")
    m1 = Image.open(ASSETS / "story_mascara1.png").convert("L")
    m2 = Image.open(ASSETS / "story_mascara2.png").convert("L")
    box = lambda m: m.point(lambda v: 255 if v > 8 else 0).getbbox()  # noqa: E731  (inclui a rampa da fusão)
    return fundo, frente, m1, m2, box(m1), box(m2)


def _ink(text: str, style: str, size: int) -> tuple[Image.Image, int, int]:
    """(tinta recortada na horizontal, pad, altura da caixa-alta); topo da caixa-alta em y=pad."""
    mask, pad = _text_mask(text, style, size)
    box = mask.getbbox()
    cap_px = round(_cap_ratio(style) * size)
    if not box:
        return Image.new("L", (1, mask.height), 0), pad, cap_px
    return mask.crop((box[0], 0, box[2], mask.height)), pad, cap_px


def _fit_story(text: str, style: str, cap: int, max_w: int, min_shrink: float | None = None) -> tuple[str, int]:
    """Texto e tamanho de fonte: caixa-alta `cap`, encolhida até caber em `max_w`; se nem no
    mínimo couber, abrevia por palavras com "…" (nunca invade a outra coluna)."""
    size0 = max(8, round(cap / _cap_ratio(style)))
    if min_shrink is None:
        min_shrink = STORY_MIN_SHRINK_BY_STYLE.get(style, STORY_MIN_SHRINK)
    min_size = max(8, round(size0 * min_shrink))
    words = text.split()
    while True:
        for size in range(size0, min_size - 1, -1):
            if _ink(text, style, size)[0].width <= max_w:
                return text, size
        if len(words) > 1:
            words.pop()
            text = " ".join(words) + "…"
        elif len(text) > 2:  # palavra única enorme: corta caracteres
            text = text.rstrip("…")[:-1] + "…"
        else:
            return text, min_size


def _fits(text: str, style: str, cap: int, max_w: int, floor: float) -> int | None:
    """Maior tamanho de fonte (cap -> até floor*cap) em que `text` cabe em `max_w`; None se não couber."""
    size0 = max(8, round(cap / _cap_ratio(style)))
    for size in range(size0, max(8, round(size0 * floor)) - 1, -1):
        if _ink(text, style, size)[0].width <= max_w:
            return size
    return None


def _story_model(img: Image.Image, text: str) -> None:
    """Modelo em caixa alta, sempre dentro da coluna da esquerda. Ordem: 1 linha (encolhe até 60%);
    2 linhas (melhor quebra de palavras); 1 linha menor (até 30%); só em caso extremo abrevia com "…"."""
    M, L2 = STORY_MODEL, STORY_MODEL_2L
    if not text:
        return
    if _fits(text, M["style"], M["cap"], M["max_w"], 0.75) is None:
        words = text.split()
        best = None
        for i in range(1, len(words)):
            a, b = " ".join(words[:i]), " ".join(words[i:])
            sa = _fits(a, M["style"], L2["cap"], M["max_w"], L2["min_shrink"])
            sb = _fits(b, M["style"], L2["cap"], M["max_w"], L2["min_shrink"])
            if sa and sb and (best is None or min(sa, sb) > best[0]):
                best = (min(sa, sb), a, b)
        if best:
            size, a, b = best
            for line, base in ((b, M["baseline"]), (a, M["baseline"] - L2["cap"] - L2["gap"])):
                ink, pad, cap_px = _ink(line, M["style"], size)
                img.paste(Image.new("RGB", ink.size, M["color"]), (M["left"], base - cap_px - pad), ink)
            return
    _story_text(img, text, **{**M, "min_shrink": 0.3})


def _story_brand(img: Image.Image, text: str) -> None:
    """Marca nunca abreviada: encolhe até 20%; se ainda não couber, 2 linhas (hífen/espaço) na mesma faixa."""
    B = STORY_BRAND
    if not text:
        return
    if _fits(text, B["style"], B["cap"], B["max_w"], 0.2) is None:
        parts = text.replace("-", "- ").split()
        if len(parts) > 1:
            best = None
            for i in range(1, len(parts)):
                a, b = "".join(parts[:i]) if parts[i - 1].endswith("-") else " ".join(parts[:i]), " ".join(parts[i:])
                s1, s2 = (_fits(t, B["style"], 28, B["max_w"], 0.4) for t in (a, b))
                if s1 and s2 and (best is None or min(s1, s2) > best[0]):
                    best = (min(s1, s2), a, b)
            if best:
                size, a, b = best
                for line, base in ((b, B["baseline"]), (a, B["baseline"] - 28 - 6)):
                    ink, pad, cap_px = _ink(line, B["style"], size)
                    img.paste(_gradient(ink.size, *B["gradient"]), (B["left"], base - cap_px - pad), ink)
                return
    _story_text(img, text, **B)


def _gradient(size: tuple[int, int], top, bottom) -> Image.Image:
    w, h = size
    t = Image.linear_gradient("L").resize((w, h), Image.Resampling.BILINEAR)  # 0 (topo) -> 255 (base)
    return Image.composite(Image.new("RGB", size, bottom), Image.new("RGB", size, top), t)


def _story_text(img: Image.Image, text: str, *, style: str, cap: int, baseline: int, max_w: int,
                color=None, gradient=None, left: int | None = None, center_x: int | None = None,
                min_shrink: float | None = None, **_) -> None:
    if not text:
        return
    text, size = _fit_story(text, style, cap, max_w, min_shrink)
    ink, pad, cap_px = _ink(text, style, size)
    x = left if left is not None else round(center_x - ink.width / 2)
    fill = _gradient(ink.size, *gradient) if gradient else Image.new("RGB", ink.size, color)
    img.paste(fill, (x, baseline - cap_px - pad), ink)


def _story_price(img: Image.Image, price: str | None) -> None:
    if price is None:
        _story_text(img, "CONSULTE", **STORY_CONSULT)
        return
    P, C = STORY_PRICE, STORY_CENTS
    for cap in range(P["cap"], 20, -2):  # encolhe o grupo "154.900," + "00" até caber
        size = max(8, round(cap / _cap_ratio(P["style"])))
        csize = max(8, round(cap * C["cap_ratio"] / _cap_ratio(C["style"])))
        big, bpad, bcap = _ink(price + ",", P["style"], size)
        cents, cpad, ccap = _ink("00", C["style"], csize)
        gap = round(cap * C["gap_ratio"])
        total = big.width + gap + cents.width
        if total <= P["max_w"]:
            break
    x = round(P["center_x"] - total / 2)
    img.paste(Image.new("RGB", big.size, P["color"]), (x, P["baseline"] - bcap - bpad), big)
    cb = P["baseline"] - round(cap * C["raise_ratio"])
    img.paste(Image.new("RGB", cents.size, P["color"]), (x + big.width + gap, cb - ccap - cpad), cents)


def _story_version(img: Image.Image, version: str) -> None:
    V, L = STORY_VERSION, STORY_VERSION_LINE
    text, size = _fit_story(version, V["style"], V["cap"], V["max_w"])
    ink, pad, cap_px = _ink(text, V["style"], size)
    x = round(V["center_x"] - ink.width / 2)
    img.paste(Image.new("RGB", ink.size, V["color"]), (x, V["baseline"] - cap_px - pad), ink)
    # linhas dos lados acompanham a largura do texto (como no molde)
    draw = ImageDraw.Draw(img)
    for x0, x1 in ((L["x0"], x - L["gap"]), (x + ink.width + L["gap"], L["x1"])):
        if x1 - x0 >= L["min_len"]:
            draw.rectangle((x0, L["y"], x1 - 1, L["y"] + L["thickness"] - 1), fill=L["color"])


def _story_badge_box(box: tuple[int, int, int, int], slot: tuple) -> tuple[int, int, int, int]:
    """Caixa do selo do feed (relativa à moldura da capa) levada à foto de cima do Stories,
    na mesma proporção em relação à largura da foto."""
    fx0, fy0, fx1, fy1 = COVER_PHOTO_BOX
    sx0, sy0, sx1, _ = slot
    k = (sx1 - sx0) / (fx1 - fx0)
    return tuple(round(v) for v in (sx0 + (box[0] - fx0) * k, sy0 + (box[1] - fy0) * k,
                                    sx0 + (box[2] - fx0) * k, sy0 + (box[3] - fy0) * k))


def _paste_story_badges(img: Image.Image, detail, slot: tuple) -> Image.Image:
    badges = cover_badges(detail)  # mesma regra do feed: armored / km == 0
    if not badges:
        return img
    out = img.convert("RGBA")
    for name, box in badges:
        badge, pos = _badge(name, _story_badge_box(box, slot))
        out.alpha_composite(badge, pos)
    return out.convert("RGB")


def story_frames(armored: bool = False) -> dict:
    """Caixas das fotos (px) e enquadramento padrão DO CARRO, para o editor montar a prévia local."""
    _, _, _, _, box1, box2 = _story_layers()
    cx0, cy0, cx1, cy1 = COVER_PHOTO_BOX

    def frame(w, h, adj):
        return {"w": w, "h": h, "z": adj[0], "cx": adj[1], "cy": adj[2]}

    top_adj, int_adj = story_default_adj(armored)
    return {"story_top": frame(box1[2] - box1[0], box1[3] - box1[1], top_adj),
            "story_int": frame(box2[2] - box2[0], box2[3] - box2[1], int_adj),
            "cover": frame(cx1 - cx0, cy1 - cy0, COVER_DEFAULT_ADJ)}


def render_story(detail, photo_top: Image.Image, photo_interior: Image.Image, adj_top=None, adj_int=None) -> Image.Image:
    """Stories 1080x1920: foto 1 (frente) + foto 2 (interior) + marca/modelo/KM/ano/preço/versão.
    As duas fotos se fundem em degradê e o topo da foto 1 some em degradê sob o cabeçalho (assets)."""
    fundo, frente, m1, m2, box1, box2 = _story_layers()
    img = fundo.copy()
    def_top, def_int = story_default_adj(bool(getattr(detail, "armored", False)))
    for photo, mask, box, adj, default in ((photo_top, m1, box1, adj_top, def_top),
                                           (photo_interior, m2, box2, adj_int, def_int)):
        x0, y0, x1, y1 = box
        z, cx, cy = norm_adj(adj, default)
        layer = Image.new("RGB", STORY_SIZE, (0, 0, 0))
        layer.paste(cover_fit(photo, (x1 - x0, y1 - y0), z, (cx, cy)), (x0, y0))
        img.paste(layer, (0, 0), mask)
    img = Image.alpha_composite(img.convert("RGBA"), frente).convert("RGB")
    img = _paste_story_badges(img, detail, (box1[0], STORY_BADGE_TOP, box1[2], box1[3]))

    _story_brand(img, fmt_brand(detail.brand))
    _story_model(img, " ".join((detail.model or "").upper().split()))
    _story_text(img, f"KM: {fmt_km(detail.km)}", **STORY_KM)
    _story_text(img, f"ANO: {fmt_years(detail.year_fab, detail.year_model)}", **STORY_YEAR)
    _story_price(img, fmt_price(detail.price_cents))
    version = " ".join((detail.version or "").upper().split())
    if version:
        _story_version(img, version)
    return img


_STORY_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
_STORY_CACHE_MAX = 12  # ~3 MB por PNG
_story_lock = threading.Lock()


def story_png(detail, url_top: str, url_interior: str, adj_top=None, adj_int=None) -> bytes:
    """PNG do Stories, com cache LRU em memória (chave: fotos + textos que entram na arte)."""
    from .photos import load_photo

    def_top, def_int = story_default_adj(bool(getattr(detail, "armored", False)))
    adj_top, adj_int = norm_adj(adj_top, def_top), norm_adj(adj_int, def_int)
    key = (url_top, url_interior, adj_top, adj_int, detail.brand, detail.model, detail.version, detail.year_fab,
           detail.year_model, detail.km, detail.price_cents, tuple(n for n, _ in cover_badges(detail)))
    with _story_lock:
        if key in _STORY_CACHE:
            _STORY_CACHE.move_to_end(key)
            return _STORY_CACHE[key]
    top = load_photo(url_top)
    interior = top if url_interior == url_top else load_photo(url_interior)
    data = to_png(render_story(detail, top, interior, adj_top, adj_int))
    with _story_lock:
        _STORY_CACHE[key] = data
        while len(_STORY_CACHE) > _STORY_CACHE_MAX:
            _STORY_CACHE.popitem(last=False)
    return data
