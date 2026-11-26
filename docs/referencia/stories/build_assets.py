"""Gera os assets do Stories v2 (1080x1920) reaproveitando as peças da v1 desta pasta.

Uso (da raiz do projeto, Python com Pillow e numpy — ferramenta de dev, fora do requirements):
  python docs/referencia/stories/build_assets.py

Entradas (941x1672): frente.png (ícones) e referencia_original.png (molde v1 achatado: cabeçalho,
barra do preço e swooshes). A referência v2 (story_exemplo_v2.jpg, JPEG de WhatsApp) é só MEDIDA,
nunca camada. Saídas em app/assets/encarte/: story_fundo.png, story_frente.png,
story_mascara1.png, story_mascara2.png (1080x1920).

Layout v2 (px em 1080x1920): cabeçalho UNION um pouco menor e mais alto, SEM rodapé; foto de cima y 192..922,
foto do interior 852..1505 (fusão curta 852..922); o interior some em degradê 1405..1505 no bloco
de texto cinza com swooshes vermelhos nos cantos inferiores; ícones, divisória e barra do preço
reposicionados. Textos são desenhados no código (app/encarte/render.py).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
OUT = HERE.parent.parent.parent / "app" / "assets" / "encarte"
W, H = 1080, 1920

# cabeçalho
HEADER_SCALE = (0.945, 3.0)  # cabeçalho da v1 um pouco menor na vertical: y_v2 = 0.945 * y_v1 + 3
PHOTO_TOP = 130             # a foto de cima começa aqui (aparece nas quinas, por baixo do cabeçalho)
HEADER_BOTTOM = 192         # cabeçalho opaco (no centro) até aqui
TOP_FADE = 70               # degradê cabeçalho -> foto
# fotos
SEAM_FADE = (852, 922)      # crossfade direto foto 1 -> foto 2 (centro ~887)
BOTTOM_FADE = (1405, 1505)  # interior some no bloco de texto
# bloco de texto (caixas medidas na referência)
BAR_BOX_V1 = (60, 1510, 1020, 1637)   # contorno da barra do preço na v1 (1080)
BAR_BOX_V2 = (45, 1686, 1037, 1812)
SPEEDO_V2 = (656, 1533, 743, 1596)
CAL_V2 = (662, 1604, 733, 1663)
VLINE = dict(x=586, y0=1535, y1=1665, width=4, color=(160, 22, 28))


def smoothstep(t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3 - 2 * t)


def load(name: str, mode: str) -> np.ndarray:
    return np.array(Image.open(HERE / name).convert(mode)).astype(np.float32)


def to_img(arr: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(arr + 0.5, 0, 255).astype(np.uint8))


def fill_holes(mask: Image.Image) -> Image.Image:
    """Preenche buracos internos de uma máscara L binária (flood fill a partir da borda)."""
    w, h = mask.size
    padded = Image.new("L", (w + 2, h + 2), 0)
    padded.paste(mask, (1, 1))
    ImageDraw.floodfill(padded, (0, 0), 128)
    arr = np.array(padded)[1:-1, 1:-1]
    return Image.fromarray(np.where(arr == 128, 0, 255).astype(np.uint8))


def price_bar_v1(orig: np.ndarray) -> Image.Image:
    """Barra do preço (RGBA, recorte 941 x40..900 y1296..1428): "R$" e borda originais, dígitos
    apagados e painel escuro repintado com o gradiente original."""
    X0, X1, Y0, Y1 = 40, 900, 1296, 1428
    bar = orig[Y0:Y1, X0:X1, :3].copy()
    PX0, PX1, TOP = 266, 843, 1318

    def bottom_border(x: float) -> float:  # início da borda inferior: ~1415, sobe até 1408 perto da ponta direita
        return 1415 - max(0.0, x - 790) * 7 / 56

    def profile(cols: tuple[int, int], b: float) -> np.ndarray:
        return np.median(orig[TOP:int(b) - 1, cols[0]:cols[1], :3], axis=1)

    left = profile((262, 265), bottom_border(262))
    right = profile((845, 848), bottom_border(846))

    region = orig[TOP:1422, PX0:PX1, :3]
    bright = Image.fromarray((region.min(axis=2) > 50).astype(np.uint8) * 255)
    bright = bright.filter(ImageFilter.MaxFilter(11)).filter(ImageFilter.GaussianBlur(1.2))
    a = np.array(bright).astype(np.float32)[..., None] / 255.0
    for x in range(PX0, PX1):
        b = bottom_border(x)
        ys = np.arange(TOP, int(b) - 1)
        t = (ys - TOP) / (b - TOP)
        wx = (x - 263.5) / (846.5 - 263.5)
        col = np.stack([np.interp(t, np.linspace(0, 1, len(left)), left[:, c]) * (1 - wx)
                        + np.interp(t, np.linspace(0, 1, len(right)), right[:, c]) * wx for c in range(3)], axis=1)
        sl = slice(ys[0] - Y0, ys[-1] + 1 - Y0)
        aa = a[ys[0] - TOP:ys[-1] + 1 - TOP, x - PX0]
        bar[sl, x - X0] = bar[sl, x - X0] * (1 - aa) + col * aa

    nonbg = ((bar.max(axis=2) < 195) | (bar[..., 0] - bar[..., 1] > 45)).astype(np.uint8) * 255
    m = fill_holes(Image.fromarray(nonbg))
    # só o maior corpo (a barra): descarta pontinhas de textura do fundo fora do contorno vermelho
    mw, mh = m.size
    pad = Image.new("L", (mw + 2, mh + 2), 0)
    pad.paste(m, (1, 1))
    ImageDraw.floodfill(pad, (400 - X0 + 1, 1360 - Y0 + 1), 128)
    m = Image.fromarray(np.where(np.array(pad)[1:-1, 1:-1] == 128, 255, 0).astype(np.uint8))
    m = m.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(0.7))
    alpha = np.array(m).astype(np.float32)
    alpha[:, :50 - X0] = 0   # texturas do fundo fora da ponta esquerda da barra
    alpha[:, 892 - X0:] = 0
    out = to_img(np.dstack([bar, alpha]))
    # escala para 1080x1920 e recorta no contorno (BAR_BOX_V1)
    sx, sy = W / 941, H / 1672
    full = Image.new("RGBA", (W, H))
    full.paste(out.resize((round((X1 - X0) * sx), round((Y1 - Y0) * sy)), Image.Resampling.LANCZOS),
               (round(X0 * sx), round(Y0 * sy)))
    return full


def paste_bar(frente: Image.Image, orig: np.ndarray) -> None:
    full = price_bar_v1(orig)
    (ax0, ay0, ax1, ay1), (bx0, by0, bx1, by1) = BAR_BOX_V1, BAR_BOX_V2
    kx, ky = (bx1 - bx0) / (ax1 - ax0), (by1 - by0) / (ay1 - ay0)
    m = 8  # margem para a borda suavizada
    crop = full.crop((ax0 - m, ay0 - m, ax1 + m, ay1 + m))
    crop = crop.resize((round(crop.width * kx), round(crop.height * ky)), Image.Resampling.LANCZOS)
    frente.alpha_composite(crop, (round(bx0 - m * kx), round(by0 - m * ky)))


def paste_icon(frente: Image.Image, src: np.ndarray, box: tuple, dest: tuple) -> None:
    """Ícone preto da frente.png (941) recolocado em `dest` (1080); alfa pela escuridão."""
    x0, y0, x1, y1 = box
    lum = src[y0:y1, x0:x1, :3].mean(axis=2)
    alpha = np.clip((235 - lum) / (235 - 45), 0, 1) * 255
    icon = Image.new("RGBA", (x1 - x0, y1 - y0), (20, 19, 21, 0))
    icon.putalpha(to_img(alpha))
    icon = icon.resize((dest[2] - dest[0], dest[3] - dest[1]), Image.Resampling.LANCZOS)
    frente.alpha_composite(icon, (dest[0], dest[1]))


def photo_wedges(head: np.ndarray) -> np.ndarray:
    """0..1: onde o molde v1 mostra a foto antiga (bege/marrom) nas quinas inferiores do cabeçalho."""
    r, g, b = head[..., 0], head[..., 1], head[..., 2]
    brown = (r - b > 14) & (g > r * 0.68) & (head.mean(axis=2) < 232)
    brown[:PHOTO_TOP] = False
    brown[:, 244:886] = False  # o centro é só cabeçalho (UNION / VEÍCULOS)
    m = to_img(brown.astype(np.float32) * 255).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(3))
    return np.array(m.filter(ImageFilter.GaussianBlur(0.8)), np.float32) / 255


def text_background() -> np.ndarray:
    """Cinza claro do bloco de texto: centro branco-acinzentado, laterais e base mais escuras + grão."""
    x = np.arange(W, dtype=np.float32)[None, :]
    y = np.arange(H, dtype=np.float32)[:, None]
    side = smoothstep((np.abs(x - 540) - 250) / 300)
    base = 238 - 92 * side - 42 * smoothstep((y - 1850) / 70) - 10 * smoothstep((1560 - y) / 100)
    noise = np.random.default_rng(7).normal(0, 1.6, (H // 2, W // 2)).astype(np.float32)
    noise = np.kron(noise, np.ones((2, 2), np.float32))
    lum = base + noise
    return np.dstack([lum, lum - 1, lum + 1])


def corner_swoosh(header: np.ndarray, flip_h: bool) -> Image.Image:
    """Swoosh vermelho/cinza do canto sup. esq. do cabeçalho, espelhado para um canto inferior."""
    cw, ch = 244, 170                      # antes das letras do UNION e da quina de foto do molde
    piece = header[:ch, :cw]
    x = np.arange(cw, dtype=np.float32)[None, :]
    y = np.arange(ch, dtype=np.float32)[:, None]
    d = x / cw + y / ch
    alpha = (1 - smoothstep((d - 0.80) / 0.40)) * 255
    alpha = np.minimum(alpha, (1 - smoothstep((x - (cw - 30)) / 30)) * 255)
    alpha = np.minimum(alpha, (1 - smoothstep((y - (ch - 30)) / 30)) * 255)
    img = to_img(np.dstack([piece, alpha]))
    img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    return img.transpose(Image.Transpose.FLIP_LEFT_RIGHT) if flip_h else img


def build() -> None:
    orig = load("referencia_original.png", "RGB")
    icons = load("frente.png", "RGBA")
    orig_big = np.array(Image.open(HERE / "referencia_original.png").convert("RGB")
                        .resize((W, H), Image.Resampling.LANCZOS)).astype(np.float32)

    frente = np.zeros((H, W, 4), np.float32)
    # cabeçalho (molde v1, um pouco mais alto): as quinas com a foto antiga da loja ficam transparentes
    k, off = HEADER_SCALE
    src = np.clip((np.arange(HEADER_BOTTOM) - off) / k, 0, H - 1)
    lo = np.floor(src).astype(int)
    t = (src - lo)[:, None, None]
    head = orig_big[lo] * (1 - t) + orig_big[np.minimum(lo + 1, H - 1)] * t
    wedge = photo_wedges(head)
    frente[:HEADER_BOTTOM, :, :3] = head
    frente[:HEADER_BOTTOM, :, 3] = 255 * (1 - wedge)
    # degradê cabeçalho -> foto: continua a cor da última linha branca; nas quinas a foto aparece direto
    white = 1 - np.array(to_img(wedge[HEADER_BOTTOM - 1:HEADER_BOTTOM] * 255)
                         .filter(ImageFilter.GaussianBlur(28)), np.float32) / 255
    row = head[-1:].copy()
    clean = wedge[-1] < 0.05
    for c in range(3):  # colunas das quinas herdam o branco vizinho (nada de marrom no degradê)
        row[0, :, c] = np.interp(np.arange(W), np.arange(W)[clean], row[0, clean, c])
    last = np.array(to_img(row).filter(ImageFilter.GaussianBlur(4)), np.float32)
    band = slice(HEADER_BOTTOM, HEADER_BOTTOM + TOP_FADE)
    frente[band, :, :3] = last
    frente[band, :, 3] = (1 - smoothstep(np.arange(TOP_FADE) / TOP_FADE))[:, None] * 255 * white

    # bloco de texto: o interior some em degradê
    y0, y1 = BOTTOM_FADE
    bg = text_background()
    frente[y0:, :, :3] = bg[y0:]
    frente[y0:y1, :, 3] = (smoothstep((np.arange(y0, y1) - y0) / (y1 - y0)) * 255)[:, None]
    frente[y1:, :, 3] = 255

    img = to_img(frente).convert("RGBA")
    # swooshes nos cantos inferiores (cabeçalho espelhado): esquerdo menor, direito maior
    for flip_h, k in ((False, 0.72), (True, 1.05)):
        piece = corner_swoosh(orig_big, flip_h)
        piece = piece.resize((round(piece.width * k), round(piece.height * k)), Image.Resampling.LANCZOS)
        img.alpha_composite(piece, (W - piece.width if flip_h else 0, H - piece.height))

    paste_bar(img, orig)
    paste_icon(img, icons, (572, 1179, 652, 1233), SPEEDO_V2)
    paste_icon(img, icons, (584, 1240, 646, 1286), CAL_V2)
    # linha vertical vermelha (marca | KM/ANO)
    v = VLINE
    ImageDraw.Draw(img).rounded_rectangle((v["x"], v["y0"], v["x"] + v["width"] - 1, v["y1"]), radius=2,
                                          fill=v["color"] + (255,))

    # máscaras das fotos
    m1 = np.zeros((H, W), np.float32)
    m2 = np.zeros((H, W), np.float32)
    s0, s1 = SEAM_FADE
    m1[PHOTO_TOP:s1] = 255
    m2[s0:s1] = (smoothstep((np.arange(s0, s1) - s0) / (s1 - s0)) * 255)[:, None]
    m2[s1:BOTTOM_FADE[1]] = 255

    OUT.mkdir(parents=True, exist_ok=True)
    img.save(OUT / "story_frente.png", optimize=True)
    to_img(bg).save(OUT / "story_fundo.png", optimize=True)
    to_img(m1).save(OUT / "story_mascara1.png", optimize=True)
    to_img(m2).save(OUT / "story_mascara2.png", optimize=True)
    print("ok ->", OUT)


if __name__ == "__main__":
    build()
