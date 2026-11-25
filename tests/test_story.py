"""F17 — Stories 1080x1920: render (app/encarte/render.py) e rota /encarte/{id}/story.png. Sem rede."""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image, ImageChops

from app import main as app_main
from app.encarte import render
# fixtures/helpers compartilhados com os testes do encarte (feed)
from test_encarte import _isolate, cars, color_for, detail, photo_fetch, scrape  # noqa: F401

SIZE = (1080, 1920)


@pytest.fixture(autouse=True)
def _clear_story_cache():
    render._STORY_CACHE.clear()
    yield
    render._STORY_CACHE.clear()


def car(**kw):
    base = dict(brand="Gac", model="GS3", version="1.5 TURBO GASOLINA ELITE DCT", km=500, year_fab=2026,
                year_model=2027, price_cents=15490000, armored=False)
    return SimpleNamespace(**{**base, **kw})


def photo(color):
    return Image.new("RGB", (1440, 1080), color)


def story(d=None, top=(200, 30, 30), interior=(30, 30, 200)):
    return render.render_story(d or car(), photo(top), photo(interior))


def box_mid(box):
    x0, y0, x1, y1 = box
    return (x0 + x1) // 2, (y0 + y1) // 2


def diff_box(a, b, box):
    return ImageChops.difference(a.crop(box), b.crop(box)).getbbox()


# --- assets e render ---------------------------------------------------------

def test_assets_are_story_size():
    for name in ("story_fundo.png", "story_frente.png", "story_mascara1.png", "story_mascara2.png"):
        assert Image.open(render.ASSETS / name).size == SIZE, name


def test_story_size_and_photo_slots():
    img = story()
    assert img.size == SIZE and img.mode == "RGB"
    _, _, _, _, box1, box2 = render._story_layers()
    assert img.getpixel(box_mid(box1)) == pytest.approx((200, 30, 30), abs=3)       # foto de cima
    x, y = box_mid(box2)
    assert img.getpixel((x, y - 100)) == pytest.approx((30, 30, 200), abs=3)         # interior
    assert box1[1] < box2[1] < box1[3]                                              # fusão: caixas se sobrepõem


def test_header_opaque_over_photo():
    """Máscara 1 desce abaixo do cabeçalho: foto nunca cobre o 'VEÍCULOS' (y<~190 no 1080x1920)."""
    img = story(top=(0, 255, 0))
    _, _, _, _, box1, _ = render._story_layers()
    assert box1[1] >= 190
    assert max(img.getpixel((x, 150))[1] - img.getpixel((x, 150))[0] for x in range(0, 1080, 20)) < 120  # sem verde


def test_texts_change_with_data():
    base = story()
    for change in (dict(brand="Chevrolet"), dict(model="TRACKER"), dict(km=123456), dict(year_fab=2020, year_model=2021),
                   dict(price_cents=9990000), dict(version="2.0 TURBO")):
        assert ImageChops.difference(base, story(car(**change))).getbbox() is not None, change


def _drawn_texts(monkeypatch, d):
    seen = []
    real = render._ink
    monkeypatch.setattr(render, "_ink", lambda text, style, size: seen.append(text) or real(text, style, size))
    render.render_story(d, photo((90, 90, 90)), photo((90, 90, 90)))
    return seen


def test_price_none_shows_consulte_without_cents(monkeypatch):
    seen = _drawn_texts(monkeypatch, car(price_cents=None))
    assert "CONSULTE" in seen and "00" not in seen
    assert not any(t.startswith("R$") or "," in t for t in seen)
    seen = _drawn_texts(monkeypatch, car())
    assert "154.900," in seen and "00" in seen and "CONSULTE" not in seen
    # o painel do preço muda: dígitos brancos no painel escuro só com preço
    priced, consulte = story(), story(car(price_cents=None))
    assert ImageChops.difference(priced, consulte).crop((300, 1520, 1000, 1620)).getbbox() is not None


def test_km_year_formats_whole_in_code():
    """'KM: 500' e 'ANO: 26/27' vêm do código: sem km/ano o texto muda por inteiro, 'KM:' e 'ANO:' não ficam na arte."""
    img = story(car(km=None, year_fab=None, year_model=None))
    assert img.size == SIZE
    left = story(car(km=0))
    assert ImageChops.difference(img, left).getbbox() is not None


