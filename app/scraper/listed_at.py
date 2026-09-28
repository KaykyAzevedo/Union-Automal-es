"""Data de cadastro (aproximada) de um anúncio AutoCerto.

Nem a listagem nem o detalhe trazem data de cadastro/publicação. O que existe é o
header Last-Modified das fotos (autocerto.com/fotos/<loja>/<id>/...): todas as fotos de
um anúncio são enviadas no mesmo minuto do cadastro, e as datas acompanham os ids
sequenciais. A foto só existe depois do anúncio, então a data é sempre >= cadastro real
(se a foto principal for trocada depois, a data avança) → "há pelo menos X dias".
"""
from __future__ import annotations

import email.utils
import time
from collections.abc import Iterable
from datetime import date
from zoneinfo import ZoneInfo

import httpx

from ._http import log, new_client

TZ = ZoneInfo("America/Sao_Paulo")
# HEAD é só enfeite (data de entrada): curto, para nunca atrasar o poll.
HEAD_TIMEOUT = httpx.Timeout(3.0, connect=2.0)


def parse_last_modified(value: str | None) -> date | None:
    """'Fri, 25 Sep 2026 19:23:46 GMT' -> date(2026, 9, 25) no fuso de São Paulo."""
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None or parsed.tzinfo is None:
        return None
    return parsed.astimezone(TZ).date()


def fetch_listed_at(photo_url: str | None, client: httpx.Client | None = None,
                    timeout: httpx.Timeout | float | None = None) -> date | None:
    """HEAD na foto principal → data de envio. None sem foto ou em qualquer falha (nunca levanta).

    Sem retry e com timeout próprio (HEAD_TIMEOUT), independente do timeout do client.
    """
    if not photo_url:
        return None
    owns_client = client is None
    client = client or new_client()
    try:
        resp = client.head(photo_url, timeout=HEAD_TIMEOUT if timeout is None else timeout)
        if resp.status_code >= 400:
            return None
        return parse_last_modified(resp.headers.get("last-modified"))
    except Exception as exc:
        log.warning("listed_at: falha no HEAD de %s: %s", photo_url, type(exc).__name__)
        return None
    finally:
        if owns_client:
            client.close()


def fill_listed_at(cars: Iterable, client: httpx.Client | None = None,
                   only_ids: set[str] | None = None, budget: float | None = None) -> int:
    """Preenche car.listed_at (ScrapedCar/CarDetail) in place, 1 HEAD por carro.

    only_ids: limita aos external_id informados (ex.: só carros novos ou ainda sem data),
    para não fazer ~50 HEADs a cada poll.
    budget: prazo total em segundos. Esgotado, os carros restantes ficam com listed_at
    intocado (tente de novo no próximo poll); cada HEAD também é limitado ao que sobra.
    Retorna quantos carros foram consultados. Nunca levanta.
    """
    deadline = None if budget is None else time.monotonic() + budget
    owns_client = client is None
    client = client or new_client()
    done = 0
    try:
        for car in cars:
            if only_ids is not None and car.external_id not in only_ids:
                continue
            timeout = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining < 0.5:
                    log.info("listed_at: prazo de %.0fs esgotado; restantes ficam para o próximo poll", budget)
                    break
                timeout = httpx.Timeout(min(3.0, remaining), connect=min(2.0, remaining))
            photo = getattr(car, "photo_url", None) or (getattr(car, "photos", None) or [None])[0]
            car.listed_at = fetch_listed_at(photo, client, timeout=timeout)
            done += 1
    finally:
        if owns_client:
            client.close()
    return done
