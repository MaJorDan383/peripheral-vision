"""The macOS backend's contract, without a Mac.

Every test here runs on Windows and Linux CI: pyobjc is never imported, the Quartz
calls are driven from fakes shaped exactly the way ``CGWindowListCopyWindowInfo`` and
``CGDisplayBounds`` return their data, and the two CLI seams (``screencapture``,
``system_profiler``, ``ps``) are stubbed at ``subprocess.run``.

What this suite can prove: the schema other backends emit is the schema this one emits,
the tier selection and its reasons are the documented ones, a missing permission or a
missing pyobjc degrades instead of raising, and the system prompt is asked for from a
capture attempt only.

What it cannot prove, and no CI runner can: that a live window grab returns pixels on a
real Mac (Screen Recording is a GUI-session grant). That check needs a Mac.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
DASHBOARD = REPO / "dashboard"
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# conftest.py supplies cv2 for the whole rootdir — a stub when the package is absent, which
# is what CI has. FakeCap is what a default probe answers with: no device attached.
class FakeCap:
    """A capture device: ``opened=False`` is a device that is not there."""

    def __init__(self, opened=True, width=1280, height=720):
        self.opened, self.width, self.height = opened, width, height
        self.released = False

    def isOpened(self):
        return self.opened

    def get(self, prop):
        return self.width if prop == mac.cv2.CAP_PROP_FRAME_WIDTH else self.height

    def release(self):
        self.released = True


import capture_macos as mac  # noqa: E402  (after the path insert above)

# The keys capture_windows/capture_linux emit, and therefore the keys the pane reads.
MONITOR_KEYS = {"id", "index", "device", "label", "x", "y", "width", "height",
                "primary", "primary_label", "kind"}
WINDOW_KEYS = {"id", "kind", "hwnd", "title", "class", "exe", "pid", "x", "y",
               "width", "height", "minimized", "foreground", "label", "badge"}
CAMERA_KEYS = {"id", "kind", "index", "label", "name", "width", "height",
               "default", "readable", "badge"}


# ── Harness ───────────────────────────────────────────────────────────────

class FakeRect:
    """A CGRect stand-in: ``CGDisplayBounds`` hands back a struct, not a dict."""

    class _Point:
        def __init__(self, x, y):
            self.x, self.y = x, y

    class _Size:
        def __init__(self, width, height):
            self.width, self.height = width, height

    def __init__(self, x=0.0, y=0.0, width=0.0, height=0.0):
        self.origin = self._Point(x, y)
        self.size = self._Size(width, height)


class FakeQuartz:
    """A Quartz module with the surface this backend touches, and nothing else."""

    kCGWindowListOptionOnScreenOnly = 1
    kCGWindowListOptionAll = 0
    kCGWindowListOptionIncludingWindow = 8
    kCGWindowListExcludeDesktopElements = 16
    kCGWindowImageBoundsIgnoreFraming = 256
    kCGWindowImageBestResolution = 1024
    kCGNullWindowID = 0
    kCFURLPOSIXPathStyle = 0
    CGRectNull = "<null>"

    def __init__(self, displays=(1,), windows=(), granted=True, empty_image=True, broken=False):
        self._displays = tuple(displays)
        self._windows = list(windows)
        self._granted = granted
        self._empty_image = empty_image
        self._broken = broken
        self.requested = 0
        self.image_requests = []

    # -- the calls under test -------------------------------------------
    def CGGetActiveDisplayList(self, max_displays, none1, none2):
        if self._broken:
            raise RuntimeError("no window server")
        return (0, list(self._displays), len(self._displays))

    def CGDisplayBounds(self, display_id):
        return FakeRect(0.0, 0.0, 1440.0, 900.0) if display_id == 1 else FakeRect(-1280.0, 0.0, 1280.0, 800.0)

    def CGDisplayPixelsWide(self, display_id):
        return 2880 if display_id == 1 else 1280

    def CGDisplayIsMain(self, display_id):
        return display_id == 1

    def CGMainDisplayID(self):
        return 1

    def CGWindowListCopyWindowInfo(self, options, relative_to):
        return [dict(row) for row in self._windows]

    def CGDisplayCreateImage(self, display_id):
        self.image_requests.append(("display", display_id))
        return None if self._empty_image else object()

    def CGWindowListCreateImage(self, rect, option, window_id, image_option):
        self.image_requests.append(("window", window_id))
        return None if self._empty_image else object()

    def CGImageGetWidth(self, image):
        return 800

    def CGImageGetHeight(self, image):
        return 600

    # -- the permission surface -----------------------------------------
    def CGPreflightScreenCaptureAccess(self):
        return self._granted

    def CGRequestScreenCaptureAccess(self):
        self.requested += 1
        return self._granted


def window_row(number=42, pid=501, owner="Safari", title="Docs", x=10, y=20, w=900, h=600,
               layer=0, onscreen=True):
    """One ``CGWindowListCopyWindowInfo`` entry, keyed as pyobjc keys it."""
    row = {
        "kCGWindowNumber": number,
        "kCGWindowOwnerPID": pid,
        "kCGWindowOwnerName": owner,
        "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h},
        "kCGWindowLayer": layer,
        "kCGWindowIsOnscreen": onscreen,
    }
    if title is not None:
        row["kCGWindowName"] = title
    return row


@pytest.fixture(autouse=True)
def clean_slate(monkeypatch):
    """Every test starts with no grabbed frame, no cached probe and no prompt asked."""
    monkeypatch.setattr(mac, "_QUARTZ", {"module": None, "checked": False, "error": ""})
    monkeypatch.setattr(mac, "_PERMISSION", {"state": "unknown", "reason": "", "asked": False})
    monkeypatch.setattr(mac, "_PID_NAMES", {})
    monkeypatch.setattr(mac, "_CAMERA_NAMES", {"at": 0.0, "names": []})
    monkeypatch.setattr(mac, "_CAMERA_CACHE", {"at": 0.0, "devices": [], "probing": False})
    monkeypatch.setattr(mac, "_CONVERT", {"error": ""})
    monkeypatch.setitem(mac._GRAB_STATE.__dict__, "method", "")
    monkeypatch.setitem(mac._GRAB_STATE.__dict__, "waiting", "")
    # A probe answers "nothing attached" — no real webcam, and the same answer whether cv2
    # is the real package or a stub. A test that wants a device patches VideoCapture itself.
    monkeypatch.setattr(mac.cv2, "VideoCapture", lambda index, backend: FakeCap(opened=False))
    yield


@pytest.fixture
def quartz(monkeypatch):
    """Install a fake Quartz; the probe cache is filled so no import is attempted."""

    def _install(fake: FakeQuartz) -> FakeQuartz:
        monkeypatch.setattr(mac, "_QUARTZ", {"module": fake, "checked": True, "error": ""})
        return fake

    return _install


@pytest.fixture
def no_pyobjc(monkeypatch):
    """The CLI tier: pyobjc genuinely absent, with the error a real host would give."""
    monkeypatch.setattr(mac, "_QUARTZ", {
        "module": None, "checked": True,
        "error": "pyobjc-framework-Quartz is not available (ModuleNotFoundError); "
                 "window capture needs it (pip install pyobjc-framework-Quartz)",
    })


# ── The seam the facade reaches through ───────────────────────────────────

def test_the_facade_finds_every_required_seam():
    for name in ("list_monitors", "list_windows", "list_cameras", "list_sources",
                 "grab", "is_minimized", "get_window_rect"):
        assert callable(getattr(mac, name)), f"capture.py calls {name} unconditionally"


def test_the_guarded_seams_the_facade_looks_for_are_present():
    """capture.py hasattr()s these; a missing one silently degrades the snap paths."""
    for name in ("grab_window_thumbnail", "supports_snap_full_res", "cleanup", "release_camera",
                 "probe_cameras_now", "cameras_probing", "abort_camera_probe",
                 "_camera_source", "_CAMERA_CACHE", "_window_iconic", "_window_rested",
                 "_window_exe", "_window_rect", "_CAM_HANDLE"):
        assert hasattr(mac, name), f"{name} is missing"


def test_capture_dispatches_darwin_to_this_backend(monkeypatch):
    """The platform route itself: darwin must not land on the X11 backend."""
    import capture

    monkeypatch.setattr(sys, "platform", "darwin")
    assert capture._backend() is mac


def test_darwin_is_not_the_only_thing_that_changed(monkeypatch):
    """The other two platforms still route where they did."""
    import capture

    for platform, module_name in (("win32", "capture_windows"), ("linux", "capture_linux"),
                                  ("freebsd12", "capture_linux")):
        monkeypatch.setattr(sys, "platform", platform)
        assert capture._backend().__name__ == module_name


# ── Monitors ──────────────────────────────────────────────────────────────

def test_monitor_rows_match_the_schema_the_other_backends_emit(quartz):
    quartz(FakeQuartz(displays=(1, 2)))
    monitors = mac.list_monitors()
    assert len(monitors) == 2
    for row in monitors:
        assert MONITOR_KEYS <= set(row)
        assert row["kind"] == "monitor"
        assert row["id"] == f"monitor-{row['index']}"
    assert monitors[0]["primary"] is True
    assert monitors[0]["primary_label"] == "primary"
    assert (monitors[0]["width"], monitors[0]["height"]) == (1440, 900)
    assert monitors[0]["device"] == "Display 1"


def test_a_second_display_keeps_its_negative_origin(quartz):
    """A display left of the main one has a negative x — a crop that flattens it breaks."""
    quartz(FakeQuartz(displays=(1, 2)))
    left = mac.list_monitors()[1]
    assert (left["x"], left["y"]) == (-1280, 0)
    assert left["primary"] is False
    assert left["primary_label"] == ""


def test_retina_scale_is_reported_not_guessed(quartz):
    """Frames come back at native pixels; the row says how many pixels one point is."""
    quartz(FakeQuartz(displays=(1, 2)))
    scales = {row["index"]: row["scale"] for row in mac.list_monitors()}
    assert scales[0] == 2.0      # 2880 px over 1440 pt
    assert scales[1] == 1.0      # 1280 px over 1280 pt


def test_the_display_list_reads_either_shape_pyobjc_returns():
    """(err, ids, count) and a bare ids list both appear in the wild."""
    class Bare:
        def CGGetActiveDisplayList(self, *args):
            return [7, 8]

    assert mac._active_display_ids(Bare()) == [7, 8]
    assert mac._active_display_ids(FakeQuartz(displays=(3, 4, 5))) == [3, 4, 5]


def test_a_broken_window_server_falls_back_instead_of_raising(quartz):
    quartz(FakeQuartz(broken=True))
    assert mac.list_monitors() == [] or mac.list_monitors()[0]["kind"] == "monitor"
    assert mac._capture_state()["tier"] == "coregraphics"  # pyobjc is still there


def test_without_pyobjc_a_monitor_row_still_comes_back(no_pyobjc, monkeypatch):
    """The CLI tier must never hand the picker an empty list."""
    monkeypatch.setattr(mac, "_desktop_size", lambda: (1512, 982))
    monitors = mac.list_monitors()
    assert len(monitors) == 1
    assert (monitors[0]["width"], monitors[0]["height"]) == (1512, 982)
    assert monitors[0]["kind"] == "monitor"
    assert monitors[0]["display_id"] == 0  # no per-display id in this tier


# ── Geometry ──────────────────────────────────────────────────────────────

def test_rect_parts_reads_both_shapes():
    assert mac._rect_parts({"X": 1, "Y": 2, "Width": 3, "Height": 4}) == (1, 2, 3, 4)
    assert mac._rect_parts(FakeRect(5.4, 6.6, 7.2, 8.0)) == (5, 7, 7, 8)
    assert mac._rect_parts(None) == (0, 0, 0, 0)
    assert mac._rect_parts({"X": 1}) == (0, 0, 0, 0)


def test_flat_rect_still_accepts_the_legacy_bounds_tuple():
    assert mac._flat_rect({"x": 1, "y": 2, "width": 3, "height": 4}) == (1, 2, 3, 4)
    assert mac._flat_rect({"bounds": (5, 6, 7, 8)}) == (5, 6, 7, 8)
    assert mac._flat_rect({}) == (0, 0, 0, 0)


# ── Windows ───────────────────────────────────────────────────────────────

def test_window_rows_match_the_schema_the_other_backends_emit(quartz, monkeypatch):
    monkeypatch.setattr(mac, "_pid_name", lambda pid: "Safari")
    quartz(FakeQuartz(windows=[window_row()]))
    windows = mac.list_windows()
    assert len(windows) == 1
    row = windows[0]
    assert WINDOW_KEYS <= set(row)
    assert row["hwnd"] == 42
    assert row["pid"] == 501
    assert row["exe"] == "Safari"
    assert row["label"] == "Safari — Docs"
    assert (row["x"], row["y"], row["width"], row["height"]) == (10, 20, 900, 600)


def test_a_redacted_title_still_labels_the_window(quartz, monkeypatch):
    """Without Screen Recording, kCGWindowName is absent — the row must survive it."""
    monkeypatch.setattr(mac, "_pid_name", lambda pid: "")
    quartz(FakeQuartz(windows=[window_row(title=None, owner="Terminal")]))
    row = mac.list_windows()[0]
    assert row["title"] == ""
    assert row["label"] == "Terminal"
    assert row["exe"] == "Terminal"  # falls back to the owner name


def test_non_windows_are_filtered_out_of_the_list(quartz, monkeypatch):
    monkeypatch.setattr(mac, "_pid_name", lambda pid: "app")
    quartz(FakeQuartz(windows=[
        window_row(number=1, layer=25),               # the menu bar / Dock / a HUD
        window_row(number=2, w=20, h=20),             # a 20 px sliver, not a window
        window_row(number=3, title="Real"),
    ]))
    windows = mac.list_windows()
    assert [row["hwnd"] for row in windows] == [3]


def test_the_window_list_is_capped(quartz, monkeypatch):
    monkeypatch.setattr(mac, "_pid_name", lambda pid: "app")
    quartz(FakeQuartz(windows=[window_row(number=n, title=f"w{n}") for n in range(1, 30)]))
    assert len(mac.list_windows(limit=5)) == 5


def test_without_pyobjc_windows_are_empty_and_the_reason_is_reported(no_pyobjc):
    assert mac.list_windows() == []
    state = mac._capture_state()
    assert state["window_listing"] is False
    assert "pyobjc-framework-Quartz" in mac._capture_reason()


def test_a_minimized_window_is_absent_from_the_list_but_still_answerable(quartz):
    """CoreGraphics hides minimized windows from the on-screen list; is_minimized still knows."""
    cg = quartz(FakeQuartz(windows=[window_row(number=42, onscreen=False)]))

    def on_screen_only(options, relative_to):
        if options & FakeQuartz.kCGWindowListOptionOnScreenOnly:
            return []
        return [window_row(number=42, onscreen=False)]

    cg.CGWindowListCopyWindowInfo = on_screen_only
    assert mac.list_windows() == []
    assert mac.is_minimized({"kind": "window", "hwnd": 42}) is True


def test_a_window_on_screen_is_not_minimized(quartz):
    quartz(FakeQuartz(windows=[window_row(number=42)]))
    assert mac.is_minimized({"kind": "window", "hwnd": 42}) is False


def test_a_window_that_no_longer_exists_is_not_reported_as_minimized(quartz):
    """Gone is not minimized: the facade has a different note (and a different retry) for it."""
    quartz(FakeQuartz(windows=[window_row(number=7)]))
    assert mac.is_minimized({"kind": "window", "hwnd": 99}) is False


def test_is_minimized_is_false_for_everything_that_is_not_a_window(quartz):
    quartz(FakeQuartz())
    assert mac.is_minimized({"kind": "monitor", "index": 0}) is False
    assert mac.is_minimized({"kind": "camera", "index": 0}) is False


def test_window_rect_is_x1_y1_x2_y2_like_the_other_backends(quartz):
    quartz(FakeQuartz(windows=[window_row(number=42, x=10, y=20, w=900, h=600)]))
    assert mac._window_rect(42) == (10, 20, 910, 620)
    assert mac._window_rect(99) is None
    assert mac._window_rect(0) is None


def test_get_window_rect_is_x_y_w_h_and_resolves_live(quartz):
    """A window moved since the snap must be grabbed where it is now."""
    quartz(FakeQuartz(windows=[window_row(number=42, x=100, y=200, w=300, h=400)]))
    assert mac.get_window_rect({"kind": "window", "hwnd": 42, "x": 0, "y": 0, "width": 1, "height": 1}) \
        == (100, 200, 300, 400)


def test_get_window_rect_uses_the_stored_rect_when_the_window_is_unlisted(quartz):
    quartz(FakeQuartz(windows=[window_row(number=7)]))
    source = {"kind": "window", "hwnd": 99, "x": 5, "y": 6, "width": 7, "height": 8}
    assert mac.get_window_rect(source) == (5, 6, 7, 8)


def test_get_window_rect_of_a_non_window_is_all_zeroes(quartz):
    quartz(FakeQuartz())
    assert mac.get_window_rect({"kind": "monitor", "x": 1, "y": 2, "width": 3, "height": 4}) == (0, 0, 0, 0)


def test_a_minimized_window_still_reports_the_rect_it_will_restore_to(quartz):
    """The snap upgrade path needs the geometry before the window is back on screen."""
    quartz(FakeQuartz(windows=[window_row(number=42, x=400, y=300, w=800, h=500, onscreen=False)]))
    assert mac._restored_rect(42) == (400, 300, 1200, 800)


def test_window_rested_is_true_when_the_window_has_no_geometry(quartz):
    quartz(FakeQuartz(windows=[window_row(number=7)]))
    assert mac._window_rested(99) is True  # unknown geometry must not block the upgrade


def test_pid_names_come_from_ps_and_are_cached(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "ps":
            return type("P", (), {"returncode": 0, "stdout": "/Applications/Safari.app/Contents/MacOS/Safari\n"})()
        raise AssertionError(argv)

    monkeypatch.setattr(mac.subprocess, "run", fake_run)
    assert mac._window_exe(501) == "Safari"
    assert mac._window_exe(501) == "Safari"
    assert len(calls) == 1, "a window list must not fork ps once per row"


def test_a_dead_pid_reports_no_name_instead_of_raising(monkeypatch):
    monkeypatch.setattr(mac.subprocess, "run",
                       lambda argv, **kw: type("P", (), {"returncode": 1, "stdout": ""})())
    assert mac._window_exe(999999) == ""


def test_a_failing_ps_is_survivable(monkeypatch):
    def boom(argv, **kwargs):
        raise OSError("ps is gone")

    monkeypatch.setattr(mac.subprocess, "run", boom)
    assert mac._window_exe(501) == ""


# ── Grabs ─────────────────────────────────────────────────────────────────

def test_a_window_grab_copies_the_windows_own_pixels(quartz, monkeypatch):
    """The PrintWindow peer: an occluded window still comes back whole."""
    sentinel = Image.new("RGB", (800, 600), (1, 2, 3))
    monkeypatch.setattr(mac, "_cgimage_to_pil", lambda cg, image: sentinel)
    cg = quartz(FakeQuartz(empty_image=False))
    assert mac.grab({"kind": "window", "hwnd": 42}) is sentinel
    assert cg.image_requests == [("window", 42)]
    assert mac._GRAB_STATE.method == "coregraphics"
    assert mac._GRAB_STATE.waiting == ""


def test_a_display_grab_uses_the_display_not_a_region(quartz, monkeypatch):
    sentinel = Image.new("RGB", (2880, 1800), (0, 0, 0))
    monkeypatch.setattr(mac, "_cgimage_to_pil", lambda cg, image: sentinel)
    monkeypatch.setattr(mac, "_screencapture_region", lambda *a: pytest.fail("should not fall back"))
    cg = quartz(FakeQuartz(empty_image=False))
    source = {"kind": "monitor", "display_id": 1, "x": 0, "y": 0, "width": 1440, "height": 900}
    assert mac.grab(source) is sentinel
    assert cg.image_requests == [("display", 1)]
    assert mac._GRAB_STATE.method == "coregraphics"


def test_a_display_grab_falls_back_to_the_cli_tier(monkeypatch):
    """pyobjc present but CGDisplayCreateImage produced nothing: screencapture still answers."""
    monkeypatch.setattr(mac, "_QUARTZ", {"module": FakeQuartz(empty_image=True), "checked": True, "error": ""})
    sentinel = Image.new("RGB", (1440, 900), (9, 9, 9))
    calls = []

    def region(x, y, w, h):
        calls.append((x, y, w, h))
        return sentinel

    monkeypatch.setattr(mac, "_screencapture_region", region)
    source = {"kind": "monitor", "display_id": 1, "x": 0, "y": 0, "width": 1440, "height": 900}
    assert mac.grab(source) is sentinel
    assert calls == [(0, 0, 1440, 900)]
    assert mac._GRAB_STATE.method == "screencapture"


def test_a_window_grab_without_pyobjc_is_a_region_and_says_so(no_pyobjc, monkeypatch):
    """Region grabs can include whatever covers the window — the facade's note says so."""
    sentinel = Image.new("RGB", (900, 600), (4, 4, 4))
    monkeypatch.setattr(mac, "_screencapture_region", lambda *a: sentinel)
    source = {"kind": "window", "hwnd": 42, "x": 10, "y": 20, "width": 900, "height": 600}
    assert mac.grab(source) is sentinel
    assert mac._GRAB_STATE.method == "screencapture"
    assert "another window may cover it" in mac._GRAB_STATE.waiting


