"""
Windows capture backend — self-contained.

Implements the cross-platform capture API using:
- Win32/GDI for monitor/window enumeration and screen capture
- DWM thumbnail API for minimized windows
- OpenCV/DSHOW for camera capture

No imports from plugin_api.py — this module is fully standalone.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import numpy as np

# ── Paths / constants (independent of plugin_api) ──────────────────────────
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
STATE_DIR = HERMES_HOME / "cache" / "peripheral-vision"
CAMERA_CACHE_PATH = STATE_DIR / "cameras.json"
CAMERA_MODES_PATH = STATE_DIR / "camera_modes.json"

# Timeout constants
THUMB_CAPTURE_TIMEOUT_S = 45
SNAP_GRAB_TIMEOUT_S = 25


def _write_json(path: Path, data: dict) -> None:
    """Atomic JSON write (same semantics as plugin_api's version)."""
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        pass


# ── Win32 structures ────────────────────────────────────────────────────────

class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_ulong),
        ("szDevice", ctypes.c_wchar * 32),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("showCmd", ctypes.c_uint),
        ("ptMinPosition", _POINT),
        ("ptMaxPosition", _POINT),
        ("rcNormalPosition", _RECT),
    ]


class _DWM_THUMBNAIL_PROPERTIES(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint),
        ("fMask", ctypes.c_uint),
        ("fOptions", ctypes.c_uint),
        ("rctDestination", _RECT),
        ("rctSource", _RECT),
        ("clrOpacity", ctypes.c_double),
        ("fVisible", ctypes.c_int),
    ]


class _WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_long),
        ("cbWndExtra", ctypes.c_long),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_long),
        ("time", ctypes.c_uint),
        ("ptX", ctypes.c_long),
        ("ptY", ctypes.c_long),
    ]


# ── Global state ────────────────────────────────────────────────────────────

from shared_state import (
    THUMB_HOST, THUMB_PROC, THUMB_LOCK, THUMB_EXECUTOR, THUMB_MAX_EDGE,
    CAM_LOCK, CAM_HANDLE, CAM_PROBE_ABORT, CAMERA_CACHE,
    GRAB_STATE, LAST_FRAMES, LAST_FRAME_LOCK,
)

_THUMB_HOST = THUMB_HOST
_THUMB_PROC = THUMB_PROC
_THUMB_LOCK = THUMB_LOCK
_THUMB_EXECUTOR = THUMB_EXECUTOR
_THUMB_MAX_EDGE = THUMB_MAX_EDGE

_CAM_LOCK = CAM_LOCK
_CAM_HANDLE = CAM_HANDLE
_CAM_PROBE_ABORT = CAM_PROBE_ABORT
_CAMERA_CACHE = CAMERA_CACHE

_GRAB_STATE = GRAB_STATE
_LAST_FRAMES = LAST_FRAMES
_LAST_FRAME_LOCK = LAST_FRAME_LOCK


# ── Camera mode negotiation ─────────────────────────────────────────────────

CAMERA_MAX_INDEX = int(os.environ.get("PV_CAMERA_MAX_INDEX") or 4)
CAMERA_CACHE_S = int(os.environ.get("PV_CAMERA_CACHE_S") or 600)
CAMERA_PROBE_TIMEOUT_S = float(os.environ.get("PV_CAMERA_PROBE_TIMEOUT_S") or 5.0)
CAMERA_PROBE_WAVE = int(os.environ.get("PV_CAMERA_PROBE_WAVE") or 2)

CAMERA_MODES: tuple[tuple[int, int], ...] = ((3840, 2160), (1920, 1080), (1280, 720))
_CAMERA_MODES: dict[int, tuple[int, int]] = {}
_CAMERA_MODES_LOADED = False
_MODES_LOCK = threading.Lock()

_CAMERA_NAMES: Optional[list[str]] = None
_NAMES_LOCK = threading.Lock()


def _camera_mode(index: int) -> Optional[tuple[int, int]]:
    """The mode this camera settled on last time, if it is known."""
    global _CAMERA_MODES_LOADED
    with _MODES_LOCK:
        if not _CAMERA_MODES_LOADED:
            try:
                stored = json.loads(CAMERA_MODES_PATH.read_text(encoding="utf-8")) or {}
                for key, value in (stored.get("modes") or {}).items():
                    width, height = (int(v) for v in value)
                    if width and height:
                        _CAMERA_MODES[int(key)] = (width, height)
            except Exception:
                pass
            _CAMERA_MODES_LOADED = True
        return _CAMERA_MODES.get(index)


