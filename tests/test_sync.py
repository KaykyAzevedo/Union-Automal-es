"""F1 — poll: baseline, carros novos → chamados, idempotência, desativação (app/services/sync.py)."""
from __future__ import annotations

from app.services.sync import run_poll


def _count(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def test_first_run_is_baseline_without_tickets(conn, car):
    r = run_poll(conn, [car("1"), car("2"), car("3")])
    assert r["baseline"] is True
    assert r["new"] == 0 and r["new_car_ids"] == []
    assert _count(conn, "SELECT COUNT(*) FROM tickets") == 0
    assert _count(conn, "SELECT COUNT(*) FROM cars WHERE posted = 1 AND active = 1") == 3
    assert _count(conn, "SELECT COUNT(*) FROM price_history") == 3


def test_baseline_with_empty_scrape_keeps_db_empty(conn, car):
    r = run_poll(conn, [])
    assert r["baseline"] is False
    assert _count(conn, "SELECT COUNT(*) FROM cars") == 0
    # a próxima coleta de verdade ainda é baseline
    assert run_poll(conn, [car("1")])["baseline"] is True
    assert _count(conn, "SELECT COUNT(*) FROM tickets") == 0


def test_new_car_after_baseline_creates_one_pending_ticket(conn, car):
    run_poll(conn, [car("1"), car("2")])
    r = run_poll(conn, [car("1"), car("2"), car("3", photo=None)])
    assert r["baseline"] is False and r["new"] == 1
    rows = conn.execute("SELECT t.status, c.external_id, c.posted FROM tickets t JOIN cars c ON c.id=t.car_id").fetchall()
    assert [tuple(x) for x in rows] == [("pending", "3", 0)]


def test_poll_twice_is_idempotent(conn, car):
    run_poll(conn, [car("1")])
    stock = [car("1"), car("2"), car("3")]
    assert run_poll(conn, stock)["new"] == 2
    assert run_poll(conn, stock)["new"] == 0
    assert run_poll(conn, list(reversed(stock)))["new"] == 0
    assert _count(conn, "SELECT COUNT(*) FROM tickets") == 2
    assert _count(conn, "SELECT COUNT(*) FROM cars") == 3


def test_duplicate_ids_in_same_scrape_do_not_duplicate(conn, car):
    run_poll(conn, [car("1")])
    run_poll(conn, [car("1"), car("2"), car("2")])
    assert _count(conn, "SELECT COUNT(*) FROM tickets") == 1


def test_homonyms_are_distinct_cars(conn, car):
    run_poll(conn, [car("1", "Song Plus 2027")])
    r = run_poll(conn, [car("1", "Song Plus 2027"), car("2", "Song Plus 2027"), car("3", "Song Plus 2027")])
    assert r["new"] == 2
    assert _count(conn, "SELECT COUNT(*) FROM cars") == 3


def test_relisted_with_new_id_matches_by_name_no_ticket(conn, car):
    run_poll(conn, [car("1", "Onix 1.0 LT 2020"), car("2")])
    r = run_poll(conn, [car("100", "ONIX 1.0 LT 2020"), car("2")])
    assert r["new"] == 0
    row = conn.execute("SELECT external_id, active FROM cars WHERE name_key = 'onix 1.0 lt 2020'").fetchone()
    assert tuple(row) == ("100", 1)
    assert _count(conn, "SELECT COUNT(*) FROM cars") == 2


def test_missing_car_deactivated_and_reactivated_without_ticket(conn, car):
    run_poll(conn, [car("1"), car("2")])
    assert run_poll(conn, [car("1")])["deactivated"] == 0  # F3: 1 falta ainda não conta
    r = run_poll(conn, [car("1")])
    assert r["deactivated"] == 1
    assert _count(conn, "SELECT active FROM cars WHERE external_id='2'") == 0
    r = run_poll(conn, [car("1"), car("2")])
    assert r["new"] == 0
    assert _count(conn, "SELECT active FROM cars WHERE external_id='2'") == 1
    assert _count(conn, "SELECT COUNT(*) FROM tickets") == 0


def test_empty_scrape_after_baseline_does_not_deactivate(conn, car):
    run_poll(conn, [car("1"), car("2")])
    r = run_poll(conn, [])
    assert r["deactivated"] == 0
    assert _count(conn, "SELECT COUNT(*) FROM cars WHERE active = 1") == 2


def test_poll_does_not_touch_price(conn, car):
    run_poll(conn, [car("1", price=5_000_000)])
    run_poll(conn, [car("1", price=4_000_000)])
    assert _count(conn, "SELECT price_cents FROM cars") == 5_000_000
    assert _count(conn, "SELECT COUNT(*) FROM price_alerts") == 0


def test_poll_keeps_photo_when_new_scrape_has_none(conn, car):
    run_poll(conn, [car("1")])
    run_poll(conn, [car("1", photo=None)])
    assert conn.execute("SELECT photo_url FROM cars").fetchone()[0] == "https://img.test/1.jpg"


def test_new_homonym_of_sold_car_gets_ticket(conn, car):
    """Estoque real tem Song Plus idênticos: um vendido (inativo) não pode 'engolir' o novo."""
    run_poll(conn, [car("1", "Song Plus 2027", 32_490_000), car("9")])
    run_poll(conn, [car("9")])
    run_poll(conn, [car("9")])  # 2 coletas sem o id 1 → vendido (F3)
    r = run_poll(conn, [car("9"), car("2", "Song Plus 2027", 32_990_000)])
    assert r["new"] == 1
    assert _count(conn, "SELECT COUNT(*) FROM cars") == 3
    assert _count(conn, "SELECT active FROM cars WHERE external_id='1'") == 0
