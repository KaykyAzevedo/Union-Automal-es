"""Casamento de anúncios raspados com carros do banco."""
from __future__ import annotations

import re
import sqlite3
import unicodedata

from ..models import Car


def normalize_name(name: str) -> str:
    """Minúsculas, sem acento, sem pontuação, espaços colapsados.

    Mantém o ponto decimal entre dígitos ("1.0") para não confundir
    "Onix 1.0" com "Onix 10"; vírgula decimal vira ponto.
    """
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    text = re.sub(r"[^a-z0-9.]+", " ", text)
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text)
    return " ".join(text.split())


def find_match(conn: sqlite3.Connection, scraped, exclude_ids=frozenset()) -> Car | None:
    """Acha o carro do banco para um ScrapedCar.

    1) mesmo external_id; 2) mesmo nome normalizado, só entre carros ATIVOS e
    ignorando `exclude_ids` (carros já casados nesta execução). Re-anúncio troca
    o id enquanto o antigo ainda está ativo; carro inativo com mesmo nome é
    outra unidade (ex.: Song Plus idênticos) — melhor um chamado a mais.
    Entre homônimos prefere o de preço mais próximo.
    """
    row = conn.execute("SELECT * FROM cars WHERE external_id = ?", (scraped.external_id,)).fetchone()
    if row is not None:
        return Car.from_row(row)

    key = normalize_name(scraped.name)
    if not key:
        return None
    # carros demo nunca casam por nome com anúncios reais
    rows = conn.execute(
        "SELECT * FROM cars WHERE name_key = ? AND active = 1 AND external_id NOT LIKE 'demo-%'", (key,)
    ).fetchall()
    candidates = [Car.from_row(r) for r in rows if r["id"] not in exclude_ids]
    if not candidates:
        return None

    def score(car: Car):
        if scraped.price_cents is None or car.price_cents is None:
            diff = 0
        else:
            diff = abs(car.price_cents - scraped.price_cents)
        return (diff, -car.id)

    return min(candidates, key=score)
