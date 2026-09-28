"""Rotas FastAPI + templates (TestClient, scraper mockado via fixture `site`)."""
from __future__ import annotations

from app import db
from app.services.prices import run_price_check
from app.services.sync import run_poll


def _seed(car, *polls, price_check=None):
    conn = db.connect()
    try:
        for stock in polls:
            run_poll(conn, stock)
        if price_check:
            run_price_check(conn, price_check)
    finally:
        conn.close()


def test_empty_dashboard_and_cars(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Tudo em dia" in r.text
    r = client.get("/cars")
    assert r.status_code == 200
    assert "Nenhum carro ainda" in r.text


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["jobs"] == []
    assert body["stats"]["pending"] == 0


def test_dashboard_renders_ticket_with_none_price_and_photo(client, car):
    _seed(car, [car("1")], [car("1"), car("2", "Fiat Uno Mille 2010", price=None, photo=None)])
    r = client.get("/")
    assert r.status_code == 200
    assert "Fiat Uno Mille 2010" in r.text
    assert "Sob consulta" in r.text
    assert "photo-placeholder" in r.text
    assert 'hx-post="/tickets/1/done"' in r.text
    assert "None" not in r.text


def test_cars_page_prices_and_status(client, car):
    _seed(car, [car("1", "Onix 2020", price=8_990_000), car("2", "HB20 2019", price=None, photo=None)],
          [car("1", "Onix 2020", price=8_990_000)], [car("1", "Onix 2020", price=8_990_000)])
    r = client.get("/cars")
    assert "R$ 89.900,00" in r.text
    assert "Sob consulta" in r.text
    assert "Vendido" in r.text
    assert "None" not in r.text


def test_ticket_done_returns_partial_and_is_idempotent(client, car):
    _seed(car, [car("1")], [car("1"), car("2", "Renegade 2021")])
    r = client.post("/tickets/1/done")
    assert r.status_code == 200
    assert 'id="ticket-1"' in r.text and "Postado" in r.text
    assert "<html" not in r.text.lower()  # partial, não página inteira
    assert client.post("/tickets/1/done").status_code == 200
    assert "Renegade 2021" not in client.get("/").text
    assert client.get("/health").json()["stats"]["done_today"] == 1


def test_ticket_done_404(client):
    assert client.post("/tickets/999/done").status_code == 404


def test_alert_render_and_dismiss(client, car):
    _seed(car, [car("1", "Compass 2022", price=15_000_000)],
          price_check=[car("1", "Compass 2022", price=14_250_000)])
    html = client.get("/").text
    assert "R$ 150.000,00" in html and "R$ 142.500,00" in html
    assert "R$ 7.500,00" in html and "5,0%" in html
    assert 'hx-post="/alerts/1/dismiss"' in html
    r = client.post("/alerts/1/dismiss")
    assert r.status_code == 200 and r.text == ""
    assert "Compass 2022" not in client.get("/").text


def test_alert_dismiss_404(client):
    assert client.post("/alerts/999/dismiss").status_code == 404


def test_admin_check_now_redirects(client, site, car):
    site.cars = [car("1")]
    r = client.post("/admin/check-now", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?msg=checked"
    assert site.calls == 1


def test_admin_check_now_scraper_error_changes_nothing(client, site, car):
    from app.scraper import ScraperError

    site.cars = [car("1"), car("2")]
    client.post("/admin/check-now")
    site.error = ScraperError("site fora")
    r = client.post("/admin/check-now", follow_redirects=False)
    assert r.headers["location"] == "/?msg=check_failed"
    stats = client.get("/health").json()["stats"]
    assert stats["total_cars"] == 2 and stats["pending"] == 0
    assert "Falha ao consultar o site" in client.get("/?msg=check_failed").text


def test_admin_price_check_now_redirects(client, site, car):
    site.cars = [car("1")]
    r = client.post("/admin/price-check-now", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?msg=price_checked"


def test_price_history_api(client, car):
    _seed(car, [car("1", price=10_000_000)], price_check=[car("1", price=9_000_000)])
    r = client.get("/api/cars/1/prices")
    assert [p["price_cents"] for p in r.json()] == [10_000_000, 9_000_000]


# --- F3: carro vendido -------------------------------------------------------

def _sell(car):
    """Baseline com 1 e 2 (+ chamado p/ 3); 2 coletas sem 2 e 3 → vendidos."""
    base = [car("1", "Onix 2020"), car("2", "Fiat Uno Mille 2010", price=None, photo=None)]
    keep = [car("1", "Onix 2020")]
    _seed(car, base, base + [car("3", "Renegade 2021")], keep, keep)


def test_dashboard_shows_sold_card_with_none_price_and_photo(client, car):
    _sell(car)
    html = client.get("/").text
    assert "Fiat Uno Mille 2010" in html and "Renegade 2021" in html
    assert 'hx-post="/sold/1/dismiss"' in html and 'hx-post="/sold/2/dismiss"' in html
    assert "None" not in html
    stats = client.get("/health").json()["stats"]
    assert stats["sold"] == 2 and stats["pending"] == 0 and stats["total_cars"] == 1


def test_cancelled_ticket_not_on_dashboard(client, car):
    _sell(car)
    html = client.get("/").text
    assert 'hx-post="/tickets/1/done"' not in html


def test_sold_dismiss(client, car):
    _sell(car)
    r = client.post("/sold/1/dismiss")
    assert r.status_code == 200 and r.text == ""
    assert client.post("/sold/1/dismiss").status_code == 200  # idempotente
    assert 'hx-post="/sold/1/dismiss"' not in client.get("/").text
    assert client.get("/health").json()["stats"]["sold"] == 1


def test_sold_dismiss_404(client):
    assert client.post("/sold/999/dismiss").status_code == 404


def test_cars_page_sold_status(client, car):
    _sell(car)
    html = client.get("/cars").text
    assert html.count("Vendido") >= 2
    assert "None" not in html


def test_scraper_errors_do_not_count_as_absence(client, site, car):
    from app.scraper import ScraperError

    site.cars = [car("1"), car("2")]
    client.post("/admin/check-now")
    site.cars = [car("1")]
    client.post("/admin/check-now")                 # 1 falta
    site.error = ScraperError("site fora")
    for _ in range(3):
        client.post("/admin/check-now")
        client.post("/admin/price-check-now")
    stats = client.get("/health").json()["stats"]
    assert stats["sold"] == 0 and stats["total_cars"] == 2


def test_poll_job_notifies_sold(client, site, car, monkeypatch):
    from app.services import jobs

    calls = []
    monkeypatch.setattr(jobs, "notify_sold", lambda names: calls.append(list(names)))
    site.cars = [car("1"), car("2", "Compass 2022")]
    client.post("/admin/check-now")
    site.cars = [car("1")]
    client.post("/admin/check-now")
    assert [c for c in calls if c] == []
    client.post("/admin/check-now")
    assert [c for c in calls if c] == [["Compass 2022"]]