def _remember_camera_mode(index: int, size: tuple[int, int]) -> None:
    """Remember what this camera settled on so the next open gets there in one step."""
    with _MODES_LOCK:
        if _CAMERA_MODES.get(index) == size:
            return
        _CAMERA_MODES[index] = size
        _write_json(
            CAMERA_MODES_PATH,
            {"v": 1, "modes": {str(i): [w, h] for i, (w, h) in sorted(_CAMERA_MODES.items())}},
        )
        changed = False
        for device in _CAMERA_CACHE.get("devices") or []:
            if int(device.get("index", -1)) == index and (device.get("width"), device.get("height")) != size:
                device["width"], device["height"] = size
                changed = True
        if changed:
            _write_json(
                CAMERA_CACHE_PATH,
                {"v": 1, "at": _CAMERA_CACHE.get("at", 0.0), "devices": _CAMERA_CACHE["devices"]},
            )


def _apply_camera_mode(cap, width: int, height: int) -> tuple[int, int]:
    """Ask a device for one mode and report the size it actually delivered."""
    try:
        import cv2
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    except Exception:
        pass
    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
    except Exception:
        return (0, 0)
    for attempt in range(3):
        try:
            ok, frame = cap.read()
        except Exception:
            ok, frame = False, None
        if ok and frame is not None:
            return (int(frame.shape[1]), int(frame.shape[0]))
        if attempt < 2:
            time.sleep(0.15)
    return (0, 0)


def _configure_camera_capture(cap, index: int) -> tuple[int, int]:
    """Ask for the biggest mode this camera honours and return the delivered size."""
    known = _camera_mode(index)
    if known:
        got_w, got_h = _apply_camera_mode(cap, *known)
        if got_w and got_h:
            if (got_w, got_h) != known:
                _remember_camera_mode(index, (got_w, got_h))
            return (got_w, got_h)
    best = (0, 0)
    for width, height in CAMERA_MODES:
        got_w, got_h = _apply_camera_mode(cap, width, height)
        if not got_w or not got_h:
            continue
        best = (got_w, got_h)
        _remember_camera_mode(index, best)
        if got_w >= width and got_h >= height:
            break
        if max(got_w, got_h) >= 1280:
            break
    return best


# Load last run's cameras from disk
try:
    _cached_cameras = json.loads(CAMERA_CACHE_PATH.read_text(encoding="utf-8"))
    if isinstance(_cached_cameras.get("devices"), list):
        _CAMERA_CACHE["devices"] = _cached_cameras["devices"]
        _CAMERA_CACHE["at"] = float(_cached_cameras.get("at") or 0.0)
except Exception:
    pass


