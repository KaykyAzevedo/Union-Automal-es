"""F5 — detail_cache no banco: instâncias diferentes (memória vazia) reaproveitam o detalhe raspado."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from dataclasses import asdict, replace
from datetime import timedelta

import pytest

from app import db
from app.encarte import source
from app.scraper.detail import parse_detail
from conftest import FIXTURES, ROOT, make_car, requires_pg

URL = "https://www.unionrioveiculos.com.br/Veiculo/compass-2.0-td350-turbo-diesel-longitude-at9-diesel-2023/5462415/detalhes"
DETAIL = parse_detail((FIXTURES / "detail_blindado_compass_gr_5462415.html").read_text(encoding="utf-8"), URL)


@pytest.fixture
def the_car(conn):
    from app.services import queries
    from app.services.sync import run_poll

    run_poll(conn, [replace(make_car("5462415"), url=URL)])
    return queries.get_car(conn, conn.execute("SELECT id FROM cars").fetchone()[0])


@pytest.fixture
def scrape(monkeypatch):
    import app.scraper.detail as detail_mod

    state = {"calls": 0}

    def fake(url, client=None):
        state["calls"] += 1
        return DETAIL

    monkeypatch.setattr(detail_mod, "scrape_detail", fake)
    return state


def _new_instance():
    """Outra função serverless: mesma base, memória vazia."""
    source.clear_cache()


def test_detail_written_to_db(conn, the_car, scrape):
    source.get_detail(the_car, conn)
    row = conn.execute("SELECT external_id, data, fetched_at FROM detail_cache").fetchone()
    assert row["external_id"] == "5462415"
    data = json.loads(row["data"])
    assert data["photos"] == DETAIL.photos and data["armor_company"] == "GR Vidros Eternity"
    assert db.from_iso(row["fetched_at"]) is not None


def test_other_instance_reuses_db_cache(conn, the_car, scrape):
    first = source.get_detail(the_car, conn)
    _new_instance()
    second = source.get_detail(the_car, conn)
    assert scrape["calls"] == 1
    assert asdict(second) == asdict(first) == asdict(DETAIL)


def test_get_detail_without_conn_uses_env_db(the_car, scrape):
    source.get_detail(the_car)
    _new_instance()
    source.get_detail(the_car)
    assert scrape["calls"] == 1


def test_ttl_expired_scrapes_again(conn, the_car, scrape):
    source.get_detail(the_car, conn)
    old = db.to_iso(db.now() - timedelta(seconds=source.TTL_SECONDS + 5))
    conn.execute("UPDATE detail_cache SET fetched_at = ?", (old,))
    conn.commit()
    _new_instance()
    source.get_detail(the_car, conn)
    assert scrape["calls"] == 2
    fresh = conn.execute("SELECT fetched_at FROM detail_cache").fetchone()[0]
    assert fresh > old
    assert conn.execute("SELECT COUNT(*) FROM detail_cache").fetchone()[0] == 1  # upsert, sem duplicar


def test_clear_cache_with_conn_deletes_rows(conn, the_car, scrape):
    source.get_detail(the_car, conn)
    source.clear_cache(conn)
    assert conn.execute("SELECT COUNT(*) FROM detail_cache").fetchone()[0] == 0
    source.get_detail(the_car, conn)
    assert scrape["calls"] == 2


def test_corrupt_cache_row_is_ignored(conn, the_car, scrape):
    source.get_detail(the_car, conn)
    conn.execute("UPDATE detail_cache SET data = ?", ("{isso não é json",))
    conn.commit()
    _new_instance()
    assert asdict(source.get_detail(the_car, conn)) == asdict(DETAIL)
    assert scrape["calls"] == 2


def test_scraper_error_not_cached(conn, the_car, monkeypatch):
    import app.scraper.detail as detail_mod
    from app.scraper import ScraperError

    def boom(url, client=None):
        raise ScraperError("fora")

    monkeypatch.setattr(detail_mod, "scrape_detail", boom)
    with pytest.raises(ScraperError):
        source.get_detail(the_car, conn)
    assert conn.execute("SELECT COUNT(*) FROM detail_cache").fetchone()[0] == 0


WORKER = textwrap.dedent('''
    import sys, time, pathlib
    sys.path.insert(0, sys.argv[1])
    import app.scraper.detail as d
    from app import db
    from app.encarte import source
    from app.services import queries
    log = pathlib.Path(sys.argv[2])
    real = d.parse_detail(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"), sys.argv[4])
    def fake(url, client=None):
        with open(log, "a") as f:
            f.write("scrape\\n")
        time.sleep(1.0)
        return real
    d.scrape_detail = fake
    conn = db.connect()
    car = queries.get_car(conn, int(sys.argv[5]))
    print(source.get_detail(car, conn).external_id)
''')


@requires_pg
def test_single_flight_across_processes(conn, the_car, tmp_path):
    """6 processos (instâncias) pedem o mesmo detalhe juntos: pg_advisory_lock → 1 scrape."""
    script = tmp_path / "worker.py"
    script.write_text(WORKER, encoding="utf-8")
    log = tmp_path / "scrapes.log"
    env = {**os.environ}
    procs = [subprocess.Popen([sys.executable, str(script), str(ROOT), str(log),
                               str(FIXTURES / "detail_blindado_compass_gr_5462415.html"), URL, str(the_car.id)],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(6)]
    outs = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), [o[1][-500:] for o in outs]
    assert [o[0].strip() for o in outs] == ["5462415"] * 6
    assert log.read_text().count("scrape") == 1


@requires_pg
def test_postgres_migration_from_old_schema(conn):
    """Banco Postgres criado antes da F3 (sem missing_count/sold_at/sold_alerts/detail_cache)."""
    for t in reversed(db.TABLES):
        conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    conn.execute("""CREATE TABLE cars (id SERIAL PRIMARY KEY, external_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
        name_key TEXT NOT NULL, photo_url TEXT, price_cents INTEGER, url TEXT NOT NULL,
        posted INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)""")
    conn.execute("INSERT INTO cars (external_id, name, name_key, url, first_seen, last_seen) "
                 "VALUES ('1', 'Onix', 'onix', 'u', '2026-09-01T10:00:00-03:00', '2026-09-01T10:00:00-03:00')")
    conn.commit()
    db.init_db(conn)
    db.init_db(conn)  # idempotente
    cols = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'cars'")}
    assert {"missing_count", "sold_at"} <= cols
    assert tuple(conn.execute("SELECT external_id, missing_count, sold_at FROM cars").fetchone()) == ("1", 0, None)
    for t in ("sold_alerts", "detail_cache", "tickets", "price_alerts", "price_history", "runs"):
        conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
    # INSERT depois da migração continua gerando id (sequência intacta)
    from app.services.sync import run_poll

    run_poll(conn, [make_car("1", "Onix"), make_car("2")])
    assert conn.execute("SELECT COUNT(*) FROM cars").fetchone()[0] == 2