def test_a_grab_with_no_path_at_all_returns_a_placeholder_and_a_reason(no_pyobjc, monkeypatch):
    """Never raise, never claim a frame: the pane shows the note verbatim."""
    monkeypatch.setattr(mac, "_screencapture_region", lambda *a: None)
    monkeypatch.setattr(mac, "_screen_recording_state",
                       lambda: {"state": "denied", "reason": mac._PERMISSION_REASON})
    image = mac.grab({"kind": "monitor", "x": 0, "y": 0, "width": 640, "height": 480})
    assert image.size == (640, 480)
    assert image.getpixel((0, 0)) == (0, 0, 0)
    assert "Screen Recording" in mac._GRAB_STATE.waiting


def test_a_failed_conversion_is_reported_instead_of_a_silent_region(quartz, monkeypatch):
    """The window had pixels and the host could not decode them: that is not 'nothing to copy'."""
    monkeypatch.setattr(mac, "_cgimage_to_pil", lambda cg, image: None)
    monkeypatch.setattr(mac, "_CONVERT",
                       {"error": "the captured image could not be converted to a frame (RuntimeError: boom)"})
    quartz(FakeQuartz(empty_image=False))
    monkeypatch.setattr(mac, "_screencapture_region", lambda *a: Image.new("RGB", (8, 8)))
    image = mac.grab({"kind": "window", "hwnd": 42, "x": 0, "y": 0, "width": 8, "height": 8})
    assert image.size == (8, 8)
    assert "could not be converted" in mac._GRAB_STATE.waiting


