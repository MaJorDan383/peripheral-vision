"""Linux backend must emit the same source-dict schema the dashboard consumes.

The consumers (plugin_api.py, desktop/plugin.js) read flat geometry keys
(`x`/`y`/`width`/`height`), `device`/`primary` on monitors, `exe`/`minimized`
on windows and `default`/`readable` on cameras — all shaped by capture_windows.
These tests pin that contract so a Linux-only drift fails here rather than
rendering as blank rows in the dashboard.
"""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

# capture_linux imports cv2/numpy at module scope; stub them when absent so the
# parser contract stays testable on hosts without a camera stack.
for _name in ("cv2", "numpy"):
    try:
        __import__(_name)
    except Exception:  # pragma: no cover - environment dependent
        _mod = types.ModuleType(_name)
        _mod.ndarray = object
        _mod.VideoCapture = None
        _mod.CAP_V4L2 = 0
        _mod.CAP_PROP_FRAME_WIDTH = 3
        _mod.CAP_PROP_FRAME_HEIGHT = 4
        _mod.CAP_PROP_FPS = 5
        sys.modules[_name] = _mod

# pytest.approx inspects numpy itself (python_api calls np.isscalar); a numpy
# stub missing the API pytest expects poisons every later test file that uses
# approx, because sys.modules persists across the whole pytest process. Give
# the stub the surface pytest actually touches.
if "numpy" in sys.modules and not hasattr(sys.modules["numpy"], "isscalar"):
    import numbers as _numbers

    _np = sys.modules["numpy"]
    _np.isscalar = lambda x: isinstance(x, _numbers.Number) or isinstance(x, (str, bytes, bool))
    _np.number = _numbers.Number
    _np.integer = int
    _np.floating = float
    _np.bool_ = bool
    _np.ndarray = getattr(_np, "ndarray", object)
    # approx() may route scalar-looking values through np.asarray; enough for floats.
    def _asarray(x, dtype=None):
        try:
            return float(x)
        except (TypeError, ValueError):
            return x
    _np.asarray = _asarray
    _np.ndindex = lambda shape: iter([()])

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

import capture_linux as L  # noqa: E402

MONITOR_KEYS = {
    "id", "index", "device", "label", "x", "y", "width", "height",
    "primary", "primary_label", "kind",
}
WINDOW_KEYS = {
    "id", "kind", "hwnd", "title", "class", "exe", "pid", "x", "y",
    "width", "height", "minimized", "foreground", "label", "badge",
}
CAMERA_KEYS = {
    "id", "kind", "index", "label", "name", "width", "height",
    "default", "readable", "badge",
}

XRANDR = (
    "Screen 0: minimum 8 x 8, current 3840 x 1080, maximum 32767 x 32767\n"
    "eDP-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 344mm x 194mm\n"
    "   1920x1080     60.05*+  48.00\n"
    "HDMI-1 connected 1920x1080+1920+0 (normal left inverted right x axis y axis) 510mm x 290mm\n"
    "   1920x1080     60.00*+\n"
    "DP-1 disconnected (normal left inverted right x axis y axis)\n"
)

# wmctrl -lp -G: id, desktop, pid, x, y, w, h, host, title
WMCTRL_PID = (
    "0x04200085 0 1234 746 443 468 205 host-1 Niet-opgeslagen document 1 - gedit\n"
    "0x04200086 1 5678 0 0 48 48 host-1 xfce4-panel\n"
)

# wmctrl -l -G (no PID column): same line minus the pid field.
WMCTRL_NO_PID = (
    "0x04200085 0 746 443 468 205 host-1 Niet-opgeslagen document 1 - gedit\n"
)

XWININFO = """xwininfo: Window id: 0x4200085 "Terminal"

  Absolute upper-left X:  100
  Absolute upper-left Y:  50
  Relative upper-left X:  0
  Relative upper-left Y:  0
  Width: 800
  Height: 600
"""


def test_list_windows_accepts_positional_limit():
    """capture.py calls list_windows(limit); a zero-arg signature crashed."""
    assert isinstance(L.list_windows(150), list)


def test_xrandr_monitor_schema_and_position():
    monitors = L.LinuxCapture()._parse_xrandr(XRANDR)
    assert len(monitors) == 2
    assert set(monitors[0]) == MONITOR_KEYS
    # Position must come from the connector line, not default to 0,0.
    assert (monitors[0]["x"], monitors[0]["y"]) == (0, 0)
    assert (monitors[1]["x"], monitors[1]["y"]) == (1920, 0)
    assert monitors[0]["primary"] is True
    assert monitors[0]["primary_label"] == "primary"
    assert monitors[1]["primary"] is False
    assert monitors[1]["primary_label"] == ""


