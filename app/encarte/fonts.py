"""Fontes do encarte — ÚNICO lugar para trocar tipografia.

Ordem de busca por estilo:
1. TT Lakes Neue em app/assets/fonts/ (primeiro arquivo que casar com os padrões
   do estilo; se o estilo pede itálico e existir um arquivo itálico, não há
   inclinação sintética);
2. Bahnschrift variável do Windows (peso/largura via eixos);
3. fonte padrão do Pillow (último recurso, só para não quebrar).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
FALLBACK_FONT = Path(os.environ.get("UNION_FALLBACK_FONT", "C:/Windows/Fonts/bahnschrift.ttf"))
LAKES_GLOB = "*Lakes*"


@dataclass(frozen=True)
class FontStyle:
    lakes_patterns: tuple[str, ...]  # substrings (minúsculas) do nome do arquivo, em ordem de preferência
    weight: int                      # eixo wght da Bahnschrift (300–700)
    width: int = 100                 # eixo wdth da Bahnschrift (75–100)
    italic: bool = False             # inclinar (sintético se a fonte não for itálica)
    stretch: float = 1.0             # alargamento horizontal só no fallback (Lakes Neue já é larga)


FONTS: dict[str, FontStyle] = {
    # stretch calibrado para a Bahnschrift igualar as larguras da capa_exemplo
    "brand":   FontStyle(("extrabold", "bold"), weight=700, stretch=1.17),
    "model":   FontStyle(("bold",), weight=700, stretch=1.10),
    "version": FontStyle(("light", "thin", "regular"), weight=300),
    "value":   FontStyle(("regular", "medium"), weight=400, stretch=1.15),
    "price":   FontStyle(("black italic", "extrabold italic", "bold italic", "black", "extrabold", "bold"),
                         weight=700, italic=True, stretch=1.45),
}


def _lakes_files() -> list[Path]:
    if not FONT_DIR.is_dir():
        return []
    return sorted(p for p in FONT_DIR.glob(LAKES_GLOB) if p.suffix.lower() in (".ttf", ".otf"))


def _norm(p: Path) -> str:
    return p.stem.lower().replace("-", " ").replace("_", " ")


def _pick_lakes(style: FontStyle) -> Path | None:
    files = _lakes_files()
    for pattern in style.lakes_patterns:
        for f in files:
            name = _norm(f)
            if pattern in name and ("italic" in name) == ("italic" in pattern):
                return f
    return None


@lru_cache(maxsize=64)
def get_font(style_name: str, size: int) -> tuple[ImageFont.FreeTypeFont, bool, float]:
    """Retorna (fonte, precisa_inclinar, alargamento_horizontal)."""
    style = FONTS[style_name]
    lakes = _pick_lakes(style)
    if lakes is not None:
        return ImageFont.truetype(str(lakes), size), style.italic and "italic" not in _norm(lakes), 1.0
    if FALLBACK_FONT.exists():
        font = ImageFont.truetype(str(FALLBACK_FONT), size)
        try:
            font.set_variation_by_axes([style.weight, style.width])
        except OSError:  # fonte não variável
            pass
        return font, style.italic, style.stretch
    return ImageFont.load_default(size), style.italic, style.stretch


def font_source() -> str:
    """Descrição da fonte em uso (para log/diagnóstico)."""
    return "TT Lakes Neue" if _lakes_files() else (str(FALLBACK_FONT) if FALLBACK_FONT.exists() else "Pillow default")
