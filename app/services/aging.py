"""F6: tempo em estoque e sugestão de redução de preço (Painel da Equipe).

Regras (constantes só aqui):
- since = min(first_seen, listed_at). listed_at = data de envio da foto principal
  (sempre >= cadastro real). Carros do registro inicial (in_baseline) → since_is_estimate
  =True ("há pelo menos X dias"); carros detectados depois pelo poll → False (first_seen
  tem precisão de 15 min).
- Relógio da sugestão = dias desde a última REDUÇÃO de preço (price_history), ou desde
  `since` se nunca baixou. Subida de preço não reinicia o relógio.
- TIERS: relógio >= 60 → -8%; >= 45 → -5%; >= 30 → -3%; < 30 → sem sugestão.
- Preço sugerido arredondado para baixo terminando em 900 (126.003 → 125.900).
"""
from __future__ import annotations

from datetime import date, timedelta

from .. import db

TIERS: tuple[tuple[int, int], ...] = ((60, 8), (45, 5), (30, 3))  # (dias mínimos, % de redução), decrescente
MIN_DAYS = min(days for days, _ in TIERS)
SOLD_WINDOW_DAYS = 30


# ---- funções puras ------------------------------------------------------------

def round_down_900(cents: int) -> int | None:
    """Maior valor em reais terminado em 900 que seja <= cents (em centavos). < R$ 900 → None."""
    reais = cents // 100
    rounded = reais - (reais - 900) % 1000
    return rounded * 100 if rounded >= 900 else None


def suggest(price_cents: int | None, clock_days: int) -> dict | None:
    """{pct, new_price_cents, reason} ou None (sem preço, relógio < MIN_DAYS ou arredondamento inválido)."""
    if price_cents is None or clock_days < MIN_DAYS:
        return None
    pct = next(p for days, p in TIERS if clock_days >= days)
    new_price = round_down_900(price_cents * (100 - pct) // 100)
    if new_price is None or new_price >= price_cents:
        return None
    return {"pct": pct, "new_price_cents": new_price,
            "reason": f"{clock_days} dias sem redução de preço"}


def last_reduction(history: list[tuple[date, int | None]]) -> date | None:
    """Data da última queda de preço em `history` (ordenado por data), comparando cada
    preço com o último preço conhecido anterior (None é ignorado)."""
    last_price: int | None = None
    found: date | None = None
    for day, price in history:
        if price is None:
            continue
        if last_price is not None and price < last_price:
            found = day
        last_price = price
    return found


def _date(value: str | None) -> date | None:
    if not value:
        return None
    if len(value) == 10:
        return date.fromisoformat(value)
    return db.from_iso(value).astimezone(db.TZ).date()


# ---- visão da equipe ------------------------------------------------------------

def team_view(conn, today: date | None = None) -> dict:
    """Contexto de GET /equipe: {summary, cars}."""
    today = today or db.now().date()
    rows = conn.execute(
        """SELECT id, name, photo_url, price_cents, posted, first_seen, in_baseline, listed_at
           FROM cars WHERE active = 1"""
    ).fetchall()

    history: dict[int, list[tuple[date, int | None]]] = {}
    for h in conn.execute(
            """SELECT ph.car_id, ph.price_cents, ph.recorded_at FROM price_history ph
               JOIN cars c ON c.id = ph.car_id WHERE c.active = 1 ORDER BY ph.recorded_at, ph.id"""):
        history.setdefault(h["car_id"], []).append((_date(h["recorded_at"]), h["price_cents"]))

    cars = []
    for r in rows:
        first_seen, listed = _date(r["first_seen"]), _date(r["listed_at"])
        since = min(first_seen, listed) if listed else first_seen
        reduced = last_reduction(history.get(r["id"], []))
        clock_from = max(reduced, since) if reduced else since
        days_in_stock = max(0, (today - since).days)
        clock = max(0, (today - clock_from).days)
        cars.append({
            "id": r["id"],
            "name": r["name"],
            "photo_url": r["photo_url"],
            "price_cents": r["price_cents"],
            "days_in_stock": days_in_stock,
            "since": since,
            "since_is_estimate": bool(r["in_baseline"]),
            "posted": bool(r["posted"]),
            "last_price_change": reduced,
            "days_since_price_change": clock,
            "suggestion": suggest(r["price_cents"], clock),
        })
    cars.sort(key=lambda c: (-c["days_in_stock"], c["id"]))

    sold_from = (today - timedelta(days=SOLD_WINDOW_DAYS)).isoformat()
    sold_30d = sum(1 for (sold_at,) in conn.execute("SELECT sold_at FROM cars WHERE sold_at IS NOT NULL")
                   if (_date(sold_at) or today).isoformat() >= sold_from)
    days = [c["days_in_stock"] for c in cars]
    summary = {
        "in_stock": len(cars),
        "posted": sum(1 for c in cars if c["posted"]),
        "pending": conn.execute("SELECT COUNT(*) FROM tickets WHERE status = 'pending'").fetchone()[0],
        "sold_30d": sold_30d,
        "avg_days": round(sum(days) / len(days)) if days else 0,
        "over_30": sum(1 for d in days if d >= 30),
        "over_60": sum(1 for d in days if d >= 60),
    }
    return {"summary": summary, "cars": cars}