def test_long_names_never_invade_other_column():
    """Marca/modelo/versão enormes encolhem; coluna da esquerda não passa de x=560 (a linha vertical está em ~570)."""
    blank = story(car(brand="", model=""))
    long = story(car(brand="Mercedes-Benz Superlongname", model="CLASSE C 300 SPORT EXTRA LONGO COUPÉ AMG"))
    bbox = diff_box(blank, long, (0, 1300, 1080, 1500))
    assert bbox is not None and bbox[0] >= 90 and bbox[2] <= 560
    # KM/ANO enormes não passam da margem direita nem da coluna da esquerda
    huge = story(car(km=123456789012, year_fab=2026, year_model=2027))
    nokm = story(car(km=0))
    bbox = diff_box(nokm, huge, (0, 1340, 1080, 1420))
    assert bbox is not None and bbox[0] >= 700 and bbox[2] <= 1060


def test_long_version_fits_and_lines_follow_text_width():
    short = story(car(version="1.0"))
    long_v = "2.0 TURBO DIESEL MHEV FIRST EDITION AWD AUTOMATICO DIESEL E ELETRICO PREMIUM PLUS SPORT PACK"
    huge = story(car(version=long_v))
    none = story(car(version=""))
    band = (0, 1650, 1080, 1690)
    for img in (short, huge):
        bbox = diff_box(none, img, band)
        assert bbox is not None and bbox[0] >= 70 and bbox[2] <= 1010
    # versão curta -> linhas dos lados mais compridas que no molde, longa -> mais curtas
    def line_len(img):
        return sum(1 for x in range(0, 540) if min(img.getpixel((x, 1669))) < 150)
    assert line_len(short) > line_len(story()) > line_len(huge) or line_len(huge) == 0


def test_badges_follow_feed_rule_on_top_photo():
    _, _, _, _, box1, _ = render._story_layers()
    base = story()
    half = (box1[0] + box1[2]) // 2
    arm = diff_box(base, story(car(armored=True)), (0, 0, 1080, box1[3]))
    zero = diff_box(base, story(car(km=0)), (0, 0, 1080, box1[3]))
    both = diff_box(base, story(car(armored=True, km=0)), (0, 0, 1080, box1[3]))
    for bb in (arm, zero, both):
        assert bb and bb[1] >= box1[1] and bb[3] <= box1[3]           # só sobre a foto de cima
    assert arm[2] < half and zero[2] < half                           # um só: canto superior esquerdo
    assert both[0] < half and both[2] > half + 200                    # os dois: escudo à esquerda, 0KM à direita
    # 0 km muda o texto de KM também; o selo do blindado sozinho não mexe no resto da arte
    assert diff_box(base, story(car(armored=True)), (0, box1[3], 1080, 1920)) is None
    assert ImageChops.difference(story(car(armored=True)), story(car(armored=False))).getbbox() is not None


def test_story_png_cache(monkeypatch, photo_fetch):
    calls = []
    real = render.render_story
    monkeypatch.setattr(render, "render_story", lambda *a, **k: calls.append(1) or real(*a, **k))
    d = car()
    a = render.story_png(d, "http://x/1.jpg", "http://x/2.jpg")
    b = render.story_png(d, "http://x/1.jpg", "http://x/2.jpg")
    assert a is b and len(calls) == 1
    render.story_png(d, "http://x/2.jpg", "http://x/1.jpg")        # ordem diferente = outra chave
    render.story_png(car(km=1), "http://x/1.jpg", "http://x/2.jpg")
    render.story_png(car(armored=True), "http://x/1.jpg", "http://x/2.jpg")   # selo entra na chave
    assert len(calls) == 4
    assert Image.open(io.BytesIO(a)).size == SIZE


def test_story_cache_is_bounded(monkeypatch, photo_fetch):
    monkeypatch.setattr(render, "render_story", lambda *a, **k: Image.new("RGB", (8, 8)))
    for i in range(render._STORY_CACHE_MAX + 5):
        render.story_png(car(km=i), "http://x/1.jpg", "http://x/2.jpg")
    assert len(render._STORY_CACHE) == render._STORY_CACHE_MAX


# --- _story_urls -------------------------------------------------------------

def d_with(n):
    return SimpleNamespace(photos=[f"u{i}" for i in range(n)])


def test_story_urls_defaults():
    assert app_main._story_urls(d_with(5), None) == ("u0", "u1")
    assert app_main._story_urls(d_with(5), "  ") == ("u0", "u1")
    assert app_main._story_urls(d_with(2), None) == ("u0", "u1")
    assert app_main._story_urls(d_with(1), None) == ("u0", "u0")   # anúncio de 1 foto: 0,0 só no default