def test_a_conversion_failure_with_nothing_behind_it_is_what_the_pane_says(quartz, monkeypatch):
    monkeypatch.setattr(mac, "_cgimage_to_pil", lambda cg, image: None)
    monkeypatch.setattr(mac, "_CONVERT", {"error": "conversion exploded"})
    quartz(FakeQuartz(empty_image=False))
    monkeypatch.setattr(mac, "_screencapture_region", lambda *a: None)
    mac.grab({"kind": "window", "hwnd": 42, "x": 0, "y": 0, "width": 4, "height": 4})
    assert mac._GRAB_STATE.waiting == "conversion exploded"


def test_an_empty_surface_is_not_a_conversion_failure(monkeypatch):
    """A minimized window's 0x0 image must clear a stale conversion error, not preserve it."""

    class Empty:
        def CGImageGetWidth(self, image):
            return 0

        def CGImageGetHeight(self, image):
            return 0

    monkeypatch.setattr(mac, "_CONVERT", {"error": "stale"})
    assert mac._cgimage_to_pil(Empty(), object()) is None
    assert mac._CONVERT["error"] == ""


def test_an_empty_window_image_is_a_placeholder_sized_like_the_window(quartz, monkeypatch):
    """A minimized window has no surface: black of the right size plus the reason."""
    quartz(FakeQuartz(empty_image=True))
    monkeypatch.setattr(mac, "_screencapture_region", lambda *a: None)
    monkeypatch.setattr(mac, "_screen_recording_state", lambda: {"state": "granted", "reason": ""})
    image = mac.grab({"kind": "window", "hwnd": 42, "x": 0, "y": 0, "width": 320, "height": 200})
    assert image.size == (320, 200)
    assert mac._GRAB_STATE.waiting


