"""APScheduler (app local): uma verificação diária às 18:00 America/Sao_Paulo.

Se o app estava fechado às 18:00, a verificação perdida roda logo ao abrir
(uma vez), desde que ainda não haja execução bem-sucedida depois das últimas 18:00.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .db import TZ, connect, from_iso, now
from .services.daily import DAILY_HOUR, DAILY_MINUTE, last_scheduled
from .services.jobs import daily_job

log = logging.getLogger(__name__)
CATCH_UP_DELAY = timedelta(seconds=10)


def missed_daily_run() -> bool:
    """True se não houve verificação bem-sucedida desde as últimas 18:00."""
    conn = connect()
    try:
        # só runs que revisam preços contam; um /cron/poll (compat) não substitui a diária
        row = conn.execute("SELECT MAX(finished_at) FROM runs"
                           " WHERE ok = 1 AND kind IN ('daily', 'price_check')").fetchone()
    finally:
        conn.close()
    last = from_iso(row[0]) if row else None
    return last is None or last < last_scheduled()


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=TZ, job_defaults={"coalesce": True, "max_instances": 1,
                                                               "misfire_grace_time": 3600})
    first_run = now() + CATCH_UP_DELAY if missed_daily_run() else None
    if first_run:
        log.info("verificação das %02d:%02d perdida: rodando agora", DAILY_HOUR, DAILY_MINUTE)
    scheduler.add_job(daily_job, CronTrigger(hour=DAILY_HOUR, minute=DAILY_MINUTE, timezone=TZ), id="daily",
                      name="Verificação diária", replace_existing=True,
                      **({"next_run_time": first_run} if first_run else {}))
    return scheduler
