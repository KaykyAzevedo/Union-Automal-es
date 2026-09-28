"""F4 — encarte: render (Forja: app/encarte/*), rotas /editor e /encarte. Sem rede: detalhe e fotos mockados."""
from __future__ import annotations

import io
import zipfile
from dataclasses import replace

import httpx
import pytest
from PIL import Image, ImageChops

from app.encarte import photos as encarte_photos
from app.encarte import render, source
from app.encarte.caption import build_caption
from app.scraper import ScraperError
from app.scraper.detail import parse_detail
from conftest import FIXTURES, make_car

BASE = "https://www.unionrioveiculos.com.br/Veiculo"
URLS = {
    "5575766": f"{BASE}/tracker-1.0-turbo-flex-ltz-automatico-flex-2026/5575766/detalhes",
    "5555997": f"{BASE}/320i-2.0-16v-turbo-flex-m-sport-automatico-flex-2022/5555997/detalhes",
    "5509677": (f"{BASE}/range-rover-sport-3.0-d350-turbo-diesel-mhev-first-edition-awd-automatico-"
                "diesel-e-eletrico-2023/5509677/detalhes"),
}
FILES = {
    "5575766": "detail_tracker_5575766.html",
    "5555997": "detail_bmw320i_sem_foto_5555997.html",
    "5509677": "detail_range_rover_sport_5509677.html",
}


def detail(ext: str):
    return parse_detail((FIXTURES / FILES[ext]).read_text(encoding="utf-8"), URLS[ext])


