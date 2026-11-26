"""Detecta o selo "ZERO-KM" que o site cola no canto superior direito das fotos de alguns anúncios.

O selo é uma imagem fixa, posicionada em proporção à LARGURA da foto, e não aparece no HTML nem
segue o km do anúncio (ex.: HB20S com 1.000 km traz o selo). Por isso a detecção é na imagem:
a região do selo é comparada com o modelo story_selo_site_0km.png (recortado de uma foto real),
só dentro do círculo do selo, para o fundo não pesar.

Pontuação = diferença absoluta média (0-255, média de R, G, B); menor = mais parecido.
Calibração em 470 fotos de 45 anúncios ao vivo: as 11 com selo ficaram em <= 5,4 e as 459 sem
selo em >= 49. O limiar SEAL_THRESHOLD = 20 deixa folga dos dois lados (recompressão JPEG e outra
resolução de um lado; fundos avermelhados do outro).
"""
from __future__ import annotations

import io
import logging
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

import httpx
from PIL import Image, ImageChops, ImageDraw, ImageStat

from . import photos

log = logging.getLogger(__name__)

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "assets" / "encarte" / "story_selo_site_0km.png"
SEAL_BOX = (0.90625, 0.00375, 0.9975, 0.095)  # (x0, y0, x1, y1), todos em fração da LARGURA da foto
SEAL_SIZE = (73, 73)
SEAL_MASK_INSET = 8
SEAL_THRESHOLD = 20.0


@lru_cache(maxsize=1)
def _template() -> tuple[Image.Image, Image.Image]:
    ref = Image.open(TEMPLATE_PATH).convert("RGB")
    mask = Image.new("L", SEAL_SIZE, 0)
    ImageDraw.Draw(mask).ellipse((SEAL_MASK_INSET, SEAL_MASK_INSET, SEAL_SIZE[0] - 1 - SEAL_MASK_INSET,
                                  SEAL_SIZE[1] - 1 - SEAL_MASK_INSET), fill=255)
    return ref, mask


def seal_region(photo: Image.Image) -> Image.Image | None:
    """Região onde o selo fica, no tamanho do modelo; None se a foto for baixa demais para contê-la."""
    box = tuple(f * photo.width for f in SEAL_BOX)
    if box[3] > photo.height:
        return None
    return photo.convert("RGB").resize(SEAL_SIZE, Image.Resampling.LANCZOS, box=box)


def seal_score(photo: Image.Image) -> float:
    region = seal_region(photo)
    if region is None:
        return 255.0
    ref, mask = _template()
    return sum(ImageStat.Stat(ImageChops.difference(region, ref), mask).mean) / 3


def has_zero_km_seal(photo: Image.Image) -> bool:
    return seal_score(photo) <= SEAL_THRESHOLD


# --- por URL, com cache --------------------------------------------------------

CACHE_MAX = 1024
# Falha ao baixar/analisar vale "sem selo" por este tempo (o mesmo do cache do anúncio): não tenta de
# novo a cada request, e editor, photos.json e story.png continuam respondendo o mesmo padrão.
FAILURE_TTL = 600.0
# É só o enquadramento padrão: não vale segurar o editor. UMA tentativa, com este prazo TOTAL (fila do
# site, DNS, conexão e leitura). O download do story.png (photos.fetch_bytes, com retry) é outro caminho.
DOWNLOAD_DEADLINE = 4.0

_cache: "OrderedDict[str, tuple[bool, float | None]]" = OrderedDict()  # url -> (tem selo, expira em)
_cache_lock = threading.Lock()
_url_locks: dict[str, threading.Lock] = {}


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cached(url: str) -> bool | None:
    with _cache_lock:
        hit = _cache.get(url)
        if hit is None or (hit[1] is not None and time.monotonic() >= hit[1]):
            return None
        _cache.move_to_end(url)
        return hit[0]


def _get_once(url: str, deadline: float) -> bytes:
    """Um único GET, sem retry, abandonado quando passa de `deadline` (relógio monotônico)."""
    def left() -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError(f"prazo de {DOWNLOAD_DEADLINE:g}s estourado: {url}")
        return seconds

    if not photos.SITE_SEMAPHORE.acquire(timeout=left()):
        raise TimeoutError(f"fila de downloads do site ocupada: {url}")
    try:
        with httpx.Client(timeout=left(), follow_redirects=True, headers=photos.HEADERS) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                chunks = []
                for chunk in resp.iter_bytes():
                    chunks.append(chunk)
                    left()  # resposta a conta-gotas não estica o prazo
                return b"".join(chunks)
    finally:
        photos.SITE_SEMAPHORE.release()


def _download(url: str) -> bytes:
    """_get_once em thread própria: o timeout do httpx vale por operação (e não cobre o DNS), então
    quem garante o prazo total é o join. A thread abandonada termina sozinha pelos timeouts."""
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    result: list = []

    def run() -> None:
        try:
            result.append((_get_once(url, deadline), None))
        except Exception as exc:
            result.append((None, exc))

    worker = threading.Thread(target=run, name="selo-download", daemon=True)
    worker.start()
    worker.join(DOWNLOAD_DEADLINE)
    if not result:
        raise TimeoutError(f"prazo de {DOWNLOAD_DEADLINE:g}s estourado: {url}")
    data, error = result[0]
    if error is not None:
        raise error
    return data


def _load(url: str) -> Image.Image:
    """Foto do cache em disco, se já baixada; senão uma tentativa curta (não grava no cache)."""
    data = photos._read_cache(photos._cache_path(url)) or _download(url)
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def url_has_zero_km_seal(url: str) -> bool:
    """Nunca levanta: qualquer falha = sem selo."""
    found = _cached(url)
    if found is not None:
        return found
    with photos.key_lock(_url_locks, _cache_lock, url):
        found = _cached(url)  # outra thread pode ter analisado enquanto esperávamos
        if found is not None:
            return found
        expires = None
        try:
            found = has_zero_km_seal(_load(url))
        except Exception as exc:  # enquadramento padrão não pode derrubar editor/Stories
            log.warning("selo ZERO-KM: não analisei %s: %s: %s", url, type(exc).__name__, exc)
            found, expires = False, time.monotonic() + FAILURE_TTL
        with _cache_lock:
            _cache[url] = (found, expires)
            _cache.move_to_end(url)
            while len(_cache) > CACHE_MAX:
                _cache.popitem(last=False)
        return found