def test_wmctrl_window_schema_pid_and_title(monkeypatch):
    # The app name comes from the host's /proc/<pid>, so pin it: otherwise a real
    # process sitting at the fixture's pid leaks into the label (CI's pid 1234 was
    # a runner process, and the label came back "cleanup — ...").
    monkeypatch.setattr(L, "_window_exe", lambda pid: "")
    windows = L.LinuxCapture()._parse_wmctrl(WMCTRL_PID)
    # xfce4-panel (48x48) is below the size floor.
    assert len(windows) == 1
    w = windows[0]
    assert set(w) == WINDOW_KEYS
    assert w["pid"] == 1234
    assert w["title"] == "Niet-opgeslagen document 1 - gedit"
    assert (w["x"], w["y"], w["width"], w["height"]) == (746, 443, 468, 205)
    # Label follows Windows: "<app> — <title>", falling back to "window".
    assert w["label"] == "window — Niet-opgeslagen document 1 - gedit"


def test_window_label_uses_the_resolved_app_name(monkeypatch):
    monkeypatch.setattr(L, "_window_exe", lambda pid: "firefox")
    w = L.LinuxCapture()._window(
        hwnd=42, pid=4321, title="Docs", x=0, y=0, width=800, height=600,
    )
    assert w["exe"] == "firefox"
    assert w["label"] == "firefox — Docs"


def test_window_label_without_pid_or_title_falls_back_to_the_handle():
    w = L.LinuxCapture()._window(
        hwnd=42, pid=0, title="", x=0, y=0, width=800, height=600,
    )
    assert w["exe"] == ""
    assert w["label"] == "Window 42"


def test_wmctrl_without_pid_column_keeps_full_title():
    """A multi-word title pads both layouts to 9 fields, so column count alone
    cannot distinguish them; the host/height field at index 6 can."""
    windows = L.LinuxCapture()._parse_wmctrl(WMCTRL_NO_PID)
    assert len(windows) == 1
    w = windows[0]
    assert set(w) == WINDOW_KEYS
    assert w["pid"] == 0
    assert w["title"] == "Niet-opgeslagen document 1 - gedit"
    assert (w["x"], w["y"], w["width"], w["height"]) == (746, 443, 468, 205)


def test_get_window_rect_returns_width_height_from_hwnd_source():
    """The facade hands us {"kind", "hwnd"} with no geometry; reading only a
    "bounds" key meant every call returned (0, 0, 0, 0)."""
    c = L.LinuxCapture()
    # No X server here, so fall back to geometry carried on the source.
    rect = c.get_window_rect(
        {"kind": "window", "hwnd": 1, "x": 100, "y": 50, "width": 1000, "height": 500}
    )
    assert rect == (100, 50, 1000, 500)
    assert c.get_window_rect({"kind": "monitor"}) == (0, 0, 0, 0)


def test_legacy_bounds_tuple_still_resolves():
    c = L.LinuxCapture()
    assert c.get_window_rect(
        {"kind": "window", "hwnd": 1, "bounds": (10, 20, 640, 480)}
    ) == (10, 20, 640, 480)


def test_xwininfo_geometry_parser_returns_absolute_bounds():
    assert L._parse_xwininfo_geometry(XWININFO) == (100, 50, 900, 650)


def test_window_exe_resolves_through_proc(monkeypatch):
    assert L._window_exe(0) == ""
    assert L._window_exe("not-a-pid") == ""

    def fake_readlink(path):
        return "/usr/bin/firefox"

    monkeypatch.setattr(L.os, "readlink", fake_readlink)
    assert L._window_exe(4321) == "firefox"


def test_list_sources_reports_consistent_count():
    src = L.list_sources()
    assert "count" in src
    assert src["count"] == (
        len(src["monitors"]) + len(src["windows"]) + len(src["cameras"])
    )


def test_grab_reads_flat_geometry(monkeypatch):
    """Grab must read x/y/width/height, not the retired "bounds" tuple."""
    c = L.LinuxCapture()
    cmds = []

    def fake_run(cmd, **kwargs):
        cmds.append(cmd)

        class R:
            returncode = 1
            stdout = ""

        return R()

    monkeypatch.setattr(L.subprocess, "run", fake_run)
    c._grab_monitor({"x": 10, "y": 20, "width": 640, "height": 480})
    # First backend: ImageMagick `import -crop WxH+X+Y`; fallback: grim `-g X,Y WxH`.
    assert any("-crop" in cmd and "640x480+10+20" in cmd for cmd in cmds)
    assert any("10,20 640x480" in cmd for cmd in cmds)