def test_an_unknown_source_kind_is_a_programming_error():
    with pytest.raises(ValueError):
        mac.grab({"kind": "hologram"})


def test_the_thumbnail_seam_never_raises_for_a_minimized_window(quartz, monkeypatch):
    """The facade's fallback needs a value, not an exception, from this seam."""
    quartz(FakeQuartz(empty_image=True))
    monkeypatch.setattr(mac, "_screencapture_region", lambda *a: None)
    image = mac.grab_window_thumbnail(42)
    assert isinstance(image, Image.Image)


def test_full_resolution_snaps_are_supported(quartz):
    quartz(FakeQuartz())
    assert mac.supports_snap_full_res() is True


# ── The Screen Recording grant ────────────────────────────────────────────

def test_a_granted_permission_is_reported_as_granted(quartz):
    quartz(FakeQuartz(granted=True))
    assert mac._capture_state()["permission"]["screen_recording"] == "granted"


def test_a_denied_permission_is_reported_with_the_system_settings_route(quartz):
    quartz(FakeQuartz(granted=False))
    state = mac._capture_state()["permission"]
    assert state["screen_recording"] == "denied"
    assert "System Settings" in state["reason"]
    assert "black" in state["reason"]


def test_without_pyobjc_the_permission_state_is_unknown_not_denied(no_pyobjc):
    """pyobjc missing says nothing about the grant: report the gap, not a guess."""
    assert mac._capture_state()["permission"]["screen_recording"] == "unknown"


