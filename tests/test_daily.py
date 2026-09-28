"""F7 — verificação diária única às 18:00 America/Sao_Paulo (app/services/daily.py, jobs.daily_job,
GET /cron/daily, scheduler com recuperação de execução perdida, vercel.json, poll.yml)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import db
from app.services import daily
from app.services.daily import last_scheduled, next_check, run_daily
from app.services.sync import run_poll

SP = ZoneInfo("America/Sao_Paulo")
ROOT = Path(__file__).resolve().parent.parent
SECRET = "daily-cron-secret"


def _sp(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=SP)


def _one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def _car(conn, ext):
    return conn.execute("SELECT * FROM cars WHERE external_id = ?", (ext,)).fetchone()


# --- run_daily: uma coleta aplica tudo -------------------------------------

@pytest.fixture
def stock(conn, car):
    """Baseline com 1, 2, 3 (R$ 100 mil cada)."""
    base = [car("1", "Onix 2020", price=10_000_000), car("2", "Uno 2010", price=10_000_000),
            car("3", "Gol 2015", price=10_000_000)]
    run_daily(conn, base)
    return base


def test_first_daily_is_baseline(conn, car):
    r = run_daily(conn, [car("1"), car("2")])
    assert r["baseline"] is True and r["found"] == 2
    assert r["drops"] == 0 and r["sold"] == 0
    assert _one(conn, "SELECT COUNT(*) FROM tickets") == 0


def test_daily_creates_ticket_alert_and_counts_absence_once(conn, car, stock):
    scraped = [car("1", "Onix 2020", price=9_000_000),       # queda
               car("2", "Uno 2010", price=10_000_000),       # igual
               car("4", "Renegade 2021", price=12_000_000)]  # novo; 3 ausente
    r = run_daily(conn, scraped)
    # carro novo → chamado
    assert r["new"] == 1 and len(r["new_car_ids"]) == 1
    assert _one(conn, "SELECT COUNT(*) FROM tickets WHERE status = 'pending'") == 1
    # queda → alerta (e só um)
    assert r["drops"] == 1 and len(r["alert_ids"]) == 1
    assert _one(conn, "SELECT COUNT(*) FROM price_alerts") == 1
    assert _car(conn, "1")["price_cents"] == 9_000_000
    # ausência contada UMA vez (run_poll + run_price_check sobre a mesma coleta)
    row = _car(conn, "3")
    assert (row["active"], row["missing_count"], row["sold_at"]) == (1, 1, None)
    assert r["sold"] == 0 and r["sold_car_ids"] == []
    assert _one(conn, "SELECT COUNT(*) FROM sold_alerts") == 0


def test_daily_new_car_has_no_extra_price_history(conn, car, stock):
    run_daily(conn, stock + [car("4", price=7_000_000)])
    new_id = _car(conn, "4")["id"]
    assert _one(conn, "SELECT COUNT(*) FROM price_history WHERE car_id = ?", new_id) == 1
    assert _one(conn, "SELECT COUNT(*) FROM price_alerts") == 0


def test_two_dailies_without_car_sells(conn, car, stock):
    without_3 = stock[:2]
    run_daily(conn, without_3)
    assert _car(conn, "3")["active"] == 1
    r = run_daily(conn, without_3)
    row = _car(conn, "3")
    assert r["sold"] == 1 and r["sold_car_ids"] == [row["id"]]
    assert row["active"] == 0 and row["sold_at"] is not None
    assert _one(conn, "SELECT COUNT(*) FROM sold_alerts WHERE dismissed = 0") == 1


def test_absence_then_return_resets(conn, car, stock):
    run_daily(conn, stock[:2])
    run_daily(conn, stock)
    run_daily(conn, stock[:2])
    row = _car(conn, "3")
    assert (row["active"], row["missing_count"]) == (1, 1)


def test_daily_idempotent_same_collection(conn, car, stock):
    scraped = [car("1", "Onix 2020", price=9_000_000)] + stock[1:]
    run_daily(conn, scraped)
    r = run_daily(conn, scraped)
    assert r["new"] == 0 and r["drops"] == 0 and r["changed"] == 0
    assert _one(conn, "SELECT COUNT(*) FROM price_alerts") == 1


def test_daily_result_is_json_serializable(conn, car, stock):
    r = run_daily(conn, stock[:2] + [car("9")])
    json.dumps(r)
    for key in ("new", "new_car_ids", "sold", "sold_car_ids", "checked", "changed", "drops", "alert_ids"):
        assert key in r, key


# --- next_check / last_scheduled -------------------------------------------

@pytest.mark.parametrize("at, expected", [
    (_sp(2026, 9, 28, 9, 0), _sp(2026, 9, 28, 18, 0)),          # manhã → hoje
    (_sp(2026, 9, 28, 17, 59, 59), _sp(2026, 9, 28, 18, 0)),    # 1 s antes → hoje
    (_sp(2026, 9, 28, 18, 0), _sp(2026, 9, 29, 18, 0)),         # exatamente 18:00 → amanhã
    (_sp(2026, 9, 28, 18, 0, 1), _sp(2026, 9, 29, 18, 0)),      # passou → amanhã
    (_sp(2026, 9, 28, 23, 59), _sp(2026, 9, 29, 18, 0)),
    (_sp(2026, 9, 29, 0, 0), _sp(2026, 9, 29, 18, 0)),          # virada do dia
    (_sp(2026, 9, 30, 20, 0), _sp(2026, 10, 1, 18, 0)),         # virada do mês
    (_sp(2026, 12, 31, 19, 0), _sp(2027, 1, 1, 18, 0)),         # virada do ano
])
def test_next_check(at, expected):
    got = next_check(at)
    assert got == expected
    assert got.astimezone(SP).replace(tzinfo=None) == expected.replace(tzinfo=None)


def test_next_check_accepts_utc_and_converts():
    # 22:30 UTC = 19:30 SP → amanhã 18:00 SP (21:00 UTC)
    got = next_check(datetime(2026, 9, 28, 22, 30, tzinfo=timezone.utc))
    assert got == _sp(2026, 9, 29, 18, 0)
    assert got.astimezone(timezone.utc).hour == 21
    # 20:59 UTC = 17:59 SP → hoje
    assert next_check(datetime(2026, 9, 28, 20, 59, tzinfo=timezone.utc)) == _sp(2026, 9, 28, 18, 0)
    # 02:00 UTC do dia 29 = 23:00 SP do dia 28 → dia 29 18:00
    assert next_check(datetime(2026, 9, 29, 2, 0, tzinfo=timezone.utc)) == _sp(2026, 9, 29, 18, 0)


def test_next_check_default_is_future_and_within_a_day():
    got = next_check()
    now = db.now()
    assert now < got <= now + timedelta(days=1)
    assert (got.astimezone(SP).hour, got.minute, got.second) == (18, 0, 0)


@pytest.mark.parametrize("at, expected", [
    (_sp(2026, 9, 28, 17, 0), _sp(2026, 9, 27, 18, 0)),
    (_sp(2026, 9, 28, 18, 0), _sp(2026, 9, 28, 18, 0)),
    (_sp(2026, 9, 28, 20, 0), _sp(2026, 9, 28, 18, 0)),
    (_sp(2026, 10, 1, 1, 0), _sp(2026, 9, 30, 18, 0)),
])
def test_last_scheduled(at, expected):
    assert last_scheduled(at) == expected


# --- recuperação de execução perdida (app/scheduler.py) --------------------

@pytest.fixture
def clock(monkeypatch):
    """Congela o relógio usado por daily.next_check/last_scheduled e pelo scheduler."""
    from app import scheduler as sched_mod

    def set_(at):
        monkeypatch.setattr(daily, "now", lambda: at)
        monkeypatch.setattr(sched_mod, "now", lambda: at)
        return at
    return set_


def _add_run(conn, finished, ok=1, kind="daily"):
    conn.execute("INSERT INTO runs (kind, started_at, finished_at, ok) VALUES (?, ?, ?, ?)",
                 (kind, db.to_iso(finished - timedelta(seconds=5)), db.to_iso(finished), ok))
    conn.commit()


def test_missed_when_no_runs(clock):
    from app.scheduler import missed_daily_run

    clock(_sp(2026, 9, 28, 10, 0))
    assert missed_daily_run() is True


@pytest.mark.parametrize("now_, finished, ok, missed", [
    # hoje 20:00; última ok hoje 18:05 → em dia
    (_sp(2026, 9, 28, 20, 0), _sp(2026, 9, 28, 18, 5), 1, False),
    # hoje 20:00; última ok hoje 17:00 (antes das 18:00) → perdeu a de hoje
    (_sp(2026, 9, 28, 20, 0), _sp(2026, 9, 28, 17, 0), 1, True),
    # hoje 10:00; última ok ontem 18:30 → em dia (a de hoje ainda não chegou)
    (_sp(2026, 9, 28, 10, 0), _sp(2026, 9, 27, 18, 30), 1, False),
    # hoje 10:00; última ok ontem 17:59 → perdeu a de ontem
    (_sp(2026, 9, 28, 10, 0), _sp(2026, 9, 27, 17, 59), 1, True),
    # hoje 10:00; última ok há 3 dias → perdeu
    (_sp(2026, 9, 28, 10, 0), _sp(2026, 9, 25, 18, 30), 1, True),
    # hoje 20:00; só uma execução com FALHA depois das 18:00 → perdeu
    (_sp(2026, 9, 28, 20, 0), _sp(2026, 9, 28, 18, 5), 0, True),
    # exatamente às 18:00 com última ok ontem 19:00 → perdeu a de hoje (vai rodar)
    (_sp(2026, 9, 28, 18, 0), _sp(2026, 9, 27, 19, 0), 1, True),
])
def test_missed_daily_run(conn, clock, now_, finished, ok, missed):
    from app.scheduler import missed_daily_run

    clock(now_)
    _add_run(conn, finished, ok=ok)
    assert missed_daily_run() is missed


def test_missed_ignores_failed_but_accepts_older_ok(conn, clock):
    from app.scheduler import missed_daily_run

    clock(_sp(2026, 9, 28, 20, 0))
    _add_run(conn, _sp(2026, 9, 28, 18, 1), ok=1)
    _add_run(conn, _sp(2026, 9, 28, 19, 0), ok=0)
    assert missed_daily_run() is False


def _daily_next_run(clock_at):
    from app.scheduler import create_scheduler

    s = create_scheduler()
    s.start(paused=True)
    try:
        return s.get_job("daily").next_run_time
    finally:
        s.shutdown(wait=False)


def test_scheduler_schedules_catch_up_when_missed(conn, clock):
    from app.scheduler import CATCH_UP_DELAY

    at = clock(_sp(2026, 9, 28, 20, 0))
    _add_run(conn, _sp(2026, 9, 27, 18, 10))
    assert _daily_next_run(at) == at + CATCH_UP_DELAY


def test_scheduler_catch_up_on_empty_db(clock):
    from app.scheduler import CATCH_UP_DELAY

    at = clock(_sp(2026, 9, 28, 9, 0))
    assert _daily_next_run(at) == at + CATCH_UP_DELAY


def test_scheduler_no_catch_up_when_up_to_date(conn, clock):
    at = clock(_sp(2026, 9, 28, 20, 0))
    _add_run(conn, _sp(2026, 9, 28, 18, 2))
    nxt = _daily_next_run(at)
    # sem catch-up: próxima execução é às 18:00 SP de algum dia (o trigger usa o relógio real)
    local = nxt.astimezone(SP)
    assert (local.hour, local.minute, local.second) == (18, 0, 0)
    assert nxt > db.now()


# --- GET /cron/daily ---------------------------------------------------------

@pytest.fixture
def cron_env(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", SECRET)


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer errado"}, {"Authorization": SECRET}])
def test_cron_daily_requires_bearer(client, site, cron_env, headers):
    r = client.get("/cron/daily", headers=headers, follow_redirects=False)
    assert r.status_code == 401
    assert site.calls == 0


def test_cron_daily_runs_everything(client, site, cron_env, car, conn):
    auth = {"Authorization": f"Bearer {SECRET}"}
    site.cars = [car("1", price=10_000_000), car("2"), car("3")]
    r = client.get("/cron/daily", headers=auth)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["ok"] is True and body["job"] == "daily"
    assert body["result"]["baseline"] is True and body["result"]["found"] == 3

    site.cars = [car("1", price=9_000_000), car("2"), car("4", "Compass 2022")]
    body = client.get("/cron/daily", headers=auth).json()["result"]
    assert (body["new"], body["drops"], body["sold"]) == (1, 1, 0)
    assert _car(conn, "3")["missing_count"] == 1

    body = client.get("/cron/daily", headers=auth).json()["result"]
    assert (body["new"], body["drops"], body["sold"]) == (0, 0, 1)
    assert site.calls == 3

    stats = client.get("/health").json()["stats"]
    assert stats["pending"] == 1 and stats["alerts"] == 1 and stats["sold"] == 1
    assert [tuple(x) for x in conn.execute("SELECT DISTINCT kind FROM runs")] == [("daily",)]


def test_cron_daily_uses_bigger_listed_at_batch(client, site, cron_env, car, monkeypatch):
    from app.services import scraping

    seen = []
    monkeypatch.setattr(scraping, "enrich_listed_at", lambda conn, scraped, **kw: seen.append(kw) or 0)
    site.cars = [car("1")]
    client.get("/cron/daily", headers={"Authorization": f"Bearer {SECRET}"})
    assert seen == [{"batch": scraping.DAILY_LISTED_AT_BATCH, "deadline": scraping.DAILY_LISTED_AT_DEADLINE}]
    assert scraping.DAILY_LISTED_AT_BATCH == 60 and scraping.DAILY_LISTED_AT_DEADLINE == 25.0


def test_cron_daily_notifies(client, site, cron_env, car, monkeypatch):
    from app.services import jobs

    sent = {"new": [], "drops": [], "sold": []}
    monkeypatch.setattr(jobs, "notify_new_cars", lambda cars: sent["new"].append([c["name"] for c in cars]))
    monkeypatch.setattr(jobs, "notify_price_drops", lambda n: sent["drops"].append(n))
    monkeypatch.setattr(jobs, "notify_sold", lambda names: sent["sold"].append(list(names)))
    auth = {"Authorization": f"Bearer {SECRET}"}
    site.cars = [car("1", price=10_000_000), car("2", "Uno 2010")]
    client.get("/cron/daily", headers=auth)
    site.cars = [car("1", price=9_000_000), car("3", "Compass 2022")]
    client.get("/cron/daily", headers=auth)
    client.get("/cron/daily", headers=auth)
    assert ["Compass 2022"] in sent["new"]
    assert 1 in sent["drops"]
    assert ["Uno 2010"] in sent["sold"]


# --- perfil equipe -------------------------------------------------------------

def test_staff_cannot_run_daily(client, site, car, monkeypatch):
    from app import auth

    auth.reset_failures()
    monkeypatch.setenv("APP_PASSWORD", "senha-admin-123")
    monkeypatch.setenv("STAFF_PASSWORD", "senha-equipe-456")
    monkeypatch.setenv("SESSION_SECRET", "segredo-" + "z" * 30)
    monkeypatch.setenv("CRON_SECRET", SECRET)
    r = client.post("/login", data={"password": "senha-equipe-456"}, follow_redirects=False)
    assert r.headers["location"] == "/equipe"
    site.cars = [car("1")]
    assert client.get("/cron/daily", follow_redirects=False).status_code == 401
    for path in ("/admin/check-now", "/admin/price-check-now"):
        assert client.post(path, follow_redirects=False).status_code == 403
        assert client.get(path, follow_redirects=False).status_code in (303, 403, 405)
    assert site.calls == 0
    auth.reset_failures()


# --- dashboard ----------------------------------------------------------------

def test_dashboard_shows_daily_schedule(client):
    html = client.get("/").text
    assert "15 min" not in html and "15min" not in html
    assert "Próxima" in html and "18:00" in html
    assert 'data-iso="' in html


def test_health_exposes_next_check(client):
    stats = client.get("/health").json()["stats"]
    nc = datetime.fromisoformat(stats["next_check"])
    local = nc.astimezone(SP)
    assert (local.hour, local.minute) == (18, 0)
    assert "poll" not in [j["id"] for j in client.get("/health").json()["jobs"]]


# --- deploy: vercel.json e poll.yml -------------------------------------------

def test_vercel_json_single_daily_cron():
    cfg = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    assert cfg["crons"] == [{"path": "/cron/daily", "schedule": "0 21 * * *"}]


def test_poll_workflow_manual_only():
    text = (ROOT / ".github" / "workflows" / "poll.yml").read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml:
        cfg = yaml.safe_load(text)
        on = cfg.get("on", cfg.get(True))  # PyYAML lê `on:` como True
        assert "schedule" not in on and "workflow_dispatch" in on
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not any(ln.strip().startswith("schedule:") for ln in lines)
    assert not any("cron:" in ln for ln in lines)
    body = "\n".join(lines)
    assert "/cron/daily" in body and "/cron/poll" not in body
