"""
macOS capture backend.

Implements the cross-platform capture API with:

- **CoreGraphics** (pyobjc: ``pyobjc-framework-Quartz``) as the primary tier —
  ``CGWindowListCopyWindowInfo`` for the source list, ``CGDisplayCreateImage`` /
  ``CGWindowListCreateImage`` for frames, which include a window's *own* pixels even
  when another app covers it (the ``PrintWindow`` peer);
- **``screencapture`` + ``system_profiler``** as the dependency-free tier: displays
  as regions by flag, camera names from the profiler. It cannot enumerate windows and
  cannot read a window's own pixels, so it is a fallback for displays — never the
  primary path;
- **AVFoundation/OpenCV** for camera capture.

Two macOS facts are surfaced rather than hidden, because both look like success:

- **Screen Recording permission (TCC).** Until the *host* process (the Hermes app or
  its backend) holds the grant, window titles come back redacted and every grab comes
  back black — with no error. The state rides on the ``capture`` block of ``/sources``
  and the reason rides on the watch status line, the same pattern the PipeWire tier
  uses for a missing ``gstreamer1.0-pipewire``.
- **No minimized-window thumbnail.** Nothing renders a minimized window's pixels, so
  there is no ``DwmRegisterThumbnail`` peer here. ``_window_iconic`` reports the state
  and the facade serves that source's last good frame through its own fallback; this
  backend never claims a live frame it cannot produce.

The per-thread grab state (``_GRAB_STATE.method`` / ``.waiting``) is the same object
``plugin_api`` reads, so what a grab did — and why a frame is not live — reaches the
pane without a second channel.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Optional

import cv2
from PIL import Image

from shared_state import (
    CAM_HANDLE as _CAM_HANDLE,
    CAM_LOCK as _CAM_LOCK,
    GRAB_STATE as _GRAB_STATE,
)

_LOG = logging.getLogger("peripheral_vision.capture_macos")

# Lazy pyobjc probe: the module must import (and the CLI tier must work) on a host
# without pyobjc, so `import Quartz` is never at module scope.
_QUARTZ: dict = {"module": None, "checked": False, "error": ""}

# The Screen Recording grant, as last read. `asked` bounds the system prompt to once.
_PERMISSION: dict = {"state": "unknown", "reason": "", "asked": False}

# pid -> (read_at, name). A window list would otherwise fork `ps` once per row.
_PID_NAMES: dict = {}
_PID_NAMES_LOCK = threading.Lock()
_PID_NAMES_TTL_S = 30.0

# Camera probing mirrors the other backends: one client per device, a cache the
# facade reads through `_CAMERA_CACHE`, and an abort flag a pane pick can set.
_CAMERA_PROBE_MAX = 6          # AVFoundation logs a warning per miss; two misses in a row end it
_CAMERA_PROBE_ABORT = threading.Event()
_CAMERA_CACHE: dict = {"at": 0.0, "devices": [], "probing": False}
_CAMERA_NAMES: dict = {"at": 0.0, "names": []}

# The most recent CGImage -> PIL conversion failure, cleared by the next success. A grab
# reports it instead of quietly showing a region when the window's pixels existed but
# could not be decoded.
_CONVERT: dict = {"error": ""}

# `layer 0` still carries 1-2 px helpers (shadows, HUD strips); the Windows tier filters
# the same way by requiring a plausible size.
_WINDOW_MIN_EDGE = 40

_SCREENCAPTURE = "/usr/sbin/screencapture"


# ---------------------------------------------------------------------------
# Grab-state reporting (the pane reads both through plugin_api)
# ---------------------------------------------------------------------------

def _method(name: str) -> None:
    """Record how this thread's frame was obtained (the pane displays it)."""
    _GRAB_STATE.method = name


def _waiting(reason: str) -> None:
    """Record why this thread's frame is late or wrong (the pane displays it)."""
    _GRAB_STATE.waiting = reason


# ---------------------------------------------------------------------------
# Availability probes (pure, cheap, no side effects)
# ---------------------------------------------------------------------------

