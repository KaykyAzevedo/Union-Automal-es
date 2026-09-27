"""Modo demo: popula o painel com dados falsos (external_id 'demo-*').

Uso: python -m app.demo seed | clear   (respeita UNION_DB_PATH)

Carros demo ficam fora da contagem de ausências (não viram "vendidos" nos
polls) e não impedem o baseline do 1º poll real. `clear` apaga só eles.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import timedelta

from . import db
from .services.matching import normalize_name
from .services.sold import DEMO_PREFIX

_BASE = "https://www.unionrioveiculos.com.br/Veiculos"
_PHOTO = "https://placehold.co/800x600/0f172a/f8fafc?text={}"

# (sufixo, nome, preço atual, preço antigo p/ alerta, cenário)
_CARS = [
    ("1", "BYD SONG PLUS 1.5 DM-I TURBO HÍBRIDO AUTOMÁTICO 2027", 23990000, None, "ticket"),
    ("2", "TOYOTA COROLLA CROSS 2.0 XRE AUTOMÁTICO 2024", 15490000, None, "ticket"),
    ("3", "JEEP COMPASS 1.3 T270 LONGITUDE AUTOMÁTICO 2023", 13990000, 14990000, "price_drop"),
    ("4", "HONDA HR-V 1.5 EXL AUTOMÁTICO 2023", 12790000, None, "sold"),
    ("5", "VOLKSWAGEN T-CROSS 1.0 TSI COMFORTLINE 2022", 10490000, None, "posted"),
]


def clear(conn: sqlite3.Connection) -> int:
    """Remove carros demo e tudo ligado a eles. Retorna quantos carros saíram."""
    ids = [r[0] for r in conn.execute("SELECT id FROM cars WHERE external_id LIKE ?", (DEMO_PREFIX + "%",))]
    for table in ("tickets", "price_history", "price_alerts", "sold_alerts"):
        conn.executemany(f"DELETE FROM {table} WHERE car_id = ?", [(i,) for i in ids])
    conn.executemany("DELETE FROM cars WHERE id = ?", [(i,) for i in ids])
    conn.commit()
    return len(ids)


def seed(conn: sqlite3.Connection) -> dict:
    """Recria os dados demo (chama clear antes). Retorna contagens por cenário."""
    clear(conn)
    now = db.now()
    counts = {"cars": 0, "tickets": 0, "price_alerts": 0, "sold_alerts": 0}
    for i, (suffix, name, price, old_price, scenario) in enumerate(_CARS):
        ts = db.to_iso(now - timedelta(minutes=10 * (len(_CARS) - i)))
        first_seen = db.to_iso(now - timedelta(days=3)) if old_price or scenario == "sold" else ts
        sold = scenario == "sold"
        car_id = conn.execute(
            """INSERT INTO cars (external_id, name, name_key, photo_url, price_cents, url, posted, active,
                                 first_seen, last_seen, missing_count, sold_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (DEMO_PREFIX + suffix, f"{name} (DEMO)", normalize_name(f"{name} (DEMO)"),
             _PHOTO.format(name.split()[1].replace("-", "")), price, f"{_BASE}#demo-{suffix}",
             int(scenario != "ticket"), int(not sold), first_seen, ts, 2 if sold else 0, ts if sold else None),
        ).lastrowid
        counts["cars"] += 1
        if old_price:
            conn.execute("INSERT INTO price_history (car_id, price_cents, recorded_at) VALUES (?, ?, ?)",
                         (car_id, old_price, first_seen))
        conn.execute("INSERT INTO price_history (car_id, price_cents, recorded_at) VALUES (?, ?, ?)",
                     (car_id, price, ts))
        if scenario == "ticket":
            conn.execute("INSERT INTO tickets (car_id, status, created_at) VALUES (?, 'pending', ?)", (car_id, ts))
            counts["tickets"] += 1
        elif scenario == "price_drop":
            conn.execute("""INSERT INTO price_alerts (car_id, old_price_cents, new_price_cents, created_at)
                            VALUES (?, ?, ?, ?)""", (car_id, old_price, price, ts))
            counts["price_alerts"] += 1
        elif sold:
            conn.execute("INSERT INTO sold_alerts (car_id, created_at) VALUES (?, ?)", (car_id, ts))
            counts["sold_alerts"] += 1
    conn.commit()
    return counts


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in ("seed", "clear"):
        print("uso: python -m app.demo seed|clear")
        return 2
    conn = db.connect()
    try:
        db.init_db(conn)
        if argv[0] == "seed":
            print("demo criado:", seed(conn), "em", db.db_path())
        else:
            print("carros demo removidos:", clear(conn), "de", db.db_path())
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
