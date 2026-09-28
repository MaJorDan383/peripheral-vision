"""Cross-platform screen/window/camera capture for Peripheral Vision.

Dispatches to the OS-specific backend selected once at import time:

    win32  → ``capture_windows``  (Win32/GDI + DWM thumbnails + OpenCV)
    linux  → ``capture_linux``    (xrandr/wmctrl/import/grim/V4L2)

Public API (used by ``plugin_api.py``):

    list_monitors()          -> list[dict]
    list_windows(limit)      -> list[dict]
    list_cameras(force)      -> list[dict]
    list_sources()           -> dict with monitors/windows/cameras
    grab(source)             -> PIL Image
    grab_window_thumbnail(hwnd) -> PIL Image
    is_minimized(source)     -> bool
    get_window_rect(source)  -> (x, y, w, h)
    supports_snap_full_res() -> bool
    release_camera()         -> None
    cleanup()                -> None

    # Camera probing (Windows; no-ops on Linux)
    probe_cameras_now()      -> list[dict]
    cameras_probing()        -> bool
    abort_camera_probe()     -> None

    # Source resolution helpers
    camera_source(index)     -> dict | None
    camera_cache()           -> dict (with "devices" key)

    # Window state (for snap upgrade logic)
    window_iconic(hwnd)      -> bool
    window_rested(hwnd)      -> bool

Exceptions:
    CaptureError, SourceMinimized, CaptureSourceGone
"""

from __future__ import annotations

import sys


class CaptureError(RuntimeError):
    """Raised when a capture source cannot be grabbed."""


class SourceMinimized(CaptureError):
    """The window is minimized and no frame can be produced for it."""


class CaptureSourceGone(CaptureError):
    """The source no longer exists (window closed, monitor unplugged)."""


def _backend():
    if sys.platform == "win32":
        import capture_windows as _be
    elif sys.platform.startswith("linux"):
        import capture_linux as _be
    else:  # macOS falls back to Linux (X11) paths
        import capture_linux as _be
    return _be


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------

def list_monitors() -> list[dict]:
    """Enumerate connected displays."""
    return _backend().list_monitors()


def list_windows(limit: int = 150) -> list[dict]:
    """Enumerate top-level windows with titles and geometry."""
    return _backend().list_windows(limit)


def list_cameras(force: bool = False) -> list[dict]:
    """Enumerate attached cameras (cached)."""
    return _backend().list_cameras(force)


def list_sources() -> dict:
    """Everything the user can watch: displays, windows, cameras."""
    return _backend().list_sources()


# ---------------------------------------------------------------------------
# Grab (returns PIL Image)
# ---------------------------------------------------------------------------

def grab(source: dict):
    """PIL image of the chosen source (window, monitor, or camera)."""
    return _backend().grab(source)


def grab_window_thumbnail(hwnd: int):
    """PIL image from a DWM thumbnail (minimized window last frame)."""
    be = _backend()
    if hasattr(be, "grab_window_thumbnail"):
        return be.grab_window_thumbnail(hwnd)
    raise CaptureError("thumbnail capture not supported on this backend")




def camera_open() -> bool:
    """True when a camera device is currently held open by this process."""
    be = _backend()
    handle = getattr(be, "_CAM_HANDLE", None)
    if isinstance(handle, dict):
        return handle.get("cap") is not None
    return False


def window_rect(hwnd: int):
    """(x1, y1, x2, y2) of a window, or None when it has no geometry."""
    be = _backend()
    fn = getattr(be, "_window_rect", None)
    if fn is not None:
        return fn(hwnd)
    return None

# ---------------------------------------------------------------------------
# State queries
# ---------------------------------------------------------------------------

def is_minimized(source: dict) -> bool:
    """True when the source window is minimized."""
    return _backend().is_minimized(source)


def get_window_rect(source: dict) -> tuple:
    """(x, y, width, height) of the source window."""
    return _backend().get_window_rect(source)


def supports_snap_full_res() -> bool:
    """Whether this backend can produce full-resolution stills."""
    be = _backend()
    return be.supports_snap_full_res() if hasattr(be, "supports_snap_full_res") else False


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def release_camera() -> None:
    """Close the camera device so its LED turns off when watching stops."""
    be = _backend()
    if hasattr(be, "release_camera"):
        be.release_camera()


def cleanup() -> None:
    """Release any process-wide capture resources."""
    be = _backend()
    if hasattr(be, "cleanup"):
        be.cleanup()


# ---------------------------------------------------------------------------
# Camera probing (Windows; no-ops on Linux)
# ---------------------------------------------------------------------------

def probe_cameras_now() -> list[dict]:
    """Force a camera re-probe and return the device list."""
    be = _backend()
    if hasattr(be, "probe_cameras_now"):
        return be.probe_cameras_now()
    return list_cameras(force=True)


def cameras_probing() -> bool:
    """Whether a camera probe is in flight."""
    be = _backend()
    if hasattr(be, "cameras_probing"):
        return be.cameras_probing()
    return False


def abort_camera_probe() -> None:
    """Signal an in-flight camera probe to stop."""
    be = _backend()
    if hasattr(be, "abort_camera_probe"):
        be.abort_camera_probe()


# ---------------------------------------------------------------------------
# Source resolution helpers (for plugin_api._source_from_id)
# ---------------------------------------------------------------------------

def camera_source(index: int):
    """Build a camera source dict for the given index."""
    be = _backend()
    if hasattr(be, "_camera_source"):
        return be._camera_source(index)
    return {"kind": "camera", "index": index, "id": f"camera-{index}"}


def camera_cache() -> dict:
    """The backend's camera cache (with 'devices' key)."""
    be = _backend()
    if hasattr(be, "_CAMERA_CACHE"):
        return be._CAMERA_CACHE
    return {"at": 0.0, "devices": [], "probing": False, "thread": None}


# ---------------------------------------------------------------------------
# Window state (for snap upgrade logic)
# ---------------------------------------------------------------------------

def window_iconic(hwnd: int) -> bool:
    """True if the window is minimized (iconic)."""
    be = _backend()
    if hasattr(be, "_window_iconic"):
        return be._window_iconic(hwnd)
    return False


def window_rested(hwnd: int) -> bool:
    """True if the window is in a normal (restored) state."""
    be = _backend()
    if hasattr(be, "_window_rested"):
        return be._window_rested(hwnd)
    return True


def window_exe(pid: int) -> str:
    """Executable name of a window's process (Windows only)."""
    be = _backend()
    if hasattr(be, "_window_exe"):
        return be._window_exe(pid)
    return ""
