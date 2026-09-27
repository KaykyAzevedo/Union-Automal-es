"""F2 — revisão de preços: alerta só em queda, histórico, idempotência (app/services/prices.py)."""
from __future__ import annotations

from app.services.prices import run_price_check
from app.services.sync import run_poll


def _alerts(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT c.external_id, a.old_price_cents, a.new_price_cents FROM price_alerts a "
        "JOIN cars c ON c.id = a.car_id ORDER BY a.id")]


def test_drop_creates_alert_and_updates_price(conn, car):
    run_poll(conn, [car("1", price=10_000_000), car("2", price=5_000_000)])
    r = run_price_check(conn, [car("1", price=9_500_000), car("2", price=5_000_000)])
    assert r["drops"] == 1 and r["changed"] == 1
    assert _alerts(conn) == [("1", 10_000_000, 9_500_000)]
    assert conn.execute("SELECT price_cents FROM cars WHERE external_id='1'").fetchone()[0] == 9_500_000


def test_rise_updates_price_without_alert(conn, car):
    run_poll(conn, [car("1", price=10_000_000)])
    r = run_price_check(conn, [car("1", price=11_000_000)])
    assert r["drops"] == 0 and r["changed"] == 1
    assert _alerts(conn) == []
    assert conn.execute("SELECT price_cents FROM cars").fetchone()[0] == 11_000_000


def test_rise_then_drop_compares_with_latest_price(conn, car):
    run_poll(conn, [car("1", price=10_000_000)])
    run_price_check(conn, [car("1", price=11_000_000)])
    run_price_check(conn, [car("1", price=10_500_000)])
    assert _alerts(conn) == [("1", 11_000_000, 10_500_000)]


def test_price_check_twice_is_idempotent(conn, car):
    run_poll(conn, [car("1", price=10_000_000)])
    stock = [car("1", price=9_000_000)]
    assert run_price_check(conn, stock)["drops"] == 1
    assert run_price_check(conn, stock)["drops"] == 0
    assert len(_alerts(conn)) == 1


def test_sob_consulta_keeps_last_known_price(conn, car):
    run_poll(conn, [car("1", price=10_000_000)])
    r = run_price_check(conn, [car("1", price=None)])
    assert r["changed"] == 0
    assert conn.execute("SELECT price_cents FROM cars").fetchone()[0] == 10_000_000
    # volta com preço menor → alerta contra o último conhecido
    run_price_check(conn, [car("1", price=9_000_000)])
    assert _alerts(conn) == [("1", 10_000_000, 9_000_000)]


def test_price_appears_after_sob_consulta_no_alert(conn, car):
    run_poll(conn, [car("1", price=None)])
    r = run_price_check(conn, [car("1", price=9_000_000)])
    assert r["drops"] == 0 and r["changed"] == 1
    assert conn.execute("SELECT price_cents FROM cars").fetchone()[0] == 9_000_000


def test_price_history_records_every_change(conn, car):
    run_poll(conn, [car("1", price=10_000_000)])
    run_price_check(conn, [car("1", price=9_000_000)])
    run_price_check(conn, [car("1", price=9_000_000)])
    run_price_check(conn, [car("1", price=9_500_000)])
    hist = [r[0] for r in conn.execute("SELECT price_cents FROM price_history ORDER BY id")]
    assert hist == [10_000_000, 9_000_000, 9_500_000]


def test_drop_matched_by_name_when_id_changes(conn, car):
    run_poll(conn, [car("1", "Onix 1.0 LT 2020", price=7_000_000)])
    r = run_price_check(conn, [car("77", "onix 1.0 lt 2020", price=6_500_000)])
    assert r["drops"] == 1
    assert _alerts(conn) == [("77", 7_000_000, 6_500_000)]


def test_homonyms_matched_by_id_not_crossed(conn, car):
    run_poll(conn, [car("1", "Song Plus 2027", 32_490_000), car("2", "Song Plus 2027", 32_990_000)])
    r = run_price_check(conn, [car("1", "Song Plus 2027", 32_490_000), car("2", "Song Plus 2027", 32_990_000)])
    assert r["drops"] == 0
    r = run_price_check(conn, [car("2", "Song Plus 2027", 32_490_000), car("1", "Song Plus 2027", 32_490_000)])
    assert _alerts(conn) == [("2", 32_990_000, 32_490_000)]


def test_car_first_seen_in_price_check_still_gets_ticket(conn, car):
    """Carro que aparece pela 1ª vez no job das 18:00 não pode perder o chamado."""
    run_poll(conn, [car("1")])
    run_price_check(conn, [car("1"), car("2")])
    run_poll(conn, [car("1"), car("2")])
    n = conn.execute("SELECT COUNT(*) FROM tickets t JOIN cars c ON c.id=t.car_id "
                     "WHERE c.external_id='2'").fetchone()[0]
    assert n == 1


def test_price_check_on_empty_db_does_not_break_baseline(conn, car):
    run_price_check(conn, [car("1"), car("2")])
    run_poll(conn, [car("1"), car("2")])
    run_poll(conn, [car("1"), car("2"), car("3")])
    tickets = [r[0] for r in conn.execute(
        "SELECT c.external_id FROM tickets t JOIN cars c ON c.id=t.car_id")]
    assert tickets == ["3"]