def test_a_grab_asks_for_the_grant_once(quartz, monkeypatch):
    cg = quartz(FakeQuartz(granted=False, empty_image=True))
    monkeypatch.setattr(mac, "_screencapture_region", lambda *a: None)
    source = {"kind": "monitor", "display_id": 1, "x": 0, "y": 0, "width": 100, "height": 100}
    mac.grab(source)
    mac.grab(source)
    assert cg.requested == 1, "the system dialog must be asked for once, not per poll"


def test_enumerating_sources_never_asks_for_the_grant(quartz, monkeypatch):
    """A pane poll must not pop a permission dialog at someone who is not capturing."""
    cg = quartz(FakeQuartz(granted=False, windows=[window_row()]))
    monkeypatch.setattr(mac, "_pid_name", lambda pid: "app")
    mac.list_sources()
    mac.list_monitors()
    mac.list_windows()
    mac.list_cameras()
    assert cg.requested == 0


def test_a_failed_request_call_does_not_fail_the_grab(quartz, monkeypatch):
    cg = quartz(FakeQuartz(granted=False, empty_image=True))

    def boom():
        raise RuntimeError("no GUI session")

    cg.CGRequestScreenCaptureAccess = boom
    monkeypatch.setattr(mac, "_screencapture_region", lambda *a: None)
    image = mac.grab({"kind": "monitor", "x": 0, "y": 0, "width": 10, "height": 10})
    assert image.size == (10, 10)


