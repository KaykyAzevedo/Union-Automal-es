"""F7: verificação diária única (18:00 America/Sao_Paulo).

Uma coleta do site aplica tudo: carros novos → chamados, ausências → vendido (2 coletas
seguidas = 2 dias), preços → histórico e alertas de queda.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from ..db import TZ, now
from .prices import run_price_check
from .sync import run_poll

DAILY_HOUR = 18  # hora local (America/Sao_Paulo); na Vercel o cron é "0 21 * * *" (UTC)
DAILY_MINUTE = 0


def run_daily(conn: sqlite3.Connection, scraped: list) -> dict:
    """run_poll + run_price_check sobre a MESMA coleta. As ausências são contadas uma
    única vez (no run_poll). Retorna as chaves dos dois resultados num dict só."""
    poll = run_poll(conn, scraped)
    price = run_price_check(conn, scraped, track=False)
    return {**poll, "checked": price["checked"], "changed": price["changed"],
            "drops": price["drops"], "alert_ids": price["alert_ids"]}


def next_check(at: datetime | None = None) -> datetime:
    """Próxima verificação diária (18:00 locais): hoje se ainda não passou, senão amanhã."""
    at = (at or now()).astimezone(TZ)
    target = at.replace(hour=DAILY_HOUR, minute=DAILY_MINUTE, second=0, microsecond=0)
    return target if at < target else target + timedelta(days=1)


def last_scheduled(at: datetime | None = None) -> datetime:
    """Horário agendado mais recente que já passou (para recuperar execução perdida)."""
    return next_check(at) - timedelta(days=1)