def test_camera_schema_matches_windows(monkeypatch):
    """Cameras must carry exactly the Windows keys — no unread extras."""

    class _Cap:
        def __init__(self, *a, **k):
            pass

        def isOpened(self):
            return True

        def get(self, code):
            return {3: 640, 4: 480}[code]

        def release(self):
            pass

    def _nofile(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(L.os.path, "exists", lambda p: p == "/dev/video0")
    monkeypatch.setattr(L.cv2, "VideoCapture", _Cap)
    monkeypatch.setattr(L.subprocess, "run", _nofile)

    cams = L.LinuxCapture().list_cameras(force=True)
    assert len(cams) == 1
    assert set(cams[0]) == CAMERA_KEYS, set(cams[0]) ^ CAMERA_KEYS
    assert cams[0]["default"] is True
    assert cams[0]["readable"] is True
    assert cams[0]["badge"] == "default"


# ---------------------------------------------------------------------------
# Linux/Wayland fixes (v1.4.0): Xauth resolution, wayland-info monitors,
# AT-SPI window listing, portal capture, loud black frames.
# ---------------------------------------------------------------------------

WAYLAND_INFO_SAMPLE = """interface: 'zxdg_output_manager_v1',                                   version:  3, name:  1
interface: 'wl_output',                                  version:  4, name:  3
	name: Virtual-1
	description: Unknown Display
	x: 0, y: 0, scale: 1,
	physical_width: 0 mm, physical_height: 0 mm,
	make: 'unknown', model: 'unknown',
	mode:
		width: 1280 px, height: 800 px, refresh: 60.000 Hz,
		flags: current preferred
	mode:
		width: 1024 px, height: 768 px, refresh: 60.000 Hz,
		flags: preferred
interface: 'wl_output',                                  version:  4, name:  4
	name: Virtual-2
	x: 1280, y: 0, scale: 1,
	make: 'unknown', model: 'unknown',
	mode:
		width: 1280 px, height: 800 px, refresh: 60.000 Hz,
		flags: current
"""


def test_wayland_info_parser_reads_outputs_and_current_mode():
    """wayland-info blocks -> flat monitor schema with the current mode only."""
    ms = L.LinuxCapture()._parse_wayland_info(WAYLAND_INFO_SAMPLE)
    assert [m["device"] for m in ms] == ["Virtual-1", "Virtual-2"]
    first, second = ms
    assert (first["x"], first["y"], first["width"], first["height"]) == (0, 0, 1280, 800)
    assert first["primary"] is True
    assert (second["x"], second["width"]) == (1280, 1280)
    assert second["primary"] is False
    for m in ms:
        for key in ("device", "label", "x", "y", "width", "height",
                    "primary", "id", "index", "kind", "primary_label"):
            assert key in m


def test_wayland_info_ignores_foreign_interface_blocks():
    ms = L.LinuxCapture()._parse_wayland_info(
        "interface: 'wl_registry', version: 1, name: 1\n\tname: nope\n")
    assert ms == []


def test_drm_label_strips_sysfs_card_prefix():
    """'card1-Virtual-1' must label as 'Virtual-1', not '1 Virtual 1'."""
    drm_label = L.LinuxCapture()._drm_label
    assert drm_label("card1-Virtual-1") == "Virtual-1"
    assert drm_label("card0-eDP-1") == "eDP-1"
    assert drm_label("HDMI-A-1") == "HDMI-A-1"


def test_atspi_payload_filters_shell_chrome_and_slivers():
    """Untitled toplevels (GNOME Shell's own overlay) and <50px slivers drop."""
    payload = json.dumps([
        {"pid": 42, "title": "Terminal", "x": 5, "y": 6, "width": 800, "height": 500},
        {"pid": 1, "title": "", "x": 0, "y": 0, "width": 1280, "height": 800},
        {"pid": 43, "title": "tiny", "x": 0, "y": 0, "width": 20, "height": 20},
        {"pid": 44, "title": "Dialog", "x": -5, "y": -5, "width": "900", "height": 70},
    ])
    rows = L._windows_from_atspi(payload)
    assert [r["pid"] for r in rows] == [42, 44]
    assert rows[0] == {"pid": 42, "title": "Terminal", "x": 5, "y": 6,
                       "width": 800, "height": 500}


def test_atspi_payload_tolerates_garbage():
    assert L._windows_from_atspi("") == []
    assert L._windows_from_atspi("not json") == []
    assert L._windows_from_atspi('{"pid": 1}') == []


def test_list_windows_wayland_falls_back_to_atspi(monkeypatch):
    """No X11 tools + Wayland -> AT-SPI rows become schema windows (hwnd=pid)."""
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")

    def no_x11(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    payload = json.dumps([
        {"pid": 777, "title": "Editor", "x": 10, "y": 20, "width": 640, "height": 480},
    ])
    monkeypatch.setattr(L.subprocess, "run", no_x11)
    monkeypatch.setattr(L, "_run_atspi", lambda timeout=15.0: payload)

    wins = L.list_windows()
    assert len(wins) == 1
    w = wins[0]
    assert w["hwnd"] == 777 and w["pid"] == 777
    assert w["title"] == "Editor"
    assert (w["x"], w["y"], w["width"], w["height"]) == (10, 20, 640, 480)
    assert w["minimized"] is False
    assert w["badge"] in ("background", "foreground")
    assert w["label"]


def test_grab_on_wayland_skips_xwayland_root_and_ends_black(monkeypatch):
    """import -window root lies on Wayland (Xwayland subtree only) — skipped.

    grim fails on mutter (no wlr-screencopy), the portal is stubbed to a miss,
    and the result must be an EXACT-size black frame with a logged warning —
    never a silent wrong frame.
    """
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(L, "_portal_screenshot", lambda timeout: None)

    cmds = []

    def fake_run(cmd, **kwargs):
        cmds.append(list(cmd))

        class R:
            returncode = 1
            stdout = b""

        return R()

    monkeypatch.setattr(L.subprocess, "run", fake_run)

    with _caplog_at_warning(monkeypatch) as caplog:
        img = L.LinuxCapture().grab({"kind": "monitor", "x": 10, "y": 20,
                                             "width": 640, "height": 480})

    assert not any("-window" in c and "root" in c for c in cmds), \
        "X11 import must not run under Wayland"
    assert any(c and c[0] == "grim" for c in cmds), "grim should be attempted"
    assert img.size == (640, 480)
    assert img.getbbox() is None, "black frame expected"
    assert any("no working capture path" in r.message for r in caplog.records)


def _caplog_at_warning(monkeypatch):
    """Context manager: pytest caplog at WARNING (fixture-free helper)."""
    import logging as _logging
    from contextlib import contextmanager

    @contextmanager
    def cm():
        logger = _logging.getLogger("peripheral_vision.capture_linux")
        records = []

        class H(_logging.Handler):
            def emit(self, record):
                records.append(record)

        h = H()
        logger.addHandler(h)
        try:
            yield type("R", (), {"records": records})()
        finally:
            logger.removeHandler(h)

    return cm()


def test_portal_failure_arms_cooldown(monkeypatch):
    """A denied portal call must not be retried in a tight watch loop."""
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    L._PORTAL_STATE["primed"] = False
    L._PORTAL_STATE["fail_at"] = 0.0
    monkeypatch.setattr(L, "_helper_python", lambda: "/usr/bin/python3")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class R:
            returncode = 0
            stdout = json.dumps({"response": 2, "uri": None}) + "\n"
            stderr = ""

        return R()

    monkeypatch.setattr(L.subprocess, "run", fake_run)
    try:
        assert L._portal_screenshot(1.0) is None
        assert L._PORTAL_STATE["fail_at"] > 0
        assert L._portal_screenshot(1.0) is None
        assert len(calls) == 1, "second attempt must be suppressed by cooldown"
    finally:
        L._PORTAL_STATE["primed"] = False
        L._PORTAL_STATE["fail_at"] = 0.0


def test_xenv_resolves_mutter_xwayland_cookie(monkeypatch, tmp_path):
    """A backend/SSH session with no XAUTHORITY must inherit mutter's cookie."""
    if hasattr(os, "getuid") is False:
        pytest.skip("POSIX only")
    cookie = tmp_path / ".mutter-Xwaylandauth.abc123"
    cookie.write_text("fake")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("XAUTHORITY", raising=False)
    env = L._xenv()
    assert env.get("XAUTHORITY") == str(cookie)

    monkeypatch.setenv("XAUTHORITY", "/explicit/auth")
    assert L._xenv().get("XAUTHORITY") == "/explicit/auth"