def _camera_device_names() -> list[str]:
    """DirectShow camera names, in enumeration order (matches cv2's index order)."""
    global _CAMERA_NAMES
    if _CAMERA_NAMES is not None:
        return _CAMERA_NAMES
    with _NAMES_LOCK:
        if _CAMERA_NAMES is not None:
            return _CAMERA_NAMES
        names: list[str] = []
        try:
            import shutil
            import subprocess
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg:
                proc = subprocess.run(
                    [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8,
                )
                text = (proc.stderr or "") + (proc.stdout or "")
                names = re.findall(r'"([^"]+)"\s*\(video\)', text)
        except Exception:
            names = []
        _CAMERA_NAMES = names
        return names


def _probe_one_camera(
    index: int, names: list[str], out: dict[int, dict[str, Any]], lock: Any, deadline: float = 0.0
) -> None:
    """Open one camera, read a frame, close it. Runs on its own thread."""
    with _CAM_LOCK:
        if _CAM_HANDLE.get("cap") is not None and _CAM_HANDLE.get("index") == index:
            return
    if deadline and time.time() > deadline:
        return
    if _CAM_PROBE_ABORT.is_set():
        return
    cap = None
    try:
        import cv2
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        ok, frame = False, None
        for attempt in range(3):
            if _CAM_PROBE_ABORT.is_set():
                return
            try:
                ok, frame = cap.read()
            except Exception:
                ok, frame = False, None
            if ok and frame is not None:
                break
            if attempt < 2:
                time.sleep(0.12)
        if not ok or frame is None:
            return
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 0
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 0
        if not width and frame is not None:
            height, width = frame.shape[:2]
        name = names[index] if index < len(names) else ""
        entry = {
            "id": f"camera-{index}",
            "kind": "camera",
            "index": index,
            "label": name or f"Camera {index}",
            "name": name,
            "width": width,
            "height": height,
            "default": index == 0,
            "readable": bool(ok),
            "badge": "default" if index == 0 else "camera",
        }
        with lock:
            out[index] = entry
    except Exception:
        return
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


def _probe_cameras(deadline_s: float) -> None:
    """Probe camera indices in small waves, each wave bounded by ``deadline_s``."""
    try:
        _CAM_PROBE_ABORT.clear()
        names = _camera_device_names()
        out: dict[int, dict[str, Any]] = {}
        lock = threading.Lock()
        indices = list(range(CAMERA_MAX_INDEX))
        for start in range(0, len(indices), CAMERA_PROBE_WAVE):
            if _CAM_PROBE_ABORT.is_set():
                break
            wave = indices[start : start + CAMERA_PROBE_WAVE]
            deadline = time.time() + deadline_s
            workers = [
                threading.Thread(target=_probe_one_camera, args=(i, names, out, lock, deadline), daemon=True)
                for i in wave
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=max(0.05, deadline - time.time()))
        devices = [out[i] for i in sorted(out)]
        if devices:
            _CAMERA_CACHE["devices"] = devices
            _CAMERA_CACHE["at"] = time.time()
            _write_json(CAMERA_CACHE_PATH, {"v": 1, "at": _CAMERA_CACHE["at"], "devices": devices})
    finally:
        _CAMERA_CACHE["probing"] = False


def _wait_for_probe(timeout_s: float) -> None:
    """Wait for the in-flight probe to finish, but never longer than ``timeout_s``."""
    thread = _CAMERA_CACHE.get("thread")
    if thread is None or not thread.is_alive() or thread is threading.current_thread():
        return
    thread.join(timeout=max(0.05, timeout_s))


def _start_camera_probe(deadline_s: float) -> None:
    if _CAMERA_CACHE.get("probing"):
        return
    _CAMERA_CACHE["probing"] = True
    thread = threading.Thread(target=_probe_cameras, args=(deadline_s,), daemon=True)
    _CAMERA_CACHE["thread"] = thread
    thread.start()


# ── Public enumeration API ──────────────────────────────────────────────────

def list_monitors() -> list[dict[str, Any]]:
    """Enumerate displays in the same coordinate space screen grabs use."""
    monitors: list[dict[str, Any]] = []
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()
    except Exception:
        pass

    try:
        enum_proc = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.POINTER(_RECT), ctypes.c_double,
        )

        def _cb(hmon: int, _hdc: int, _rect, _data) -> int:
            info = _MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
            if ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                monitors.append(
                    {
                        "id": f"monitor-{len(monitors)}",
                        "index": len(monitors),
                        "device": info.szDevice,
                        "label": f"Display {len(monitors) + 1}",
                        "x": int(info.rcMonitor.left),
                        "y": int(info.rcMonitor.top),
                        "width": int(info.rcMonitor.right - info.rcMonitor.left),
                        "height": int(info.rcMonitor.bottom - info.rcMonitor.top),
                        "primary": bool(info.dwFlags & 1),
                    }
                )
            return 1

        ctypes.windll.user32.EnumDisplayMonitors(None, None, enum_proc(_cb), 0)
    except Exception:
        monitors = []

    if not monitors:
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            monitors = [
                {
                    "id": "monitor-0", "index": 0, "device": "VIRTUAL", "label": "Display 1",
                    "x": 0, "y": 0, "width": img.width, "height": img.height, "primary": True,
                }
            ]
        except Exception:
            monitors = []

    for m in monitors:
        m["primary_label"] = "primary" if m.get("primary") else ""
        m["kind"] = "monitor"
    return monitors


def _window_exe(pid: int) -> str:
    """Executable name of a window's process, for a readable label."""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return ""
        try:
            size = ctypes.c_ulong(4096)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""
    return ""


def _window_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    """DWM frame bounds when available (the visually real frame), else the window rect."""
    try:
        rect = _RECT()
        ok = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd), ctypes.c_uint(9), ctypes.byref(rect), ctypes.sizeof(rect)
        )
        if ok != 0:
            ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except Exception:
        return None


