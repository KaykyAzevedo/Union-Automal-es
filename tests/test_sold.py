"""F3 — carro vendido: 2 coletas seguidas sem o carro → vendido (app/services/sold.py + sync/prices)."""
from __future__ import annotations

import sqlite3

import pytest

from app.services.prices import run_price_check
from app.services.sync import run_poll


def _one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def _car(conn, ext):
    return conn.execute("SELECT * FROM cars WHERE external_id = ?", (ext,)).fetchone()


def _open_sold(conn):
    return [r[0] for r in conn.execute(
        "SELECT c.external_id FROM sold_alerts s JOIN cars c ON c.id = s.car_id "
        "WHERE s.dismissed = 0 ORDER BY s.id")]


@pytest.fixture
def stock(conn, car):
    """Baseline com 3 carros; devolve helper que gera a coleta sem os ids indicados."""
    cars = [car("1"), car("2"), car("3")]
    run_poll(conn, cars)
    return lambda *missing: [c for c in cars if c.external_id not in missing]


# --- contagem de ausências -------------------------------------------------

def test_one_absence_is_not_sold(conn, stock):
    r = run_poll(conn, stock("2"))
    assert r["sold"] == 0 and r["sold_car_ids"] == [] and r["deactivated"] == 0
    row = _car(conn, "2")
    assert (row["active"], row["missing_count"], row["sold_at"]) == (1, 1, None)
    assert _open_sold(conn) == []


def test_two_consecutive_absences_sell(conn, stock):
    run_poll(conn, stock("2"))
    r = run_poll(conn, stock("2"))
    row = _car(conn, "2")
    assert r["sold"] == 1 and r["sold_car_ids"] == [row["id"]]
    assert r["deactivated"] == r["sold"]
    assert row["active"] == 0 and row["sold_at"] is not None and row["missing_count"] >= 2
    assert _open_sold(conn) == ["2"]
    # os outros continuam ativos e zerados
    assert _one(conn, "SELECT COUNT(*) FROM cars WHERE active = 1 AND missing_count = 0") == 2


def test_presence_resets_counter(conn, stock):
    run_poll(conn, stock("2"))
    run_poll(conn, stock())           # voltou: zera
    r = run_poll(conn, stock("2"))    # 1 falta de novo
    assert r["sold"] == 0
    row = _car(conn, "2")
    assert (row["active"], row["missing_count"]) == (1, 1)


def test_empty_scrape_does_not_count(conn, stock):
    run_poll(conn, stock("2"))
    run_poll(conn, [])
    run_price_check(conn, [])
    row = _car(conn, "2")
    assert (row["active"], row["missing_count"]) == (1, 1)
    assert _one(conn, "SELECT missing_count FROM cars WHERE external_id='1'") == 0


def test_price_check_counts_as_a_collection(conn, stock):
    run_poll(conn, stock("2"))
    r = run_price_check(conn, stock("2"))
    assert r["sold"] == 1 and r["sold_car_ids"] == [_car(conn, "2")["id"]]
    assert _car(conn, "2")["active"] == 0
    assert _open_sold(conn) == ["2"]


def test_sold_is_idempotent(conn, stock):
    run_poll(conn, stock("2"))
    run_poll(conn, stock("2"))
    sold_at = _car(conn, "2")["sold_at"]
    for _ in range(3):
        r = run_poll(conn, stock("2"))
        assert r["sold"] == 0 and r["sold_car_ids"] == []
    run_price_check(conn, stock("2"))
    assert _one(conn, "SELECT COUNT(*) FROM sold_alerts") == 1
    assert _car(conn, "2")["sold_at"] == sold_at


def test_several_sold_at_once(conn, stock):
    run_poll(conn, stock("1", "3"))
    r = run_poll(conn, stock("1", "3"))
    assert r["sold"] == 2 and sorted(r["sold_car_ids"]) == sorted(
        [_car(conn, "1")["id"], _car(conn, "3")["id"]])
    assert _open_sold(conn) == ["1", "3"]


def test_new_car_absent_twice_is_sold(conn, stock, car):
    run_poll(conn, stock() + [car("4")])
    run_poll(conn, stock())
    r = run_poll(conn, stock())
    assert r["sold_car_ids"] == [_car(conn, "4")["id"]]


# --- chamados --------------------------------------------------------------

def test_pending_ticket_is_cancelled(conn, stock, car):
    run_poll(conn, stock() + [car("4")])         # chamado pendente p/ 4
    run_poll(conn, stock())
    run_poll(conn, stock())
    t = conn.execute("SELECT t.status, t.done_at FROM tickets t JOIN cars c ON c.id=t.car_id "
                     "WHERE c.external_id='4'").fetchone()
    assert t["status"] == "cancelled" and t["done_at"] is not None
    assert _one(conn, "SELECT COUNT(*) FROM tickets WHERE status='pending'") == 0


