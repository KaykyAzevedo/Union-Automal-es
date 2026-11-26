"""F17 QA (Sentinela): achados da revisão do Stories. Sem rede."""
from __future__ import annotations

import io
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from app import main as app_main
from app.encarte import render
from test_story import _clear_story_cache, car  # noqa: F401


def d_with(n):
    return SimpleNamespace(photos=[f"u{i}" for i in range(n)])


def test_single_photo_ad_accepts_the_pair_the_editor_sends():
    assert app_main._story_urls(d_with(1), "0,0") == ("u0", "u0")


@pytest.mark.parametrize("photos", ["1_0,2", "0x1,2", "0,1,", ",0,1", "0,,1", "٠,١", "1,٢", "+1,2", " ,1"])
def test_story_urls_python_int_quirks_are_not_accepted(photos):
    with pytest.raises(Exception) as e:
        app_main._story_urls(d_with(12), photos)
    assert e.value.status_code == 400


def test_single_photo_pair_only_for_one_photo_ads():
    for n in (2, 5):
        with pytest.raises(Exception) as e:
            app_main._story_urls(d_with(n), "0,0")
        assert e.value.status_code == 400
    with pytest.raises(Exception) as e:
        app_main._story_urls(d_with(1), "0,1")
    assert e.value.status_code == 400


def test_cache_key_isolation(monkeypatch):
    monkeypatch.setattr(render, "load_photo" if hasattr(render, "load_photo") else "to_png", render.to_png, raising=False)
    import app.encarte.photos as ph
    monkeypatch.setattr(ph, "load_photo", lambda u: Image.new("RGB", (40, 30), (len(u) * 7 % 255, 9, 9)))
    a = render.story_png(car(model="A"), "http://x/1.jpg", "http://x/22.jpg")
    b = render.story_png(car(model="B"), "http://x/1.jpg", "http://x/22.jpg")
    c = render.story_png(car(price_cents=None), "http://x/1.jpg", "http://x/22.jpg")
    d = render.story_png(car(armored=True), "http://x/1.jpg", "http://x/22.jpg")
    assert len({a, b, c, d}) == 4


def test_story_png_concurrent_same_key_all_valid(monkeypatch):
    import app.encarte.photos as ph
    monkeypatch.setattr(ph, "load_photo", lambda u: Image.new("RGB", (400, 300), (90, 20, 20)))
    out, errs = [], []

    def run(i):
        try:
            out.append(render.story_png(car(km=i % 3), "http://x/1.jpg", "http://x/2.jpg"))
        except Exception as e:  # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=run, args=(i,)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and len(out) == 12
    assert all(Image.open(io.BytesIO(p)).size == (1080, 1920) for p in out)
    assert len(render._STORY_CACHE) == 3


@pytest.mark.parametrize("brand", [
    "MERCEDES-BENZ", "Mercedes-Benz", "LAND ROVER"])
def test_long_brand_is_not_truncated_with_ellipsis(brand):
    text, size = render._fit_story(render.fmt_brand(brand), **{k: render.STORY_BRAND[k] for k in ("style", "cap", "max_w")})
    assert "…" not in text, f"marca {brand!r} abreviada para {text!r}"


def drawn(monkeypatch, d):
    seen = []
    real = render._ink
    monkeypatch.setattr(render, "_ink", lambda t, st, sz: seen.append((t, st, sz)) or real(t, st, sz))
    img = render.render_story(d, Image.new("RGB", (400, 300), (90, 90, 90)), Image.new("RGB", (400, 300), (90, 90, 90)))
    return seen, img


@pytest.mark.parametrize("brand", ["MERCEDES-BENZ", "Mercedes-Benz", "ROLLS-ROYCE"])
def test_brand_never_ellipsis_and_stays_in_column(monkeypatch, brand):
    seen, img = drawn(monkeypatch, car(brand=brand, model="CLA 200"))
    assert not any("…" in t for t, st, _ in seen if st == "story_brand")
    from PIL import ImageChops
    blank = render.render_story(car(brand="", model="CLA 200"), Image.new("RGB", (400, 300), (90, 90, 90)),
                                Image.new("RGB", (400, 300), (90, 90, 90)))
    bb = ImageChops.difference(blank, img).crop((0, 1470, 1080, 1700)).getbbox()
    assert bb and bb[2] <= 575 and bb[1] + 1470 >= 1485 and bb[3] + 1470 <= 1580   # só na faixa da marca


@pytest.mark.parametrize("model", ["CLA 200", "CLA 200 PROGRESSIVE", "ROYCE GHOST SERIES II", "GRAND CHEROKEE LIMITED"])
def test_long_model_prefers_wrap_or_shrink_over_ellipsis(monkeypatch, model):
    seen, _ = drawn(monkeypatch, car(model=model))
    assert not any("…" in t for t, st, _ in seen if st == "story_model")


def test_absurd_model_still_fits_with_ellipsis(monkeypatch):
    seen, img = drawn(monkeypatch, car(model="X" * 60))
    assert img.size == (1080, 1920)


# --- F18: parse_adj (rota) -----------------------------------------------------
from fastapi import HTTPException  # noqa: E402


@pytest.mark.parametrize("v", ["1.0000,0.5,0.5", "0.99,0.5,0.5", "3.001,0.5,0.5", "2,1.001,0.5", "2,0.5,-0.1",
                               "a,b,c", "2,0.5", "2,0.5,0.5,0.5", "2,,0.5", "1e1,0.5,0.5", ".5,0.5,0.5",
                               "2.,0.5,0.5", "nan,0.5,0.5", "inf,0.5,0.5", "\uff12,0.5,0.5", "2,0.5,0.5,"])
def test_parse_adj_invalid_400(v):
    with pytest.raises(HTTPException) as e:
        app_main.parse_adj(v, "adj_top")
    assert e.value.status_code == 400 and "adj_top" in e.value.detail


@pytest.mark.parametrize("v,exp", [("1,0.5,0.5", (1, .5, .5)), ("3,1,0", (3, 1, 0)), (" 2 , 0.605 , 0.58 ", (2, .605, .58)),
                                   (None, None), ("", None), ("  ", None)])
def test_parse_adj_valid(v, exp):
    assert app_main.parse_adj(v) == exp