def _restored_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    """The rectangle a minimized window restores to."""
    try:
        placement = _WINDOWPLACEMENT()
        placement.length = ctypes.sizeof(_WINDOWPLACEMENT)
        if not ctypes.windll.user32.GetWindowPlacement(ctypes.c_void_p(hwnd), ctypes.byref(placement)):
            return None
        rect = placement.rcNormalPosition
        left, top = int(rect.left), int(rect.top)
        width, height = int(rect.right - rect.left), int(rect.bottom - rect.top)
        if width < 120 or height < 90:
            return None
        return (left, top, left + width, top + height)
    except Exception:
        return None


def _is_cloaked(hwnd: int) -> bool:
    """UWP/ghost windows that exist but render nothing."""
    try:
        value = ctypes.c_int(0)
        ok = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd), ctypes.c_uint(14), ctypes.byref(value), ctypes.sizeof(value)
        )
        return bool(ok == 0 and value.value)
    except Exception:
        return False




def list_windows(limit: int = 150) -> list[dict[str, Any]]:
    """Top-level application windows, in z-order, that can be watched in the background."""
    windows: list[dict[str, Any]] = []
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()
    except Exception:
        return windows

    own_pid = os.getpid()
    seen: set[int] = set()

    # Read from shared_state so test monkeypatches on plugin_api._THUMB_HOST
    # (which is shared_state.THUMB_HOST) propagate here at call time.
    import shared_state
    thumb_host = shared_state.THUMB_HOST

    try:
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _cb(hwnd, _lparam) -> bool:
            try:
                h = int(hwnd)
                if h in seen or len(windows) >= limit:
                    return True
                seen.add(h)
                if not user32.IsWindowVisible(ctypes.c_void_p(h)):
                    return True
                pid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(pid))
                if h == int(thumb_host.get("hwnd") or 0) or int(pid.value) == own_pid:
                    return True
                length = user32.GetWindowTextLengthW(ctypes.c_void_p(h))
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 2)
                user32.GetWindowTextW(ctypes.c_void_p(h), buf, length + 2)
                title = buf.value.strip()
                if not title:
                    return True
                ex_style = 0
                for getter in ("GetWindowLongPtrW", "GetWindowLongW"):
                    try:
                        fn = getattr(user32, getter)
                        ex_style = int(fn(ctypes.c_void_p(h), -20))
                        break
                    except Exception:
                        continue
                if ex_style & 0x00000080:
                    return True
                if _is_cloaked(h):
                    return True
                minimized = bool(user32.IsIconic(ctypes.c_void_p(h)))
                rect = _window_rect(h)
                if minimized:
                    rect = _restored_rect(h) or rect
                if not rect:
                    return True
                left, top, right, bottom = rect
                width, height = right - left, bottom - top
                if width < 120 or height < 90:
                    return True
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(ctypes.c_void_p(h), cls, 256)
                windows.append(
                    {
                        "id": f"window-{h}",
                        "kind": "window",
                        "hwnd": h,
                        "title": title[:120],
                        "class": cls.value,
                        "exe": _window_exe(int(pid.value)),
                        "pid": int(pid.value),
                        "x": left, "y": top, "width": width, "height": height,
                        "minimized": minimized,
                        "foreground": h == int(user32.GetForegroundWindow() or 0),
                    }
                )
            except Exception:
                return True
            return True

        user32.EnumWindows(enum_proc(_cb), 0)
    except Exception:
        return windows

    for w in windows:
        app = w.get("exe") or w.get("class") or "window"
        w["label"] = f"{app} — {w['title']}"
        w["badge"] = "minimized" if w.get("minimized") else ("front" if w.get("foreground") else "background")
    return windows


def list_cameras(force: bool = False) -> list[dict[str, Any]]:
    """Attached cameras, never blocking on a slow driver."""
    now = time.time()
    cached = _CAMERA_CACHE["devices"]
    if cached and not force and (now - _CAMERA_CACHE["at"]) < CAMERA_CACHE_S:
        return cached
    if _CAMERA_CACHE.get("probing"):
        if force:
            _wait_for_probe(CAMERA_PROBE_TIMEOUT_S)
        return _CAMERA_CACHE["devices"]
    if force:
        _CAMERA_CACHE["probing"] = True
        _probe_cameras(CAMERA_PROBE_TIMEOUT_S)
        return _CAMERA_CACHE["devices"]
    _start_camera_probe(CAMERA_PROBE_TIMEOUT_S)
    return cached