def test_a_host_too_old_to_answer_the_preflight_says_unknown(quartz):
    cg = quartz(FakeQuartz())
    cg.CGPreflightScreenCaptureAccess = None  # the call this backend can't make
    assert mac._capture_state()["permission"]["screen_recording"] == "unknown"


def test_the_permission_reason_names_the_trade_the_user_is_making(quartz):
    quartz(FakeQuartz(granted=False))
    assert mac._PERMISSION_REASON in mac._capture_reason()


# ── The capture block on /sources ─────────────────────────────────────────

def test_the_capture_block_names_the_platform_and_tier(quartz):
    quartz(FakeQuartz())
    block = mac.list_sources()["capture"]
    assert block["platform"] == "macos"
    assert block["tier"] == "coregraphics"
    assert block["window_listing"] is True


def test_the_block_falls_back_to_the_cli_tier_without_pyobjc(no_pyobjc, monkeypatch):
    monkeypatch.setattr(mac, "_screencapture_path", lambda: "/usr/sbin/screencapture")
    block = mac._capture_state()
    assert block["tier"] == "screencapture"
    assert block["window_listing"] is False


def test_the_block_reports_no_tier_when_the_tool_is_missing_too(no_pyobjc, monkeypatch):
    monkeypatch.setattr(mac, "_screencapture_path", lambda: "")
    assert mac._capture_state()["tier"] == "none"
    assert "screencapture" in mac._capture_reason()