@pytest.mark.parametrize("photos", ["3,1", "0,4", " 2 , 0 "])
def test_story_urls_explicit(photos):
    a, b = [int(x) for x in photos.split(",")]
    assert app_main._story_urls(d_with(5), photos) == (f"u{a}", f"u{b}")


@pytest.mark.parametrize("photos", ["1,1", "0,0", "1", "0,1,2", "a,b", "0,5", "-1,0", ",,", "1,", "0,1,", ",0,1", "0,,1", "1_0,2"])
def test_story_urls_invalid_400(photos):
    with pytest.raises(HTTPException) as e:
        app_main._story_urls(d_with(5), photos)
    assert e.value.status_code == 400


def test_story_urls_single_photo_explicit_pair_accepted():
    assert app_main._story_urls(d_with(1), "0,0") == ("u0", "u0")   # o editor manda 0,0 em anúncio de 1 foto


def test_story_urls_no_photos_422():
    with pytest.raises(HTTPException) as e:
        app_main._story_urls(d_with(0), None)
    assert e.value.status_code == 422


# --- rota ---------------------------------------------------------------------

def story_resp(resp):
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(resp.content))
    assert img.size == SIZE
    return img.convert("RGB")


def test_route_default_photos_and_headers(client, cars, scrape, photo_fetch):
    cid = cars["5575766"]
    d = detail("5575766")
    r = client.get(f"/encarte/{cid}/story.png")
    img = story_resp(r)
    _, _, _, _, box1, box2 = render._story_layers()
    assert img.getpixel(box_mid(box1)) == pytest.approx(color_for(d.photos[0]), abs=3)
    x, y = box_mid(box2)
    assert img.getpixel((x, y - 100)) == pytest.approx(color_for(d.photos[1]), abs=3)
    cd = r.headers["content-disposition"]
    assert cd.startswith("inline;") and cd.rstrip('"').endswith("-stories.png")
    assert r.headers["cache-control"] == "private, max-age=600"


def test_route_photos_param_order(client, cars, scrape, photo_fetch):
    cid = cars["5575766"]
    d = detail("5575766")
    img = story_resp(client.get(f"/encarte/{cid}/story.png?photos=4,7"))
    _, _, _, _, box1, box2 = render._story_layers()
    assert img.getpixel(box_mid(box1)) == pytest.approx(color_for(d.photos[4]), abs=3)
    x, y = box_mid(box2)
    assert img.getpixel((x, y - 100)) == pytest.approx(color_for(d.photos[7]), abs=3)


def test_route_price_param(client, cars, scrape, photo_fetch):
    cid = cars["5575766"]
    a = story_resp(client.get(f"/encarte/{cid}/story.png"))
    b = story_resp(client.get(f"/encarte/{cid}/story.png?price=125900"))
    assert ImageChops.difference(a, b).getbbox() is not None
    assert client.get(f"/encarte/{cid}/story.png?price=abc").status_code == 400
    assert client.get(f"/encarte/{cid}/story.png?price=0").status_code == 400


@pytest.mark.parametrize("q", ["?photos=1,1", "?photos=1", "?photos=0,1,2", "?photos=a,b", "?photos=0,99", "?photos=-1,2"])
def test_route_bad_photos_400(client, cars, scrape, photo_fetch, q):
    assert client.get(f"/encarte/{cars['5575766']}/story.png{q}").status_code == 400


def test_route_empty_photos_uses_default(client, cars, scrape, photo_fetch):
    story_resp(client.get(f"/encarte/{cars['5575766']}/story.png?photos="))


def test_route_404_car(client, cars, scrape, photo_fetch):
    assert client.get("/encarte/9999/story.png").status_code == 404


def test_route_scraper_error_502(client, cars, scrape):
    from app.scraper import ScraperError

    scrape["error"] = ScraperError("fora")
    assert client.get(f"/encarte/{cars['5575766']}/story.png").status_code == 502


def test_route_photo_download_fails_502(client, cars, scrape, photo_fetch):
    d = detail("5575766")
    photo_fetch.failing.add(d.photos[1])
    cid = cars["5575766"]
    assert client.get(f"/encarte/{cid}/story.png").status_code == 502          # interior (índice 1) falha
    assert client.get(f"/encarte/{cid}/story.png?photos=0,2").status_code == 200