def probe_cameras_now() -> list[dict[str, Any]]:
    """A camera probe that actually finishes."""
    if _CAMERA_CACHE.get("probing"):
        _wait_for_probe(CAMERA_PROBE_TIMEOUT_S * 2)
    if _CAMERA_CACHE["devices"]:
        return _CAMERA_CACHE["devices"]
    if _CAMERA_CACHE.get("probing"):
        return _CAMERA_CACHE["devices"]
    return list_cameras(force=True)


def cameras_probing() -> bool:
    return bool(_CAMERA_CACHE.get("probing"))


def abort_camera_probe() -> None:
    """Tell an exploratory camera probe to let go of the devices now."""
    _CAM_PROBE_ABORT.set()


def _camera_source(index: int, label: str = "") -> dict[str, Any]:
    """A camera source built from its index alone — no enumeration, no probe."""
    cached = next(
        (c for c in (_CAMERA_CACHE.get("devices") or []) if int(c.get("index", -1)) == index), None
    )
    if not label and cached:
        label = str(cached.get("label") or "")
    if not label and _CAMERA_NAMES and index < len(_CAMERA_NAMES):
        label = _CAMERA_NAMES[index]
    return {
        "id": f"camera-{index}",
        "kind": "camera",
        "index": index,
        "label": label or ("Default camera" if index == 0 else f"Camera {index}"),
    }


def _source_from_id(source_id: str) -> Optional[dict[str, Any]]:
    """Resolve a source id (e.g. 'window-12345', 'monitor-0', 'camera-2') to its dict."""
    if not source_id:
        return None
    parts = source_id.split("-", 1)
    if len(parts) != 2:
        return None
    kind, ident = parts[0], parts[1]
    try:
        idx = int(ident)
    except ValueError:
        return None
    if kind == "camera":
        return _camera_source(idx)
    if kind == "monitor":
        for m in list_monitors():
            if m.get("index") == idx:
                return m
    if kind == "window":
        for w in list_windows():
            if w.get("hwnd") == idx:
                return w
    return None


def list_sources() -> dict[str, Any]:
    """Everything the user can watch: displays, application windows and cameras."""
    monitors = list_monitors()
    windows = list_windows()
    cameras = list_cameras()
    return {
        "monitors": monitors,
        "windows": windows,
        "cameras": cameras,
        "count": len(monitors) + len(windows) + len(cameras),
    }


# ── Capture / diff / describe ─────────────────────────────────────────────

class _SourceMinimized(RuntimeError):
    """The chosen window is minimized: no public API renders its pixels until it is restored."""


class _SourceGone(RuntimeError):
    """The chosen window no longer exists (closed, or its process exited)."""


def _grab_method() -> str:
    """How this thread's last frame was obtained (printwindow / screen / thumbnail / camera)."""
    return getattr(_GRAB_STATE, "method", "") or ""


def _grab_waiting() -> str:
    """Human-readable reason a grab is slow, for the pane's status line."""
    method = _grab_method()
    if method == "thumbnail":
        return "rendering minimized window…"
    if method == "camera":
        return "reading camera…"
    return ""


def _frame_key(source: dict[str, Any]) -> str:
    """A stable per-source key for the last-frame cache."""
    return source.get("id") or source.get("kind", "") + str(source.get("index") or source.get("hwnd") or "")


def _remember_frame(source: dict[str, Any], img: Any) -> None:
    """Store the last captured frame (and its PNG bytes) for diffing."""
    key = _frame_key(source)
    try:
        import io
        from PIL import Image
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        with _LAST_FRAME_LOCK:
            _LAST_FRAMES[key] = (time.time(), buf.getvalue())
    except Exception:
        pass