def test_one_absence_keeps_ticket_pending(conn, stock, car):
    run_poll(conn, stock() + [car("4")])
    run_poll(conn, stock())
    assert _one(conn, "SELECT status FROM tickets") == "pending"


def test_done_ticket_stays_done(conn, stock, car):
    from app.services.queries import mark_ticket_done

    run_poll(conn, stock() + [car("4")])
    mark_ticket_done(conn, 1)
    run_poll(conn, stock())
    run_poll(conn, stock())
    assert _one(conn, "SELECT status FROM tickets") == "done"


# --- reaparecimento --------------------------------------------------------

def test_reappearance_reactivates_and_dismisses_alert(conn, stock):
    run_poll(conn, stock("2"))
    run_poll(conn, stock("2"))
    r = run_poll(conn, stock())
    assert r["new"] == 0
    row = _car(conn, "2")
    assert (row["active"], row["sold_at"], row["missing_count"]) == (1, None, 0)
    assert _open_sold(conn) == []
    assert _one(conn, "SELECT COUNT(*) FROM sold_alerts") == 1  # marcado, não apagado
    assert _one(conn, "SELECT COUNT(*) FROM tickets") == 0


def test_reappearance_in_price_check_reactivates(conn, stock):
    run_poll(conn, stock("2"))
    run_poll(conn, stock("2"))
    run_price_check(conn, stock())
    row = _car(conn, "2")
    assert (row["active"], row["sold_at"], row["missing_count"]) == (1, None, 0)
    assert _open_sold(conn) == []


def test_sold_again_after_reappearing_creates_new_alert(conn, stock):
    for scrape in (stock("2"), stock("2"), stock(), stock("2"), stock("2")):
        run_poll(conn, scrape)
    assert _open_sold(conn) == ["2"]
    assert _one(conn, "SELECT COUNT(*) FROM sold_alerts") == 2


# --- track_missing direto --------------------------------------------------

def test_track_missing_contract(conn, stock):
    from app.db import now, to_iso
    from app.services.sold import track_missing

    ids = {r[0] for r in conn.execute("SELECT id FROM cars")}
    keep = ids - {_car(conn, "3")["id"]}
    assert track_missing(conn, keep, to_iso(now())) == []
    assert track_missing(conn, keep, to_iso(now())) == [_car(conn, "3")["id"]]


# --- migração --------------------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE cars (id INTEGER PRIMARY KEY AUTOINCREMENT, external_id TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL, name_key TEXT NOT NULL, photo_url TEXT, price_cents INTEGER, url TEXT NOT NULL,
  posted INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE TABLE price_history (id INTEGER PRIMARY KEY AUTOINCREMENT, car_id INTEGER NOT NULL
  REFERENCES cars(id) ON DELETE CASCADE, price_cents INTEGER, recorded_at TEXT NOT NULL);
CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, car_id INTEGER NOT NULL
  REFERENCES cars(id) ON DELETE CASCADE, status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL, done_at TEXT);
CREATE TABLE price_alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, car_id INTEGER NOT NULL
  REFERENCES cars(id) ON DELETE CASCADE, old_price_cents INTEGER NOT NULL,
  new_price_cents INTEGER NOT NULL, created_at TEXT NOT NULL, dismissed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, started_at TEXT NOT NULL,
  finished_at TEXT, ok INTEGER, summary TEXT, error TEXT);
INSERT INTO cars (external_id, name, name_key, url, posted, active, first_seen, last_seen)
  VALUES ('1', 'Onix 2020', 'onix 2020', 'u1', 1, 1, '2026-09-01T10:00:00-03:00', '2026-09-01T10:00:00-03:00'),
         ('2', 'HB20 2019', 'hb20 2019', 'u2', 1, 1, '2026-09-01T10:00:00-03:00', '2026-09-01T10:00:00-03:00');
"""


def test_migration_of_old_db(tmp_path, car):
    from app import db

    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.commit()
    raw.close()

    conn = db.connect(path)
    try:
        db.init_db(conn)
        db.init_db(conn)  # idempotente
        db.migrate(conn)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(cars)")}
        assert {"missing_count", "sold_at"} <= cols
        assert _one(conn, "SELECT COUNT(*) FROM sold_alerts") == 0
        rows = conn.execute("SELECT external_id, missing_count, sold_at FROM cars ORDER BY id").fetchall()
        assert [tuple(r) for r in rows] == [("1", 0, None), ("2", 0, None)]
        # e o fluxo funciona sobre o banco migrado
        run_poll(conn, [car("1", "Onix 2020")])
        r = run_poll(conn, [car("1", "Onix 2020")])
        assert r["sold"] == 1 and _open_sold(conn) == ["2"]
    finally:
        conn.close()
