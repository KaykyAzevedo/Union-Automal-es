"""Calibração do Stories: renderiza o GS3 do molde e compara com story_exemplo_gs3.png (1080x1920).

Uso (raiz do projeto, Python com Pillow e numpy — ferramenta de dev, fora do requirements):
  python docs/referencia/stories/calibrar.py
Saída: docs/referencia/stories/calibracao.png (molde | render | diferença x3, região dos textos
+ visão geral lado a lado) e as caixas de tinta medidas dos dois.
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
    molde = Image.open(HERE / "story_exemplo_gs3.png").convert("RGB").resize(SIZE, Image.Resampling.LANCZOS)
    _, _, _, _, box1, box2 = render._story_layers()
    detail = SimpleNamespace(brand="Gac", model="GS3", version="1.5 TURBO GASOLINA ELITE DCT", km=500,
                             year_fab=2026, year_model=2027, price_cents=15490000, armored=False)
    out = render.render_story(detail, molde.crop(box1), molde.crop(box2))

    diff = ImageChops.difference(molde, out).point(lambda v: min(255, v * 3))
    y0, y1 = 1290, 1720
    panels = [molde.crop((0, y0, 1080, y1)), out.crop((0, y0, 1080, y1)), diff.crop((0, y0, 1080, y1))]
    sheet = Image.new("RGB", (1080, 3 * (y1 - y0 + 24)), "white")
    d = ImageDraw.Draw(sheet)
    y = 0
    for label, p in zip(("molde", "render", "diferenca x3"), panels):
        d.text((6, y + 4), label, fill=(200, 0, 0))
        sheet.paste(p, (0, y + 24))
        y += p.height + 24
    sheet.save(HERE / "calibracao.png")

    a, b = np.array(molde).astype(int), np.array(out).astype(int)
    region = (slice(1300, 1720), slice(0, 1080))
    print("erro medio abs (regiao textos):", abs(a[region] - b[region]).mean().round(2))


if __name__ == "__main__":
    main()
