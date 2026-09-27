"""Agendamento (fuso America/Sao_Paulo) e filtros Jinja."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.scheduler import create_scheduler
from app.services.formatting import brl, dt

SP = ZoneInfo("America/Sao_Paulo")


def _trigger(job_id):
    s = create_scheduler()
    s.start(paused=True)
    try:
        return s.get_job(job_id).trigger
    finally:
        s.shutdown(wait=False)


def test_price_check_fires_at_18h_sao_paulo():
    trig = _trigger("price_check")
    # 17:00 em SP == 20:00 UTC → próxima 18:00 SP (21:00 UTC) do mesmo dia
    base = datetime(2026, 9, 27, 20, 0, tzinfo=timezone.utc)
    nxt = trig.get_next_fire_time(None, base)
    assert nxt.astimezone(SP).replace(tzinfo=None) == datetime(2026, 9, 27, 18, 0)
    assert nxt.astimezone(timezone.utc).hour == 21
    # passou das 18:00 → dia seguinte
    nxt2 = trig.get_next_fire_time(None, nxt + timedelta(minutes=1))
    assert nxt2.astimezone(SP).replace(tzinfo=None) == datetime(2026, 9, 28, 18, 0)


def test_poll_interval_is_15_minutes():
    trig = _trigger("poll")
    assert trig.interval == timedelta(minutes=15)


@pytest.mark.parametrize("cents, text", [
    (8_990_000, "R$ 89.900,00"),
    (77_990_000, "R$ 779.900,00"),
    (123_456_789, "R$ 1.234.567,89"),
    (95_000, "R$ 950,00"),
    (5, "R$ 0,05"),
    (0, "R$ 0,00"),
    (None, "—"),
])
def test_brl(cents, text):
    assert brl(cents) == text


def test_dt_converts_to_sao_paulo():
    assert dt(datetime(2026, 9, 27, 21, 0, tzinfo=timezone.utc)) == "27/09/2026 18:00"
    assert dt(None) == "—"
