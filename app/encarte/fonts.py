"""Fontes do encarte — ÚNICO lugar para trocar tipografia.

Ordem de busca por estilo:
1. TT Lakes Neue em app/assets/fonts/ (arquivos com "Lakes" no nome; primeiro que
   casar com os padrões do estilo; se o estilo pede itálico e existir um arquivo
   itálico, não há inclinação sintética);
2. Saira (OFL, empacotada em app/assets/fonts/saira/, licença em OFL.txt): fonte
   variável com eixos de peso e largura + arquivo itálico próprio. É o padrão em
   todos os ambientes, para a arte sair igual no PC e na Vercel;
3. fonte padrão do Pillow (último recurso, só para não quebrar).

Peso/largura da Saira calibrados para igualar as larguras da capa_exemplo.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
LAKES_GLOB = "*Lakes*"
DEFAULT_UPRIGHT = FONT_DIR / "saira" / "Saira[wdth,wght].ttf"
DEFAULT_ITALIC = FONT_DIR / "saira" / "Saira-Italic[wdth,wght].ttf"


@dataclass(frozen=True)
class FontStyle:
    lakes_patterns: tuple[str, ...]  # substrings (minúsculas) do nome do arquivo Lakes, em ordem de preferência
    weight: int                      # eixo wght da Saira (100–900)
    width: int = 100                 # eixo wdth da Saira (50–125)
    italic: bool = False
    stretch: float = 1.0             # alargamento horizontal extra (1.0 = nenhum)


FONTS: dict[str, FontStyle] = {
    "brand":   FontStyle(("extrabold", "bold"), weight=700, width=114),
    "model":   FontStyle(("bold",), weight=700, width=105),
    "version": FontStyle(("light", "thin", "regular"), weight=300, width=93),
    "value":   FontStyle(("regular", "medium"), weight=500, width=95),
    "price":   FontStyle(("black italic", "extrabold italic", "bold italic", "black", "extrabold", "bold"),
                         weight=800, width=118, italic=True),
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
    path = DEFAULT_ITALIC if style.italic and DEFAULT_ITALIC.exists() else DEFAULT_UPRIGHT
    if path.exists():
        font = ImageFont.truetype(str(path), size)
        font.set_variation_by_axes([style.weight, style.width])  # ordem dos eixos da Saira: wght, wdth
        return font, style.italic and path != DEFAULT_ITALIC, style.stretch
    return ImageFont.load_default(size), style.italic, style.stretch


def font_source() -> str:
    """Descrição da fonte em uso (para log/diagnóstico)."""
    if _lakes_files():
        return "TT Lakes Neue"
    return "Saira (OFL)" if DEFAULT_UPRIGHT.exists() else "Pillow default"