def _recall_frame(source: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Retrieve the last captured frame for diffing. Returns None if no prior frame."""
    key = _frame_key(source)
    with _LAST_FRAME_LOCK:
        entry = _LAST_FRAMES.get(key)
    if entry is None:
        return None
    ts, png_bytes = entry
    return {"at": ts, "png": png_bytes}


def _fallback_note(source: dict[str, Any], entry: dict[str, Any], reason: str) -> str:
    """A short note explaining why a fallback capture was used."""
    kind = source.get("kind", "?")
    label = source.get("label") or source.get("title") or source.get("id", "")
    return f"[{kind}:{label}] {reason}"


# ── Monitor grab ────────────────────────────────────────────────────────────

def _grab_monitor(source: dict[str, Any]):
    """Capture a monitor region via GDI."""
    from PIL import ImageGrab

    x = int(source.get("x", 0))
    y = int(source.get("y", 0))
    w = int(source.get("width", 1920))
    h = int(source.get("height", 1080))
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    _GRAB_STATE.method = "monitor"
    return img


def _dib_image(hdc: int, hbmp: int, width: int, height: int):
    """Read a GDI bitmap into a PIL image (BGR→RGB)."""
    from PIL import Image

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_ulong),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", ctypes.c_short),
            ("biBitCount", ctypes.c_short),
            ("biCompression", ctypes.c_ulong),
            ("biSizeImage", ctypes.c_ulong),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", ctypes.c_ulong),
            ("biClrImportant", ctypes.c_ulong),
        ]

    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = width
    bmi.biHeight = -height  # top-down
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0

    buf_size = width * height * 4
    buf = ctypes.create_string_buffer(buf_size)
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    # GetDIBits from gdi32
    gdi32.GetDIBits(ctypes.c_void_p(hdc), ctypes.c_void_p(hbmp), 0, height, buf, ctypes.byref(bmi), 0)

    # Convert BGRA to RGB
    arr = np.frombuffer(buf.raw, dtype=np.uint8).reshape(height, width, 4)
    rgb = arr[:, :, :3][:, :, ::-1]  # BGR → RGB
    return Image.fromarray(rgb, "RGB")


def _grab_window_printwindow(source: dict[str, Any]):
    """Capture a window via PrintWindow (works for most non-minimized windows)."""
    from PIL import Image

    hwnd = source.get("hwnd")
    if not hwnd:
        raise RuntimeError("No hwnd in source")

    rect = _window_rect(hwnd)
    if not rect:
        raise _SourceGone()
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise _SourceGone()

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    hdc = gdi32.CreateCompatibleDC(0)
    hbmp = gdi32.CreateCompatibleBitmap(hdc, width, height)
    try:
        old_hdc = user32.SetWindowPos  # no-op; we need a compatible DC
        # Create a fresh DC for the bitmap
        ps = gdi32.SelectObject(hdc, hbmp)
        # PrintWindow with PW_RENDERFULLCONTENT (0x2)
        ok = user32.PrintWindow(ctypes.c_void_p(hwnd), hdc, 2)
        if not ok:
            raise RuntimeError("PrintWindow returned 0")
        img = _dib_image(hdc, hbmp, width, height)
    finally:
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc)

    _GRAB_STATE.method = "printwindow"
    return img


def _is_blank(img) -> bool:
    """Heuristic: is this frame essentially all one color (blank / not yet rendered)?"""
    try:
        arr = np.array(img)
        if arr.size == 0:
            return True
        # Check if std dev across all channels is near zero
        return float(np.std(arr)) < 2.0
    except Exception:
        return False






# ── DWM thumbnail (for minimized windows) ─────────────────────────────────

def _pump(hwnd: int, seconds: float) -> None:
    """Pump messages for a window's thread for ``seconds`` (keeps it alive)."""
    try:
        user32 = ctypes.windll.user32
        msg = _MSG()
        deadline = time.time() + seconds
        while time.time() < deadline:
            if not user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE=1
                time.sleep(0.01)
                continue
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except Exception:
        pass


def _thumb_host(width: int, height: int) -> int:
    """Get or create the off-screen thumbnail host window. Returns its hwnd."""
    global _THUMB_HOST, _THUMB_PROC
    with _THUMB_LOCK:
        hwnd = _THUMB_HOST.get("hwnd")
        if hwnd:
            return hwnd

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        # Register a window class
        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_long
        )

        def _wnd_proc(_hwnd, msg, wparam, lparam):
            if msg == 0x0010:  # WM_DESTROY
                return 0
            return user32.DefWindowProcW(_hwnd, msg, wparam, lparam)

        wnd_proc = WNDPROC(_wnd_proc)
        wc = _WNDCLASS()
        wc.style = 0
        wc.lpfnWndProc = ctypes.cast(wnd_proc, ctypes.c_void_p)
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = None
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = gdi32.GetStockObject(0)  # NULL_BRUSH
        wc.lpszMenuName = None
        wc.lpszClassName = "PVThumbHost"

        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            raise RuntimeError("RegisterClass failed")

        hwnd = int(user32.CreateWindowExW(
            0x8000000 | 0x20,  # WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            ctypes.cast(wnd_proc, ctypes.c_void_p),  # need a valid proc
            "PVThumbHost",
            0x16,  # WS_OVERLAPPEDWINDOW
            -32000, -32000, width, height,
            None, None, None, None,
        ))
        if not hwnd:
            raise RuntimeError("CreateWindowEx failed")

        _THUMB_HOST = {"hwnd": hwnd}
        return hwnd




