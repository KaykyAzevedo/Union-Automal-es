"""F21 — selo "ZERO-KM" do site: detecção na 1ª foto e enquadramento padrão do Stories. Sem rede."""
from __future__ import annotations

import io
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image, ImageChops, ImageDraw

from app import main as app_main
from app.encarte import photos, render, site_seal, source
# fixtures/helpers compartilhados com os testes do encarte (feed)
from test_encarte import _isolate, cars, photo_fetch, scrape  # noqa: F401

REAL_LOAD = site_seal._load  # antes de o conftest trocar pelo "sem rede"
HIDE = render.story_frames(True)
PLAIN = render.story_frames()


@pytest.fixture(autouse=True)
def _clear_story_cache():
    render._STORY_CACHE.clear()
    render._SLIDE_CACHE.clear()
    yield
    render._STORY_CACHE.clear()
    render._SLIDE_CACHE.clear()


def car(**kw):
    base = dict(brand="Hyundai", model="HB20S", version="1.0 TGDI FLEX LIMITED", km=1000, year_fab=2025,
                year_model=2026, price_cents=9890000, armored=False, photos=["http://x/1.jpg", "http://x/2.jpg"])
    return SimpleNamespace(**{**base, **kw})


def background(size=(1440, 1080)):
    """Foto com textura (faixas claras, escuras e vermelhas) para o fundo variar sob o selo."""
    img = Image.new("RGB", size, (110, 105, 100))
    draw = ImageDraw.Draw(img)
    step = size[0] // 24
    for i, color in enumerate([(230, 230, 225), (40, 40, 45), (170, 25, 30)] * 8):
        draw.polygon([(i * step, 0), ((i + 1) * step, 0), (i * step - step * 4, size[1] // 3)], fill=color)
    return img


def sealed(size=(1440, 1080), base=None):
    """Foto como a do HB20S: o selo (disco do modelo) colado onde o site o coloca."""
    img = (base or background(size)).copy()
    x0, y0, x1, y1 = (round(f * img.width) for f in site_seal.SEAL_BOX)
    seal = Image.open(site_seal.TEMPLATE_PATH).convert("RGB").resize((x1 - x0, y1 - y0), Image.Resampling.LANCZOS)
    mask = Image.new("L", seal.size, 0)
    ImageDraw.Draw(mask).ellipse((1, 1, seal.width - 2, seal.height - 2), fill=255)
    img.paste(seal, (x0, y0), mask)
    return img


def jpeg(img, quality=60):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return Image.open(io.BytesIO(buf.getvalue()))


@pytest.fixture
def seal_loads(monkeypatch):
    """Faz a 1ª foto de qualquer anúncio "ter o selo"; devolve a lista de URLs baixadas."""
    calls = []

    def load(url):
        calls.append(url)
        return sealed()

    monkeypatch.setattr(site_seal, "_load", load)
    return calls


# --- função pura ----------------------------------------------------------------

@pytest.mark.parametrize("size", [(1440, 1080), (1024, 768), (800, 600), (1280, 720)])
def test_seal_detected(size):
    assert site_seal.has_zero_km_seal(sealed(size))
    assert site_seal.has_zero_km_seal(jpeg(sealed(size)))


@pytest.mark.parametrize("img", [
    background(), background((1024, 768)), Image.new("RGB", (1440, 1080), (190, 20, 25)),
    Image.new("RGB", (1440, 1080), (212, 170, 60)), Image.new("RGB", (1440, 1080), (255, 255, 255)),
    Image.new("RGB", (40, 30)), Image.new("RGB", (3000, 100))])  # a última é baixa demais para conter o selo
def test_photo_without_seal_not_detected(img):
    assert not site_seal.has_zero_km_seal(img)
    assert site_seal.seal_score(img) > site_seal.SEAL_THRESHOLD * 1.5          # folga mesmo no vermelho chapado


def test_seal_elsewhere_not_detected():
    moved = background()
    moved.paste(sealed().crop((1300, 0, 1440, 140)), (0, 0))
    assert not site_seal.has_zero_km_seal(moved)


# --- por URL: cache e falhas ----------------------------------------------------

def real_network(monkeypatch, handler):
    """_load verdadeiro, com o transporte HTTP trocado por `handler` (nada sai para a rede)."""
    monkeypatch.setattr(site_seal, "_load", REAL_LOAD)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, request: handler(request))


def png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def forbid_retry_path(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a detecção não pode usar o download com retry")

    for name in ("get_with_retry", "fetch_bytes", "load_photo"):
        monkeypatch.setattr(photos, name, boom)


def join_download_threads():
    for t in threading.enumerate():
        if t.name == "selo-download":
            t.join(5)


def test_detection_downloads_the_photo(monkeypatch):
    forbid_retry_path(monkeypatch)
    real_network(monkeypatch, lambda r: httpx.Response(200, content=png_bytes(sealed())))
    assert site_seal.url_has_zero_km_seal("http://x/1.jpg") is True


@pytest.mark.parametrize("failure", [503, 429, 404, "connect", "timeout", "not_image"])
def test_detection_makes_a_single_attempt(monkeypatch, failure):
    calls = []

    def handler(request):
        calls.append(1)
        if failure == "connect":
            raise httpx.ConnectError("x")
        if failure == "timeout":
            raise httpx.ReadTimeout("x")
        if failure == "not_image":
            return httpx.Response(200, content=b"<html>desafio</html>")
        return httpx.Response(failure, headers={"retry-after": "10"})

    forbid_retry_path(monkeypatch)
    real_network(monkeypatch, handler)
    assert site_seal.url_has_zero_km_seal("http://x/1.jpg") is False
    assert site_seal.url_has_zero_km_seal("http://x/1.jpg") is False  # falha em cache
    assert len(calls) == 1


def test_hanging_cdn_is_abandoned_at_the_deadline(monkeypatch):
    assert site_seal.DOWNLOAD_DEADLINE <= 4.0
    release = threading.Event()

    def hang(request):
        release.wait(10)
        raise httpx.ReadTimeout("x")

    monkeypatch.setattr(site_seal, "DOWNLOAD_DEADLINE", 0.3)
    real_network(monkeypatch, hang)
    start = time.perf_counter()
    try:
        assert site_seal.url_has_zero_km_seal("http://x/1.jpg") is False
        assert time.perf_counter() - start < 2
    finally:
        release.set()
        join_download_threads()


def test_trickling_response_is_abandoned_and_frees_the_site_slot(monkeypatch):
    sent = []

    class Trickle(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(100):
                time.sleep(0.05)
                sent.append(1)
                yield b"x" * 70000

    monkeypatch.setattr(site_seal, "DOWNLOAD_DEADLINE", 0.3)
    monkeypatch.setattr(photos, "SITE_SEMAPHORE", threading.BoundedSemaphore(1))
    real_network(monkeypatch, lambda r: httpx.Response(200, stream=Trickle()))
    assert site_seal.url_has_zero_km_seal("http://x/1.jpg") is False
    join_download_threads()
    assert len(sent) < 50 and photos.SITE_SEMAPHORE.acquire(blocking=False)


def test_busy_site_queue_counts_against_the_deadline(monkeypatch):
    calls = []
    busy = threading.BoundedSemaphore(1)
    busy.acquire()
    monkeypatch.setattr(photos, "SITE_SEMAPHORE", busy)
    monkeypatch.setattr(site_seal, "DOWNLOAD_DEADLINE", 0.2)
    real_network(monkeypatch, calls.append)
    assert site_seal.url_has_zero_km_seal("http://x/1.jpg") is False
    join_download_threads()
    assert calls == []


def test_photo_already_on_disk_needs_no_network(monkeypatch):
    calls = []
    url = "http://x/1.jpg"
    photos.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    photos._cache_path(url).write_bytes(png_bytes(sealed()))
    real_network(monkeypatch, calls.append)
    assert site_seal.url_has_zero_km_seal(url) is True and calls == []


def test_url_detection_is_cached(seal_loads):
    for _ in range(3):
        assert site_seal.url_has_zero_km_seal("http://x/1.jpg")
    assert seal_loads == ["http://x/1.jpg"]


def test_network_failure_means_no_seal_and_is_not_retried_for_a_while(monkeypatch):
    calls = []

    def down(request):
        calls.append(str(request.url))
        raise httpx.ConnectError("fora do ar")

    real_network(monkeypatch, down)
    clock = [1000.0]
    monkeypatch.setattr(site_seal.time, "monotonic", lambda: clock[0])
    assert site_seal.url_has_zero_km_seal("http://x/1.jpg") is False
    assert site_seal.url_has_zero_km_seal("http://x/1.jpg") is False
    assert len(calls) == 1
    clock[0] += site_seal.FAILURE_TTL + 1                              # passado o prazo, tenta de novo
    monkeypatch.setattr(site_seal, "_load", lambda url: sealed())
    assert site_seal.url_has_zero_km_seal("http://x/1.jpg") is True


@pytest.mark.parametrize("error", [OSError("corrompida"), ValueError("x"), httpx.ReadTimeout("lento")])
def test_any_error_means_no_seal(monkeypatch, error):
    def boom(url):
        raise error

    monkeypatch.setattr(site_seal, "_load", boom)
    assert render.story_hides_site_seal(car()) is False


def test_cache_is_bounded(monkeypatch, seal_loads):
    monkeypatch.setattr(site_seal, "CACHE_MAX", 3)
    for i in range(6):
        site_seal.url_has_zero_km_seal(f"u{i}")
    assert len(site_seal._cache) == 3


# --- regra do padrão ------------------------------------------------------------

def test_default_rule(seal_loads):
    assert render.story_hides_site_seal(car()) is True
    assert seal_loads == ["http://x/1.jpg"]                            # sempre a 1ª foto do anúncio
    assert render.story_hides_site_seal(car(photos=[])) is False


@pytest.mark.parametrize("km", [0, 1000, None])
def test_no_km_rule(km):
    assert render.story_hides_site_seal(car(km=km)) is False          # sem selo detectado, km não importa


def test_armored_unchanged_and_needs_no_download(seal_loads):
    assert render.story_hides_site_seal(car(armored=True)) is True
    assert render.story_hides_site_seal(car(armored=True, photos=[])) is True
    assert seal_loads == []


def test_render_uses_hiding_default_and_hides_seal(monkeypatch):
    monkeypatch.setattr(site_seal, "_load", lambda url: sealed())
    base = Image.new("RGB", (1440, 1080), (90, 90, 90))
    ph = sealed(base=base)
    auto = render.render_story(car(), ph, ph)
    explicit = render.render_story(car(), ph, ph, render.STORY_ARMORED_TOP_ADJ, render.STORY_ARMORED_INT_ADJ)
    assert ImageChops.difference(auto, explicit).getbbox() is None
    assert ImageChops.difference(auto, render.render_story(car(), base, base,
                                                           render.STORY_ARMORED_TOP_ADJ,
                                                           render.STORY_ARMORED_INT_ADJ)).getbbox() is None


def test_manual_adjust_overrides_and_skips_detection(seal_loads, photo_fetch):
    manual = render.STORY_DEFAULT_ADJ
    render.story_png(car(), "http://x/1.jpg", "http://x/2.jpg", manual, manual)
    assert seal_loads == []
    assert list(render._STORY_CACHE)[0][2:4] == (manual, manual)


def test_story_png_default_key_follows_detection(seal_loads, photo_fetch):
    a = render.story_png(car(), "http://x/2.jpg", "http://x/3.jpg")  # fotos escolhidas não incluem a 1ª
    b = render.story_png(car(), "http://x/2.jpg", "http://x/3.jpg", render.STORY_ARMORED_TOP_ADJ,
                         render.STORY_ARMORED_INT_ADJ)
    assert a is b and seal_loads == ["http://x/1.jpg"]


# --- rotas: editor, photos.json e story.png iguais; feed intacto -----------------

def _editor_frames(client, cid, monkeypatch):
    captured = {}
    real = app_main.templates.TemplateResponse

    def spy(request, name, ctx, *a, **k):
        captured.update(ctx)
        return real(request, name, ctx, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(app_main.templates, "TemplateResponse", spy)
        assert client.get(f"/editor/{cid}").status_code == 200
    return captured["frames"]


def _adj(frame):
    return "{z},{cx},{cy}".format(**frame)


@pytest.mark.parametrize("has_seal", [True, False])
def test_editor_json_and_png_share_default(client, cars, scrape, photo_fetch, monkeypatch, has_seal):
    calls = []
    photo = sealed() if has_seal else background()
    monkeypatch.setattr(site_seal, "_load", lambda url: calls.append(url) or photo)
    cid = cars["5575766"]
    expected = HIDE if has_seal else PLAIN
    assert _editor_frames(client, cid, monkeypatch) == expected
    j = client.get(f"/encarte/{cid}/photos.json").json()
    assert j["frames"] == expected
    default = client.get(f"/encarte/{cid}/story.png")
    render._STORY_CACHE.clear()
    explicit = client.get(f"/encarte/{cid}/story.png?adj_top={_adj(j['frames']['story_top'])}"
                          f"&adj_int={_adj(j['frames']['story_int'])}")
    assert default.status_code == explicit.status_code == 200 and default.content == explicit.content
    assert calls == [j["photos"][0]]                                   # um único download para as 3 rotas


def test_routes_survive_detection_failure(client, cars, scrape, photo_fetch, monkeypatch):
    cid = cars["5575766"]                                              # conftest: _load falha
    assert _editor_frames(client, cid, monkeypatch) == PLAIN
    assert client.get(f"/encarte/{cid}/photos.json").json()["frames"] == PLAIN
    assert client.get(f"/encarte/{cid}/story.png").status_code == 200


def test_armored_routes_unchanged(client, cars, scrape, photo_fetch, monkeypatch, seal_loads):
    real = source.get_detail
    monkeypatch.setattr(source, "get_detail", lambda c, conn: replace(real(c, conn), armored=True))
    cid = cars["5575766"]
    assert _editor_frames(client, cid, monkeypatch) == HIDE
    assert client.get(f"/encarte/{cid}/photos.json").json()["frames"] == HIDE
    assert client.get(f"/encarte/{cid}/story.png").status_code == 200
    assert seal_loads == []


def test_feed_untouched_by_seal(client, cars, scrape, photo_fetch, seal_loads):
    cid = cars["5575766"]
    first = client.get(f"/encarte/{cid}/slide/0.png")
    render._SLIDE_CACHE.clear()
    explicit = client.get(f"/encarte/{cid}/slide/0.png?adj_cover=" + ",".join(map(str, render.COVER_DEFAULT_ADJ)))
    assert first.status_code == explicit.status_code == 200 and first.content == explicit.content
    assert client.get(f"/encarte/{cid}.zip").status_code == 200
    assert seal_loads == [] and HIDE["cover"] == PLAIN["cover"]
