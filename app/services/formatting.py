"""Filtros Jinja."""
from __future__ import annotations

from datetime import datetime

from ..db import TZ


def brl(cents: int | None) -> str:
    """8990000 -> 'R$ 89.900,00'; None -> '—'."""
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    reais, centavos = divmod(abs(int(cents)), 100)
    inteiro = f"{reais:,}".replace(",", ".")
    return f"{sign}R$ {inteiro},{centavos:02d}"


def dt(value: datetime | None, fmt: str = "%d/%m/%Y %H:%M") -> str:
    """datetime -> '27/09/2026 18:00' (fuso America/Sao_Paulo); None -> '—'."""
    if value is None:
        return "—"
    if value.tzinfo is not None:
        value = value.astimezone(TZ)
    return value.strftime(fmt)