def _grab_window_thumbnail(source: dict[str, Any]):
    """Capture a window via DWM thumbnail API (works for minimized windows)."""
    from PIL import Image

    hwnd = source.get("hwnd")
    if not hwnd:
        raise RuntimeError("No hwnd in source")

    # Check if window still exists
    user32 = ctypes.windll.user32
    if not user32.IsWindow(ctypes.c_void_p(hwnd)):
        raise _SourceGone()

    # Get the window's size (use restored rect for minimized)
    rect = _window_rect(hwnd)
    if not rect:
        raise _SourceGone()
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        # Minimized: use restored rect
        rect = _restored_rect(hwnd)
        if not rect:
            raise _SourceMinimized()
        left, top, right, bottom = rect
        width, height = right - left, bottom - top

    # Scale down for thumbnail
    scale = min(1.0, _THUMB_MAX_EDGE / max(width, height))
    thumb_w = max(1, int(width * scale))
    thumb_h = max(1, int(height * scale))

    # Create the off-screen host window
    host_hwnd = _thumb_host(thumb_w, thumb_h)

    # Create a DWM thumbnail
    dwmapi = ctypes.windll.dwmapi
    thumb_id = ctypes.c_uint(0)
    r = dwmapi.DwmRegisterThumbnail(ctypes.c_void_p(host_hwnd), ctypes.c_void_p(hwnd), ctypes.byref(thumb_id))
    if r != 0:
        raise RuntimeError(f"DwmRegisterThumbnail failed: {r}")

    try:
        # Set thumbnail properties (size to fill the host)
        props = _DWM_THUMBNAIL_PROPERTIES()
        props.dwSize = ctypes.sizeof(_DWM_THUMBNAIL_PROPERTIES)
        props.fMask = 0x1 | 0x2 | 0x4 | 0x8  # destination, source, opacity, visible
        props.rctDestination.left = 0
        props.rctDestination.top = 0
        props.rctDestination.right = thumb_w
        props.rctDestination.bottom = thumb_h
        props.fVisible = 1
        dwmapi.DwmUpdateThumbnail(ctypes.c_void_p(host_hwnd), ctypes.c_void_p(hwnd), ctypes.byref(props))

        # Pump messages briefly to let DWM render
        _pump(host_hwnd, 0.5)

        # Capture the host window via PrintWindow
        gdi32 = ctypes.windll.gdi32
        hdc = gdi32.CreateCompatibleDC(0)
        hbmp = gdi32.CreateCompatibleBitmap(hdc, thumb_w, thumb_h)
        try:
            gdi32.SelectObject(hdc, hbmp)
            user32.PrintWindow(ctypes.c_void_p(host_hwnd), hdc, 2)
            img = _dib_image(hdc, hbmp, thumb_w, thumb_h)
        finally:
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc)

        _GRAB_STATE.method = "thumbnail"
        return img
    finally:
        dwmapi.DwmUnregisterThumbnail(ctypes.c_void_p(host_hwnd), ctypes.c_void_p(hwnd))


def _thumb_capture(source: dict[str, Any]):
    """Alias for _grab_window_thumbnail (used in some code paths)."""
    return _grab_window_thumbnail(source)


# ── Window grab (dispatches to printwindow or thumbnail) ───────────────────

def _grab_window(source: dict[str, Any]):
    """Capture a window, choosing the best method available."""
    hwnd = source.get("hwnd")
    if not hwnd:
        raise RuntimeError("No hwnd in source")

    user32 = ctypes.windll.user32
    if not user32.IsWindow(ctypes.c_void_p(hwnd)):
        raise _SourceGone()

    minimized = bool(user32.IsIconic(ctypes.c_void_p(hwnd)))
    if minimized:
        return _grab_window_thumbnail(source)

    # Try PrintWindow first
    try:
        img = _grab_window_printwindow(source)
        if not _is_blank(img):
            return img
    except Exception:
        pass

    # Fallback: screen-region capture
    rect = _window_rect(hwnd)
    if rect:
        left, top, right, bottom = rect
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=(left, top, right, bottom))
        _GRAB_STATE.method = "screen"
        return img

    raise RuntimeError(f"Cannot capture window {hwnd}")


# ── Main grab dispatcher ────────────────────────────────────────────────────

