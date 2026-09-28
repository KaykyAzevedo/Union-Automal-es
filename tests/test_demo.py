"""Dados de demonstração (app/demo.py): seed/clear só mexem em carros 'demo-*'."""
from __future__ import annotations

import subprocess
import sys

from app.services.prices import run_price_check
from app.services.sync import run_poll
from conftest import ROOT


def _snapshot(conn):
    """Tudo que pertence a carros reais (não demo)."""
    q = {
        "cars": "SELECT * FROM cars WHERE external_id NOT LIKE 'demo-%' ORDER BY id",
        "tickets": "SELECT t.* FROM tickets t JOIN cars c ON c.id=t.car_id "
                   "WHERE c.external_id NOT LIKE 'demo-%' ORDER BY t.id",
        "alerts": "SELECT a.* FROM price_alerts a JOIN cars c ON c.id=a.car_id "
                  "WHERE c.external_id NOT LIKE 'demo-%' ORDER BY a.id",
        "sold": "SELECT s.* FROM sold_alerts s JOIN cars c ON c.id=s.car_id "
                "WHERE c.external_id NOT LIKE 'demo-%' ORDER BY s.id",
        "history": "SELECT h.* FROM price_history h JOIN cars c ON c.id=h.car_id "
                   "WHERE c.external_id NOT LIKE 'demo-%' ORDER BY h.id",
    }
    return {k: [tuple(r) for r in conn.execute(sql)] for k, sql in q.items()}


def _real_db(conn, car):
    run_poll(conn, [car("1", price=10_000_000), car("2"), car("3")])
    run_poll(conn, [car("1", price=10_000_000), car("2"), car("4")])       # 4 novo, 3 falta 1x
    run_price_check(conn, [car("1", price=9_000_000), car("2"), car("4")])  # queda; 3 vendido
    return _snapshot(conn)


def _count(conn, sql):
    return conn.execute(sql).fetchone()[0]


def test_seed_creates_only_demo_cars_and_clear_removes_them(conn, car):
    from app import demo

    before = _real_db(conn, car)
    assert before["sold"] and before["alerts"] and before["tickets"]

    info = demo.seed(conn)
    assert isinstance(info, dict)
    n_demo = _count(conn, "SELECT COUNT(*) FROM cars WHERE external_id LIKE 'demo-%'")
    assert n_demo > 0
    assert _snapshot(conn) == before

    removed = demo.clear(conn)
    assert removed == n_demo
    assert _snapshot(conn) == before
    assert _count(conn, "SELECT COUNT(*) FROM cars WHERE external_id LIKE 'demo-%'") == 0
    for table in ("tickets", "price_alerts", "sold_alerts", "price_history"):
        orphans = _count(conn, f"SELECT COUNT(*) FROM {table} WHERE car_id NOT IN (SELECT id FROM cars)")
        assert orphans == 0, table


def test_seed_populates_every_panel_section(conn):
    from app import demo
    from app.services import queries

    demo.seed(conn)
    assert queries.pending_tickets(conn)
    assert queries.open_alerts(conn)
    assert queries.stats(conn)["sold"] > 0


def test_seed_twice_does_not_duplicate(conn):
    from app import demo

    demo.seed(conn)
    n = _count(conn, "SELECT COUNT(*) FROM cars")
    demo.seed(conn)
    assert _count(conn, "SELECT COUNT(*) FROM cars") == n


def test_clear_on_db_without_demo_is_noop(conn, car):
    from app import demo

    before = _real_db(conn, car)
    assert demo.clear(conn) == 0
    assert _snapshot(conn) == before


def test_demo_cars_never_counted_as_missing(conn, car):
    from app import demo

    run_poll(conn, [car("1")])
    demo.seed(conn)
    demo_state = [tuple(r) for r in conn.execute(
        "SELECT id, active, sold_at, missing_count FROM cars WHERE external_id LIKE 'demo-%' ORDER BY id")]
    open_demo_sold = _count(conn, "SELECT COUNT(*) FROM sold_alerts s JOIN cars c ON c.id=s.car_id "
                                  "WHERE c.external_id LIKE 'demo-%'")
    for _ in range(3):
        r = run_poll(conn, [car("1")])
        assert r["sold"] == 0
    run_price_check(conn, [car("1")])
    assert [tuple(r) for r in conn.execute(
        "SELECT id, active, sold_at, missing_count FROM cars WHERE external_id LIKE 'demo-%' ORDER BY id")] == demo_state
    assert _count(conn, "SELECT COUNT(*) FROM sold_alerts s JOIN cars c ON c.id=s.car_id "
                        "WHERE c.external_id LIKE 'demo-%'") == open_demo_sold


def test_seed_on_empty_db_keeps_real_baseline(conn, car):
    """Demo num banco novo não pode fazer o 1º poll real virar 47 chamados."""
    from app import demo

    demo.seed(conn)
    demo_tickets = _count(conn, "SELECT COUNT(*) FROM tickets")
    r = run_poll(conn, [car("1"), car("2"), car("3")])
    assert r["new"] == 0
    assert _count(conn, "SELECT COUNT(*) FROM tickets") == demo_tickets


def test_cli_seed_and_clear_use_env_db(tmp_db):
    """CLI respeita o banco do ambiente (UNION_DB_PATH no SQLite, DATABASE_URL no Postgres)."""
    import os

    from app import db

    for cmd, expected in (("seed", True), ("clear", False)):
        p = subprocess.run([sys.executable, "-m", "app.demo", cmd], cwd=ROOT, env=dict(os.environ),
                           capture_output=True, text=True, timeout=60)
        assert p.returncode == 0, p.stderr
        c = db.connect()
        try:
            n = c.execute("SELECT COUNT(*) FROM cars WHERE external_id LIKE 'demo-%'").fetchone()[0]
        finally:
            c.close()
        assert (n > 0) is expected, cmd
