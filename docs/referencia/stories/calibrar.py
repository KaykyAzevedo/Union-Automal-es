"""Calibração do Stories v2: renderiza o Corolla Cross da referência e compara com story_exemplo_v2.jpg.

Uso (raiz do projeto, Python com Pillow e numpy — ferramenta de dev, fora do requirements):
  python docs/referencia/stories/calibrar.py
As fotos usadas no render são recortes da própria referência (nas caixas das fotos), para comparar
posição, tamanho e textos. Saída: docs/referencia/stories/calibracao_v2.png
(molde | render | diferença x3, lado a lado).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageChops, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent.parent))

from app.encarte import render  # noqa: E402

SIZE = (1080, 1920)


def main() -> None:
    molde = Image.open(HERE / "story_exemplo_v2.jpg").convert("RGB").resize(SIZE, Image.Resampling.LANCZOS)
    _, _, _, _, box1, box2 = render._story_layers()
    detail = SimpleNamespace(brand="Toyota", model="COROLLA CROSS", version="2.0 VVT-IE FLEX XRE DIRECT SHIFT",
                             km=25000, year_fab=2025, year_model=2026, price_cents=21990000, armored=True)
    # recortes da própria referência: enquadramento neutro (não o padrão de blindado, que é para fotos do site)
    out = render.render_story(detail, molde.crop(box1), molde.crop(box2), render.STORY_DEFAULT_ADJ,
                              render.STORY_DEFAULT_ADJ)
    diff = ImageChops.difference(molde, out).point(lambda v: min(255, v * 3))
    sheet = Image.new("RGB", (SIZE[0] * 3 + 40, SIZE[1] + 30), "white")
    d = ImageDraw.Draw(sheet)
    for i, (label, img) in enumerate((("molde (v2)", molde), ("render", out), ("diferenca x3", diff))):
        x = i * (SIZE[0] + 20)
        d.text((x + 8, 8), label, fill=(200, 0, 0))
        sheet.paste(img, (x, 30))
    sheet.resize((sheet.width // 2, sheet.height // 2), Image.Resampling.LANCZOS).save(HERE / "calibracao_v2.png")
    a, b = np.array(molde).astype(int), np.array(out).astype(int)
    region = (slice(1480, 1880), slice(0, 1080))
    print("erro medio abs (bloco de texto):", abs(a[region] - b[region]).mean().round(2))


if __name__ == "__main__":
    main()