def test_route_car_without_photos_422(client, cars, scrape):
    assert client.get(f"/encarte/{cars['5555997']}/story.png").status_code == 422


def test_route_cached_no_second_render(client, cars, scrape, photo_fetch):
    cid = cars["5575766"]
    story_resp(client.get(f"/encarte/{cid}/story.png"))
    photo_fetch.calls.clear()
    story_resp(client.get(f"/encarte/{cid}/story.png"))
    assert photo_fetch.calls == []


def test_feed_fonts_untouched():
    """Estilos do feed continuam os mesmos; os do Stories são 'story_*' separados."""
    from app.encarte.fonts import FONTS, STORY_FONTS

    assert set(FONTS) == {"brand", "model", "version", "value", "price"}
    assert all(k.startswith("story_") for k in STORY_FONTS)


def test_long_model_wraps_to_two_lines_inside_left_column():
    blank = story(car(model=""))
    one = story(car(model="GS3"))
    two = story(car(model="HAVAL H6 GT"))
    b1, b2 = diff_box(blank, one, (0, 1390, 560, 1500)), diff_box(blank, two, (0, 1390, 560, 1500))
    assert b2 is not None and b2[2] <= 560 and b2[0] >= 120
    assert (b2[3] - b2[1]) >= 60                      # duas linhas ocupam altura parecida com a de 1 linha grande
    assert b2[1] >= 0                                 # 1ª linha começa abaixo da marca (baseline 1385)


def test_price_bar_has_no_stray_specks():
    """Pontinha de textura fora do contorno da barra (canto inf. esquerdo) removida."""
    f = Image.open(render.ASSETS / "story_frente.png").convert("RGBA")
    assert all(p[3] == 255 and min(p[:3]) >= 240 for p in f.crop((50, 1618, 60, 1640)).getdata())


# --- F18: fades, ajuste de enquadramento, frames ---------------------------------------

def test_story_fades_present_between_photos_and_under_header():
    """Foto 1 vermelha, foto 2 azul: na faixa de fusão a cor passa gradualmente (sem corte seco) e o topo da
    foto 1 clareia em degradê sob o cabeçalho."""
    img = story(top=(200, 0, 0), interior=(0, 0, 200))
    _, _, _, _, box1, box2 = render._story_layers()
    xs = 540
    seam = [img.getpixel((xs, y)) for y in range(box2[1], box1[3], 4)]
    assert seam[0][0] > 150 and seam[-1][2] > 150                     # começa vermelho, termina azul
    steps = [abs(a[0] - b[0]) for a, b in zip(seam, seam[1:])]
    assert max(steps) < 60 and len([p for p in seam if 40 < p[0] < 160]) >= 3  # transição gradual, ~100 px
    top = [img.getpixel((xs, y)) for y in range(box1[1], box1[1] + 90, 6)]
    assert top[0][1] > 150 and top[-1][1] < 40                         # claro (cabeçalho) -> vermelho puro
    assert all(a[1] >= b[1] - 6 for a, b in zip(top, top[1:]))          # monotônico
    base = story(top=(0, 255, 0))                                      # VEÍCULOS (acima do fade) intacto
    assert diff_box(img, base, (0, 0, 1080, box1[1])) is None


def test_default_adj_is_identical_to_no_adj():
    a = render.render_story(car(), photo((10, 120, 30)), photo((90, 90, 10)))
    b = render.render_story(car(), photo((10, 120, 30)), photo((90, 90, 10)),
                            render.STORY_DEFAULT_ADJ, render.STORY_DEFAULT_ADJ)
    assert ImageChops.difference(a, b).getbbox() is None


