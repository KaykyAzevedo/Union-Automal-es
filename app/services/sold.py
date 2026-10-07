"""F3: carro vendido — some do site por 2 coletas seguidas."""
from __future__ import annotations

import sqlite3

SOLD_AFTER_MISSES = 2
DEMO_PREFIX = "demo-"


def track_missing(conn: sqlite3.Connection, seen_ids: set[int], ts: str) -> list[int]:
    """Atualiza missing_count após uma coleta bem-sucedida e não vazia.

    Presentes: zera contagem; se estavam vendidos, reativa, descarta o
    sold_alert aberto e limpa sale_price_cents (F13). Ativos ausentes: +1; ao chegar em SOLD_AFTER_MISSES viram
    vendidos (active=0, sold_at, sold_alert, chamado pendente → 'cancelled').
    Carros demo ficam de fora. Não faz commit. Retorna ids vendidos agora.
    """
    seen = tuple(seen_ids)
    marks = ",".join("?" * len(seen))
    conn.execute(
        f"""UPDATE sold_alerts SET dismissed = 1
            WHERE dismissed = 0 AND car_id IN (SELECT id FROM cars WHERE sold_at IS NOT NULL AND id IN ({marks}))""",
        seen,
    )
    # reapareceu: o valor de venda digitado era da "venda" desfeita (custo e despesas ficam)
    conn.execute(f"UPDATE cars SET sale_price_cents = NULL WHERE sold_at IS NOT NULL AND id IN ({marks})", seen)
    conn.execute(f"UPDATE cars SET missing_count = 0, sold_at = NULL, active = 1 WHERE id IN ({marks})", seen)
    conn.execute(
        f"""UPDATE cars SET missing_count = missing_count + 1
            WHERE active = 1 AND id NOT IN ({marks}) AND external_id NOT LIKE '{DEMO_PREFIX}%'""",
        seen,
    )

    sold_ids = [r[0] for r in conn.execute(
        f"""SELECT id FROM cars WHERE active = 1 AND missing_count >= ?
            AND external_id NOT LIKE '{DEMO_PREFIX}%' ORDER BY id""",
        (SOLD_AFTER_MISSES,),
    )]
    for car_id in sold_ids:
        conn.execute("UPDATE cars SET active = 0, sold_at = ? WHERE id = ?", (ts, car_id))
        conn.execute("INSERT INTO sold_alerts (car_id, created_at) VALUES (?, ?)", (car_id, ts))
        conn.execute(
            "UPDATE tickets SET status = 'cancelled', done_at = ? WHERE car_id = ? AND status = 'pending'",
            (ts, car_id),
        )
    return sold_ids