def test_the_block_carries_the_live_grab_state(quartz):
    quartz(FakeQuartz())
    mac._method("coregraphics")
    mac._waiting("waiting for the window")
    block = mac._capture_state()
    assert block["method"] == "coregraphics"
    assert block["waiting"] == "waiting for the window"


def test_list_sources_counts_everything_it_returns(quartz, monkeypatch):
    monkeypatch.setattr(mac, "_pid_name", lambda pid: "app")
    monkeypatch.setattr(mac, "_CAMERA_CACHE", {"at": 0.0, "devices": [], "probing": False})
    quartz(FakeQuartz(windows=[window_row()]))
    sources = mac.list_sources()
    assert sources["count"] == len(sources["monitors"]) + len(sources["windows"]) + len(sources["cameras"])


# ── Cameras ───────────────────────────────────────────────────────────────


def test_camera_rows_match_the_schema_the_other_backends_emit(monkeypatch):
    monkeypatch.setattr(mac, "_camera_names", lambda: ["FaceTime HD Camera"])
    monkeypatch.setattr(mac.cv2, "VideoCapture",
                       lambda index, backend: FakeCap(opened=index == 0, width=1280, height=720))
    cameras = mac.list_cameras(force=True)
    assert len(cameras) == 1
    assert CAMERA_KEYS <= set(cameras[0])
    assert cameras[0]["index"] == 0
    assert cameras[0]["name"] == "FaceTime HD Camera"
    assert cameras[0]["label"] == "FaceTime HD Camera"
    assert cameras[0]["default"] is True
    assert (cameras[0]["width"], cameras[0]["height"]) == (1280, 720)


def test_a_camera_probe_stops_at_the_first_gap(monkeypatch):
    """AVFoundation logs a warning for every index past the last device."""
    seen = []

    def fake_capture(index, backend):
        seen.append(index)
        return FakeCap(opened=index == 0)

    monkeypatch.setattr(mac, "_camera_names", lambda: [])
    monkeypatch.setattr(mac.cv2, "VideoCapture", fake_capture)
    mac.list_cameras(force=True)
    assert seen == [0, 1, 2], "two misses in a row must end the probe"


def test_a_device_that_will_not_open_is_still_listed_by_index(monkeypatch):
    monkeypatch.setattr(mac, "_camera_names", lambda: [])
    monkeypatch.setattr(mac.cv2, "VideoCapture",
                       lambda index, backend: FakeCap(opened=False))
    assert mac.list_cameras(force=True) == []


