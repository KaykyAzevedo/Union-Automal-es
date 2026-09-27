"""F2: revisão diária de preços → histórico + alertas de queda."""
from __future__ import annotations

import sqlite3

from ..db import now, to_iso
from .sold import track_missing
from .sync import match_all, refresh_car


def run_price_check(conn: sqlite3.Connection, scraped: list) -> dict:
    """Casa cada anúncio (external_id, depois nome) e compara preço.

    Toda mudança vai para price_history; queda (novo < antigo, ambos conhecidos)
    gera price_alert. Anúncio sem par é ignorado: inserir carro (e criar chamado
    ou baseline) é papel exclusivo do poll. Também conta ausências (F3).
    Retorna {checked, changed, drops, sold, alert_ids, sold_car_ids};
    `checked` conta só os casados.
    """
    ts = to_iso(now())
    seen: set[int] = set()
    checked = changed = 0
    alert_ids: list[int] = []

    for item, car in match_all(conn, scraped, seen):
        if car is None:
            continue
        checked += 1
        refresh_car(conn, car.id, item, ts)

        old, new = car.price_cents, item.price_cents
        if new is None or old == new:
            continue  # preço sumiu ("consulte") não apaga o último conhecido
        conn.execute("UPDATE cars SET price_cents = ? WHERE id = ?", (new, car.id))
        conn.execute(
            "INSERT INTO price_history (car_id, price_cents, recorded_at) VALUES (?, ?, ?)",
            (car.id, new, ts),
        )
        changed += 1
        if old is not None and new < old:
            cur = conn.execute(
                """INSERT INTO price_alerts (car_id, old_price_cents, new_price_cents, created_at)
                   VALUES (?, ?, ?, ?)""",
                (car.id, old, new, ts),
            )
            alert_ids.append(cur.lastrowid)

    sold_ids = track_missing(conn, seen, ts) if seen else []
    conn.commit()
    return {"checked": checked, "changed": changed, "drops": len(alert_ids), "sold": len(sold_ids),
            "alert_ids": alert_ids, "sold_car_ids": sold_ids}
