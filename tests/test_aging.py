"""F6 — tempo em estoque e sugestão de redução (Forja: app/services/aging.py)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from app import db
from app.services import aging
from app.services.prices import run_price_check
from app.services.sync import run_poll

D0 = date(2026, 1, 10)


# --- round_down_900 -----------------------------------------------------------------

@pytest.mark.parametrize("reais_cents, expected", [
    (126_003_00, 125_900_00),
    (125_900_00, 125_900_00),        # já termina em 900: fica
    (125_900_50, 125_900_00),        # centavos somem
    (125_899_99, 124_900_00),
    (125_999_99, 125_900_00),
    (100_000_00, 99_900_00),
    (1_900_00, 1_900_00),
    (1_000_00, 900_00),
    (900_00, 900_00),
    (899_99, None),
    (0, None),
])
def test_round_down_900(reais_cents, expected):
    assert aging.round_down_900(reais_cents) == expected


def test_round_down_900_properties():
    for cents in range(90_000, 50_000_000, 123_457):
        r = aging.round_down_900(cents)
        if r is None:
            assert cents < 90_000
            continue
        assert r <= cents and r % 100 == 0 and (r // 100) % 1000 == 900
        assert cents - r < 1000_00  # nunca desce mais de R$ 1.000


# --- suggest: limites -----------------------------------------------------------------

@pytest.mark.parametrize("days, pct", [
    (0, None), (29, None), (30, 3), (44, 3), (45, 5), (59, 5), (60, 8), (61, 8), (400, 8),
])
def test_suggest_tiers(days, pct):
    s = aging.suggest(12_990_000, days)
    if pct is None:
        assert s is None
    else:
        assert s["pct"] == pct
        assert s["new_price_cents"] == aging.round_down_900(12_990_000 * (100 - pct) // 100)
        assert s["new_price_cents"] < 12_990_000
        assert s["reason"]


def test_suggest_example_from_spec():
    # R$ 129.900 com -3% = 126.003 → 125.900
    assert aging.suggest(12_990_000, 30)["new_price_cents"] == 12_590_000
    assert aging.suggest(12_990_000, 45)["new_price_cents"] == 12_290_000   # 123.405 → 122.900
    assert aging.suggest(12_990_000, 60)["new_price_cents"] == 11_890_000   # 119.508 → 118.900


def test_suggest_none_price():
    assert aging.suggest(None, 90) is None


def test_suggest_tiny_price_never_negative_or_none_crash():
    s = aging.suggest(90_000, 60)  # R$ 900 → 828 → abaixo de 900
    assert s is None or (s["new_price_cents"] is not None and 0 < s["new_price_cents"] < 90_000)


def test_constants():
    assert aging.MIN_DAYS == 30
    assert tuple(aging.TIERS) == ((60, 8), (45, 5), (30, 3))


# --- last_reduction -----------------------------------------------------------------

def d(n):
    return D0 + timedelta(days=n)


@pytest.mark.parametrize("history, expected", [
    ([], None),
    ([(d(0), 100)], None),
    ([(d(0), None), (d(1), 100)], None),
    ([(d(0), 100), (d(5), 90)], d(5)),
    ([(d(0), 100), (d(5), 90), (d(9), 95)], d(5)),            # subida não reinicia
    ([(d(0), 100), (d(5), 90), (d(9), 95), (d(12), 93)], d(12)),  # queda relativa ao anterior conhecido
    ([(d(0), 100), (d(3), None), (d(7), 80)], d(7)),           # None ignorado
    ([(d(0), 100), (d(3), 90), (d(4), None), (d(8), 90)], d(3)),  # igual depois de None não é queda
    ([(d(0), 100), (d(2), 120)], None),
])
def test_last_reduction(history, expected):
    assert aging.last_reduction(history) == expected


# --- team_view ---------------------------------------------------------------------

def _at(day: date) -> str:
    return db.to_iso(datetime.combine(day, time(10, 0), tzinfo=db.TZ))


def _set_first_seen(conn, ext, day):
    conn.execute("UPDATE cars SET first_seen = ? WHERE external_id = ?", (_at(day), ext))
    conn.execute("UPDATE price_history SET recorded_at = ? WHERE car_id = (SELECT id FROM cars WHERE external_id = ?)"
                 " AND recorded_at = (SELECT MIN(recorded_at) FROM price_history WHERE car_id ="
                 " (SELECT id FROM cars WHERE external_id = ?))", (_at(day), ext, ext))
    conn.commit()


def _by_id(view):
    return {c["id"]: c for c in view["cars"]}


def _car_id(conn, ext):
    return conn.execute("SELECT id FROM cars WHERE external_id = ?", (ext,)).fetchone()[0]


def test_baseline_is_estimate_and_new_car_exact(conn, car):
    run_poll(conn, [car("1"), car("2")])
    run_poll(conn, [car("1"), car("2"), car("3")])
    flags = {r[0]: r[1] for r in conn.execute("SELECT external_id, in_baseline FROM cars")}
    assert flags == {"1": 1, "2": 1, "3": 0}
    today = db.now().date()
    view = aging.team_view(conn, today=today + timedelta(days=10))
    cars = _by_id(view)
    assert cars[_car_id(conn, "1")]["since_is_estimate"] is True
    assert cars[_car_id(conn, "3")]["since_is_estimate"] is False
    assert all(c["days_in_stock"] == 10 for c in cars.values())
    assert all(c["since"] == today for c in cars.values())


def test_listed_at_earlier_moves_since_back(conn, car):
    """listed_at (Last-Modified da foto) é >= cadastro real: since = min(first_seen, listed_at),
    e no baseline continua sendo estimativa ("há pelo menos")."""
    run_poll(conn, [car("1")])
    conn.execute("UPDATE cars SET listed_at = ? WHERE external_id = '1'", (_at(D0),))
    conn.commit()
    c = aging.team_view(conn, today=d(40))["cars"][0]
    assert c["since"] == D0 and c["days_in_stock"] == 40 and c["since_is_estimate"] is True
    assert c["suggestion"]["pct"] == 3


def test_listed_at_later_than_first_seen_ignored(conn, car):
    run_poll(conn, [car("1")])
    _set_first_seen(conn, "1", D0)
    conn.execute("UPDATE cars SET listed_at = ? WHERE external_id = '1'", (_at(d(20)),))
    conn.commit()
    assert aging.team_view(conn, today=d(40))["cars"][0]["since"] == D0


def test_car_after_baseline_with_listed_at_is_exact(conn, car):
    run_poll(conn, [car("1")])
    run_poll(conn, [car("1"), car("2")])
    conn.execute("UPDATE cars SET listed_at = ? WHERE external_id = '2'", (_at(D0),))
    conn.commit()
    c = _by_id(aging.team_view(conn, today=d(40)))[_car_id(conn, "2")]
    assert c["since"] == D0 and c["since_is_estimate"] is False


@pytest.mark.parametrize("age, pct", [(29, None), (30, 3), (44, 3), (45, 5), (59, 5), (60, 8)])
def test_view_suggestion_by_age(conn, car, age, pct):
    run_poll(conn, [car("1", price=12_990_000)])
    _set_first_seen(conn, "1", D0)
    c = aging.team_view(conn, today=d(age))["cars"][0]
    assert c["days_in_stock"] == age
    assert (c["suggestion"] or {}).get("pct") == pct
    assert c["last_price_change"] is None


def test_clock_restarts_after_reduction(conn, car):
    run_poll(conn, [car("1", price=12_990_000)])
    _set_first_seen(conn, "1", D0)
    run_price_check(conn, [car("1", price=12_590_000)])  # redução
    conn.execute("UPDATE price_history SET recorded_at = ? WHERE price_cents = 12590000", (_at(d(50)),))
    conn.commit()
    c = aging.team_view(conn, today=d(70))["cars"][0]
    assert c["days_in_stock"] == 70
    assert c["last_price_change"] == d(50) and c["days_since_price_change"] == 20
    assert c["suggestion"] is None                     # relógio = 20 < 30
    c = aging.team_view(conn, today=d(95))["cars"][0]  # 45 dias desde a redução
    assert c["suggestion"]["pct"] == 5
    assert c["suggestion"]["new_price_cents"] == aging.round_down_900(12_590_000 * 95 // 100)


def test_price_rise_does_not_restart_clock(conn, car):
    run_poll(conn, [car("1", price=12_990_000)])
    _set_first_seen(conn, "1", D0)
    run_price_check(conn, [car("1", price=13_490_000)])
    conn.execute("UPDATE price_history SET recorded_at = ? WHERE price_cents = 13490000", (_at(d(40)),))
    conn.commit()
    c = aging.team_view(conn, today=d(46))["cars"][0]
    assert c["last_price_change"] is None
    assert c["suggestion"]["pct"] == 5
    assert c["suggestion"]["new_price_cents"] == aging.round_down_900(13_490_000 * 95 // 100)  # sobre o preço atual


def test_none_price_no_suggestion(conn, car):
    run_poll(conn, [car("1", price=None)])
    _set_first_seen(conn, "1", D0)
    c = aging.team_view(conn, today=d(90))["cars"][0]
    assert c["suggestion"] is None and c["price_cents"] is None


def test_order_and_summary(conn, car):
    from app.services.queries import mark_ticket_done

    run_poll(conn, [car("1"), car("2"), car("9")])
    run_poll(conn, [car("1"), car("2"), car("9"), car("3"), car("4")])  # 3 e 4 com chamado
    mark_ticket_done(conn, 1)                                           # carro 3 postado
    _set_first_seen(conn, "1", d(-70))
    _set_first_seen(conn, "2", d(-35))
    _set_first_seen(conn, "3", d(-5))
    _set_first_seen(conn, "4", d(-5))
    _set_first_seen(conn, "9", d(-100))
    # 9 vendido há 10 dias: ausente 2x, depois ajusta sold_at
    run_poll(conn, [car("1"), car("2"), car("3"), car("4")])
    run_poll(conn, [car("1"), car("2"), car("3"), car("4")])
    conn.execute("UPDATE cars SET sold_at = ? WHERE external_id = '9'", (_at(d(-10)),))
    conn.commit()
    view = aging.team_view(conn, today=D0)
    ids = [c["id"] for c in view["cars"]]
    assert ids == [_car_id(conn, "1"), _car_id(conn, "2"), _car_id(conn, "3"), _car_id(conn, "4")]
    s = view["summary"]
    assert s["in_stock"] == 4
    assert s["posted"] == 3          # 1 e 2 (baseline) + 3 (feito)
    assert s["pending"] == 1         # carro 4
    assert s["sold_30d"] == 1
    assert s["avg_days"] == round((70 + 35 + 5 + 5) / 4)
    assert s["over_30"] == 2 and s["over_60"] == 1


def test_sold_30d_window(conn, car):
    run_poll(conn, [car("1"), car("2"), car("3")])
    for ext in ("2", "3"):
        conn.execute("UPDATE cars SET active = 0, sold_at = ? WHERE external_id = ?",
                     (_at(d(-30) if ext == "2" else d(-31)), ext))
    conn.commit()
    assert aging.team_view(conn, today=D0)["summary"]["sold_30d"] == 1


def test_empty_view(conn):
    view = aging.team_view(conn, today=D0)
    assert view["cars"] == []
    assert view["summary"] == {**view["summary"], "in_stock": 0, "avg_days": 0, "over_30": 0, "over_60": 0}


def test_migration_backfills_in_baseline(conn, car):
    """Banco antigo (sem in_baseline): o backfill marca os carros da 1ª coleta."""
    run_poll(conn, [car("1"), car("2")])
    run_poll(conn, [car("1"), car("2"), car("3")])
    _set_first_seen(conn, "1", D0)
    _set_first_seen(conn, "2", D0)
    _set_first_seen(conn, "3", d(5))
    conn.execute("UPDATE cars SET in_baseline = 0")
    conn.commit()
    db.migrate(conn)
    flags = {r[0]: r[1] for r in conn.execute("SELECT external_id, in_baseline FROM cars")}
    assert flags == {"1": 1, "2": 1, "3": 0}
    conn.execute("UPDATE cars SET in_baseline = 0 WHERE external_id = '2'")
    conn.commit()
    db.migrate(conn)  # já existe flag: não refaz o backfill
    assert conn.execute("SELECT in_baseline FROM cars WHERE external_id = '2'").fetchone()[0] == 0


def test_equipe_page_render(client, conn, car):
    run_poll(conn, [car("1", "Baseline Onix 2020", price=12_990_000), car("2", "Baseline Uno 2010", price=None, photo=None)])
    run_poll(conn, [car("1", "Baseline Onix 2020", price=12_990_000), car("2", "Baseline Uno 2010", price=None, photo=None),
                    car("3", "Novo Compass 2023")])
    _set_first_seen(conn, "1", db.now().date() - timedelta(days=46))
    html = client.get("/equipe").text
    assert "Baseline Onix 2020" in html and "Novo Compass 2023" in html and "Baseline Uno 2010" in html
    assert html.count("há pelo menos") >= 2 and "None" not in html
    assert "baixar 5%" in html and "R$ 122.900" in html  # -5%: 129.900 → 123.405 → 122.900
    novo = html[html.index("Novo Compass 2023"):][:1500]
    assert "há pelo menos" not in novo.split("Baseline")[0]