def test_a_slow_probe_can_be_abandoned(monkeypatch):
    """A pane pick must not wait behind a probe that is walking stale indices."""
    mac.abort_camera_probe()
    monkeypatch.setattr(mac, "_camera_names", lambda: [])
    monkeypatch.setattr(mac.cv2, "VideoCapture", lambda index, backend: FakeCap(opened=index == 0))
    cameras = mac.list_cameras(force=True)
    assert cameras == [] or cameras[0]["index"] == 0
    assert mac.cameras_probing() is False, "the abort must clear the in-flight flag"


def test_camera_names_are_read_from_the_profiler_and_cached(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        payload = '{"SPCameraDataType": [{"_name": "FaceTime HD Camera"}, {"_name": "OBS Virtual Camera"}]}'
        return type("P", (), {"returncode": 0, "stdout": payload})()

    monkeypatch.setattr(mac.subprocess, "run", fake_run)
    assert mac._camera_names() == ["FaceTime HD Camera", "OBS Virtual Camera"]
    assert mac._camera_names() == ["FaceTime HD Camera", "OBS Virtual Camera"]
    assert len(calls) == 1, "the profiler costs about a second: ask once"


def test_a_junk_profiler_payload_is_not_fatal(monkeypatch):
    monkeypatch.setattr(mac.subprocess, "run",
                       lambda argv, **kw: type("P", (), {"returncode": 0, "stdout": "not json"})())
    assert mac._camera_names() == []


def test_a_missing_profiler_is_not_fatal(monkeypatch):
    def boom(argv, **kwargs):
        raise OSError("no system_profiler")

    monkeypatch.setattr(mac.subprocess, "run", boom)
    assert mac._camera_names() == []


def test_camera_source_reads_the_cache(monkeypatch):
    monkeypatch.setattr(mac, "_CAMERA_CACHE",
                       {"at": 0.0, "devices": [{"id": "camera-0", "index": 0, "kind": "camera",
                                                "label": "FaceTime HD Camera"}], "probing": False})
    assert mac._camera_source(0)["label"] == "FaceTime HD Camera"


def test_camera_source_describes_an_uncached_index(monkeypatch):
    source = mac._camera_source(3)
    assert source["kind"] == "camera"
    assert source["id"] == "camera-3"
    assert source["default"] is False


def test_cleanup_releases_the_camera_and_forgets_the_probe(monkeypatch):
    cap = FakeCap()
    monkeypatch.setitem(mac._CAM_HANDLE, "cap", cap)
    monkeypatch.setattr(mac, "_CAMERA_CACHE", {"at": 1.0, "devices": [{"id": "camera-0"}], "probing": False})
    mac.cleanup()
    assert cap.released is True
    assert mac._CAM_HANDLE["cap"] is None
    assert mac._CAMERA_CACHE["devices"] == []


# ── The pyobjc probe itself ───────────────────────────────────────────────

def test_the_probe_is_cached_and_reports_its_reason_once(monkeypatch):
    """`import Quartz` must not be attempted per call, and the reason must survive."""
    monkeypatch.setattr(mac, "_QUARTZ", {"module": None, "checked": False, "error": ""})
    # A None entry in sys.modules is what `import Quartz` sees when the framework is
    # genuinely absent; it costs nothing to re-try, so the probe flag does the caching.
    monkeypatch.setitem(sys.modules, "Quartz", None)
    assert mac._quartz() is None
    assert mac._QUARTZ["checked"] is True
    monkeypatch.setattr(mac, "_QUARTZ", {**mac._QUARTZ, "error": "sentinel"})
    assert mac._quartz() is None
    assert mac._QUARTZ["error"] == "sentinel", "a second call must not re-import or re-word the reason"


def test_the_missing_framework_reason_tells_the_user_what_to_install(monkeypatch):
    monkeypatch.setattr(mac, "_QUARTZ", {"module": None, "checked": False, "error": ""})
    monkeypatch.setitem(sys.modules, "Quartz", None)
    mac._quartz()
    assert "is not available (" in mac._quartz_error()
    assert "pip install pyobjc-framework-Quartz" in mac._quartz_error()


def test_a_working_quartz_is_handed_back(monkeypatch):
    fake = FakeQuartz()
    monkeypatch.setattr(mac, "_QUARTZ", {"module": None, "checked": False, "error": ""})
    monkeypatch.setitem(sys.modules, "Quartz", fake)
    assert mac._quartz() is fake
    assert mac._quartz_error() == "pyobjc-framework-Quartz is unavailable"  # no error was recorded