def gradient_photo():
    img = Image.new("RGB", (1200, 900))
    for x in range(1200):
        img.paste((x * 255 // 1199, 60, 60), (x, 0, x + 1, 900))
    return img


def test_story_adj_changes_crop_with_cover_fit_semantics():
    base = render.render_story(car(), gradient_photo(), gradient_photo())
    zoom = render.render_story(car(), gradient_photo(), gradient_photo(), (2.0, 0.5, 0.5), None)
    left = render.render_story(car(), gradient_photo(), gradient_photo(), (2.0, 0.0, 0.5), None)
    right = render.render_story(car(), gradient_photo(), gradient_photo(), (2.0, 1.0, 0.5), None)
    px = lambda im: im.getpixel((540, 500))[0]  # noqa: E731
    assert px(left) < px(zoom) < px(right)
    assert abs(px(base) - px(zoom)) <= 6                              # zoom centrado mantém o centro
    _, _, _, _, box1, box2 = render._story_layers()
    assert diff_box(base, zoom, (0, box1[3] + 5, 1080, 1100)) is None  # ajuste da foto de cima não mexe no interior
    inter = render.render_story(car(), gradient_photo(), gradient_photo(), None, (2.0, 0.0, 0.5))
    assert diff_box(base, inter, (0, 200, 1080, box2[1] - 5)) is None  # e vice-versa
    assert diff_box(base, inter, (0, 900, 1080, 1100)) is not None


def test_story_frames_shape():
    f = render.story_frames()
    assert set(f) == {"story_top", "story_int", "cover"}
    for v in f.values():
        assert set(v) == {"w", "h", "z", "cx", "cy"} and v["w"] > 0 and v["h"] > 0
    assert f["story_top"]["w"] == f["story_int"]["w"] == 1080
    assert (f["cover"]["z"], f["cover"]["cx"], f["cover"]["cy"]) == (1.08, 0.45, 0.64)


@pytest.mark.parametrize("v, expected", [
    (None, None), ("", None), ("  ", None), ("1.00,0.50,0.50", (1.0, 0.5, 0.5)), ("3,1,0", (3.0, 1.0, 0.0)),
    ("2.00,0.605,0.580", (2.0, 0.605, 0.58)), ("1.500,0.500,0.500", (1.5, 0.5, 0.5)), ("1.123,0.001,0.999", (1.123, 0.001, 0.999)),
    (" 1.5 , 0.25 , 0.75 ", (1.5, 0.25, 0.75))])
def test_parse_adj_ok(v, expected):
    assert app_main.parse_adj(v) == expected


@pytest.mark.parametrize("v", ["0.99,0.5,0.5", "3.01,0.5,0.5", "1,1.01,0.5", "1,0.5,-0.1", "1,0.5", "1,0.5,0.5,0.5",
                               "a,b,c", "1,,0.5", "1_0,0.5,0.5", "1e0,0.5,0.5", "nan,0.5,0.5", "inf,0.5,0.5",
                               ".5,0.5,0.5", "1.,0.5,0.5", "-1,0.5,0.5", "+1,0.5,0.5", "١,0.5,0.5", "1;0.5;0.5", "1.0001,0.5,0.5", "1,0.5001,0.5", "1,0.5,0.1234"])
def test_parse_adj_invalid_400(v):
    with pytest.raises(HTTPException) as e:
        app_main.parse_adj(v, "adj_top")
    assert e.value.status_code == 400


def test_story_route_adj(client, cars, scrape, photo_fetch):
    cid = cars["5575766"]
    a = story_resp(client.get(f"/encarte/{cid}/story.png"))
    same = story_resp(client.get(f"/encarte/{cid}/story.png?adj_top=1.00,0.50,0.50&adj_int=1,0.5,0.5"))
    assert client.get(f"/encarte/{cid}/story.png?adj_top=2.00,0.605,0.580&adj_int=1.50,0.500,0.500").status_code == 200
    assert ImageChops.difference(a, same).getbbox() is None
    for q in ("adj_top=9,0.5,0.5", "adj_int=1,2,0.5", "adj_top=x"):
        assert client.get(f"/encarte/{cid}/story.png?{q}").status_code == 400


def test_story_cache_key_includes_adj(monkeypatch, photo_fetch):
    calls = []
    real = render.render_story
    monkeypatch.setattr(render, "render_story", lambda *a, **k: calls.append(a[3:5]) or real(*a, **k))
    d = car()
    u = ("http://x/1.jpg", "http://x/2.jpg")
    render.story_png(d, *u)
    render.story_png(d, *u, (1.0, 0.5, 0.5), None)            # equivale ao padrão: mesma chave
    assert len(calls) == 1
    render.story_png(d, *u, (1.5, 0.5, 0.5), None)
    render.story_png(d, *u, None, (1.5, 0.5, 0.5))
    render.story_png(d, *u, (1.5, 0.5, 0.5), None)
    assert len(calls) == 3


# --- feed: adj_cover ------------------------------------------------------------

def test_cover_default_adj_identical():
    a = render.render_cover(car(), gradient_photo())
    b = render.render_cover(car(), gradient_photo(), render.COVER_DEFAULT_ADJ)
    assert ImageChops.difference(a, b).getbbox() is None
    c = render.render_cover(car(), gradient_photo(), (2.0, 0.0, 0.5))
    assert ImageChops.difference(a, c).getbbox() is not None


def test_slide_png_cache_includes_adj_only_on_cover(monkeypatch, photo_fetch):
    calls = []
    real_cover, real_photo = render.render_cover, render.render_photo
    monkeypatch.setattr(render, "render_cover", lambda *a, **k: calls.append("c") or real_cover(*a, **k))
    monkeypatch.setattr(render, "render_photo", lambda *a, **k: calls.append("p") or real_photo(*a, **k))
    d, u = car(), "http://x/1.jpg"
    render._SLIDE_CACHE.clear()
    render.slide_png(d, u, True)
    render.slide_png(d, u, True, render.COVER_DEFAULT_ADJ)
    assert calls == ["c"]
    render.slide_png(d, u, True, (1.5, 0.5, 0.5))
    assert calls == ["c", "c"]
    render.slide_png(d, u, False)
    render.slide_png(d, u, False, (1.5, 0.5, 0.5))                    # slide comum ignora ajuste
    assert calls == ["c", "c", "p"]


def test_slide_and_zip_adj_cover_routes(client, cars, scrape, photo_fetch, monkeypatch):
    import zipfile as zf
    seen = []
    real = app_main.slide_png
    monkeypatch.setattr(app_main, "slide_png", lambda d, u, cover, adj=None: seen.append((cover, adj)) or real(d, u, cover, adj))
    cid = cars["5575766"]
    assert client.get(f"/encarte/{cid}/slide/0.png").status_code == 200
    assert client.get(f"/encarte/{cid}/slide/0.png?adj_cover=2.000,0.605,0.580").status_code == 200
    assert client.get(f"/encarte/{cid}/slide/1.png?adj_cover=2,0,0.5").status_code == 200
    assert seen == [(True, None), (True, (2.0, 0.605, 0.58)), (False, None)]   # só a capa recebe o ajuste
    seen.clear()
    z = zf.ZipFile(io.BytesIO(client.get(f"/encarte/{cid}.zip?adj_cover=1.5,0.5,0.5").content))
    assert len(z.namelist()) > 2
    assert seen[0] == (True, (1.5, 0.5, 0.5)) and all(a == (False, None) for a in seen[1:])
    for q in ("adj_cover=0.5,0.5,0.5", "adj_cover=1,2,2", "adj_cover=zzz", "adj_cover=1,0.5,0.1234"):
        assert client.get(f"/encarte/{cid}/slide/1.png?{q}").status_code == 400   # inválido 400 mesmo fora da capa
        assert client.get(f"/encarte/{cid}.zip?{q}").status_code == 400


def test_editor_car_exposes_frames(client, cars, scrape, photo_fetch, monkeypatch):
    captured = {}
    real = app_main.templates.TemplateResponse

    def spy(request, name, ctx, *a, **k):
        captured.update(ctx)
        return real(request, name, ctx, *a, **k)

    monkeypatch.setattr(app_main.templates, "TemplateResponse", spy)
    assert client.get(f"/editor/{cars['5575766']}").status_code == 200
    assert captured["frames"] == render.story_frames()


def test_single_armored_badge_drawn_once_in_one_box():
    """Um só escudo, no canto sup. esquerdo, dentro da caixa calculada (sem cópia deslocada)."""
    _, _, _, _, box1, _ = render._story_layers()
    base = story()
    arm = story(car(armored=True))
    bb = diff_box(base, arm, (0, 0, 1080, 1920))
    want = render._story_badge_box(render.BADGE_ARMORED, box1)
    assert bb is not None
    assert want[0] - 2 <= bb[0] and bb[2] <= want[2] + 2 and want[1] - 2 <= bb[1] and bb[3] <= want[3] + 2


def test_seam_fade_is_short_and_direct():
    """Faixa de fusão curta (60-90 px) e o crossfade não passa pelo fundo: valores ficam entre as duas fotos."""
    _, _, m1, m2, box1, box2 = render._story_layers()
    rows = [y for y in range(box2[1], box1[3]) if 8 < m2.getpixel((540, y)) < 247]
    assert 55 <= len(rows) <= 95
    img = story(top=(200, 20, 20), interior=(20, 20, 200))
    for y in range(box2[1], box1[3]):
        r, g, b = img.getpixel((540, y))
        assert g <= 40 and r + b <= 260   # mistura direta vermelho/azul: sem clarear para branco
