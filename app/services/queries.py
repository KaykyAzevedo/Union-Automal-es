"""Leituras para o painel e ações dos botões."""
from __future__ import annotations

import sqlite3

from ..db import from_iso, now, to_iso
from ..models import Car, PriceAlert, SoldAlert, Ticket
from .daily import next_check

_CAR_COLS = ("c.id, c.external_id, c.name, c.photo_url, c.price_cents, c.url, "
             "c.posted, c.active, c.first_seen, c.last_seen, c.missing_count, c.sold_at")

_TICKET_SQL = f"""SELECT t.id AS t_id, t.created_at AS t_created_at, t.status AS t_status,
                         t.done_at AS t_done_at, {_CAR_COLS}
                  FROM tickets t JOIN cars c ON c.id = t.car_id"""
_ALERT_SQL = f"""SELECT a.id AS a_id, a.created_at AS a_created_at, a.old_price_cents,
                        a.new_price_cents, a.dismissed, {_CAR_COLS}
                 FROM price_alerts a JOIN cars c ON c.id = a.car_id"""
_SOLD_SQL = f"""SELECT s.id AS s_id, s.created_at AS s_created_at, s.dismissed AS s_dismissed, {_CAR_COLS}
                FROM sold_alerts s JOIN cars c ON c.id = s.car_id"""


def _ticket(row: sqlite3.Row) -> Ticket:
    return Ticket(id=row["t_id"], created_at=from_iso(row["t_created_at"]), status=row["t_status"],
                  done_at=from_iso(row["t_done_at"]), car=Car.from_row(row))


def _alert(row: sqlite3.Row) -> PriceAlert:
    return PriceAlert(id=row["a_id"], created_at=from_iso(row["a_created_at"]),
                      old_price_cents=row["old_price_cents"], new_price_cents=row["new_price_cents"],
                      dismissed=bool(row["dismissed"]), car=Car.from_row(row))


def _sold(row: sqlite3.Row) -> SoldAlert:
    return SoldAlert(id=row["s_id"], created_at=from_iso(row["s_created_at"]),
                     dismissed=bool(row["s_dismissed"]), car=Car.from_row(row))


def pending_tickets(conn) -> list[Ticket]:
    rows = conn.execute(f"{_TICKET_SQL} WHERE t.status = 'pending' ORDER BY t.created_at DESC, t.id DESC")
    return [_ticket(r) for r in rows]


def get_ticket(conn, ticket_id: int) -> Ticket | None:
    row = conn.execute(f"{_TICKET_SQL} WHERE t.id = ?", (ticket_id,)).fetchone()
    return _ticket(row) if row else None


def mark_ticket_done(conn, ticket_id: int) -> Ticket | None:
    """Marca chamado como feito e o carro como postado. Idempotente."""
    ticket = get_ticket(conn, ticket_id)
    if ticket is None:
        return None
    if ticket.status != "done":
        conn.execute("UPDATE tickets SET status = 'done', done_at = ? WHERE id = ?", (to_iso(now()), ticket_id))
        conn.execute("UPDATE cars SET posted = 1 WHERE id = ?", (ticket.car.id,))
        conn.commit()
        ticket = get_ticket(conn, ticket_id)
    return ticket


def open_alerts(conn) -> list[PriceAlert]:
    rows = conn.execute(f"{_ALERT_SQL} WHERE a.dismissed = 0 ORDER BY a.created_at DESC, a.id DESC")
    return [_alert(r) for r in rows]


def dismiss_alert(conn, alert_id: int) -> bool:
    cur = conn.execute("UPDATE price_alerts SET dismissed = 1 WHERE id = ?", (alert_id,))
    conn.commit()
    return cur.rowcount > 0


def open_sold(conn) -> list[SoldAlert]:
    rows = conn.execute(f"{_SOLD_SQL} WHERE s.dismissed = 0 ORDER BY s.created_at DESC, s.id DESC")
    return [_sold(r) for r in rows]


def dismiss_sold(conn, sold_id: int) -> bool:
    cur = conn.execute("UPDATE sold_alerts SET dismissed = 1 WHERE id = ?", (sold_id,))
    conn.commit()
    return cur.rowcount > 0


def all_cars(conn) -> list[Car]:
    rows = conn.execute(f"SELECT {_CAR_COLS} FROM cars c ORDER BY c.active DESC, c.first_seen DESC, c.id DESC")
    return [Car.from_row(r) for r in rows]


def get_car(conn, car_id: int) -> Car | None:
    row = conn.execute(f"SELECT {_CAR_COLS} FROM cars c WHERE c.id = ?", (car_id,)).fetchone()
    return Car.from_row(row) if row else None


def active_cars(conn) -> list[Car]:
    rows = conn.execute(f"SELECT {_CAR_COLS} FROM cars c WHERE c.active = 1 ORDER BY c.name, c.id")
    return [Car.from_row(r) for r in rows]


def last_check(conn):
    return from_iso(conn.execute("SELECT MAX(finished_at) FROM runs WHERE ok = 1").fetchone()[0])


def stats(conn) -> dict:
    today = now().date().isoformat()

    def one(sql, *args):
        return conn.execute(sql, args).fetchone()[0]

    return {
        "pending": one("SELECT COUNT(*) FROM tickets WHERE status = 'pending'"),
        "done_today": one("SELECT COUNT(*) FROM tickets WHERE status = 'done' AND substr(done_at, 1, 10) = ?", today),
        "alerts": one("SELECT COUNT(*) FROM price_alerts WHERE dismissed = 0"),
        "sold": one("SELECT COUNT(*) FROM sold_alerts WHERE dismissed = 0"),
        "total_cars": one("SELECT COUNT(*) FROM cars WHERE active = 1"),
        "last_check": last_check(conn),
        "next_check": next_check(),
    }


def price_history(conn, car_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT price_cents, recorded_at FROM price_history WHERE car_id = ? ORDER BY recorded_at, id", (car_id,)
    )
    return [{"price_cents": r["price_cents"], "recorded_at": from_iso(r["recorded_at"])} for r in rows]
