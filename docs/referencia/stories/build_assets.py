"""Gera os assets do Stories (1080x1920) a partir das camadas 941x1672 desta pasta.

Uso (da raiz do projeto, Python com Pillow e numpy — ferramenta de dev, fora do requirements):
  python docs/referencia/stories/build_assets.py

Entradas (941x1672): frente.png, mascara_foto1.png, mascara_foto2.png, fundo.png e
referencia_original.png (molde achatado, usado para recuperar o que o ChatGPT estragou).
Saídas em app/assets/encarte/: story_fundo.png, story_frente.png, story_mascara1.png,
story_mascara2.png (todos 1080x1920, LANCZOS).

Correções feitas na frente.png:
 a) cabeçalho completo (inclui "VEÍCULOS") copiado do original até y=HEADER_BOTTOM; a máscara 1
    desce até ali para o cabeçalho ficar opaco por cima da foto;
 b) pedaço de foto antiga acima do velocímetro apagado;
 c) barra do preço refeita a partir do original (inclinada, "R$" e borda metálica originais),
    com os dígitos apagados e o painel escuro repintado com o mesmo gradiente;
 d) "KM:"/"ANO:" e as linhas laterais da versão removidos (são desenhados no código);
 + linha vertical vermelha entre marca e KM/ANO (faltava na frente.png);
 + máscara 2 estendida até onde o degradê branco da frente fica opaco (sem corte seco).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
OUT = HERE.parent.parent.parent / "app" / "assets" / "encarte"
OUT_SIZE = (1080, 1920)
BG = 247  # cinza liso da frente.png

HEADER_BOTTOM = 172      # cabeçalho opaco até aqui (px de 941x1672); máscara 1 começa aqui
MASK2_BOTTOM = 1168      # frente fica 100% opaca a partir daqui
TOP_FADE = 78            # degradê da foto de cima para o cabeçalho: frente opaca -> transparente (px de 941)
SEAM = 706               # emenda original foto 1 | foto 2
SEAM_FADE = (681, 742)   # fusão curta das duas fotos (~70 px em 1080, crossfade direto): máscara 2 sobe de 0 a 255


def smoothstep(t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3 - 2 * t)


def load(name: str, mode: str) -> np.ndarray:
    return np.array(Image.open(HERE / name).convert(mode)).astype(np.float32)


def fill_bg(rgba: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> None:
    rgba[y0:y1, x0:x1, :3] = BG  # alfa intacto (o degradê da foto 2 vai até y=1168)


def fill_holes(mask: Image.Image) -> Image.Image:
    """Preenche buracos internos de uma máscara L binária (flood fill a partir da borda)."""
    m = mask.copy()
    w, h = m.size
    padded = Image.new("L", (w + 2, h + 2), 0)
    padded.paste(m, (1, 1))
    ImageDraw.floodfill(padded, (0, 0), 128)
    arr = np.array(padded)[1:-1, 1:-1]
    return Image.fromarray(np.where(arr == 128, 0, 255).astype(np.uint8))


def rebuild_price_bar(rgba: np.ndarray, orig: np.ndarray) -> None:
    X0, X1, Y0, Y1 = 40, 900, 1296, 1428
    fill_bg(rgba, X0, 1288, X1, Y1)  # some a barra reta do ChatGPT

    bar = orig[Y0:Y1, X0:X1, :3].copy()

    # --- apaga os dígitos (painel escuro, x 266..843) ---
    PX0, PX1, TOP = 266, 843, 1318
    def bottom_border(x: float) -> float:  # início da borda inferior: ~1415, sobe até 1408 perto da ponta direita
        return 1415 - max(0.0, x - 790) * 7 / 56

    def profile(cols: tuple[int, int], b: float) -> np.ndarray:
        ys = np.arange(TOP, int(b) - 1)
        return np.median(orig[ys[0]:ys[-1] + 1, cols[0]:cols[1], :3], axis=1), ys

    left, _ = profile((262, 265), bottom_border(262))
    right, _ = profile((845, 848), bottom_border(846))

    def column_color(x: int, y: np.ndarray) -> np.ndarray:
        b = bottom_border(x)
        t = (y - TOP) / (b - TOP)
        out = np.zeros((len(y), 3), np.float32)
        wx = (x - 263.5) / (846.5 - 263.5)
        for c in range(3):
            pl = np.interp(t, np.linspace(0, 1, len(left)), left[:, c])
            pr = np.interp(t, np.linspace(0, 1, len(right)), right[:, c])
            out[:, c] = pl * (1 - wx) + pr * wx
        return out

    region = orig[TOP:1422, PX0:PX1, :3]
    bright = Image.fromarray((region.min(axis=2) > 50).astype(np.uint8) * 255)
    bright = bright.filter(ImageFilter.MaxFilter(11)).filter(ImageFilter.GaussianBlur(1.2))
    a = np.array(bright).astype(np.float32)[..., None] / 255.0
    for x in range(PX0, PX1):
        b = bottom_border(x)
        ys = np.arange(TOP, int(b) - 1)
        col = column_color(x, ys.astype(np.float32))
        sl = slice(ys[0] - Y0, ys[-1] + 1 - Y0)
        aa = a[ys[0] - TOP:ys[-1] + 1 - TOP, x - PX0]
        bar[sl, x - X0] = bar[sl, x - X0] * (1 - aa) + col * aa

    # --- máscara da barra (alfa) ---
    arr = orig[Y0:Y1, X0:X1, :3]
    nonbg = ((arr.max(axis=2) < 195) | (arr[..., 0] - arr[..., 1] > 45)).astype(np.uint8) * 255
    m = fill_holes(Image.fromarray(nonbg))
    # só o maior corpo (a barra): descarta pontinhas de textura do fundo fora do contorno vermelho
    seed = (400 - X0, 1360 - Y0)
    mw, mh = m.size
    pad = Image.new("L", (mw + 2, mh + 2), 0)
    pad.paste(m, (1, 1))
    ImageDraw.floodfill(pad, (seed[0] + 1, seed[1] + 1), 128)
    m = Image.fromarray(np.where(np.array(pad)[1:-1, 1:-1] == 128, 255, 0).astype(np.uint8))
    m = m.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(0.7))
    alpha = np.array(m).astype(np.float32)
    alpha[:, :50 - X0] = 0   # texturas do fundo fora da ponta esquerda da barra
    alpha[:, 886 - X0:] = 0
    alpha = alpha[..., None] / 255.0
    base = rgba[Y0:Y1, X0:X1, :3]
    rgba[Y0:Y1, X0:X1, :3] = base * (1 - alpha) + bar * alpha


def build() -> None:
    frente = load("frente.png", "RGBA")
    orig = load("referencia_original.png", "RGB")
    m1 = load("mascara_foto1.png", "L")
    m2 = load("mascara_foto2.png", "L")

    # a) cabeçalho completo
    frente[:HEADER_BOTTOM, :, :3] = orig[:HEADER_BOTTOM]
    frente[:HEADER_BOTTOM, :, 3] = 255
    m1[:HEADER_BOTTOM] = 0
    m2[:HEADER_BOTTOM] = 0
    # máscara 2 vai até o degradê ficar opaco
    m2[1124:MASK2_BOTTOM + 1] = 255
    # fusão entre as duas fotos: máscara 1 segue até o fim da faixa; máscara 2 entra em rampa
    y0, y1 = SEAM_FADE
    m1[HEADER_BOTTOM:y1] = 255
    m1[y1:] = 0
    ramp = smoothstep((np.arange(y0, y1) - y0) / (y1 - y0)) * 255
    m2[:y0] = 0
    m2[y0:y1] = ramp[:, None]
    m2[y1:MASK2_BOTTOM + 1] = 255
    # degradê no topo da foto 1: a frente continua o cabeçalho (cor da última linha, por coluna) e
    # some em rampa, deixando a foto aparecer por baixo do 'VEÍCULOS' (que fica acima de HEADER_BOTTOM)
    base = np.array(Image.fromarray(frente[HEADER_BOTTOM - 1:HEADER_BOTTOM, :, :3].astype(np.uint8))
                    .filter(ImageFilter.GaussianBlur(3))).astype(np.float32)
    band = slice(HEADER_BOTTOM, HEADER_BOTTOM + TOP_FADE)
    frente[band, :, :3] = base
    frente[band, :, 3] = ((1 - smoothstep(np.arange(TOP_FADE) / TOP_FADE)) * 255)[:, None]

    # b) foto antiga acima do velocímetro
    box = frente[1156:1198, 550:660, :3]
    dark = Image.fromarray((box.mean(axis=2) < 110).astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(5))
    keep = (np.array(dark) > 0)[..., None]
    frente[1156:1198, 550:660, :3] = np.where(keep, box, BG)
    # d) KM:/ANO: e linhas laterais da versão
    fill_bg(frente, 664, 1176, 800, 1272)
    fill_bg(frente, 55, 1428, 205, 1440)
    fill_bg(frente, 735, 1428, 885, 1440)
    # c) barra do preço
    rebuild_price_bar(frente, orig)

    # linha vertical vermelha (marca | KM/ANO), do original
    x0, x1, y0, y1 = 492, 502, 1172, 1294
    seg = orig[y0:y1, x0:x1, :3]
    a = np.clip((BG - seg[..., 1]) / (BG - 60.0), 0, 1)
    a = np.where(seg[..., 0] - seg[..., 1] > 25, a, 0)[..., None]
    frente[y0:y1, x0:x1, :3] = frente[y0:y1, x0:x1, :3] * (1 - a) + seg * a

    OUT.mkdir(parents=True, exist_ok=True)
    def save(arr: np.ndarray, mode: str, name: str) -> None:
        img = Image.fromarray(np.clip(arr + 0.5, 0, 255).astype(np.uint8)).convert(mode)
        img.resize(OUT_SIZE, Image.Resampling.LANCZOS).save(OUT / name, optimize=True)

    save(frente, "RGBA", "story_frente.png")
    save(load("fundo.png", "RGB"), "RGB", "story_fundo.png")
    save(m1, "L", "story_mascara1.png")
    save(m2, "L", "story_mascara2.png")
    print("ok ->", OUT)


if __name__ == "__main__":
    build()
