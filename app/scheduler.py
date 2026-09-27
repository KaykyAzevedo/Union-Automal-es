"""APScheduler: poll a cada 15 min + revisão de preços às 18:00 America/Sao_Paulo."""
from __future__ import annotations

import os
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .db import TZ, now
from .services.jobs import poll_job, price_check_job

POLL_MINUTES = int(os.environ.get("UNION_POLL_MINUTES", "15"))


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=TZ, job_defaults={"coalesce": True, "max_instances": 1,
                                                               "misfire_grace_time": 3600})
    scheduler.add_job(poll_job, IntervalTrigger(minutes=POLL_MINUTES, timezone=TZ), id="poll",
                      name="Poll de carros novos", next_run_time=now() + timedelta(seconds=5),
                      replace_existing=True)
    scheduler.add_job(price_check_job, CronTrigger(hour=18, minute=0, timezone=TZ), id="price_check",
                      name="Revisão diária de preços", replace_existing=True)
    return scheduler