def color_for(url: str) -> tuple[int, int, int]:
    """Cor sólida e distinta por URL de foto, para rastrear ordem nos slides."""
    h = abs(hash(url))
    return (h % 200 + 40, (h // 200) % 200 + 40, (h // 40000) % 200 + 40)


def fake_jpeg(url: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1440, 1080), color_for(url)).save(buf, "JPEG", quality=95)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Sem rede e sem escrever em data/cache real; caches em memória limpos."""
    monkeypatch.setattr(encarte_photos, "CACHE_DIR", tmp_path / "photos")
    source.clear_cache()
    render._SLIDE_CACHE.clear()
    yield
    source.clear_cache()
    render._SLIDE_CACHE.clear()


@pytest.fixture
def photo_fetch(monkeypatch):
    calls: list[str] = []
    failing: set[str] = set()

    def fetch(url, client=None):
        calls.append(url)
        if url in failing:
            raise httpx.ConnectError("foto fora", request=httpx.Request("GET", url))
        return fake_jpeg(url)

    monkeypatch.setattr(encarte_photos, "fetch_bytes", fetch)
    fetch.calls, fetch.failing = calls, failing
    return fetch


@pytest.fixture
def scrape(monkeypatch):
    """Substitui scrape_detail: devolve a fixture do id ou levanta o erro configurado."""
    import app.scraper.detail as detail_mod

    state = {"error": None, "calls": 0}

    def fake(url, client=None):
        state["calls"] += 1
        if state["error"]:
            raise state["error"]
        ext = url.rstrip("/").split("/")[-2]
        return detail(ext)

    monkeypatch.setattr(detail_mod, "scrape_detail", fake)
    return state


@pytest.fixture
def cars(conn):
    """Carros no banco: 1=Tracker, 2=BMW sem foto/preço, 3=Range Rover."""
    from app.services.sync import run_poll

    run_poll(conn, [replace(make_car(ext), url=URLS[ext]) for ext in ("5575766", "5555997", "5509677")])
    return {r["external_id"]: r["id"] for r in conn.execute("SELECT id, external_id FROM cars")}


# --- parse_detail: ordem das fotos -------------------------------------------

def test_tracker_photos_in_site_order():
    names = [p.rsplit("/", 1)[1] for p in detail("5575766").photos]
    assert names == ["1_042346.jpg", "1_042347.jpg", "1_042349.jpg", "1_042350.jpg", "2_042350.jpg",
                     "1_042351.jpg", "1_042352.jpg", "1_042353.jpg", "1_042354.jpg", "2_042354.jpg",
                     "1_042355.jpg"]
    assert all(p.startswith("https://www.autocerto.com/fotos/339/5575766/") for p in detail("5575766").photos)


def test_name_no_year():
    d = detail("5575766")
    assert d.name_no_year == "Chevrolet TRACKER 1.0 TURBO FLEX LTZ AUTOMÁTICO"
    assert "2026" not in d.name_no_year and "2025" not in d.name_no_year


# --- render ------------------------------------------------------------------

def _photo(color=(90, 120, 150), size=(1440, 1080)):
    return Image.new("RGB", size, color)


@pytest.mark.parametrize("ext", ["5575766", "5555997", "5509677"])
def test_cover_size(ext):
    img = render.render_cover(detail(ext), _photo())
    assert img.size == (1080, 1350) and img.mode == "RGB"


@pytest.mark.parametrize("size", [(1440, 1080), (600, 1600), (50, 40), (3000, 500)])
def test_photo_slide_size_any_aspect(size):
    assert render.render_photo(_photo(size=size)).size == (1080, 1350)
    assert render.render_cover(detail("5575766"), _photo(size=size)).size == (1080, 1350)


def test_photo_slide_fills_photo_box():
    img = render.render_photo(_photo((10, 200, 30)))
    x0, y0, x1, y1 = render.PHOTO_BOX
    assert img.getpixel((x0 + 5, y0 + 5)) == (10, 200, 30)
    assert img.getpixel(((x0 + x1) // 2, (y0 + y1) // 2)) == (10, 200, 30)
    assert img.getpixel((x1 - 5, y1 - 5)) == (10, 200, 30)


def test_cover_price_none_covers_cents_and_does_not_crash():
    d = detail("5575766")
    with_price = render.render_cover(d, _photo())
    no_price = render.render_cover(replace(d, price_cents=None), _photo())
    assert no_price.size == (1080, 1350)
    assert ImageChops.difference(with_price.crop(render.CENTS_BOX), no_price.crop(render.CENTS_BOX)).getbbox()


def test_cover_with_missing_fields():
    d = replace(detail("5575766"), km=None, year_fab=None, year_model=None, price_cents=None, version="", model="")
    assert render.render_cover(d, _photo()).size == (1080, 1350)
    d = replace(detail("5575766"), km=0, year_fab=None, year_model=2026)
    assert render.render_cover(d, _photo()).size == (1080, 1350)


def _ink_bbox(d_full, d_empty):
    a = render.render_cover(d_full, _photo())
    b = render.render_cover(d_empty, _photo())
    return ImageChops.difference(a, b).getbbox()


def test_long_model_is_shrunk_inside_left_block():
    d = detail("5575766")
    box = _ink_bbox(replace(d, model="RANGE ROVER SPORT AUTOBIOGRAPHY LONG WHEELBASE"), replace(d, model=""))
    cx, half = render.MODEL["center_x"], render.MODEL["max_w"] // 2
    assert box[0] >= cx - half - 2 and box[2] <= cx + half + 2


@pytest.mark.parametrize("version", [
    "3.0 D350 TURBO DIESEL MHEV FIRST EDITION AWD AUTOMÁTICO DIESEL E ELÉTRICO " * 3,
    "2.0 TSI CARBONBLACKEDITIONPLUS AUTOMÁTICO",  # token de 26 letras
])
def test_long_version_stays_inside_left_block(version):
    d = detail("5575766")
    box = _ink_bbox(replace(d, version=version), replace(d, version=""))
    cx, half = render.VERSION["center_x"], render.VERSION["max_w"] // 2
    assert box[0] >= cx - half - 2 and box[2] <= cx + half + 2, box
    assert box[3] <= render.VERSION["top"] + 2 * render.VERSION["line_pitch"] + 10  # no máx. 2 linhas


def test_huge_price_fits():
    d = replace(detail("5575766"), price_cents=1_234_567_890_00)
    box = _ink_bbox(d, replace(d, price_cents=None))
    assert box is not None
    assert render.render_cover(d, _photo()).size == (1080, 1350)


@pytest.mark.parametrize("fn, args, expected", [
    (render.fmt_km, (5500,), "5.500"), (render.fmt_km, (0,), "0"), (render.fmt_km, (None,), "—"),
    (render.fmt_years, (2025, 2026), "25/26"), (render.fmt_years, (None, 2026), "26"),
    (render.fmt_years, (None, None), "—"), (render.fmt_years, (2009, 2010), "09/10"),
    (render.fmt_price, (12_990_000,), "129.900"), (render.fmt_price, (None,), None),
    (render.fmt_price, (77_990_000,), "779.900"),
])
def test_formatters(fn, args, expected):
    assert fn(*args) == expected


def test_build_post_cover_first_then_photos(photo_fetch):
    d = detail("5575766")
    pngs = render.build_post(d, d.photos[:3])
    assert len(pngs) == 3
    imgs = [Image.open(io.BytesIO(p)) for p in pngs]
    assert all(i.format == "PNG" and i.size == (1080, 1350) for i in imgs)
    x0, y0, x1, y1 = render.PHOTO_BOX
    mid = ((x0 + x1) // 2, (y0 + y1) // 2)
    assert imgs[1].convert("RGB").getpixel(mid) == pytest.approx(color_for(d.photos[1]), abs=3)


def test_caption_non_empty():
    for ext in FILES:
        text = build_caption(detail(ext))
        assert isinstance(text, str) and text.strip()


# --- download de fotos (cache em disco) --------------------------------------

def test_fetch_bytes_caches_on_disk(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, content=fake_jpeg(str(request.url)))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    url = "https://www.autocerto.com/fotos/339/1/1_a.jpg"
    a = encarte_photos.fetch_bytes(url, client)
    b = encarte_photos.fetch_bytes(url, client)
    assert a == b and len(calls) == 1
    assert encarte_photos.load_photo(url, client).size == (1440, 1080)


def test_fetch_bytes_does_not_cache_non_image():
    """Página HTML com 200 (Cloudflare/erro) não pode ficar presa no cache de disco."""
    state = {"html": True}

    def handler(request):
        if state["html"]:
            return httpx.Response(200, text="<html>Just a moment...</html>", headers={"content-type": "text/html"})
        return httpx.Response(200, content=fake_jpeg("x"), headers={"content-type": "image/jpeg"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    url = "https://www.autocerto.com/fotos/339/1/1_b.jpg"
    with pytest.raises(OSError):
        encarte_photos.load_photo(url, client)
    state["html"] = False
    assert encarte_photos.load_photo(url, client).size == (1440, 1080)


def test_fetch_bytes_http_error_not_cached():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    url = "https://www.autocerto.com/fotos/339/1/1_c.jpg"
    with pytest.raises(httpx.HTTPError):
        encarte_photos.fetch_bytes(url, client)
    assert not list(encarte_photos.CACHE_DIR.glob("*")) if encarte_photos.CACHE_DIR.exists() else True


# --- rotas -------------------------------------------------------------------

def test_editor_lists_active_cars(client, cars):
    r = client.get("/editor")
    assert r.status_code == 200
    assert "Carro 5575766 1.0 2020" in r.text and "Escolha um carro" in r.text


def test_editor_car_ok(client, cars, scrape):
    r = client.get(f"/editor/{cars['5575766']}")
    assert r.status_code == 200
    assert "TRACKER" in r.text and "1.0 TURBO FLEX LTZ AUTOMÁTICO" in r.text
    assert 'id="zip-btn"' in r.text and "zipParts" in r.text  # F5: ZIP montado no navegador (JSZip)
    assert r.text.count("autocerto.com/fotos/339/5575766/") >= 11
    assert "None" not in r.text


def test_editor_car_scraper_error_shows_error(client, cars, scrape):
    scrape["error"] = ScraperError("anúncio não encontrado (redirecionou para /Veiculos)")
    r = client.get(f"/editor/{cars['5575766']}")
    assert r.status_code == 200
    assert "Não foi possível" in r.text and "redirecionou" in r.text


def test_editor_car_without_photos_renders(client, cars, scrape):
    r = client.get(f"/editor/{cars['5555997']}")
    assert r.status_code == 200
    assert "320i" in r.text and "Sob consulta" in r.text
    assert "Anúncio sem fotos no site" in r.text
    assert 'id="zip-link"' not in r.text and 'id="zip-btn"' not in r.text
    assert f'/encarte/{cars["5555997"]}.zip' not in r.text
    assert 'id="caption"' in r.text  # legenda continua disponível


def test_editor_404(client, cars):
    assert client.get("/editor/9999").status_code == 404


def test_detail_is_cached(client, cars, scrape, photo_fetch):
    cid = cars["5575766"]
    client.get(f"/editor/{cid}")
    for n in range(3):
        client.get(f"/encarte/{cid}/slide/{n}.png")
    client.get(f"/encarte/{cid}/caption.txt")
    assert scrape["calls"] == 1


def _png(resp):
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(resp.content))
    assert img.size == (1080, 1350)
    return img.convert("RGB")


def _mid(img):
    x0, y0, x1, y1 = render.PHOTO_BOX
    return img.getpixel(((x0 + x1) // 2, (y0 + y1) // 2))


def test_slide_cover_and_photo(client, cars, scrape, photo_fetch):
    cid = cars["5575766"]
    d = detail("5575766")
    _png(client.get(f"/encarte/{cid}/slide/0.png"))
    assert _mid(_png(client.get(f"/encarte/{cid}/slide/1.png"))) == pytest.approx(color_for(d.photos[1]), abs=3)
    assert _mid(_png(client.get(f"/encarte/{cid}/slide/10.png"))) == pytest.approx(color_for(d.photos[10]), abs=3)


def test_slide_reordered(client, cars, scrape, photo_fetch):
    cid = cars["5575766"]
    d = detail("5575766")
    img = _png(client.get(f"/encarte/{cid}/slide/1.png?photos=4,7,2"))
    assert _mid(img) == pytest.approx(color_for(d.photos[7]), abs=3)
    photo_fetch.calls.clear()
    _png(client.get(f"/encarte/{cid}/slide/0.png?photos=4,7,2"))
    assert photo_fetch.calls == [d.photos[4]]  # capa = 1ª escolhida


@pytest.mark.parametrize("q, code", [
    ("?photos=abc", 400), ("?photos=0,99", 400), ("?photos=-1", 400), ("?photos=,,", 400), ("?photos=", None),
])
def test_slide_bad_photos_param(client, cars, scrape, photo_fetch, q, code):
    r = client.get(f"/encarte/{cars['5575766']}/slide/0.png{q}")
    assert r.status_code == (code or 200)


def test_slide_out_of_range_404(client, cars, scrape, photo_fetch):
    cid = cars["5575766"]
    assert client.get(f"/encarte/{cid}/slide/11.png").status_code == 404
    assert client.get(f"/encarte/{cid}/slide/3.png?photos=0,1,2").status_code == 404
    assert client.get(f"/encarte/9999/slide/0.png").status_code == 404


def test_slide_scraper_error_502(client, cars, scrape):
    scrape["error"] = ScraperError("fora")
    assert client.get(f"/encarte/{cars['5575766']}/slide/0.png").status_code == 502
    assert client.get(f"/encarte/{cars['5575766']}.zip").status_code == 502


def test_slide_photo_download_fails_502(client, cars, scrape, photo_fetch):
    d = detail("5575766")
    photo_fetch.failing.add(d.photos[1])
    cid = cars["5575766"]
    assert client.get(f"/encarte/{cid}/slide/1.png").status_code == 502
    assert client.get(f"/encarte/{cid}/slide/2.png").status_code == 200
    assert client.get(f"/encarte/{cid}.zip").status_code == 502


def test_slide_car_without_photos(client, cars, scrape):
    r = client.get(f"/encarte/{cars['5555997']}/slide/0.png")
    assert 400 <= r.status_code < 500


def _zip(resp):
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(resp.content))


def test_zip_default_11_png_and_caption(client, cars, scrape, photo_fetch):
    r = client.get(f"/encarte/{cars['5509677']}.zip")   # Range Rover: 12 fotos → 11 slides
    zf = _zip(r)
    names = zf.namelist()
    assert names == [f"{i:02d}.png" for i in range(1, 12)] + ["legenda.txt"]
    for n in names[:-1]:
        assert Image.open(io.BytesIO(zf.read(n))).size == (1080, 1350)
    assert zf.read("legenda.txt").decode("utf-8").strip()
    cd = r.headers["content-disposition"]
    assert "attachment" in cd and cd.endswith('.zip"') and "land-rover" in cd and "2023" in cd


def test_zip_reordered(client, cars, scrape, photo_fetch):
    d = detail("5575766")
    zf = _zip(client.get(f"/encarte/{cars['5575766']}.zip?photos=3,0,5"))
    assert zf.namelist() == ["01.png", "02.png", "03.png", "legenda.txt"]
    img2 = Image.open(io.BytesIO(zf.read("02.png"))).convert("RGB")
    img3 = Image.open(io.BytesIO(zf.read("03.png"))).convert("RGB")
    assert _mid(img2) == pytest.approx(color_for(d.photos[0]), abs=3)
    assert _mid(img3) == pytest.approx(color_for(d.photos[5]), abs=3)


def test_caption_route(client, cars, scrape):
    r = client.get(f"/encarte/{cars['5575766']}/caption.txt")
    assert r.status_code == 200 and r.text.strip()
    assert r.headers["content-type"].startswith("text/plain")
