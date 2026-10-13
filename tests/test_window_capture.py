"""Window capture: the method ladder, its honesty rule, and the GDI surface beneath it.

Three defects lived here, none of them visible without a real desktop:
  * ``CreateCompatibleBitmap`` on a memory DC returns a 1bpp MONOCHROME bitmap, so every
    PrintWindow came back pure black;
  * the black frame tripped the blank check and every window then fell through to a SCREEN grab,
    which returns whatever is on top — how every picker row showed the focused app;
  * the DWM path could never work at all (window handle where a thumbnail handle belongs, a
    non-existent ``DwmUpdateThumbnail``, an invented ``dwSize``/``fMask`` struct, and a host
    window created with a wndproc in the class-name slot).

None of that needs a window to pin: Win32 is faked and the frames are synthetic. No numpy either —
the suite's CI installs pytest, fastapi and Pillow only.
"""
from __future__ import annotations

import ctypes
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageGrab, ImageStat

PLUGIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_DIR / "dashboard"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cw = _load("peripheral_vision_backend_half_window_capture", PLUGIN_DIR / "dashboard" / "capture_windows.py")


def _content() -> Image.Image:
    """A frame with real detail in it — the blank rule looks at the spread of pixel values."""
    img = Image.new("RGB", (8, 6), (0, 0, 0))
    for x in range(8):
        for y in range(6):
            img.putpixel((x, y), (255 if (x + y) % 2 else 30, x * 30 % 256, y * 40 % 256))
    return img


def _black() -> Image.Image:
    return Image.new("RGB", (8, 6), (0, 0, 0))


def _brightness(img: Image.Image) -> float:
    return sum(ImageStat.Stat(img).mean) / 3


def _is_blank(img: Image.Image) -> bool:
    """The blank rule, stated here so these tests never depend on numpy being installed."""
    low, high = img.convert("L").getextrema()
    return high - low <= 1


@pytest.fixture()
def windows_env(monkeypatch):
    """A fake Win32 layer plus a deterministic blank rule, so the ladder runs anywhere."""
    state = SimpleNamespace(is_window=True, is_iconic=False)
    user32 = SimpleNamespace(
        IsWindow=lambda handle: state.is_window,
        IsIconic=lambda handle: state.is_iconic,
    )
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(user32=user32, gdi32=SimpleNamespace(), kernel32=SimpleNamespace()),
        raising=False,
    )
    monkeypatch.setattr(cw, "_is_blank", _is_blank)
    return state


@pytest.fixture()
def screen_grab(monkeypatch):
    """Record whether the screen fallback was reached at all."""
    calls: list[tuple] = []

    def _grab(bbox=None):
        calls.append(bbox)
        return _content()

    monkeypatch.setattr(ImageGrab, "grab", _grab)
    return calls


def test_the_ladder_prefers_a_working_printwindow(windows_env, screen_grab, monkeypatch):
    monkeypatch.setattr(cw, "_grab_window_printwindow", lambda source: _content())
    monkeypatch.setattr(cw, "_grab_window_thumbnail", lambda source: pytest.fail("thumbnail must not run"))

    img = cw._grab_window({"hwnd": 11})

    assert _brightness(img) > 50
    assert screen_grab == [], "a working capture never reaches the screen"


def test_a_blank_printwindow_falls_through_to_the_thumbnail(windows_env, screen_grab, monkeypatch):
    """PrintWindow prints pure black for plenty of real apps here — that must not end the attempt."""
    monkeypatch.setattr(cw, "_grab_window_printwindow", lambda source: _black())
    monkeypatch.setattr(cw, "_grab_window_thumbnail", lambda source: _content())

    img = cw._grab_window({"hwnd": 12})

    assert _brightness(img) > 50
    assert screen_grab == [], "the DWM thumbnail answered it — no screen grab needed"


def test_a_covered_window_is_never_answered_with_a_screen_grab(windows_env, screen_grab, monkeypatch):
    """The reported bug: every row showed the focused app, because the grab returns what covers it."""
    monkeypatch.setattr(cw, "_grab_window_printwindow", lambda source: _black())
    monkeypatch.setattr(cw, "_grab_window_thumbnail", lambda source: _black())
    monkeypatch.setattr(cw, "_window_is_on_top", lambda hwnd: False)
    monkeypatch.setattr(cw, "_window_rect", lambda hwnd: (0, 0, 300, 200))

    with pytest.raises(RuntimeError):
        cw._grab_window({"hwnd": 13})

    assert screen_grab == [], "a covered window must fail honestly, not show whatever is on top"


def test_the_screen_grab_is_used_when_the_window_is_on_top(windows_env, screen_grab, monkeypatch):
    monkeypatch.setattr(cw, "_grab_window_printwindow", lambda source: _black())
    monkeypatch.setattr(cw, "_grab_window_thumbnail", lambda source: _black())
    monkeypatch.setattr(cw, "_window_is_on_top", lambda hwnd: True)
    monkeypatch.setattr(cw, "_window_rect", lambda hwnd: (5, 6, 105, 106))

    img = cw._grab_window({"hwnd": 14})

    assert screen_grab == [(5, 6, 105, 106)], "an unobstructed window may be grabbed from the screen"
    assert _brightness(img) > 50
    assert cw._GRAB_STATE.method == "screen"


def test_a_minimized_window_goes_straight_to_the_thumbnail(windows_env, monkeypatch):
    """A minimized window has no pixels of its own — DWM keeps its last frame."""
    windows_env.is_iconic = True
    monkeypatch.setattr(cw, "_grab_window_printwindow", lambda source: pytest.fail("no PrintWindow for minimized"))
    monkeypatch.setattr(cw, "_grab_window_thumbnail", lambda source: _content())

    img = cw._grab_window({"hwnd": 15})

    assert _brightness(img) > 50


def test_the_dwm_properties_match_the_win32_layout():
    """dwFlags first, and NO dwSize/fMask member — inventing them misaligns every later field."""
    assert [name for name, _ in cw._DWM_THUMBNAIL_PROPERTIES._fields_] == [
        "dwFlags",
        "rctDestination",
        "rctSource",
        "opacity",
        "fVisible",
        "fSourceClientAreaOnly",
    ]
    assert cw.TNP_RECTDESTINATION == 0x1 and cw.TNP_VISIBLE == 0x8


def test_the_capture_surface_is_compatible_with_the_screen(monkeypatch):
    """The root cause of every black window frame: a memory DC's bitmap is 1bpp monochrome."""
    screen, memory_dc = object(), object()
    calls: dict[str, object] = {}

    class _GDI:
        def CreateCompatibleDC(self, source):
            calls["dc_source"] = source
            return memory_dc

        def CreateCompatibleBitmap(self, source, width, height):
            calls["bmp_source"] = source
            calls["size"] = (width, height)
            return object()

        def SelectObject(self, hdc, hbmp):
            return None

        def DeleteObject(self, obj):
            return None

        def DeleteDC(self, dc):
            return None

    user32 = SimpleNamespace(GetDC=lambda hwnd: screen, ReleaseDC=lambda hwnd, dc: None)
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(user32=user32, gdi32=_GDI(), kernel32=SimpleNamespace()),
        raising=False,
    )

    with cw._CaptureBitmap(320, 180) as surface:
        assert (surface.width, surface.height) == (320, 180)

    assert calls["bmp_source"] is screen, (
        "the bitmap must be created from the SCREEN dc — a memory DC's compatible bitmap is "
        "monochrome and every capture reads back black"
    )
    assert calls["size"] == (320, 180)
