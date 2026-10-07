"""F1: detecção de carros novos → chamados."""
from __future__ import annotations

import sqlite3

from ..db import now, to_iso
from .matching import find_match, normalize_name
from .sold import DEMO_PREFIX, track_missing


def insert_car(conn: sqlite3.Connection, scraped, *, posted: bool, ts: str, baseline: bool = False) -> int:
    cur = conn.execute(
        """INSERT INTO cars (external_id, name, name_key, photo_url, price_cents, url,
                             posted, active, first_seen, last_seen, in_baseline, listed_at, listed_at_checked)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
        (scraped.external_id, scraped.name, normalize_name(scraped.name), scraped.photo_url,
         scraped.price_cents, scraped.url, int(posted), ts, ts, int(baseline), _listed_at(scraped),
         getattr(scraped, "listed_at_checked", None)),
    )
    car_id = cur.lastrowid
    conn.execute(
        "INSERT INTO price_history (car_id, price_cents, recorded_at) VALUES (?, ?, ?)",
        (car_id, scraped.price_cents, ts),
    )
    return car_id


def _listed_at(scraped) -> str | None:
    """Data real de cadastro no site (ScrapedCar.listed_at: date|None), se o scraper expuser."""
    value = getattr(scraped, "listed_at", None)
    if value is None:
        return None
    return value.isoformat()[:10] if hasattr(value, "isoformat") else str(value)[:10]


def refresh_car(conn: sqlite3.Connection, car_id: int, scraped, ts: str) -> None:
    """Atualiza metadados e reativa. Preço fica para o job das 18:00."""
    conn.execute(
        """UPDATE cars SET external_id = ?, name = ?, name_key = ?,
                  photo_url = COALESCE(?, photo_url), url = ?, active = 1, last_seen = ?,
                  listed_at = COALESCE(?, listed_at),
                  listed_at_checked = COALESCE(?, listed_at_checked)
           WHERE id = ?""",
        (scraped.external_id, scraped.name, normalize_name(scraped.name), scraped.photo_url,
         scraped.url, ts, _listed_at(scraped), getattr(scraped, "listed_at_checked", None), car_id),
    )


def match_all(conn: sqlite3.Connection, scraped: list, seen: set[int]):
    """Gera (item, Car | None) para cada anúncio único da coleta; ids casados vão para `seen`.

    Casamento por nome nunca "rouba" um carro cujo external_id também está na
    coleta atual (seria um homônimo diferente) nem um carro já casado.
    """
    unique = list({s.external_id: s for s in scraped}.values())
    current_ext = {s.external_id for s in unique}
    # primeiro os que casam por external_id, para reservá-los antes do casamento por nome
    unique.sort(key=lambda s: 0 if conn.execute(
        "SELECT 1 FROM cars WHERE external_id = ?", (s.external_id,)).fetchone() else 1)
    for item in unique:
        car = find_match(conn, item, exclude_ids=frozenset(seen))
        if car is not None and car.external_id != item.external_id and car.external_id in current_ext:
            car = None
        if car is not None:
            seen.add(car.id)
        yield item, car


def _open_ticket(conn: sqlite3.Connection, car_id: int, ts: str) -> None:
    conn.execute("INSERT INTO tickets (car_id, status, created_at) VALUES (?, 'pending', ?)", (car_id, ts))


def _photo_arrived(conn: sqlite3.Connection, car, item) -> bool:
    """Carro não postado e sem nenhum chamado que estava sem foto e agora tem: vira "carro novo"."""
    if car.posted or car.photo_url or not item.photo_url or car.external_id.startswith(DEMO_PREFIX):
        return False
    return conn.execute("SELECT 1 FROM tickets WHERE car_id = ?", (car.id,)).fetchone() is None


def run_poll(conn: sqlite3.Connection, scraped: list) -> dict:
    """Processa uma coleta.

    Retorna {found, new, baseline, deactivated, sold, new_car_ids, sold_car_ids}.
    Banco sem carros reais (demo não conta) → baseline: tudo entra como já
    postado, sem chamados. Carro sem foto não está pronto para postar: entra
    com posted=0 e sem chamado (também no baseline); o chamado abre quando a
    foto aparecer (ver _photo_arrived). Ausências e vendidos: ver
    sold.track_missing (só se a coleta não veio vazia).
    """
    ts = to_iso(now())
    baseline = conn.execute(
        "SELECT COUNT(*) FROM cars WHERE external_id NOT LIKE ?", (DEMO_PREFIX + "%",)
    ).fetchone()[0] == 0
    seen: set[int] = set()
    new_ids: list[int] = []

    for item, car in match_all(conn, scraped, seen):
        if car is not None:
            refresh_car(conn, car.id, item, ts)
            if _photo_arrived(conn, car, item):
                _open_ticket(conn, car.id, ts)
                new_ids.append(car.id)
            continue
        has_photo = bool(item.photo_url)
        car_id = insert_car(conn, item, posted=baseline and has_photo, ts=ts, baseline=baseline)
        seen.add(car_id)
        if not baseline and has_photo:
            _open_ticket(conn, car_id, ts)
            new_ids.append(car_id)

    sold_ids = track_missing(conn, seen, ts) if seen else []
    conn.commit()
    return {
        "found": len(seen),
        "new": len(new_ids),
        "baseline": baseline and bool(seen),
        "deactivated": len(sold_ids),
        "sold": len(sold_ids),
        "new_car_ids": new_ids,
        "sold_car_ids": sold_ids,
    }
