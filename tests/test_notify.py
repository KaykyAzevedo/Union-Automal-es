"""F5 — aviso por WhatsApp via CallMeBot (Lupa: app/services/notify.py). HTTP sempre mockado."""
from __future__ import annotations

import logging

import httpx
import pytest

from app.services import notify as nt

TOAST = nt._toast


class Gateway:
    """CallMeBot falso: registra as chamadas e responde conforme `mode`."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.mode = "ok"

    def handler(self, request):
        self.requests.append(request)
        if self.mode == "timeout":
            raise httpx.ConnectTimeout("lento", request=request)
        if self.mode == "500":
            return httpx.Response(500, text="erro")
        if self.mode == "badkey":
            return httpx.Response(200, text="APIKey is invalid. Please check")
        return httpx.Response(200, text="Message queued. You will receive it in a few seconds.")

    @property
    def texts(self):
        return [r.url.params["text"] for r in self.requests]


@pytest.fixture
def gw(monkeypatch):
    g = Gateway()
    real = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(g.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(nt.httpx, "Client", factory)
    monkeypatch.setattr(nt, "_toast", lambda *a, **k: None)  # sem toast do Windows nos testes
    monkeypatch.delenv("UNION_DISABLE_NOTIFY", raising=False)
    monkeypatch.setenv("WHATSAPP_PHONE", "55 (21) 99999-8888")
    monkeypatch.setenv("CALLMEBOT_APIKEY", "APIKEY-SECRETA-123")
    monkeypatch.setenv("APP_URL", "union.vercel.app/")
    return g


def test_send_whatsapp_params(gw):
    assert nt.send_whatsapp("Olá *mundo* & acentuação ✓") is True
    (req,) = gw.requests
    assert str(req.url).startswith(nt.CALLMEBOT_URL)
    p = req.url.params
    assert p["phone"] == "+5521999998888"
    assert p["apikey"] == "APIKEY-SECRETA-123"
    assert p["text"] == "Olá *mundo* & acentuação ✓"


def test_text_truncated(gw):
    nt.send_whatsapp("x" * 5000)
    assert len(gw.texts[0]) <= nt.MAX_TEXT


@pytest.mark.parametrize("mode", ["timeout", "500", "badkey"])
def test_failures_return_false_never_raise(gw, mode):
    gw.mode = mode
    assert nt.send_whatsapp("teste") is False
    nt.notify("título", "corpo")  # não levanta


@pytest.mark.parametrize("missing", ["WHATSAPP_PHONE", "CALLMEBOT_APIKEY"])
def test_not_configured_no_request(gw, monkeypatch, missing):
    monkeypatch.delenv(missing)
    assert nt.send_whatsapp("x") is False
    nt.notify("t", "b")
    assert gw.requests == []


def test_disabled_flag(gw, monkeypatch):
    monkeypatch.setenv("UNION_DISABLE_NOTIFY", "1")
    nt.notify("t", "b")
    nt.notify_new_cars([{"id": 1, "name": "Onix", "price_cents": 1}])
    assert gw.requests == []


def test_apikey_never_logged(gw, caplog):
    caplog.set_level(logging.DEBUG)
    gw.mode = "500"
    nt.send_whatsapp("teste")
    gw.mode = "timeout"
    nt.send_whatsapp("teste")
    assert "APIKEY-SECRETA-123" not in caplog.text


def test_app_url_normalized(gw, monkeypatch):
    assert nt.app_url() == "https://union.vercel.app"
    monkeypatch.setenv("APP_URL", "https://x.dev/")
    assert nt.app_url() == "https://x.dev"
    monkeypatch.delenv("APP_URL")
    monkeypatch.delenv("UNION_PANEL_URL", raising=False)
    assert nt.app_url() == "http://127.0.0.1:8000"


def test_new_cars_individual_messages(gw):
    nt.notify_new_cars([{"id": 7, "name": "Chevrolet Onix 2020", "price_cents": 12_990_000},
                        {"id": 8, "name": "Fiat Uno", "price_cents": None}])
    assert len(gw.texts) == 2
    assert "Chevrolet Onix 2020" in gw.texts[0] and "R$ 129.900,00" in gw.texts[0]
    assert "https://union.vercel.app/editor/7" in gw.texts[0]
    assert "Sob consulta" in gw.texts[1] and "None" not in gw.texts[1]


def test_many_new_cars_single_summary(gw):
    nt.notify_new_cars([{"id": i, "name": f"Carro {i}", "price_cents": 1_000_000} for i in range(10)])
    assert len(gw.texts) == 1 and "10" in gw.texts[0]


def test_sold_messages(gw):
    nt.notify_sold(["Jeep Compass 2022"])
    assert len(gw.texts) == 1 and "Vendido" in gw.texts[0] and "Jeep Compass 2022" in gw.texts[0]
    gw.requests.clear()
    nt.notify_sold([f"C{i}" for i in range(5)])
    assert len(gw.texts) == 1


def test_price_drops_message(gw):
    nt.notify_price_drops(2)
    assert len(gw.texts) == 1 and "2" in gw.texts[0]
    gw.requests.clear()
    nt.notify_price_drops(0)
    assert gw.requests == []


def test_pure_texts():
    assert "R$ 129.900,00" in nt.new_car_text(3, "Onix", 12_990_000)
    assert "Sob consulta" in nt.new_car_text(3, "Onix", None)
    assert "Onix" in nt.sold_text("Onix")


def test_vercel_skips_toast(gw, monkeypatch):
    import sys
    import types

    shown = []

    class FakeNotification:
        def __init__(self, **kw):
            self.kw = kw

        def add_actions(self, **kw):
            pass

        def show(self):
            shown.append(self.kw["title"])

    monkeypatch.setitem(sys.modules, "winotify", types.SimpleNamespace(Notification=FakeNotification))
    monkeypatch.setattr(nt, "_toast", TOAST)  # o real (a fixture gw desliga)
    monkeypatch.setenv("VERCEL", "1")
    nt.notify("t1", "b")
    assert shown == [] and len(gw.requests) == 1
    monkeypatch.delenv("VERCEL")
    nt.notify("t2", "b")
    assert shown == ["t2"] and len(gw.requests) == 2


# --- integração com os jobs: falha do WhatsApp não quebra o job -----------------

@pytest.mark.parametrize("mode", ["ok", "timeout", "500", "badkey"])
def test_poll_job_survives_whatsapp_failure(client, site, car, gw, mode):
    gw.mode = mode
    site.cars = [car("1")]
    assert client.post("/admin/check-now", follow_redirects=False).headers["location"] == "/?msg=checked"
    site.cars = [car("1"), car("2", "Jeep Compass 2022"), car("3", "Fiat Uno", price=None)]
    r = client.post("/admin/check-now", follow_redirects=False)
    assert r.headers["location"] == "/?msg=checked"
    assert client.get("/health").json()["stats"]["pending"] == 2
    assert len(gw.requests) == 2  # 1 mensagem por carro novo (≤3)
    assert all("Compass" in t or "Uno" in t for t in gw.texts)


def test_sold_and_price_drop_trigger_whatsapp(client, site, car, gw):
    site.cars = [car("1", price=10_000_000), car("2", "Jeep Compass 2022")]
    client.post("/admin/check-now")
    site.cars = [car("1", price=10_000_000)]
    client.post("/admin/check-now")
    client.post("/admin/check-now")      # 2ª ausência → vendido
    assert any("Vendido" in t and "Compass" in t for t in gw.texts)
    site.cars = [car("1", price=9_000_000)]
    gw.requests.clear()
    client.post("/admin/price-check-now")
    assert len(gw.requests) == 1


def test_empty_lists_send_nothing(gw):
    nt.notify_new_cars([])
    nt.notify_sold([])
    nt.notify_price_drops(0)
    assert gw.requests == []