def _grab(source: dict[str, Any]):
    """Capture a frame from the given source (monitor / window / camera)."""
    kind = source.get("kind")
    if kind == "monitor":
        return _grab_monitor(source)
    elif kind == "window":
        return _grab_window(source)
    elif kind == "camera":
        return _grab_camera(source)
    else:
        raise ValueError(f"Unknown source kind: {kind}")


# ── Camera grab ─────────────────────────────────────────────────────────────

def _grab_camera(source: dict[str, Any]):
    """Capture a frame from a camera (held open between frames)."""
    import cv2

    index = int(source.get("index", 0))
    with _CAM_LOCK:
        cap = _CAM_HANDLE.get("cap")
        if cap is None or _CAM_HANDLE.get("index") != index:
            # Release any previous camera
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
            cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            if not cap.isOpened():
                raise RuntimeError(f"Cannot open camera {index}")
            _configure_camera_capture(cap, index)
            _CAM_HANDLE["cap"] = cap
            _CAM_HANDLE["index"] = index

        ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError(f"Camera {index} returned empty frame")

    # Convert BGR to RGB for PIL compatibility
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    from PIL import Image
    _GRAB_STATE.method = "camera"
    return Image.fromarray(rgb)


def _release_camera_locked() -> None:
    cap = _CAM_HANDLE.get("cap")
    _CAM_HANDLE["cap"] = None
    _CAM_HANDLE["index"] = None
    if cap is not None:
        try:
            cap.release()
        except Exception:
            pass


def release_camera() -> None:
    """Close the camera device so its LED turns off when watching stops."""
    with _CAM_LOCK:
        _release_camera_locked()


# ── Window state helpers ────────────────────────────────────────────────────

def _window_iconic(hwnd: int) -> bool:
    """Check if a window is minimized."""
    try:
        return bool(ctypes.windll.user32.IsIconic(ctypes.c_void_p(hwnd)))
    except Exception:
        return False


def _window_rested(hwnd: int) -> bool:
    """Check if a window exists and is not cloaked (i.e. has been 'rested' / shown)."""
    try:
        user32 = ctypes.windll.user32
        if not user32.IsWindow(ctypes.c_void_p(hwnd)):
            return False
        return not _is_cloaked(hwnd)
    except Exception:
        return False


# ── WindowsCapture class (for the abstraction layer) ───────────────────────

class WindowsCapture:
    """Windows implementation of the capture API."""

    def __init__(self):
        self._initialized = True

    def list_monitors(self) -> list[dict]:
        return list_monitors()

    def list_windows(self) -> list[dict]:
        return list_windows()

    def list_cameras(self, force: bool = False) -> list[dict]:
        return list_cameras(force)

    def list_sources(self) -> dict:
        return list_sources()

    def grab(self, source: dict):
        """Capture a frame from the given source. Returns a PIL Image."""
        return _grab(source)

    def grab_window_thumbnail(self, hwnd: int):
        """Capture a window thumbnail (for minimized windows)."""
        return _grab_window_thumbnail({"hwnd": hwnd})

    def is_minimized(self, source: dict) -> bool:
        """Check if a window source is minimized."""
        if source.get("kind") != "window":
            return False
        hwnd = source.get("hwnd")
        if hwnd is None:
            return False
        return _window_iconic(hwnd)

    def get_window_rect(self, source: dict) -> tuple:
        """Get window rectangle (x, y, w, h)."""
        if source.get("kind") != "window":
            return (0, 0, 0, 0)
        hwnd = source.get("hwnd")
        if hwnd is None:
            return (0, 0, 0, 0)
        rect = _window_rect(hwnd)
        if rect:
            return (rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1])
        return (0, 0, 0, 0)

    def supports_snap_full_res(self) -> bool:
        """Whether full-resolution snaps are supported."""
        return True

    def cleanup(self) -> None:
        """Release any held resources."""
        release_camera()


# ── Backwards-compatible function exports ───────────────────────────────────

def grab(source: dict):
    return WindowsCapture().grab(source)


def grab_window_thumbnail(hwnd: int):
    return WindowsCapture().grab_window_thumbnail(hwnd)


def is_minimized(source: dict) -> bool:
    return WindowsCapture().is_minimized(source)


def get_window_rect(source: dict) -> tuple:
    return WindowsCapture().get_window_rect(source)


def supports_snap_full_res() -> bool:
    return True


def cleanup() -> None:
    release_camera()