def _quartz():
    """The Quartz module, or None when pyobjc is not installed / cannot load."""
    if not _QUARTZ["checked"]:
        _QUARTZ["checked"] = True
        try:
            import Quartz  # type: ignore[import-not-found]
        except Exception as exc:  # ImportError, or a broken framework link
            _QUARTZ["error"] = (
                f"pyobjc-framework-Quartz is not available ({exc.__class__.__name__}); "
                "window capture needs it (pip install pyobjc-framework-Quartz)"
            )
            _LOG.warning("%s", _QUARTZ["error"])
        else:
            _QUARTZ["module"] = Quartz
    return _QUARTZ["module"]


def _quartz_error() -> str:
    _quartz()
    return _QUARTZ["error"] or "pyobjc-framework-Quartz is unavailable"


def _screencapture_path() -> str:
    """``screencapture``'s absolute path, or "" when the host has no such tool."""
    if os.path.exists(_SCREENCAPTURE):
        return _SCREENCAPTURE
    return shutil.which("screencapture") or ""


def _screen_recording_state() -> dict:
    """``{"state": granted|denied|unknown, "reason": str}`` for Screen Recording.

    ``CGPreflightScreenCaptureAccess`` is the only API that answers this without
    attempting (and mis-reading) a capture; it exists from macOS 10.15, which is
    every macOS this plugin could run on, but the call is guarded anyway.
    """
    cg = _quartz()
    if cg is None:
        return {"state": "unknown", "reason": _quartz_error()}
    preflight = getattr(cg, "CGPreflightScreenCaptureAccess", None)
    if preflight is None:
        return {"state": "unknown",
                "reason": "this macOS cannot report the Screen Recording grant"}
    try:
        granted = bool(preflight())
    except Exception as exc:
        return {"state": "unknown",
                "reason": f"the Screen Recording preflight failed ({exc.__class__.__name__})"}
    if granted:
        return {"state": "granted", "reason": ""}
    return {"state": "denied", "reason": _PERMISSION_REASON}


_PERMISSION_REASON = (
    "macOS Screen Recording permission is required — enable it for this app in "
    "System Settings → Privacy & Security → Screen Recording, then restart the app. "
    "Until then every grab comes back black and window titles are hidden"
)


def _permission_denied() -> bool:
    return _screen_recording_state()["state"] == "denied"


def _request_screen_recording() -> None:
    """Ask for the grant once per process — that is what makes the system dialog appear.

    Called from a capture attempt (never from an enumeration), so a pane poll can
    never pop a prompt at someone who is not using capture.
    """
    if _PERMISSION["asked"]:
        return
    _PERMISSION["asked"] = True
    cg = _quartz()
    request = getattr(cg, "CGRequestScreenCaptureAccess", None) if cg is not None else None
    if request is None:
        return
    try:
        request()
    except Exception:  # pragma: no cover - a failed prompt must not fail the grab
        pass


def _capture_reason() -> str:
    """Why no live capture path exists right now, for the pane's status line."""
    notes = []
    if _quartz() is None:
        notes.append(_quartz_error())
    permission = _screen_recording_state()
    if permission["state"] == "denied":
        notes.append(permission["reason"])
    if not _screencapture_path():
        notes.append("the screencapture tool is missing (/usr/sbin/screencapture)")
    return "; ".join(notes) or "no capture backend is available"


def _capture_state() -> dict:
    """What the capture tiers are doing here — reported through ``/sources``."""
    permission = _screen_recording_state()
    tier = "coregraphics" if _quartz() is not None else ("screencapture" if _screencapture_path() else "none")
    return {
        "platform": "macos",
        "tier": tier,
        "method": getattr(_GRAB_STATE, "method", "") or "",
        "waiting": getattr(_GRAB_STATE, "waiting", "") or "",
        "permission": {"screen_recording": permission["state"], "reason": permission["reason"]},
        "window_listing": _quartz() is not None,
    }


# ---------------------------------------------------------------------------
# Geometry / image plumbing
# ---------------------------------------------------------------------------

def _rect_parts(rect) -> tuple[int, int, int, int]:
    """``(x, y, w, h)`` from a CGRect-like object or a ``{X, Y, Width, Height}`` mapping.

    ``CGWindowListCopyWindowInfo`` hands back the latter as a plain dict, and
    ``CGDisplayBounds`` an NSSize-style struct — both shapes are accepted so the
    parsers stay testable without a display server.
    """
    if isinstance(rect, dict):
        x, y = rect.get("X"), rect.get("Y")
        width, height = rect.get("Width"), rect.get("Height")
    else:
        origin, size = getattr(rect, "origin", None), getattr(rect, "size", None)
        if origin is None or size is None:
            return (0, 0, 0, 0)
        x, y = origin.x, origin.y
        width, height = size.width, size.height
    if None in (x, y, width, height):
        return (0, 0, 0, 0)
    return (int(round(x)), int(round(y)), int(round(width)), int(round(height)))


