"""Download de fotos com cache em disco (data/cache/photos/).

Robustez para o editor, que pede ~11 slides em paralelo:
- single-flight por URL: só uma thread baixa cada foto, as outras esperam o cache;
- no máximo SITE_CONCURRENCY requisições simultâneas ao site/CDN (compartilhado
  com o scrape de detalhe em source.py);
- retry com backoff exponencial em 429/5xx e erros de rede (respeita Retry-After);
- só imagem válida vai para o cache.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import threading
import time
from pathlib import Path

import httpx
from PIL import Image, UnidentifiedImageError

from ..db import BASE_DIR

log = logging.getLogger(__name__)

# Vercel: só /tmp é gravável (efêmero, por instância)
_DATA_DIR = os.environ.get("UNION_DATA_DIR") or ("/tmp/union" if os.environ.get("VERCEL") == "1" else BASE_DIR / "data")
CACHE_DIR = Path(_DATA_DIR) / "cache" / "photos"
HEADERS = {"User-Agent": "Mozilla/5.0 (UnionPainel)"}
SITE_CONCURRENCY = 3
SITE_SEMAPHORE = threading.BoundedSemaphore(SITE_CONCURRENCY)
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
BACKOFF_BASE = 0.5  # 0.5s, 1s, 2s

_url_locks: dict[str, threading.Lock] = {}
_url_locks_guard = threading.Lock()


def key_lock(registry: dict[str, threading.Lock], guard: threading.Lock, key: str) -> threading.Lock:
    """Lock dedicado a uma chave (single-flight)."""
    with guard:
        lock = registry.get(key)
        if lock is None:
            lock = registry[key] = threading.Lock()
        return lock


def _cache_path(url: str) -> Path:
    return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".img")


def _is_image(data: bytes) -> bool:
    try:
        Image.open(io.BytesIO(data)).verify()
        return True
    except Exception:
        return False


def _read_cache(path: Path) -> bytes | None:
    if not path.exists():
        return None
    data = path.read_bytes()
    if _is_image(data):
        return data
    path.unlink(missing_ok=True)  # entrada ruim (versão antiga): baixa de novo
    return None


def _retry_delay(attempt: int, resp: httpx.Response | None) -> float:
    if resp is not None:
        try:
            return min(10.0, float(resp.headers.get("retry-after", "")))
        except ValueError:
            pass
    return BACKOFF_BASE * (2 ** attempt)


def get_with_retry(client: httpx.Client, url: str) -> httpx.Response:
    """GET limitado por SITE_SEMAPHORE, com retry/backoff em 429/5xx e erro de rede."""
    for attempt in range(MAX_ATTEMPTS):
        resp = None
        try:
            with SITE_SEMAPHORE:
                resp = client.get(url)
            if resp.status_code not in RETRY_STATUSES:
                resp.raise_for_status()
                return resp
            reason = f"HTTP {resp.status_code}"
        except httpx.TransportError as exc:
            reason = f"{type(exc).__name__}: {exc}"
        if attempt == MAX_ATTEMPTS - 1:
            if resp is not None:
                resp.raise_for_status()
            raise httpx.ConnectError(f"{reason} após {MAX_ATTEMPTS} tentativas: {url}")
        delay = _retry_delay(attempt, resp)
        log.warning("foto %s: %s — nova tentativa em %.1fs", url, reason, delay)
        time.sleep(delay)
    raise AssertionError("inalcançável")


def fetch_bytes(url: str, client: httpx.Client | None = None) -> bytes:
    """Bytes da imagem (cache em disco, single-flight por URL). Resposta 200 que não
    é imagem (ex.: desafio do Cloudflare) levanta UnidentifiedImageError (OSError)
    sem cachear."""
    path = _cache_path(url)
    data = _read_cache(path)
    if data is not None:
        return data
    with key_lock(_url_locks, _url_locks_guard, url):
        data = _read_cache(path)  # outra thread pode ter baixado enquanto esperávamos
        if data is not None:
            return data
        own = client is None
        client = client or httpx.Client(timeout=30, follow_redirects=True, headers=HEADERS)
        try:
            resp = get_with_retry(client, url)
            data = resp.content
        finally:
            if own:
                client.close()
        if not _is_image(data):
            raise UnidentifiedImageError(
                f"resposta não é imagem ({resp.headers.get('content-type', '?')}): {url}")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return data


def load_photo(url: str, client: httpx.Client | None = None) -> Image.Image:
    img = Image.open(io.BytesIO(fetch_bytes(url, client)))
    img.load()
    return img.convert("RGB")
