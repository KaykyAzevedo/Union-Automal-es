"""F5 — locks consultivos no Postgres: um lock vazado (ex.: pooler do Neon em modo transação,
função da Vercel morta no meio) não pode travar cron nem editor para sempre."""
from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from app import db
from conftest import make_car, requires_pg



HANG_LIMIT = 45  # s: bem abaixo do maxDuration da função; o app tem de desistir antes disso


def _hold(key: str):
    """Outra sessão segura o lock e não solta (o que o pooler faz com um lock vazado)."""
    holder = db.connect()
    holder.execute("SELECT pg_advisory_lock(hashtext(?))", (key,))
    holder.commit()
    return holder


def _run_with_deadline(fn, seconds):
    box = {}

    def target():
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    return t.is_alive(), box


@requires_pg
def test_leaked_jobs_lock_does_not_hang_cron(client, site, car, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "c")
    site.cars = [car("1")]
    holder = _hold("union:jobs")
    try:
        alive, box = _run_with_deadline(
            lambda: client.get("/cron/poll", headers={"Authorization": "Bearer c"}), HANG_LIMIT)
        assert not alive, f"/cron/poll ficou preso > {HANG_LIMIT}s esperando pg_advisory_lock('union:jobs')"
        assert box["result"].status_code in (409, 423, 429, 502, 503)
    finally:
        holder.close()


@requires_pg
def test_leaked_detail_lock_does_not_hang_editor(conn, monkeypatch):
    import app.scraper.detail as detail_mod
    from app.encarte import source
    from app.services import queries
    from app.services.sync import run_poll

    run_poll(conn, [replace(make_car("777"))])
    the_car = queries.get_car(conn, conn.execute("SELECT id FROM cars").fetchone()[0])
    monkeypatch.setattr(detail_mod, "scrape_detail", lambda url, client=None: pytest.fail("não deveria raspar"))
    holder = _hold("union:detail:777")
    try:
        alive, box = _run_with_deadline(lambda: source.get_detail(the_car), HANG_LIMIT)
        assert not alive, f"get_detail ficou preso > {HANG_LIMIT}s esperando pg_advisory_lock"
    finally:
        holder.close()


@pytest.mark.parametrize("env, expected", [
    ({"DATABASE_URL": "postgresql://u:p@ep-x-pooler.sa-east-1.aws.neon.tech/db"},
     "postgresql://u:p@ep-x.sa-east-1.aws.neon.tech/db"),
    ({"DATABASE_URL": "postgresql://u:p@ep-x-pooler.neon.tech/db", "POSTGRES_URL_NON_POOLING": "postgresql://np/db"},
     "postgresql://np/db"),
    ({"DATABASE_URL": "postgresql://u:p@ep-x-pooler.neon.tech/db", "POSTGRES_URL_NON_POOLING": "postgresql://np/db",
      "DATABASE_URL_UNPOOLED": "postgresql://unp/db"}, "postgresql://unp/db"),
    ({"DATABASE_URL": "postgresql://postgres:test@localhost:55432/x"}, "postgresql://postgres:test@localhost:55432/x"),
])
def test_lock_url_prefers_direct_connection(monkeypatch, env, expected):
    for var in ("DATABASE_URL", "POSTGRES_URL", "DATABASE_URL_UNPOOLED", "POSTGRES_URL_NON_POOLING"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert db.lock_url() == expected


@requires_pg
def test_lock_released_when_holder_dies():
    """Fechar a conexão do lock (função morta) libera para a próxima instância."""
    with db.advisory_lock(db.connect(), "union:test:die", wait=1) as got:
        assert got is True
        c = db.connect()
        try:
            with db.advisory_lock(c, "union:test:die", wait=0.5, required=False) as got2:
                assert got2 is False
        finally:
            c.close()
    c = db.connect()
    try:
        with db.advisory_lock(c, "union:test:die", wait=1) as got3:
            assert got3 is True
    finally:
        c.close()