def _flat_rect(source: dict) -> tuple[int, int, int, int]:
    """(x, y, w, h) from a source dict, tolerating a legacy ``bounds`` tuple."""
    if all(k in source for k in ("x", "y", "width", "height")):
        return (int(source["x"]), int(source["y"]),
                int(source["width"]), int(source["height"]))
    bounds = source.get("bounds")
    if isinstance(bounds, (tuple, list)) and len(bounds) == 4:
        return tuple(int(v) for v in bounds)  # type: ignore[return-value]
    return (0, 0, 0, 0)


def _temp_png_path() -> str:
    """A unique path for one PNG round trip; the caller unlinks it."""
    handle, path = tempfile.mkstemp(prefix="pv-macos-", suffix=".png")
    os.close(handle)
    return path


def _cgimage_to_pil(cg, image) -> Optional[Image.Image]:
    """CGImage -> PIL through a PNG round trip (pyobjc offers no pixel buffer here).

    Returns None when the image is empty: CoreGraphics hands back a 0x0 image for a
    minimized or off-screen window rather than raising, and that None is what tells the
    callers above to report a waiting state instead of a frame.

    A *failed conversion* is recorded in ``_CONVERT`` rather than swallowed, so a caller
    can say why a window that should have pixels produced none — the difference between
    "nothing to copy" and "this host could not decode what it copied".
    """
    if image is None:
        return None
    try:
        width, height = int(cg.CGImageGetWidth(image)), int(cg.CGImageGetHeight(image))
    except Exception as exc:
        _CONVERT["error"] = f"the captured image could not be measured ({exc.__class__.__name__}: {exc})"
        return None
    if width <= 0 or height <= 0:
        _CONVERT["error"] = ""  # an empty surface is a state (minimized), not a failure
        return None

    path = _temp_png_path()
    try:
        url = cg.CFURLCreateWithFileSystemPath(None, path, cg.kCFURLPOSIXPathStyle, False)
        destination = cg.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
        if destination is None:
            raise RuntimeError("CGImageDestinationCreateWithURL returned nothing")
        cg.CGImageDestinationAddImage(destination, image, None)
        if not cg.CGImageDestinationFinalize(destination):
            raise RuntimeError("CGImageDestinationFinalize reported failure")
        converted = Image.open(path).convert("RGB")  # forces the read before cleanup
    except Exception as exc:
        _CONVERT["error"] = ("the captured image could not be converted to a frame "
                             f"({exc.__class__.__name__}: {exc})")
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    _CONVERT["error"] = ""
    return converted


