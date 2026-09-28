"""F5 — /cron/poll e /cron/price-check protegidos por `Authorization: Bearer <CRON_SECRET>`."""
from __future__ import annotations

import pytest

from app.scraper import ScraperError

SECRET = "s3cr3t-cron-token"
JOBS = [("/cron/poll", "poll"), ("/cron/price-check", "price_check")]


@pytest.fixture
def cron_env(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", SECRET)


def _auth(token=SECRET):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("path, _job", JOBS)
def test_without_cron_secret_configured_is_503(client, site, path, _job):
    r = client.get(path, headers=_auth("qualquer"))
    assert r.status_code == 503
    assert site.calls == 0


@pytest.mark.parametrize("path, _job", JOBS)
@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer errado"}, {"Authorization": SECRET}, {"Authorization": f"Basic {SECRET}"},
    {"Authorization": "Bearer "}, {"Authorization": f"Bearer {SECRET}x"},
])
def test_bad_or_missing_bearer_is_401(client, site, cron_env, path, _job, headers):
    r = client.get(path, headers=headers, follow_redirects=False)
    assert r.status_code == 401
    assert site.calls == 0


def test_cron_secret_not_accepted_in_query_string(client, site, cron_env):
    assert client.get(f"/cron/poll?token={SECRET}").status_code == 401
    assert site.calls == 0


def test_poll_runs_job(client, site, cron_env, car):
    site.cars = [car("1"), car("2")]
    r = client.get("/cron/poll", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["job"] == "poll"
    assert body["result"]["baseline"] is True and body["result"]["found"] == 2
    assert site.calls == 1
    # idempotente: 2ª chamada não cria chamados
    body = client.get("/cron/poll", headers=_auth()).json()
    assert body["result"]["new"] == 0
    assert client.get("/health").json()["stats"]["pending"] == 0


def test_price_check_runs_job(client, site, cron_env, car):
    site.cars = [car("1", price=10_000_000)]
    client.get("/cron/poll", headers=_auth())
    site.cars = [car("1", price=9_000_000)]
    r = client.get("/cron/price-check", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body == {**body, "ok": True, "job": "price_check"}
    assert body["result"]["drops"] == 1
    assert client.get("/health").json()["stats"]["alerts"] == 1


@pytest.mark.parametrize("path, job", JOBS)
def test_scraper_error_is_502_and_changes_nothing(client, site, cron_env, car, path, job):
    site.cars = [car("1"), car("2")]
    client.get("/cron/poll", headers=_auth())
    site.error = ScraperError("site fora")
    r = client.get(path, headers=_auth())
    assert r.status_code == 502
    body = r.json()
    assert body["ok"] is False and body["job"] == job and body["error"]
    assert client.get("/health").json()["stats"]["total_cars"] == 2


def test_cron_bypasses_login_on_vercel(client, site, cron_env, monkeypatch, car):
    """Na nuvem o painel exige senha, mas o agendador só manda o Bearer."""
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("APP_PASSWORD", "senha-painel")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    site.cars = [car("1")]
    r = client.get("/cron/poll", headers=_auth(), follow_redirects=False)
    assert r.status_code == 200 and r.json()["ok"] is True
    r = client.get("/cron/poll", follow_redirects=False)
    assert r.status_code == 401  # sem Bearer: 401, não redirect para /login


def test_cron_poll_notifies_new_car(client, site, cron_env, car, monkeypatch):
    from app.services import jobs

    sent = []
    monkeypatch.setattr(jobs, "notify_new_cars", lambda cars: sent.append(list(cars)))
    site.cars = [car("1")]
    client.get("/cron/poll", headers=_auth())
    site.cars = [car("1"), car("2", "Jeep Compass 2022")]
    client.get("/cron/poll", headers=_auth())
    sent = [s for s in sent if s]
    assert len(sent) == 1 and [c["name"] for c in sent[0]] == ["Jeep Compass 2022"]


def test_scheduler_off_on_vercel(site, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("APP_PASSWORD", "x")
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    monkeypatch.delenv("UNION_DISABLE_SCHEDULER", raising=False)
    with TestClient(app):
        assert app.state.scheduler is None
