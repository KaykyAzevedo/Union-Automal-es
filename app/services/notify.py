"""Avisos: toast no Windows (app local) + WhatsApp via CallMeBot. Nunca levanta exceção.

Variáveis (lidas a cada envio):
- WHATSAPP_PHONE (ex. 5521999999999) + CALLMEBOT_APIKEY → WhatsApp. Ausentes → sem WhatsApp.
- APP_URL → base dos links nas mensagens (fallback UNION_PANEL_URL, senão http://127.0.0.1:8000).
- UNION_DISABLE_NOTIFY=1 → desliga tudo (testes).
- VERCEL=1 → sem toast (não há desktop).
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping

import httpx

from .formatting import brl

log = logging.getLogger(__name__)
# O log INFO do httpx imprime a URL inteira, que no CallMeBot contém a apikey.
logging.getLogger("httpx").setLevel(logging.WARNING)

APP_ID = "Union Veículos"
CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
WHATSAPP_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
MAX_INDIVIDUAL = 3  # mais carros que isso numa rodada → uma mensagem resumida
MAX_TEXT = 1000
_REFUSED_MARKERS = ("apikey is invalid", "invalid apikey", "error:")


def _disabled() -> bool:
    return os.environ.get("UNION_DISABLE_NOTIFY") == "1"


def app_url() -> str:
    base = os.environ.get("APP_URL") or os.environ.get("UNION_PANEL_URL") or "http://127.0.0.1:8000"
    base = base.strip().rstrip("/")
    if not re.match(r"^https?://", base):
        base = "https://" + base  # a Vercel costuma dar só o domínio
    return base


def whatsapp_configured() -> bool:
    return bool(os.environ.get("WHATSAPP_PHONE") and os.environ.get("CALLMEBOT_APIKEY"))


def _phone() -> str:
    return "+" + re.sub(r"\D", "", os.environ.get("WHATSAPP_PHONE", ""))


def _price(price_cents: int | None) -> str:
    return brl(price_cents) if price_cents is not None else "Sob consulta"


def send_whatsapp(text: str, client: httpx.Client | None = None) -> bool:
    """Envia `text` via CallMeBot (GET síncrono). True se aceito; False em qualquer falha."""
    if _disabled() or not whatsapp_configured():
        return False
    params = {"phone": _phone(), "text": text[:MAX_TEXT], "apikey": os.environ["CALLMEBOT_APIKEY"]}
    try:
        if client is not None:
            resp = client.get(CALLMEBOT_URL, params=params, timeout=WHATSAPP_TIMEOUT)
        else:
            with httpx.Client(timeout=WHATSAPP_TIMEOUT, follow_redirects=True) as own:
                resp = own.get(CALLMEBOT_URL, params=params)
    except Exception as exc:  # rede, timeout... nunca derruba o job
        log.warning("WhatsApp: falha de envio: %s", type(exc).__name__)
        return False
    body = resp.text[:500].lower()
    if resp.status_code >= 400 or any(m in body for m in _REFUSED_MARKERS):
        # nunca logar a URL: ela tem a apikey
        log.warning("WhatsApp: CallMeBot recusou (HTTP %s): %s", resp.status_code,
                    " ".join(re.sub(r"<[^>]+>", " ", resp.text).split())[:200])
        return False
    return True


def _toast(title: str, message: str, url: str | None) -> None:
    if os.environ.get("VERCEL") == "1":
        return
    try:
        from winotify import Notification
    except ImportError:
        return
    try:
        toast = Notification(app_id=APP_ID, title=title, msg=message)
        if url:
            toast.add_actions(label="Abrir painel", launch=url)
        toast.show()
    except Exception:
        log.exception("falha ao mostrar toast")


def notify(title: str, body: str, url: str | None = None) -> None:
    """Aviso genérico: toast local + WhatsApp ("*título*\ncorpo\nurl")."""
    if _disabled():
        return
    try:
        _toast(title, body, url)
        send_whatsapp("\n".join(p for p in (f"*{title}*", body, url) if p))
    except Exception:  # aviso nunca derruba o job
        log.exception("falha ao notificar")


# ---------- mensagens prontas para os jobs ----------

def new_car_text(car_id: int, name: str, price_cents: int | None) -> str:
    return f"🚗 Carro novo: {name} — {_price(price_cents)}\n{app_url()}/editor/{car_id}"


def sold_text(name: str) -> str:
    return f"🔴 Vendido: {name}"


def notify_new_cars(cars: Iterable[Mapping]) -> None:
    """cars: [{id, name, price_cents}]. Até MAX_INDIVIDUAL → 1 mensagem por carro; acima → 1 resumo."""
    if _disabled():
        return
    try:
        cars = list(cars)
        if not cars:
            return
        title = "Carro novo no site" if len(cars) == 1 else f"{len(cars)} carros novos no site"
        _toast(title, ", ".join(c["name"] for c in cars)[:200], app_url() + "/")
        if len(cars) <= MAX_INDIVIDUAL:
            for c in cars:
                send_whatsapp(new_car_text(c["id"], c["name"], c.get("price_cents")))
        else:
            lines = [f"🚗 {len(cars)} carros novos no site:"]
            lines += [f"• {c['name']} — {_price(c.get('price_cents'))}" for c in cars]
            lines.append(app_url() + "/")
            send_whatsapp("\n".join(lines))
    except Exception:
        log.exception("falha ao notificar carros novos")


def notify_sold(names: Iterable[str]) -> None:
    if _disabled():
        return
    try:
        names = list(names)
        if not names:
            return
        title = "Carro vendido" if len(names) == 1 else f"{len(names)} carros vendidos"
        _toast(title, ", ".join(names)[:200], app_url() + "/")
        if len(names) <= MAX_INDIVIDUAL:
            for name in names:
                send_whatsapp(sold_text(name))
        else:
            send_whatsapp("\n".join([f"🔴 {len(names)} carros vendidos:", *(f"• {n}" for n in names)]))
    except Exception:
        log.exception("falha ao notificar vendidos")


def notify_price_drops(count: int) -> None:
    if _disabled() or not count:
        return
    try:
        _toast("Queda de preço", f"{count} carro(s) baixaram de preço", app_url() + "/")
        send_whatsapp(f"📉 {count} carro(s) baixaram de preço\n{app_url()}/")
    except Exception:
        log.exception("falha ao notificar quedas de preço")