def _screencapture_region(x: int, y: int, width: int, height: int) -> Optional[Image.Image]:
    """One still of a screen region via the CLI tier — no pyobjc, no window ids.

    ``-R`` takes the region in points and the tool writes native pixels (2x on a
    Retina display), so this is the same resolution the CoreGraphics path returns.
    """
    exe = _screencapture_path()
    if not exe or width <= 0 or height <= 0:
        return None
    path = _temp_png_path()
    try:
        proc = subprocess.run(
            [exe, "-x", "-R", f"{int(x)},{int(y)},{int(width)},{int(height)}", path],
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0 or not os.path.exists(path):
            return None
        return Image.open(path).convert("RGB")
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Displays (CoreGraphics)
# ---------------------------------------------------------------------------

def _active_display_ids(cg) -> list[int]:
    """``CGGetActiveDisplayList`` in whatever shape pyobjc returns it.

    pyobjc passes the out-parameters back as a tuple (``err``, ids, count); an older
    binding returns just the list. Read the numbers, ignore the rest.
    """
    try:
        result = cg.CGGetActiveDisplayList(32, None, None)
    except Exception:
        return []
    parts = result if isinstance(result, tuple) else (result,)
    for part in parts:
        if isinstance(part, (list, tuple)) and part and all(isinstance(v, int) for v in part):
            return [int(v) for v in part]
    return []


def _display_bounds(cg, display_id: int) -> tuple[int, int, int, int]:
    try:
        return _rect_parts(cg.CGDisplayBounds(display_id))
    except Exception:
        return (0, 0, 0, 0)


def _display_scale(cg, display_id: int, width: int) -> float:
    """Pixel width ÷ point width: 2.0 on a Retina display, 1.0 otherwise.

    Frames are returned at native pixels, so this is what tells a crop why a 1440 pt
    display hands back 2880 px.
    """
    try:
        pixels = int(cg.CGDisplayPixelsWide(display_id))
    except Exception:
        return 1.0
    if width > 0 and pixels > 0:
        return round(pixels / float(width), 3)
    return 1.0


def _display_image(cg, display_id: int) -> Optional[Image.Image]:
    create = getattr(cg, "CGDisplayCreateImage", None)
    if create is None or not display_id:
        return None
    try:
        image = create(int(display_id))
    except Exception:
        return None
    return _cgimage_to_pil(cg, image)


# ---------------------------------------------------------------------------
# Windows (CoreGraphics)
# ---------------------------------------------------------------------------

def _field(entry: dict, name: str, default=None):
    """One ``CGWindowListCopyWindowInfo`` field, by its pyobjc key or a short one.

    pyobjc keys the dictionaries with the constant names (``kCGWindowNumber``); the
    short form is accepted so the parsers can be driven from plain fixtures.
    """
    for key in (f"kCGWindow{name}", name):
        if key in entry:
            return entry[key]
    return default


def _window_list(cg, on_screen_only: bool) -> list[dict]:
    """The raw window dictionaries, newest z-order first."""
    options = getattr(cg, "kCGWindowListOptionOnScreenOnly", 0) if on_screen_only else 0
    options |= getattr(cg, "kCGWindowListExcludeDesktopElements", 0)
    try:
        rows = cg.CGWindowListCopyWindowInfo(options, getattr(cg, "kCGNullWindowID", 0))
    except Exception:
        return []
    return list(rows or [])


def _window_row(entry: dict, exe_lookup) -> Optional[dict]:
    """One window dict, shaped exactly like capture_windows emits — or None to skip it.

    Skipped: anything not at the normal window layer (the menu bar, the Dock, HUD
    strips) and slivers too small to be a window.
    """
    layer = _field(entry, "Layer")
    if layer not in (0, None):
        return None
    window_id = _field(entry, "Number")
    if not window_id:
        return None
    x, y, width, height = _rect_parts(_field(entry, "Bounds"))
    if width < _WINDOW_MIN_EDGE or height < _WINDOW_MIN_EDGE:
        return None

    title = str(_field(entry, "Name") or "").strip()[:120]
    owner = str(_field(entry, "OwnerName") or "").strip()
    pid = int(_field(entry, "OwnerPID") or 0)
    exe = exe_lookup(pid) if pid else ""
    app = exe or owner
    if title:
        label = f"{app or 'window'} — {title}"
    else:
        label = app or f"Window {int(window_id)}"
    return {
        "id": f"window-{int(window_id)}",
        "kind": "window",
        "hwnd": int(window_id),
        "title": title,
        "class": owner,
        "exe": exe or owner,
        "pid": pid,
        "x": x, "y": y, "width": width, "height": height,
        # The list route is the on-screen list, so a row is live by construction; the
        # facade asks is_minimized() for the live answer instead of trusting a snapshot.
        "minimized": False,
        "foreground": False,
        "label": label,
        "badge": "background",
    }


def _window_info(cg, window_id: int) -> Optional[dict]:
    """The all-windows entry for one id — the only way to see a window that is not on screen."""
    for entry in _window_list(cg, on_screen_only=False):
        if int(_field(entry, "Number") or 0) == int(window_id):
            return entry
    return None


def _window_image(cg, window_id: int) -> Optional[Image.Image]:
    """That window's *own* pixels, occluded or not (the PrintWindow peer).

    Empty for a minimized window: nothing composites it, so CoreGraphics has no
    surface to copy.
    """
    create = getattr(cg, "CGWindowListCreateImage", None)
    if create is None or not window_id:
        return None
    try:
        image = create(
            getattr(cg, "CGRectNull", None),
            getattr(cg, "kCGWindowListOptionIncludingWindow", 0),
            int(window_id),
            getattr(cg, "kCGWindowImageBoundsIgnoreFraming", 0)
            | getattr(cg, "kCGWindowImageBestResolution", 0),
        )
    except Exception:
        return None
    return _cgimage_to_pil(cg, image)


def _window_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    """(x1, y1, x2, y2) of a window from its live CoreGraphics bounds, or None.

    CoreGraphics keeps reporting the bounds of a minimized window, so the restore
    upgrade path gets the geometry it will need *before* the window is back on screen.
    """
    cg = _quartz()
    if cg is None or not hwnd:
        return None
    entry = _window_info(cg, int(hwnd))
    if entry is None:
        return None
    x, y, width, height = _rect_parts(_field(entry, "Bounds"))
    if width <= 0 or height <= 0:
        return None
    return (x, y, x + width, y + height)


def _window_iconic(hwnd: int) -> bool:
    """True when that window is minimized (or otherwise not on screen).

    A minimized window is absent from the on-screen list and reports
    ``kCGWindowIsOnscreen`` false in the all-windows list. There is no live frame to
    grab either way, which is exactly what the facade's minimized path is for.
    """
    cg = _quartz()
    if cg is None or not hwnd:
        return False
    entry = _window_info(cg, int(hwnd))
    if entry is None:
        return False
    return not bool(_field(entry, "IsOnscreen", True))


def _window_rested(hwnd: int) -> bool:
    """True once a window's bounds stop moving (a restore animation has settled).

    Restoring from the Dock animates: two reads a beat apart tell a settling window
    from a settled one, so a crop opened on a minimized window upgrades to a frame of
    the real window instead of a mid-animation band.
    """
    first = _window_rect(int(hwnd))
    if not first:
        return True
    time.sleep(0.12)
    return _window_rect(int(hwnd)) == first


def _pid_name(pid: int) -> str:
    """The executable name behind a window's pid — ``ps``, because macOS has no /proc.

    Cached for 30 s: a window list would otherwise fork a process per row.
    """
    if not pid:
        return ""
    now = time.time()
    with _PID_NAMES_LOCK:
        cached = _PID_NAMES.get(int(pid))
        if cached and now - cached[0] < _PID_NAMES_TTL_S:
            return cached[1]
    name = ""
    try:
        proc = subprocess.run(["ps", "-p", str(int(pid)), "-o", "comm="],
                              capture_output=True, text=True, timeout=3)
        if proc.returncode == 0:
            name = os.path.basename((proc.stdout or "").strip())
    except (OSError, subprocess.SubprocessError):
        name = ""
    with _PID_NAMES_LOCK:
        _PID_NAMES[int(pid)] = (now, name)
    return name


def _window_exe(pid: int) -> str:
    """Executable name of a window's process (the ``exe`` column the pane labels rows with)."""
    return _pid_name(pid)


def _restored_rect(hwnd: int):
    # CoreGraphics reports a minimized window's bounds, and that is where it will be
    # placed again — same answer the X11 backend gives.
    return _window_rect(hwnd)


# ---------------------------------------------------------------------------
# Cameras (AVFoundation via OpenCV; names via system_profiler)
# ---------------------------------------------------------------------------

def _camera_backend() -> int:
    return int(getattr(cv2, "CAP_AVFOUNDATION", 0))


def _camera_names() -> list[str]:
    """Camera names from ``system_profiler``, in the order AVFoundation enumerates them.

    Best-effort and cached: the profiler costs ~1 s and the names are cosmetic (the
    index is the identity).
    """
    if _CAMERA_NAMES["names"] and time.time() - _CAMERA_NAMES["at"] < 60:
        return list(_CAMERA_NAMES["names"])
    names: list[str] = []
    try:
        proc = subprocess.run(
            ["system_profiler", "SPCameraDataType", "-json"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            payload = json.loads(proc.stdout or "{}")
            for row in payload.get("SPCameraDataType") or []:
                name = str(row.get("_name") or "").strip()
                if name:
                    names.append(name)
    except (OSError, subprocess.SubprocessError, ValueError):
        names = []
    _CAMERA_NAMES["at"] = time.time()
    _CAMERA_NAMES["names"] = names
    return list(names)


def _camera_row(index: int, width: int, height: int, names: list[str], default: bool) -> dict:
    """One camera dict, shaped exactly like capture_windows emits."""
    name = names[index] if 0 <= index < len(names) else f"Camera {index}"
    return {
        "id": f"camera-{index}",
        "kind": "camera",
        "index": index,
        "label": name,
        "name": name,
        "width": width,
        "height": height,
        "default": bool(default),
        "readable": True,
        "badge": "default" if default else "camera",
    }


def _camera_source(index: int) -> dict:
    """Build a camera source dict for the given index, from the cache when it has it."""
    for device in _CAMERA_CACHE.get("devices") or []:
        if int(device.get("index", -1)) == int(index):
            return dict(device)
    return {
        "id": f"camera-{index}",
        "kind": "camera",
        "index": int(index),
        "label": f"Camera {index}",
        "name": f"Camera {index}",
        "width": 0,
        "height": 0,
        "default": index == 0,
        "readable": True,
        "badge": "default" if index == 0 else "camera",
    }


def probe_cameras_now() -> list[dict]:
    """Force a camera re-probe and return the device list."""
    return _get_mac_capture().list_cameras(force=True)


def cameras_probing() -> bool:
    """Whether a camera probe is in flight."""
    return bool(_CAMERA_CACHE.get("probing"))


def abort_camera_probe() -> None:
    """Signal an in-flight probe to stop — a pane pick must not wait for a slow probe."""
    _CAMERA_PROBE_ABORT.set()


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class MacCapture:
    """macOS implementation of the capture API."""

    def __init__(self):
        self._camera_probe_lock = threading.Lock()

    # -- enumeration ------------------------------------------------------

    def list_monitors(self) -> list[dict]:
        """List all displays, in CoreGraphics order (primary first)."""
        cg = _quartz()
        monitors: list[dict] = []
        if cg is not None:
            for index, display_id in enumerate(_active_display_ids(cg)):
                x, y, width, height = _display_bounds(cg, display_id)
                if width <= 0 or height <= 0:
                    continue
                try:
                    primary = bool(cg.CGDisplayIsMain(display_id))
                except Exception:
                    primary = index == 0
                monitors.append(self._monitor(index, display_id, x, y, width, height,
                                              primary, _display_scale(cg, display_id, width)))
        if monitors:
            return monitors

        # CLI tier: no window listing and no per-display pixel size are available here,
        # so the whole desktop is offered as a single row — never an empty picker.
        width, height = _desktop_size()
        return [self._monitor(0, 0, 0, 0, width, height, True, 1.0)]

    @staticmethod
    def _monitor(index: int, display_id: int, x: int, y: int, width: int, height: int,
                 primary: bool, scale: float) -> dict:
        """One monitor dict, shaped exactly like capture_windows emits."""
        return {
            "id": f"monitor-{index}",
            "index": index,
            "device": f"Display {index + 1}",
            "label": f"Display {index + 1}",
            "x": x, "y": y, "width": width, "height": height,
            "primary": bool(primary),
            "primary_label": "primary" if primary else "",
            "kind": "monitor",
            "display_id": int(display_id),
            "scale": scale,
        }

    def list_windows(self, limit: int = 150) -> list[dict]:
        """List the on-screen windows, front to back, capped at ``limit``.

        An empty list on a machine with windows means pyobjc is missing — the reason
        reaches the pane through the ``capture`` block of ``/sources``.
        """
        cg = _quartz()
        if cg is None:
            return []
        windows: list[dict] = []
        for entry in _window_list(cg, on_screen_only=True):
            row = _window_row(entry, _pid_name)
            if row is None:
                continue
            windows.append(row)
            if len(windows) >= int(limit):
                break
        return windows

    def list_cameras(self, force: bool = False) -> list[dict]:
        """List all cameras (cached for 10 s; AVFoundation index order is the identity)."""
        if not force and _CAMERA_CACHE["devices"] and (time.time() - _CAMERA_CACHE["at"]) < 10:
            return list(_CAMERA_CACHE["devices"])

        backend = _camera_backend()
        with self._camera_probe_lock:
            _CAMERA_CACHE["probing"] = True
            devices: list[dict] = []
            names = _camera_names()
            misses = 0
            try:
                for index in range(_CAMERA_PROBE_MAX):
                    if _CAMERA_PROBE_ABORT.is_set():
                        _CAMERA_PROBE_ABORT.clear()
                        break
                    cap = cv2.VideoCapture(index, backend)
                    if not cap.isOpened():
                        cap.release()
                        misses += 1
                        if misses >= 2:  # AVFoundation logs per miss; stop at the gap
                            break
                        continue
                    misses = 0
                    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    cap.release()
                    devices.append(_camera_row(index, width, height, names, index == 0))
            finally:
                _CAMERA_CACHE["probing"] = False

            _CAMERA_CACHE["devices"] = devices
            _CAMERA_CACHE["at"] = time.time()
            return list(devices)

    def list_sources(self) -> dict:
        """List all sources (monitors, windows, cameras)."""
        monitors = self.list_monitors()
        windows = self.list_windows()
        cameras = self.list_cameras()
        return {
            "monitors": monitors,
            "windows": windows,
            "cameras": cameras,
            "count": len(monitors) + len(windows) + len(cameras),
            "capture": _capture_state(),
        }

    # -- grabs ------------------------------------------------------------

    def grab(self, source: dict):
        """Capture a frame from the given source. Returns a PIL Image."""
        _method("")
        _waiting("")
        kind = source.get("kind")
        if kind == "monitor":
            return self._grab_monitor(source)
        if kind == "window":
            return self._grab_window(source)
        if kind == "camera":
            return self._grab_camera(source)
        raise ValueError(f"Unknown source kind: {kind}")

    def _grab_monitor(self, source: dict):
        """Capture a display at native pixels. Returns a PIL Image."""
        x, y, width, height = _flat_rect(source)
        if not width or not height:
            width, height = _desktop_size()

        cg = _quartz()
        image = _display_image(cg, source.get("display_id")) if cg is not None else None
        if image is not None:
            _method("coregraphics")
            return image
        conversion = _CONVERT["error"]

        image = _screencapture_region(x, y, width, height)
        if image is not None:
            _method("screencapture")
            if conversion:
                _waiting(conversion)
            elif _permission_denied():
                # The CLI reports success for a black frame, so the permission state is
                # the only honest way to say why the pixels are flat.
                _waiting(_PERMISSION_REASON)
            return image

        self._report_empty_grab(conversion)
        return Image.new("RGB", (max(1, width), max(1, height)), (0, 0, 0))

    def _grab_window(self, source: dict):
        """Capture a window's own pixels. Returns a PIL Image.

        CoreGraphics copies the window's surface, so an occluded window comes back
        whole. A minimized window has no surface, which is the one case the facade's
        last-frame fallback exists for.
        """
        hwnd = int(source.get("hwnd") or 0)
        cg = _quartz()

        if cg is not None and hwnd:
            image = _window_image(cg, hwnd)
            if image is not None:
                _method("coregraphics")
                return image
        conversion = _CONVERT["error"]

        # No CoreGraphics (no pyobjc) or no pixels: a region grab of the window's rect
        # is the honest fallback — it can include whatever covers the window, exactly
        # the tradeoff the Wayland tier documents.
        x, y, width, height = _flat_rect(source)
        if width and height:
            image = _screencapture_region(x, y, width, height)
            if image is not None:
                _method("screencapture")
                _waiting(conversion or "shown as a screen region — another window may cover it")
                return image

        if conversion:
            _waiting(conversion)
        elif _permission_denied():
            _waiting(_PERMISSION_REASON)
        else:
            self._report_empty_grab()
        return Image.new("RGB", (max(1, width), max(1, height)), (0, 0, 0))

    def _report_empty_grab(self, conversion: str = "") -> None:
        """Ask for the grant the first time a grab comes back with nothing.

        The request is what makes the system dialog appear; it is issued from a capture
        attempt only, so enumerating sources can never prompt anyone.
        """
        _request_screen_recording()
        _waiting(conversion or _capture_reason())

    def _grab_camera(self, source: dict):
        """Capture from a camera (held open between frames, like the other backends)."""
        index = int(source.get("index", 0))
        with _CAM_LOCK:
            cap = _CAM_HANDLE.get("cap")
            if cap is None or _CAM_HANDLE.get("index") != index:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
                cap = cv2.VideoCapture(index, _camera_backend())
                if not cap.isOpened():
                    cap.release()
                    raise RuntimeError(f"Cannot open camera {index}")
                _CAM_HANDLE["cap"] = cap
                _CAM_HANDLE["index"] = index

            frame = None
            for _ in range(3):  # AVFoundation's first reads are dark: take a warm one
                try:
                    ok, candidate = cap.read()
                except Exception:
                    ok, candidate = False, None
                if ok and candidate is not None:
                    frame = candidate

        if frame is None:
            raise RuntimeError(f"Camera {index} returned empty frame")

        if frame.ndim == 3 and frame.shape[2] == 3:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return Image.fromarray(frame)

    def grab_window_thumbnail(self, hwnd: int):
        """Best-effort frame for a window the facade asked to preview.

        There is no thumbnail API to call: a minimized window has no surface, so this
        either copies the window's pixels (when it is on screen) or hands back the
        black placeholder ``_grab_window`` builds. The facade's minimized path serves
        that source's last good frame instead of asking here.
        """
        return self._grab_window({"kind": "window", "hwnd": int(hwnd)})

    # -- state ------------------------------------------------------------

    def is_minimized(self, source: dict) -> bool:
        """True when the source window is minimized (not on screen, so no live frame)."""
        if source.get("kind") != "window":
            return False
        return _window_iconic(int(source.get("hwnd") or 0))

    def get_window_rect(self, source: dict) -> tuple:
        """(x, y, width, height) of a window source — resolved live from CoreGraphics."""
        if source.get("kind") != "window":
            return (0, 0, 0, 0)
        hwnd = source.get("hwnd")
        if hwnd:
            rect = _window_rect(int(hwnd))
            if rect:
                return (rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1])
        x, y, width, height = _flat_rect(source)
        if width and height:
            return (x, y, width, height)
        return (0, 0, 0, 0)

    def supports_snap_full_res(self) -> bool:
        """Whether full-resolution snaps are supported (native pixels on every grab)."""
        return True

    def cleanup(self) -> None:
        """Release any held resources."""
        release_camera()
        _CAMERA_CACHE["devices"] = []
        _CAMERA_CACHE["at"] = 0.0
        _CAMERA_CACHE["probing"] = False


def _desktop_size() -> tuple[int, int]:
    """(width, height) of the whole desktop — the last-resort monitor row.

    CoreGraphics gives it directly; without pyobjc the union of the displays is
    unavailable, so a plausible default keeps the picker usable rather than empty.
    """
    cg = _quartz()
    if cg is not None:
        try:
            rect = cg.CGDisplayBounds(cg.CGMainDisplayID())
            x, y, width, height = _rect_parts(rect)
            if width > 0 and height > 0:
                return (width, height)
        except Exception:
            pass
    return (1920, 1080)


# ---------------------------------------------------------------------------
# Module-level facade (what capture.py re-exports)
# ---------------------------------------------------------------------------

_mac_capture: Optional[MacCapture] = None


def _get_mac_capture() -> MacCapture:
    global _mac_capture
    if _mac_capture is None:
        _mac_capture = MacCapture()
    return _mac_capture


def list_monitors() -> list[dict]:
    return _get_mac_capture().list_monitors()


def list_windows(limit: int = 150) -> list[dict]:
    return _get_mac_capture().list_windows(limit)


def list_cameras(force: bool = False) -> list[dict]:
    return _get_mac_capture().list_cameras(force)


def list_sources() -> dict:
    return _get_mac_capture().list_sources()


def grab(source: dict):
    return _get_mac_capture().grab(source)


def grab_window_thumbnail(hwnd: int):
    return _get_mac_capture().grab_window_thumbnail(hwnd)


def is_minimized(source: dict) -> bool:
    return _get_mac_capture().is_minimized(source)


def get_window_rect(source: dict) -> tuple:
    return _get_mac_capture().get_window_rect(source)


def supports_snap_full_res() -> bool:
    return _get_mac_capture().supports_snap_full_res()


def release_camera() -> None:
    """Close the camera device so its LED turns off when watching stops."""
    with _CAM_LOCK:
        cap = _CAM_HANDLE.get("cap")
        _CAM_HANDLE["cap"] = None
        _CAM_HANDLE["index"] = None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


def cleanup() -> None:
    global _mac_capture
    release_camera()
    if _mac_capture:
        _mac_capture.cleanup()
        _mac_capture = None
