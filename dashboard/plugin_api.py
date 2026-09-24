"""Peripheral Vision — Hermes plugin backend.

Captures ONE explicitly-chosen monitor on an interval, filters out frames that
barely changed, and describes genuinely new frames with a configured
OpenAI-compatible multimodal vision endpoint.

Mounted by Hermes at ``/api/plugins/peripheral-vision/*`` (manifest.json →
``"api": "plugin_api.py"``).

Route map
---------
- ``GET  /monitors``  → enumerate displays (index, name, bounds, primary)
- ``GET  /sources``   → enumerate displays, app windows, and attached cameras
- ``POST /start``     → begin the loop on one explicitly selected source
                         (never defaults; the UI must prompt for a choice)
- ``POST /stop``      → stop the loop
- ``GET  /status``    → running state + ring buffer of recent descriptions
- ``POST /inject_mode`` → when fresh readings ride turns (pane pick beats the env var)
- ``GET  /preview``   → last captured frame (downscaled data URL) for the pane

State is persisted to ``$HERMES_HOME/cache/peripheral-vision/`` so the
``pre_llm_call`` hook (which runs in the agent/gateway process, not this web
process) can inject the freshest descriptions as conversation context.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body

router = APIRouter()

# ── Paths / constants ─────────────────────────────────────────────────────
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
STATE_DIR = HERMES_HOME / "cache" / "peripheral-vision"
STATUS_PATH = STATE_DIR / "status.json"
LOG_PATH = STATE_DIR / "log.jsonl"
# Written by the plugin's agent half when it is unloaded (__init__.py::on_unload). That half
# runs in a different process and cannot reach this engine, so the request arrives as a file;
# the loop consumes it on its next tick and stops the watch itself.
STOP_REQUEST_PATH = STATE_DIR / "stop_request"
DEFAULT_INTERVAL_MS = 2000
DEFAULT_THRESHOLD = 85.0  # percent similarity above which a frame is "unchanged"
# How stale the frame behind /preview may get while nothing moves. Unchanged ticks no longer pay the
# intake resize + PNG encode, so the preview copy is refreshed on this cadence instead of every tick;
# it matches the pane's own 6s preview poll.
PREVIEW_MAX_AGE_S = float(os.environ.get("PV_PREVIEW_MAX_AGE_S") or 6.0)
# A status file is a claim, not proof. The loop heartbeats it on every tick, so a claim whose
# heartbeat has gone cold belongs to a process that died without stopping — a killed backend
# never reaches the terminal write at the end of _run. The plugin's agent half runs in ANOTHER
# process and gates context injection on this file, so liveness is judged by the heartbeat here
# and never by `running` alone. Kept equal to the reader's HEARTBEAT_MAX_AGE_S in __init__.py.
STATUS_HEARTBEAT_MAX_AGE_S = 120.0

# ── Inject mode ───────────────────────────────────────────────────────────
# WHEN a fresh reading is allowed to ride a turn. The desktop pane writes this file
# (POST /inject_mode below); the plugin's agent half reads it on every turn
# (__init__.py::inject_mode), so a pick in the pane applies to the next turn without
# restarting anything. Precedence — pane pick, then PV_VISION_INJECT_MODE (read from the
# process that runs the backend), then the default — is exactly the order the agent half
# applies, so what the pane shows is what the hook will read. Kept equal to the agent
# half's constants; the two processes must not import each other (see __init__.py::on_unload),
# so the copies move together and a test guards they cannot drift apart.
INJECT_MODE_PATH = STATE_DIR / "inject_mode"
INJECT_MODE_ENV = "PV_VISION_INJECT_MODE"
INJECT_MODES = ("always", "on_change", "on_mention", "tool_only")
DEFAULT_INJECT_MODE = "on_change"

LOG_KEEP = 400  # ring buffer size for descriptions

# ── Vision: frames go to whatever model Hermes is running ──────────────────
# There is no fixed vision endpoint here. Each changed frame is described by the
# model Hermes is CURRENTLY set to (config.yaml model.provider/model.default).
# When that model cannot accept images the loop stops uploading, records why,
# and the pane prompts for a vision-capable model — it never silently hands the
# screen to a different provider.
OVERRIDE_PATH = STATE_DIR / "vision_model.json"
VISION_TIMEOUT_S = int(os.environ.get("PV_VISION_TIMEOUT_S") or 60)
VISION_ATTEMPTS = int(os.environ.get("PV_VISION_ATTEMPTS") or 2)
SOURCE_FAILURE_LIMIT = int(os.environ.get("PV_SOURCE_FAILURE_LIMIT") or 3)
VISION_MAX_WIDTH = int(os.environ.get("PV_VISION_MAX_WIDTH") or 1024)
# ── Model intake ──────────────────────────────────────────────────────────────
# The camera captures at its native mode; what TRAVELS is sized for the model that will read it,
# always keeping the original aspect ratio. Black bars appear only when the model's encoder needs a
# square — CLIP-class models squash a non-square frame, which silently wrecks the geometry they
# then describe.
_INTAKE_BY_PROVIDER: dict[str, tuple[int, int]] = {
    # provider -> (longest edge, total-pixel budget); 0 = no known budget
    "anthropic": (1568, 0),
    "openai": (2048, 0),
    "openai-codex": (2048, 0),
    "google": (1568, 0),
    "gemini": (1568, 0),
    "xai": (2048, 0),
}
_LOCAL_PROVIDER_HINTS = (
    "ollama", "llama", "llamacpp", "lmstudio", "lm-studio", "local", "vllm", "mlx", "text-generation",
    "unsloth", "voxta",
)
_CLIP_SQUARE_HINTS = ("llava", "bakllava", "moondream", "nanollava", "clip", "siglip", "blip")
DEFAULT_INTAKE_EDGE = 1568   # Claude's recommended long edge: the conservative common intake
LOCAL_INTAKE_EDGE = 1024     # local VLMs are built around ~1MP
CLIP_SQUARE_EDGE = 336       # CLIP ViT-L/14-336: hand it a square or it gets squashed
VISION_PROMPT = os.environ.get("PV_VISION_PROMPT") or (
    "Describe what is on this computer screen in 2-4 sentences: which apps or windows are "
    "visible, what the user appears to be doing, and any notable on-screen text."
)
# Name hints only SUGGEST candidates in the picker; the capability verdict always
# comes from Hermes' own registry so there is no second model list to rot. Keep the
# hints narrow: broad family words ("claude", "gemini", "gpt-5") drag text-only
# bridges into the list, and a wrong suggestion is worse than no suggestion.
VISION_NAME_HINTS = (
    "vision", "multimodal", "-vl", "/vl", "vl/", "llava", "pixtral",
)
_LAST_DESCRIBER = ""  # "provider/model" that produced the most recent description
_WATCH_SESSION: dict[str, str] = {"id": ""}  # live chat whose model describes frames


# ── Monitor enumeration ───────────────────────────────────────────────────
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
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.POINTER(_RECT),
            ctypes.c_double,
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

    if not monitors:  # last-resort fallback: one virtual screen
        try:
            from PIL import ImageGrab

            img = ImageGrab.grab()
            monitors = [
                {
                    "id": "monitor-0",
                    "index": 0,
                    "device": "VIRTUAL",
                    "label": "Display 1",
                    "x": 0,
                    "y": 0,
                    "width": img.width,
                    "height": img.height,
                    "primary": True,
                }
            ]
        except Exception:
            monitors = []

    for m in monitors:
        m["primary_label"] = "primary" if m.get("primary") else ""
        m["kind"] = "monitor"
    return monitors


# ── Application windows (watch ONE app instead of a whole display) ────────
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _window_exe(pid: int) -> str:
    """Executable name of a window's process, for a readable label."""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
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
        )  # DWMWA_EXTENDED_FRAME_BOUNDS
        if ok != 0:
            ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except Exception:
        return None


class _WINDOWPLACEMENT(ctypes.Structure):
    """GetWindowPlacement: where a window sits when restored, and whether it is minimized."""

    _fields_ = [
        ("length", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("showCmd", ctypes.c_uint),
        ("ptMinPosition", _POINT),
        ("ptMaxPosition", _POINT),
        ("rcNormalPosition", _RECT),
    ]


def _restored_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    """The rectangle a minimized window restores to.

    A minimized window answers ``GetWindowRect`` with its tiny iconic rect (≈276×45), so a
    size check against that hides whole applications from the picker. The placement's
    ``rcNormalPosition`` is the frame the window actually occupies when restored.
    """
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
        )  # DWMWA_CLOAKED
        return bool(ok == 0 and value.value)
    except Exception:
        return False


def _root_window(hwnd: int) -> int:
    try:
        root = ctypes.windll.user32.GetAncestor(ctypes.c_void_p(hwnd), 3)  # GA_ROOTOWNER
        return int(root or hwnd)
    except Exception:
        return hwnd


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
                # Our own windows are skipped BEFORE the title calls below, and that order is
                # load-bearing: GetWindowTextLengthW/GetWindowTextW deliver a message to the
                # window's owning thread, and for a window owned by THIS process the kernel
                # applies no timeout. The off-screen thumbnail host (_THUMB_CLASS) is shown by
                # design but owned by a capture thread that pumps no messages, so a title call
                # on it would block this thread forever and every later enumeration of sources
                # would queue behind it.
                pid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(pid))
                if h == int(_THUMB_HOST.get("hwnd") or 0) or int(pid.value) == own_pid:
                    return True  # never watch ourselves
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
                        ex_style = int(fn(ctypes.c_void_p(h), -20))  # GWL_EXSTYLE
                        break
                    except Exception:
                        continue
                if ex_style & 0x00000080:  # WS_EX_TOOLWINDOW (palettes, tooltips)
                    return True
                if _is_cloaked(h):
                    return True
                minimized = bool(user32.IsIconic(ctypes.c_void_p(h)))
                rect = _window_rect(h)
                if minimized:
                    # Minimized windows report their iconic rect (~276×45) to GetWindowRect.
                    # They are still open programs, so judge them by the frame they restore to.
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
                        "x": left,
                        "y": top,
                        "width": width,
                        "height": height,
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


# ── Cameras (poll the OS default webcam, or any attached one) ─────────────
CAMERA_MAX_INDEX = int(os.environ.get("PV_CAMERA_MAX_INDEX") or 4)
CAMERA_CACHE_S = int(os.environ.get("PV_CAMERA_CACHE_S") or 600)
CAMERA_PROBE_TIMEOUT_S = float(os.environ.get("PV_CAMERA_PROBE_TIMEOUT_S") or 5.0)
CAMERA_PROBE_WAVE = int(os.environ.get("PV_CAMERA_PROBE_WAVE") or 2)
_CAMERA_CACHE: dict[str, Any] = {"at": 0.0, "devices": [], "probing": False, "thread": None}
# Set when the user picks a camera: an exploratory probe must stop opening devices immediately.
_CAM_PROBE_ABORT = threading.Event()

# Capture at the largest mode a device will actually deliver: OpenCV's default is 640x480, which
# throws away most of what a 1080p webcam can see. Walked max-first, and the winner is remembered
# per camera (and persisted) so only the first open after a restart pays for the negotiation.
CAMERA_MODES: tuple[tuple[int, int], ...] = ((3840, 2160), (1920, 1080), (1280, 720))
CAMERA_MODES_PATH = STATE_DIR / "camera_modes.json"
_CAMERA_MODES: dict[int, tuple[int, int]] = {}
_CAMERA_MODES_LOADED = False
_MODES_LOCK = threading.Lock()  # guards the lazy load + the read-modify-write of camera_modes.json


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
                pass  # no cache yet, or a corrupt one: negotiate from scratch
            # Only once the table is fully populated: a second thread must never observe the
            # loaded-flag set while _CAMERA_MODES is still empty, or it silently falls back to
            # the full mode-negotiation ladder the cache exists to skip.
            _CAMERA_MODES_LOADED = True
        return _CAMERA_MODES.get(index)


def _remember_camera_mode(index: int, size: tuple[int, int]) -> None:
    """Remember what this camera settled on so the next open gets there in one step."""
    with _MODES_LOCK:  # probe threads finish in waves: the read-modify-write must be atomic
        if _CAMERA_MODES.get(index) == size:
            return
        _CAMERA_MODES[index] = size
        _write_json(
            CAMERA_MODES_PATH,
            {"v": 1, "modes": {str(i): [w, h] for i, (w, h) in sorted(_CAMERA_MODES.items())}},
        )
        # The picker shows the camera's size, so keep the device cache honest with what we really get.
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
    """Ask a device for one mode and report the size it actually delivered.

    DSHOW substitutes a mode it prefers when the request is unsupported (and some drivers report the
    request while scaling), so the frame is the only trustworthy answer. A device that just
    renegotiated often fails its first read, hence the retries.
    """
    try:
        import cv2

        # Above 720p, webcams almost always need MJPG — the default YUY2 format caps out at 640x480.
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
    """Ask for the biggest mode this camera honours and return the delivered size.

    Never raises: capture that turns out to be merely VGA still works, it is just smaller.
    """
    known = _camera_mode(index)
    if known:
        got_w, got_h = _apply_camera_mode(cap, *known)
        if got_w and got_h:
            if (got_w, got_h) != known:
                _remember_camera_mode(index, (got_w, got_h))  # the device answered differently now
            return (got_w, got_h)
        # The remembered mode stopped working (device swapped, driver reset): ask again below.
    best = (0, 0)
    for width, height in CAMERA_MODES:
        got_w, got_h = _apply_camera_mode(cap, width, height)
        if not got_w or not got_h:
            continue
        best = (got_w, got_h)
        _remember_camera_mode(index, best)
        if got_w >= width and got_h >= height:
            break  # the device honoured the request in full
        if max(got_w, got_h) >= 1280:
            break  # a device that clamps 3840x2160 down to 1920x1080 is already at its native max
    return best


CAMERA_CACHE_PATH = STATE_DIR / "cameras.json"
try:  # last run's cameras answer the picker instantly, before any probe finishes
    _cached_cameras = json.loads(CAMERA_CACHE_PATH.read_text(encoding="utf-8"))
    if isinstance(_cached_cameras.get("devices"), list):
        _CAMERA_CACHE["devices"] = _cached_cameras["devices"]
        _CAMERA_CACHE["at"] = float(_cached_cameras.get("at") or 0.0)
except Exception:
    pass
_CAMERA_NAMES: Optional[list[str]] = None
_NAMES_LOCK = threading.Lock()  # the ffmpeg enumeration costs up to 8s: never run it twice at once


def _camera_device_names() -> list[str]:
    """DirectShow camera names, in enumeration order (matches cv2's index order)."""
    global _CAMERA_NAMES
    if _CAMERA_NAMES is not None:
        return _CAMERA_NAMES
    with _NAMES_LOCK:
        if _CAMERA_NAMES is not None:  # another thread enumerated while we waited
            return _CAMERA_NAMES
        names: list[str] = []
        try:
            import shutil
            import subprocess

            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg:
                proc = subprocess.run(
                    [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=8,
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
    """Open one camera, read a frame, close it. Runs on its own thread.

    A probe must never touch a device the watcher is already holding: DSHOW gives one client at a
    time, and a second open of the same device stalls or faults the driver — the "pick a camera and
    the backend dies" class of failure. It also gives up once its wave is out of time, so a slow
    camera does not keep a device open after the list has been answered.
    """
    with _CAM_LOCK:
        if _CAM_HANDLE.get("cap") is not None and _CAM_HANDLE.get("index") == index:
            return
    if deadline and time.time() > deadline:
        return
    if _CAM_PROBE_ABORT.is_set():
        return  # do not open a device the user is no longer waiting for
    cap = None
    try:
        import cv2

        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        # DSHOW reports isOpened() == False for a device that is still initialising and then hands
        # out frames anyway — trusting the flag alone reported "no camera attached" with a webcam
        # plugged in, and the pane retried until the app's 30s RPC timeout fired. A frame is proof.
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
    """Probe camera indices in small waves, each wave bounded by ``deadline_s``.

    Probing a camera opens the device (its LED blinks and a stalled driver can take
    tens of seconds), so the picker must never wait on it: this runs in the
    background and whatever answers in time is reported. Opening every device at once
    makes them contend for the same bus and answers get lost, so they go a couple at
    a time — a camera list is worth a few seconds of background work.
    """
    try:
        _CAM_PROBE_ABORT.clear()  # the previous pick's abort is stale once a new probe starts
        names = _camera_device_names()
        out: dict[int, dict[str, Any]] = {}
        lock = threading.Lock()
        indices = list(range(CAMERA_MAX_INDEX))
        for start in range(0, len(indices), CAMERA_PROBE_WAVE):
            if _CAM_PROBE_ABORT.is_set():
                break  # the user picked a camera: stop opening devices behind their back
            wave = indices[start : start + CAMERA_PROBE_WAVE]
            deadline = time.time() + deadline_s
            workers = [
                threading.Thread(
                    target=_probe_one_camera, args=(i, names, out, lock, deadline), daemon=True
                )
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


def list_cameras(force: bool = False) -> list[dict[str, Any]]:
    """Attached cameras, never blocking on a slow driver.

    A fresh cache (or the on-disk copy from the last run) answers immediately; a
    stale cache kicks off a bounded background re-probe whose result replaces the
    list on the next call. ``force`` waits for the probe, but only up to the bound.
    """
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
    """A camera probe that actually finishes, for callers that genuinely need the answer.

    ``list_cameras(force=True)`` returns an empty list when it only waits on a background probe that
    has not answered yet — which reads as "no camera is attached" while the webcam is sitting right
    there. This waits for the in-flight probe first, and only then probes for itself.
    """
    if _CAMERA_CACHE.get("probing"):
        _wait_for_probe(
            CAMERA_PROBE_TIMEOUT_S * 2
        )  # one DSHOW open alone can take a couple of seconds
    if _CAMERA_CACHE["devices"]:
        return _CAMERA_CACHE["devices"]
    if _CAMERA_CACHE.get("probing"):
        return _CAMERA_CACHE["devices"]  # still busy: report what is known rather than collide
    return list_cameras(force=True)


def cameras_probing() -> bool:
    return bool(_CAMERA_CACHE.get("probing"))


def abort_camera_probe() -> None:
    """Tell an exploratory camera probe to let go of the devices now.

    A probe holds a device open while it reads, and DSHOW allows one client at a time: a pick that
    lands during a probe would otherwise wait on it. The user's pick outranks the probe.
    """
    _CAM_PROBE_ABORT.set()


def _camera_source(index: int, label: str = "") -> dict[str, Any]:
    """A camera source built from its index alone — no enumeration, no probe.

    The index is the whole identity of a camera, so a pick never needs to open a device to be
    resolved. Labels come from whatever is already known (cache, then the DSHOW name list).
    """
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
        "name": label,
        "width": int((cached or {}).get("width") or 0),
        "height": int((cached or {}).get("height") or 0),
        "default": index == 0,
        "readable": True,
        "badge": "default" if index == 0 else "camera",
    }


def _source_from_id(source_id: str) -> Optional[dict[str, Any]]:
    """The source a pick names, resolved without enumerating devices.

    Only cameras and displays resolve here (index arithmetic and cheap ctypes); windows fall back to
    the full listing. Enumerating on a pick is what made it slow enough for the desktop app's 30s
    RPC timeout to fire.
    """
    match = re.fullmatch(r"camera-(\d+)", source_id)
    if match:
        return _camera_source(int(match.group(1)))
    if source_id in ("camera-default", "default-camera"):
        cached = _CAMERA_CACHE.get("devices") or []
        best = next((c for c in cached if c.get("default")), cached[0] if cached else None)
        return _camera_source(int(best.get("index") or 0) if best else 0)
    match = re.fullmatch(r"(?:monitor|display)-(\d+)", source_id)
    if match:
        index = int(match.group(1))
        return next((m for m in list_monitors() if int(m.get("index", -1)) == index), None)
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


_GRAB_STATE = threading.local()  # per-thread: the watcher loop and the pane's previews must not mix


def _grab_method() -> str:
    """How this thread's last frame was obtained (printwindow / screen / thumbnail / camera)."""
    return getattr(_GRAB_STATE, "method", "") or ""


def _grab_waiting() -> str:
    """Why this thread's last frame is not live, or an empty string when it is live."""
    return getattr(_GRAB_STATE, "waiting", "") or ""
_THUMB_HOST: dict[str, Any] = {"hwnd": 0, "size": (0, 0)}  # off-screen window hosting DWM thumbnails
_THUMB_PROC: Any = None  # the WNDPROC must outlive the window: a collected callback crashes Windows
_THUMB_MAX_EDGE = 1920  # measured: a minimized window's DWM surface carries no readable detail past
# ~1330 px wide (a 2661 px capture read exactly the same text as a 1024 px one), so this ceiling keeps
# real pixels for small/medium windows without feeding DWM's upscaler for huge ones
_CAM_LOCK = threading.Lock()
_THUMB_LOCK = threading.Lock()  # serialises DWM-thumbnail captures through the one shared host window
_LOG_LOCK = threading.Lock()    # serialises log ring-buffer reads + writes so concurrent engine ticks can't corrupt log.jsonl
_PREVIEW_LOCK = threading.Lock()
_PREVIEW_CACHE: dict[tuple[int, int], dict[str, Any]] = {}  # (frame_seq, width) -> /preview payload
_PREVIEW_CACHE_MAX = 32  # the pane asks for a handful of widths; the rest is the picker's rows
_CAM_HANDLE: dict[str, Any] = {"cap": None, "index": None}


def _grab_monitor(source: dict[str, Any]):
    """PIL image of one whole display."""
    from PIL import ImageGrab

    box = (
        source["x"],
        source["y"],
        source["x"] + source["width"],
        source["y"] + source["height"],
    )
    return ImageGrab.grab(bbox=box, all_screens=True)


def _dib_image(hdc: int, hbmp: int, width: int, height: int):
    """PIL image from a GDI bitmap selected into a memory DC."""
    from PIL import Image

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32),
            ("biWidth", ctypes.c_int32),
            ("biHeight", ctypes.c_int32),
            ("biPlanes", ctypes.c_uint16),
            ("biBitCount", ctypes.c_uint16),
            ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32),
            ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32),
            ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]

    header = _BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = -height  # negative = top-down rows, matches PIL's raw order
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = 0  # BI_RGB
    buffer = ctypes.create_string_buffer(width * height * 4)
    scanned = ctypes.windll.gdi32.GetDIBits(
        ctypes.c_void_p(hdc),
        ctypes.c_void_p(hbmp),
        0,
        ctypes.c_uint(height),
        buffer,
        ctypes.byref(header),
        0,  # DIB_RGB_COLORS
    )
    if not scanned:
        raise RuntimeError("GetDIBits returned no scanlines")
    return Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1)


def _grab_window_printwindow(source: dict[str, Any]):
    """PrintWindow render: grabs a window that is behind others (or minimized)."""
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    hwnd = int(source.get("hwnd") or 0)
    rect = _window_rect(hwnd) or (
        source["x"],
        source["y"],
        source["x"] + source["width"],
        source["y"] + source["height"],
    )
    width = max(1, rect[2] - rect[0])
    height = max(1, rect[3] - rect[1])
    hdc_window = user32.GetWindowDC(ctypes.c_void_p(hwnd))
    if not hdc_window:
        raise _SourceGone(f"window has no device context: {source.get('label') or hwnd}")
    hdc_mem = gdi32.CreateCompatibleDC(ctypes.c_void_p(hdc_window))
    hbmp = gdi32.CreateCompatibleBitmap(ctypes.c_void_p(hdc_window), width, height)
    try:
        gdi32.SelectObject(ctypes.c_void_p(hdc_mem), ctypes.c_void_p(hbmp))
        user32.PrintWindow(ctypes.c_void_p(hwnd), ctypes.c_void_p(hdc_mem), 2)  # PW_RENDERFULLCONTENT
        return _dib_image(hdc_mem, hbmp, width, height)
    finally:
        gdi32.DeleteObject(ctypes.c_void_p(hbmp))
        gdi32.DeleteDC(ctypes.c_void_p(hdc_mem))
        user32.ReleaseDC(ctypes.c_void_p(hwnd), ctypes.c_void_p(hdc_window))


def _is_blank(img) -> bool:
    """A flat frame: PrintWindow's signature for a surface it could not render."""
    try:
        low, high = img.convert("L").getextrema()
    except Exception:
        return False
    return (high - low) <= 2


def _frame_is_warmup(frame) -> bool:
    """True for the near-uniform black frames a camera hands out before its sensor settles."""
    try:
        return float(frame.mean()) < 6.0 or float(frame.std()) < 2.0
    except Exception:
        return False


def _is_unobstructed(hwnd: int, source: dict[str, Any]) -> bool:
    """True when this window's own pixels sit on top of its centre point."""
    try:
        point = _POINT(source["x"] + source["width"] // 2, source["y"] + source["height"] // 2)
        top = int(ctypes.windll.user32.WindowFromPoint(point) or 0)
        return bool(top) and _root_window(top) == _root_window(hwnd)
    except Exception:
        return False


class _DWM_THUMBNAIL_PROPERTIES(ctypes.Structure):
    """DWM_THUMBNAIL_PROPERTIES: how a live thumbnail of another window is composited."""

    _fields_ = [
        ("dwFlags", ctypes.c_uint),
        ("rcDestination", _RECT),
        ("rcSource", _RECT),
        ("opacity", ctypes.c_ubyte),
        ("fVisible", ctypes.c_int),
        ("fSourceClientAreaOnly", ctypes.c_int),
    ]


class _WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
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
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint),
        ("pt", _POINT),
    ]


_THUMB_CLASS = "HermesPeripheralVisionThumbHost"


def _pump(hwnd: int, seconds: float) -> None:
    """Let DWM (and our own window) make progress while we wait for a composite."""
    user32 = ctypes.windll.user32
    msg = _MSG()
    end = time.time() + seconds
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), ctypes.c_void_p(hwnd), 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)


def _thumb_host(width: int, height: int) -> int:
    """An off-screen window to composite DWM thumbnails into (taskbar previews use the same trick).

    It sits at (-32000, -32000) but is *shown*: DWM only composes a thumbnail for a visible
    host, and a shown window at that position is never painted on the user's display.
    """
    global _THUMB_PROC
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    existing = int(_THUMB_HOST.get("hwnd") or 0)
    if existing and _THUMB_HOST.get("size") == (width, height) and user32.IsWindow(ctypes.c_void_p(existing)):
        return existing
    if existing:
        user32.DestroyWindow(ctypes.c_void_p(existing))
        _THUMB_HOST["hwnd"] = 0
    if _THUMB_PROC is None:
        user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t

        def _wnd_proc(h, message, wparam, lparam):
            return user32.DefWindowProcW(ctypes.c_void_p(h), message, wparam, lparam)

        _THUMB_PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)(_wnd_proc)
        window_class = _WNDCLASS()
        window_class.lpfnWndProc = ctypes.cast(_THUMB_PROC, ctypes.c_void_p)
        window_class.hInstance = kernel32.GetModuleHandleW(None)
        window_class.lpszClassName = _THUMB_CLASS
        user32.RegisterClassW(ctypes.byref(window_class))
    host = user32.CreateWindowExW(
        0x00000008 | 0x00000080,  # WS_EX_TOPMOST | WS_EX_TOOLWINDOW: never in alt-tab, never focus
        _THUMB_CLASS,
        " ",
        0x80000000,  # WS_POPUP
        -32000,
        -32000,
        width,
        height,
        None,
        None,
        kernel32.GetModuleHandleW(None),
        None,
    )
    if not host:
        raise RuntimeError("could not create the thumbnail host window")
    user32.ShowWindow(ctypes.c_void_p(host), 5)  # SW_SHOW: off-screen, so DWM composes but nothing is painted
    _pump(int(host), 0.05)
    _THUMB_HOST.update({"hwnd": int(host), "size": (width, height)})
    return int(host)


def _physical_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    """A window's frame rectangle in real device pixels.

    ``GetWindowPlacement``/``GetWindowRect`` report *logical* pixels to a DPI-unaware process, so on
    a scaled display they understate how many pixels a window actually has; a capture sized from them
    would be upscaled by DWM. ``DWMWA_EXTENDED_FRAME_BOUNDS`` (9) returns physical pixels, drop shadow
    excluded, so it is the honest size for a full-frame capture.
    """
    bounds = _RECT()
    try:
        if (
            ctypes.windll.dwmapi.DwmGetWindowAttribute(
                ctypes.c_void_p(hwnd), 9, ctypes.byref(bounds), ctypes.sizeof(bounds)
            )
            == 0
            and bounds.right > bounds.left
            and bounds.bottom > bounds.top
        ):
            return (bounds.left, bounds.top, bounds.right, bounds.bottom)
    except Exception:
        pass
    return None


def _grab_window_thumbnail(source: dict[str, Any]):
    """The last-rendered frame of a minimized window, via DWM.

    ``PrintWindow`` cannot render a minimized window — it answers a blank 256×35 strip — but the
    window's last frame is still alive in the DWM redirection surface, and DWM will composite it
    into any window that registers a thumbnail (this is exactly what taskbar previews show). The
    frame is frozen until the window is restored, so the caller must label it as such.

    Captured at the window's own device-pixel size with the whole frame (title bar included): a
    larger destination only makes DWM upscale, a smaller one discards detail it can really supply.
    """
    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi
    hwnd = int(source.get("hwnd") or 0)
    explicit = source.get("thumb_size")  # callers that only need a small preview (the picker) may say so
    if explicit:
        width, height = max(64, int(explicit[0])), max(64, int(explicit[1]))
    else:
        # A minimized window's EXTENDED_FRAME_BOUNDS is the iconic strip (256x35), so that size is only
        # trustworthy while the window is restored; the placement rect is what describes a minimized one.
        iconic = bool(user32.IsIconic(ctypes.c_void_p(hwnd)))
        rect = (_restored_rect(hwnd) if iconic else _physical_rect(hwnd) or _restored_rect(hwnd)) or (
            source["x"],
            source["y"],
            source["x"] + source["width"],
            source["y"] + source["height"],
        )
        width = max(120, rect[2] - rect[0])
        height = max(90, rect[3] - rect[1])
        scale = min(1.0, _THUMB_MAX_EDGE / float(max(width, height)))
        width, height = max(120, int(width * scale)), max(90, int(height * scale))
    with _THUMB_LOCK:  # the host window is shared, so only one composited thumbnail is in flight
        host = _thumb_host(width, height)
        thumbnail = ctypes.c_void_p()
        result = dwmapi.DwmRegisterThumbnail(ctypes.c_void_p(host), ctypes.c_void_p(hwnd), ctypes.byref(thumbnail))
        if result != 0:
            raise _SourceMinimized(f"DWM refused a thumbnail for {source.get('label') or hwnd}")
        try:
            props = _DWM_THUMBNAIL_PROPERTIES()
            props.dwFlags = 0x1 | 0x4 | 0x8  # RECTDESTINATION | OPACITY | VISIBLE (whole window, frame included)
            props.rcDestination = _RECT(0, 0, width, height)
            props.opacity = 255
            props.fVisible = 1
            props.fSourceClientAreaOnly = 0
            dwmapi.DwmUpdateThumbnailProperties(thumbnail, ctypes.byref(props))
            image = None
            for _ in range(10):  # DWM needs a moment to composite the first frame
                _pump(host, 0.025)
                image = _grab_window_printwindow(
                    {"kind": "window", "hwnd": host, "x": -32000, "y": -32000, "width": width, "height": height}
                )
                if not _is_blank(image):
                    break
            if image is None or _is_blank(image):
                raise _SourceMinimized(f"DWM returned no frame for {source.get('label') or hwnd}")
            _GRAB_STATE.method = "thumbnail"
            return image
        finally:
            dwmapi.DwmUnregisterThumbnail(thumbnail)
            _pump(host, 0.01)


def _grab_window(source: dict[str, Any]):
    """One application window, without stealing focus.

    An unobstructed, on-screen window is grabbed straight from the screen — that keeps
    video and other GPU-composited content real. Occluded, minimized or hidden windows go
    through PrintWindow, which renders them in the background; a window that still comes
    back blank there raises instead, so we NEVER quietly capture whatever happens to be on
    screen instead of the app you chose.
    """
    user32 = ctypes.windll.user32
    hwnd = int(source.get("hwnd") or 0)
    _GRAB_STATE.waiting = ""  # live, unless the minimized branch below says otherwise
    if not hwnd:
        raise _SourceGone("no window handle")
    if not user32.IsWindow(ctypes.c_void_p(hwnd)):
        raise _SourceGone(f"window closed: {source.get('label') or hwnd}")
    if user32.IsIconic(ctypes.c_void_p(hwnd)):
        # PrintWindow answers a minimized window with a blank 256x35 strip (verified), so use the
        # route taskbar previews use: DWM still holds the last rendered frame and composites it into
        # an off-screen window of ours. That frame is frozen, so the status says so explicitly —
        # never present a stale picture as if it were live.
        _GRAB_STATE.waiting = (
            f"{source.get('title') or source.get('label') or 'that window'} is minimized — "
            "showing its last frame; live frames resume when you restore it"
        )
        return _grab_window_thumbnail(source)
    on_screen = not user32.IsIconic(ctypes.c_void_p(hwnd)) and bool(
        user32.IsWindowVisible(ctypes.c_void_p(hwnd))
    )
    if on_screen and _is_unobstructed(hwnd, source):
        try:
            img = _grab_monitor(source)
            if not _is_blank(img):
                _GRAB_STATE.method = "screen"
                return img
        except Exception:
            pass
    img = _grab_window_printwindow(source)
    if _is_blank(img):
        raise RuntimeError(
            "window comes back blank in the background (GPU-composited or hidden) — "
            "bring it to the front, or watch its display instead"
        )
    _GRAB_STATE.method = "printwindow"
    return img


def _grab(source: dict[str, Any]):
    """PIL image of the chosen source: an application window, a display, or a camera."""
    _GRAB_STATE.waiting = ""  # only the window path can report a non-live frame
    kind = (source or {}).get("kind") or ("window" if (source or {}).get("hwnd") else "monitor")
    if kind == "camera":
        return _grab_camera(source)
    if kind == "window":
        return _grab_window(source)
    return _grab_monitor(source)


def _grab_camera(source: dict[str, Any]):
    """One frame from a camera. The device handle is kept open between frames.

    A camera is watched the same way a window is: the derived description goes to
    whatever model Hermes is running, so the frame is treated like any other capture.
    """
    index = int(source.get("index") or 0)
    if _CAM_HANDLE.get("cap") is None or _CAM_HANDLE.get("index") != index:
        # Picking a camera usually follows a refresh whose probe threads may still hold the devices,
        # and DSHOW allows one client at a time. The pick outranks the probe: tell it to let go and
        # wait only the moment it needs to notice — waiting the whole probe budget made every switch
        # take seconds behind the pane's 30s RPC timeout.
        if _CAMERA_CACHE.get("probing"):
            abort_camera_probe()
            _wait_for_probe(0.5)
    with _CAM_LOCK:
        cap = _CAM_HANDLE.get("cap")
        if cap is None or _CAM_HANDLE.get("index") != index:
            _release_camera_locked()
            try:
                import cv2
            except Exception as exc:  # pragma: no cover - depends on the environment
                raise RuntimeError(
                    f"camera capture needs OpenCV in the Hermes venv ({type(exc).__name__})"
                ) from exc
            cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            if not cap or not cap.isOpened():
                raise RuntimeError(
                    f"camera {index} is unavailable — it may be in use by another app "
                    "(Windows only allows one client at a time)"
                )
            # Native capture, not OpenCV's 640x480 default: ask for the largest mode the device
            # honours, and remember it so every later open takes a single step.
            size = _configure_camera_capture(cap, index)
            _CAM_HANDLE["cap"] = cap
            _CAM_HANDLE["index"] = index
            _CAM_HANDLE["size"] = size
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
        frame = None
        # A freshly opened DSHOW device hands back black frames while its sensor warms up, and
        # publishing one shows the user a black rectangle right after they pick the camera (and the
        # model describes "a dark image"). Read past them, but never in an unbounded loop: a dark
        # room is dark, and the last frame is returned either way.
        warmup_deadline = time.time() + 2.5
        for _ in range(12):
            ok, frame = cap.read()
            if ok and frame is not None and not _frame_is_warmup(frame):
                break
            if time.time() > warmup_deadline:
                break
        if frame is None:
            _release_camera_locked()
            raise RuntimeError(f"camera {index} returned no frame")
        try:
            import cv2

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        except Exception:
            rgb = frame[:, :, ::-1]
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


def _env_int(name: str) -> int:
    """Positive int from the environment, else 0 — a typo must not break the watch loop."""
    try:
        value = int((os.environ.get(name) or "").strip())
    except ValueError:
        return 0
    return value if value > 0 else 0


def _vision_intake(provider: str = "", model: str = "", base_url: str = "") -> dict[str, Any]:
    """The frame size the recipient model will actually take: ``max_edge``, ``max_pixels``, ``square``.

    Order of authority: ``PV_VISION_MAX_EDGE`` (or the older ``PV_VISION_MAX_WIDTH``) plus
    ``PV_VISION_MAX_PIXELS``/``PV_VISION_SQUARE`` win outright; then a square-only encoder, because
    that is correctness rather than budget; then the provider's own budget; then a local-endpoint
    guess; then the default. Never raises — an unresolvable model still gets a sane frame.
    """
    provider_key = (provider or "").strip().lower()
    name = f"{provider_key}/{model or ''}".lower()
    env_edge = _env_int("PV_VISION_MAX_EDGE") or _env_int("PV_VISION_MAX_WIDTH") or 1024
    env_pixels = _env_int("PV_VISION_MAX_PIXELS")
    env_square = (os.environ.get("PV_VISION_SQUARE") or "").strip().lower()

    for alias, canonical in (("claude", "anthropic"), ("codex", "openai-codex"), ("gpt", "openai")):
        # A local bridge (claude-web, codex-bridge, ...) proxies a cloud model: it takes that
        # model's intake, not the local ~1MP guess, even though its base_url is loopback.
        if alias in provider_key and provider_key not in _INTAKE_BY_PROVIDER:
            provider_key = canonical
            break
    edge, pixels = _INTAKE_BY_PROVIDER.get(provider_key, (0, 0))
    source = "provider" if edge else ""
    if not edge:
        local = provider_key in _LOCAL_PROVIDER_HINTS or any(
            hint in (base_url or "").lower()
            for hint in ("127.0.0.1", "localhost", "::1", "0.0.0.0")
        )
        edge, pixels = (LOCAL_INTAKE_EDGE, LOCAL_INTAKE_EDGE ** 2) if local else (DEFAULT_INTAKE_EDGE, 0)
        source = "local" if local else "default"
    square = any(hint in name for hint in _CLIP_SQUARE_HINTS)
    if square:
        edge, pixels, source = CLIP_SQUARE_EDGE, CLIP_SQUARE_EDGE ** 2, "clip"
    if env_square in ("0", "false", "no", "off"):
        square = False
    elif env_square in ("1", "true", "yes", "on"):
        square, source = True, "env"
    if env_edge:
        edge, source = env_edge, "env"
    if env_pixels:
        pixels = env_pixels
    return {"max_edge": edge, "max_pixels": pixels, "square": square, "source": source}


def _intake_frame(img, intake: dict[str, Any]):
    """Fit ``img`` to a model's intake: shrink only, aspect preserved, black bars when square."""
    from PIL import Image

    width, height = getattr(img, "size", (0, 0))[:2]
    if not width or not height or not intake:
        return img
    edge = int(intake.get("max_edge") or 0)
    pixels = int(intake.get("max_pixels") or 0)
    scale = 1.0
    if edge and max(width, height) > edge:
        scale = edge / float(max(width, height))
    if pixels and (width * height) * scale * scale > pixels:
        scale = min(scale, (pixels / float(width * height)) ** 0.5)
    if scale < 1.0:
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", None)
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        img = img.resize(size, resampling) if resampling is not None else img.resize(size)
    if not intake.get("square") or img.width == img.height:
        return img
    side = max(img.size)
    canvas = Image.new("RGB", (side, side), (0, 0, 0))
    canvas.paste(img.convert("RGB"), ((side - img.width) // 2, (side - img.height) // 2))
    return canvas


_WATCH_INTAKE: dict[str, Any] = {"at": 0.0, "value": {}}


def _watch_intake() -> dict[str, Any]:
    """Intake for the model that will read the frames the loop is publishing (memoized ~10s)."""
    now = time.time()
    if _WATCH_INTAKE["value"] and now - _WATCH_INTAKE["at"] < 10:
        return _WATCH_INTAKE["value"]
    try:
        route = _route()
        value = _vision_intake(
            str(route.get("provider") or ""),
            str(route.get("model") or ""),
            str(route.get("base_url") or ""),
        )
    except Exception:  # noqa: BLE001 — sizing must never stop the watch loop
        value = _vision_intake()
    _WATCH_INTAKE.update(at=now, value=value)
    return value


def _publish_copy(img, intake: dict[str, Any] = None):
    """The copy that travels: fitted to the model's intake, and never larger than a window grab.

    Capture runs at the device's native mode (1080p up to 4K), so this is where a frame becomes what
    the model can take — original aspect ratio kept, black bars only for a square-only encoder.
    Publishing or diffing the raw 4K frame as PNG cost tens of megabytes plus two full-size decodes
    per similarity check; the full-resolution frame is still what a still grab hands out.
    """
    from PIL import Image

    if intake:
        img = _intake_frame(img, intake)
    width, height = getattr(img, "size", (0, 0))[:2]
    if not width or not height or max(width, height) <= _THUMB_MAX_EDGE:
        return img
    scale = _THUMB_MAX_EDGE / float(max(width, height))
    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", None)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return img.resize(size, resampling) if resampling is not None else img.resize(size)


def _png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _similarity(a: bytes, b: bytes) -> float:
    """Percent similarity of two PNGs via a 64x64 grayscale mean-abs-diff."""
    from PIL import Image, ImageChops, ImageStat

    with Image.open(io.BytesIO(a)) as ia, Image.open(io.BytesIO(b)) as ib:
        ga = ia.convert("L").resize((64, 64))
        gb = ib.convert("L").resize((64, 64))
        diff = ImageChops.difference(ga, gb)
        mean = ImageStat.Stat(diff).mean[0]
    return max(0.0, 100.0 - (mean / 255.0 * 100.0))


def _fast_small(img):
    """Cheap box-reduced copy of a frame, for diffing only.

    ``reduce()`` drops whole pixel blocks with no resampling math: 4K -> 480x270 costs a few ms
    where the intake-grade LANCZOS resize of the same frame costs ~80 ms, and a similarity check
    cannot tell the two apart (it compares 64x64 grayscale either way).
    """
    width, height = getattr(img, "size", (0, 0))[:2]
    if not width or not height:
        return img
    factor = max(1, min(width, height) // 270)
    return img.reduce(factor) if factor > 1 else img


def _thumb_bytes(img) -> bytes:
    """64x64 grayscale fingerprint of a frame: everything a similarity check needs."""
    from PIL import Image

    grey = img.convert("L")
    resampling = getattr(getattr(Image, "Resampling", Image), "BILINEAR", None)
    thumb = grey.resize((64, 64), resampling) if resampling is not None else grey.resize((64, 64))
    return thumb.tobytes()


def _similarity_bytes(a: bytes, b: bytes) -> float:
    """Percent similarity of two 64x64 fingerprints.

    Same formula, and therefore the same threshold meaning, as :func:`_similarity` — without
    decoding two full PNGs to get there.
    """
    if not a or len(a) != len(b):
        return 0.0
    total = 0
    for x, y in zip(a, b):
        total += x - y if x > y else y - x
    return max(0.0, 100.0 - (total / len(a) / 255.0 * 100.0))


def _wire_jpeg(image, width: int) -> tuple[bytes, int, int]:
    """Resize a frame for the pane and JPEG-encode it.

    The pane hands this straight to an ``<img>``, so the wire format is ours to pick: JPEG encodes
    ~15x faster than PNG at preview size and carries ~4x fewer bytes through the JSON-RPC hop.
    """
    from PIL import Image

    if image.mode != "RGB":
        image = image.convert("RGB")
    ratio = min(1.0, float(width) / max(1, image.width))
    if ratio < 1.0:
        resampling = getattr(getattr(Image, "Resampling", Image), "BILINEAR", None)
        size = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
        image = image.resize(size, resampling) if resampling is not None else image.resize(size)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=80)
    return buf.getvalue(), image.width, image.height


def _preview_get(seq: int, width: int) -> Optional[dict[str, Any]]:
    """The encoded preview for this exact frame and width, if the pane already asked for it.

    The pane polls /preview every few seconds and the picker asks row by row; on a static screen the
    frame does not change between polls, so the encode is reused instead of repeated.
    """
    with _PREVIEW_LOCK:
        return _PREVIEW_CACHE.get((int(seq), int(width)))


def _preview_put(seq: int, width: int, payload: dict[str, Any]) -> None:
    with _PREVIEW_LOCK:
        for key in [k for k in _PREVIEW_CACHE if k[0] != int(seq)]:  # only the current frame is live
            _PREVIEW_CACHE.pop(key, None)
        _PREVIEW_CACHE[(int(seq), int(width))] = payload


class _NeedsVisionModel(RuntimeError):
    """The active model cannot accept images: the pane must prompt for one that can."""

    def __init__(self, provider: str, model: str, detail: str = "") -> None:
        """Build the user-facing text: what is paused, what it means, and how to resume.

        ``detail`` is the *reason* only — provider error bodies belong in ``self.raw``, not in a
        sentence the user has to read, so it is trimmed to one clause.
        """
        self.provider = provider
        self.model = model
        self.raw = detail
        target = "/".join(part for part in (provider, model) if part) or "the current model"
        reason = " ".join((detail or "").split())
        if len(reason) > 160:
            reason = reason[:157].rstrip() + "..."
        super().__init__(
            f"Live descriptions are paused: {target} cannot process images, so no frame can be "
            f"described. Capture keeps running — switch this chat to a vision-capable model, or "
            f"pin one for descriptions only." + (f" Reason: {reason}" if reason else "")
        )


_CFG_CACHE: dict[str, Any] = {"at": 0.0, "cfg": None}
_CAP_CACHE: dict[tuple[str, str], tuple[float, Optional[bool]]] = {}


def _hermes_cfg(ttl: float = 5.0) -> dict[str, Any]:
    """Hermes' own config, read-only (short TTL: the user can switch models any time)."""
    now = time.time()
    cached = _CFG_CACHE.get("cfg")
    if isinstance(cached, dict) and now - float(_CFG_CACHE.get("at") or 0.0) < ttl:
        return cached
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
    except Exception:
        cfg = {}
    if isinstance(cfg, dict):
        _CFG_CACHE.update({"at": now, "cfg": cfg})
        return cfg
    return {}


def _live_session_model(session_id: str) -> tuple[str, str]:
    """(provider, model) the given LIVE chat session is running right now.

    The user picks a model per conversation, which outranks ``config.yaml``'s
    default: frames must go to the model on screen, not to whatever the config
    happens to name. This runs inside the desktop's own ``serve`` backend, so the
    live session registry is right here — and ``_session_info`` is the same
    resolver the app renders from, so both surfaces agree by construction.
    """
    if not session_id:
        return ("", "")
    try:
        from tui_gateway import server as gw

        with gw._sessions_lock:
            session = gw._sessions.get(session_id)
        if not isinstance(session, dict):
            return ("", "")
        info = gw._session_info(session.get("agent"), session)
        return (str(info.get("provider") or "").strip(), str(info.get("model") or "").strip())
    except Exception:
        return ("", "")


def _active_model(cfg: dict[str, Any], session_id: str = "") -> tuple[str, str, str]:
    """(provider, model, source) Hermes is currently set to.

    ``source`` is ``"session"`` when it came from the live chat session (the model
    the user actually selected) and ``"config"`` when it fell back to the profile
    default in ``config.yaml``.
    """
    provider, model = _live_session_model(session_id)
    if provider and model:
        return (provider, model, "session")
    block = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    return (
        str(block.get("provider") or "").strip(),
        str(block.get("default") or "").strip(),
        "config",
    )


def _specified_vision(cfg: dict[str, Any], provider: str, model: str) -> Optional[bool]:
    """True/False when Hermes knows this model's image support, None when unknown.

    Uses Hermes' own resolver (config overrides → managed local runtime →
    models.dev catalog → Ollama probe → provider profile) so this plugin never
    keeps a competing list of what can see.
    """
    key = (provider, model)
    now = time.time()
    hit = _CAP_CACHE.get(key)
    if hit and now - hit[0] < 30.0:
        return hit[1]
    try:
        from agent.image_routing import _lookup_supports_vision

        verdict: Optional[bool] = _lookup_supports_vision(provider, model, cfg)
        verdict = None if verdict is None else bool(verdict)
    except Exception:
        verdict = None
    _CAP_CACHE[key] = (now, verdict)
    return verdict


def _env_value(name: str) -> str:
    """Secret by env-var name: live environment first, then HERMES_HOME/.env."""
    if not name:
        return ""
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    try:
        for line in (HERMES_HOME / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(rf"\s*(?:export\s+)?{re.escape(name)}\s*=\s*(.*)$", line)
            if match:
                return match.group(1).strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _live_session_endpoint(session_id: str, provider: str) -> tuple[str, str]:
    """(base_url, api_key) of the LIVE session's own client — the endpoint actually in play.

    A session-level provider (``commandcode``, ``opencode``, a ``custom_providers`` entry, an
    OAuth/web route) usually has no ``providers.<name>`` block in ``config.yaml``, so a
    config-only lookup finds no endpoint and reports "no endpoint known". The session's agent
    already carries the endpoint AND key Hermes resolved for it, so ask the agent.
    """
    if not session_id or not provider:
        return ("", "")
    try:
        from tui_gateway import server as gw

        with gw._sessions_lock:
            session = gw._sessions.get(session_id)
        agent = (session or {}).get("agent") if isinstance(session, dict) else None
        if agent is None:
            return ("", "")
        info = gw._session_info(agent, session)
        if str(info.get("provider") or "").strip() != provider:
            return ("", "")  # pinned model on another provider — resolve it independently
        return (
            str(getattr(agent, "base_url", "") or "").strip(),
            str(getattr(agent, "api_key", "") or "").strip(),
        )
    except Exception:
        return ("", "")


def _registry_endpoint(provider: str) -> tuple[str, str]:
    """(base_url, api_key) from Hermes' own provider registry (built-ins, aliases, customs)."""
    if not provider:
        return ("", "")
    try:
        from hermes_cli.providers import resolve_provider_full

        pdef = resolve_provider_full(provider)
        if pdef is None:
            return ("", "")
        base_url = str(getattr(pdef, "base_url", "") or "").strip()
        key = ""
        for env_var in getattr(pdef, "api_key_env_vars", ()) or ():
            key = _env_value(str(env_var))
            if key:
                break
        return (base_url, key)
    except Exception:
        return ("", "")


def _endpoint(cfg: dict[str, Any], provider: str, session_id: str = "") -> tuple[str, str]:
    """(base_url, api_key) for ``provider``; ("", "") when genuinely unknown.

    Layered so ANY provider Hermes can run resolves: the live session's own client, then Hermes'
    provider registry (built-ins, aliases, ``custom_providers``), then ``config.yaml``. The key is
    resolved here and never stored in status payloads.
    """
    base_url, key = _live_session_endpoint(session_id, provider)
    if base_url:
        return (base_url, key)

    base_url, key = _registry_endpoint(provider)
    if base_url:
        return (base_url, key or _env_value(f"{provider.upper().replace('-', '_')}_API_KEY"))

    providers = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    entry = providers.get(provider) if isinstance(providers.get(provider), dict) else {}
    block = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    is_active = provider and provider == str(block.get("provider") or "").strip()

    base_url = str(entry.get("base_url") or "").strip()
    if not base_url and is_active:
        base_url = str(block.get("base_url") or "").strip()

    key = str(entry.get("api_key") or "").strip() or _env_value(str(entry.get("key_env") or "").strip())
    if not key and is_active:
        key = _env_value(str(block.get("key_env") or "").strip())
    if not key:
        key = _env_value(f"{provider.upper().replace('-', '_')}_API_KEY")
    return base_url, key


def _pin() -> dict[str, str]:
    """The user's pinned vision model, or {} when descriptions use the active model."""
    try:
        data = json.loads(OVERRIDE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {"provider": str(data.get("provider") or ""), "model": str(data.get("model") or "")}


def _save_pin(provider: str, model: str) -> dict[str, str]:
    """Pin ``model`` for descriptions; empty model clears the pin."""
    if model:
        payload = {"provider": provider, "model": model}
        _write_json(OVERRIDE_PATH, payload)
        return payload
    try:
        OVERRIDE_PATH.unlink()
    except OSError:
        pass
    return {}


def _normalize_inject_mode(raw: str) -> str:
    """A mode name, case/space-insensitively (hyphens read as underscores); '' when unknown."""
    value = str(raw or "").strip().lower().replace("-", "_")
    return value if value in INJECT_MODES else ""


def _inject_mode_state() -> dict[str, str]:
    """Which mode rides turns, and where that choice comes from.

    The file the pane writes outranks ``PV_VISION_INJECT_MODE``, which outranks the default
    — the same order the agent half applies per turn. ``source`` names the winner so the
    pane can say why instead of guessing.
    """
    try:
        pinned = _normalize_inject_mode(INJECT_MODE_PATH.read_text(encoding="utf-8"))
    except OSError:
        pinned = ""
    if pinned:
        return {"mode": pinned, "source": "pane"}
    from_env = _normalize_inject_mode(os.environ.get(INJECT_MODE_ENV) or "")
    if from_env:
        return {"mode": from_env, "source": "environment"}
    return {"mode": DEFAULT_INJECT_MODE, "source": "default"}


def _save_inject_mode(mode: str) -> None:
    """Persist the pane's pick (a bare mode name); '' clears it back to env/default.

    Plain text, not JSON: the agent half reads this file on every turn and one line IS the
    payload — but it still gets the atomic swap, because a torn read would flip the mode
    for a turn.
    """
    if mode:
        _write_text_atomic(INJECT_MODE_PATH, mode + "\n")
        return
    try:
        INJECT_MODE_PATH.unlink()
    except OSError:
        pass


# How often the watch checks for changes. The pane's pick persists in this file (plain
# one-line text, atomic swap, same reason as the inject-mode pick); 0 means MANUAL —
# nothing is checked on a rhythm and the pane offers two snapshot buttons instead.
INTERVAL_PATH = STATE_DIR / "interval"


def _normalize_interval(raw: Any) -> int:
    """A rhythm in ms the engine can run — 0 (manual) or 500..600000; -1 when invalid."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return -1
    return value if value == 0 or 500 <= value <= 600_000 else -1


def _save_interval(ms: int) -> None:
    """Persist the pane's rhythm pick (a bare integer, ms; 0 = manual)."""
    _write_text_atomic(INTERVAL_PATH, f"{int(ms)}\n")


def _interval_state(running: bool) -> dict[str, Any]:
    """The rhythm the watch checks at, and where that choice comes from (0 = manual)."""
    if running:
        return {"ms": int(getattr(ENGINE, "interval_ms", DEFAULT_INTERVAL_MS)), "source": "engine"}
    try:
        from_file = _normalize_interval(INTERVAL_PATH.read_text(encoding="utf-8").strip())
    except OSError:
        from_file = -1
    if from_file >= 0:
        return {"ms": from_file, "source": "pane"}
    return {"ms": DEFAULT_INTERVAL_MS, "source": "default"}


def _route(session_id: str = "") -> dict[str, Any]:
    """Resolve the model that describes frames.

    The model the CURRENT SESSION is running always wins while it can see — that is
    the point: frames go to whatever model the conversation is on right now. A
    pinned vision model is swapped in only when Hermes knows the session model is
    text-only, and otherwise stays in ``fallback_*`` so a runtime refusal can fall
    back to it exactly once.
    """
    sid = session_id or str(_WATCH_SESSION.get("id") or "")
    cfg = _hermes_cfg()
    active_provider, active_model, active_source = _active_model(cfg, sid)
    active_caps = (
        _specified_vision(cfg, active_provider, active_model)
        if active_provider and active_model
        else None
    )
    pinned = _pin()
    use_pin = bool(pinned.get("model")) and active_caps is False
    provider = pinned.get("provider") if use_pin else active_provider
    model = pinned.get("model") if use_pin else active_model
    return {
        "provider": provider,
        "model": model,
        "source": "pinned" if use_pin else active_source,
        "active_provider": active_provider,
        "active_model": active_model,
        "active_source": active_source,
        "session_id": sid,
        "active_supports_vision": active_caps,
        "supports_vision": (
            _specified_vision(cfg, provider, model) if provider and model else None
        ),
        "fallback_provider": pinned.get("provider", ""),
        "fallback_model": pinned.get("model", ""),
        "base_url": _endpoint(cfg, provider, sid)[0],
    }


def _pin_route() -> Optional[dict[str, Any]]:
    """Route dict for the pinned model, or None when nothing is pinned."""
    pinned = _pin()
    pin_model = pinned.get("model") or ""
    if not pin_model:
        return None
    cfg = _hermes_cfg()
    provider = pinned.get("provider") or _active_model(cfg, str(_WATCH_SESSION.get("id") or ""))[0]
    return {
        "provider": provider,
        "model": pin_model,
        "source": "pinned",
        "supports_vision": _specified_vision(cfg, provider, pin_model),
        "base_url": _endpoint(cfg, provider)[0],
    }


def _looks_multimodal(name: str) -> bool:
    lowered = (name or "").lower()
    return any(hint in lowered for hint in VISION_NAME_HINTS)


def _vision_candidates(cfg: dict[str, Any], route: dict[str, Any]) -> list[dict[str, str]]:
    """Vision-capable models the user can pin, the currently active one first.

    Candidates come from configured providers: models declaring
    ``supports_vision: true``, then models whose name suggests image input and
    that Hermes does not report as text-only.
    """
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(provider: str, model: str, label: str = "") -> None:
        if not provider or not model or (provider, model) in seen or len(out) >= 40:
            return
        seen.add((provider, model))
        out.append({"provider": provider, "model": model, "label": label or f"{provider} · {model}"})

    active_provider = str(route.get("active_provider") or "")
    active_model = str(route.get("active_model") or "")
    where = "this session" if route.get("active_source") == "session" else "config default"
    if route.get("supports_vision") is not False:
        add(active_provider, active_model, f"{active_provider} · {active_model} ({where})")

    verified: list[tuple[str, str]] = []
    suggested: list[tuple[str, str]] = []
    providers = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    for provider, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        models = entry.get("models")
        undecided: list[str] = []
        if isinstance(models, dict):
            for name, meta in models.items():
                declared = meta.get("supports_vision") if isinstance(meta, dict) else None
                if declared is True:
                    verified.append((str(provider), str(name)))
                elif declared is None:
                    undecided.append(str(name))
        elif isinstance(models, list):
            undecided = [str(name) for name in models]
        for name in undecided:
            if _looks_multimodal(name) and _specified_vision(cfg, str(provider), name) is not False:
                suggested.append((str(provider), name))
    for provider, model in verified:
        add(provider, model)
    for provider, model in suggested:
        add(provider, model)
    return out


def _jpeg_b64(png: bytes, max_width: int = None, intake: dict[str, Any] = None) -> str:
    """Re-encode a grab for the wire: fit the model's intake, JPEG, base64 (no data: prefix)."""
    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        image = image.convert("RGB")
        if intake:
            image = _intake_frame(image, intake)
        else:
            limit = max_width or VISION_MAX_WIDTH
            if limit and image.width > limit:
                image = image.resize((limit, max(1, round(image.height * limit / image.width))))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=80)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _message_text(payload: dict[str, Any]) -> str:
    """Plain text out of an OpenAI-style completion (handles part lists)."""
    choices = payload.get("choices") or []
    if not choices:
        return ""
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):  # some gateways return content parts
        return " ".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("text")
        ).strip()
    return ""


def _rejects_images(status: int, detail: str) -> bool:
    """True when the endpoint is refusing image input (capability) rather than failing transiently."""
    text = (detail or "").lower()
    mentions_image = any(
        word in text
        for word in ("image", "vision", "multimodal", "content part", "content type", "media", "modality")
    )
    if not mentions_image:
        return False
    return status in (400, 404, 415, 422) or any(
        word in text for word in ("not support", "unsupported", "does not support", "invalid")
    )


class _UpstreamError(RuntimeError):
    """An HTTP error from the model endpoint, with status code and body preserved."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def _affinity_headers(provider: str, base_url: str, session_id: str) -> dict[str, str]:
    """Hermes' own session-affinity headers (``x-opencode-session``, custom affinity headers).

    OpenCode Zen/Go rejects requests without it (HTTP 400 MissingSessionID), so a plugin that
    talks to the model directly must send exactly what the turn sends — reuse the core helper.
    """
    try:
        from agent.opencode_affinity import merge_session_affinity_headers

        kwargs: dict[str, Any] = {}
        merge_session_affinity_headers(kwargs, provider, base_url, session_id or None)
        return kwargs.get("extra_headers") or {}
    except Exception:
        return {}


def _client_user_agent() -> str:
    """The UA the live transport sends — some relays ban the default Python UA (Cloudflare 1010)."""
    try:
        import openai

        return f"OpenAI/Python {openai.__version__}"
    except Exception:
        return "HermesAgent/1.0"


def _post_chat(base_url: str, key: str, model: str, messages: list[Any], headers: dict[str, str]) -> str:
    """One chat completion, sent the way Hermes itself sends it.

    Uses the OpenAI SDK (same transport, TLS fingerprint, UA and affinity headers as a real turn)
    when available; a plain request with matching headers otherwise.
    """
    try:
        from openai import OpenAI
    except Exception:
        OpenAI = None  # type: ignore[assignment]

    if OpenAI is not None:
        client = OpenAI(base_url=base_url, api_key=key or "unused", timeout=VISION_TIMEOUT_S, max_retries=0)
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, max_tokens=400, extra_headers=headers or None
            )
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if isinstance(status, int):
                body = ""
                try:
                    body = (exc.response.text or "")[:300]
                except Exception:
                    body = str(exc)[:300]
                raise _UpstreamError(status, body) from exc
            raise
        return _message_text(resp.model_dump())

    import urllib.error
    import urllib.request

    payload = {"model": model, "max_tokens": 400, "messages": messages}
    request_headers = {"Content-Type": "application/json", "User-Agent": _client_user_agent()}
    request_headers.update(headers or {})
    if key:
        request_headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        f"{base_url}/chat/completions", data=json.dumps(payload).encode("utf-8"), method="POST", headers=request_headers
    )
    try:
        with urllib.request.urlopen(request, timeout=VISION_TIMEOUT_S) as response:
            return _message_text(json.loads(response.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        try:
            detail = (exc.read().decode("utf-8", "replace") or "")[:300].strip()
        except Exception:
            detail = ""
        raise _UpstreamError(exc.code, detail or str(exc.reason)) from exc


def _describe_with_model(png: bytes, route: dict[str, Any]) -> str:
    """One image-in/text-out call to the resolved model's own endpoint."""
    base_url = (route.get("base_url") or "").rstrip("/")
    if not base_url:
        raise RuntimeError(
            f"no endpoint known for provider {route.get('provider')!r} — tried the live session, "
            "Hermes' provider registry and providers.<name>.base_url in config"
        )
    _, key = _endpoint(_hermes_cfg(), route["provider"], str(route.get("session_id") or ""))
    label = f"{route.get('provider')}/{route.get('model')}"
    headers = _affinity_headers(
        str(route.get("provider") or ""), base_url, str(route.get("session_id") or "")
    )
    prompt = VISION_PROMPT
    if _grab_waiting():
        # The watched window is minimized: this is its frozen last frame, and the description
        # should say so rather than implying the user is looking at a live screen.
        prompt = (
            f"{prompt}\n\nThe watched window is currently minimized, so this image is its last "
            "rendered frame, not a live view — say that it is minimized."
        )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/jpeg;base64,"
                        + _jpeg_b64(
                            png,
                            intake=_vision_intake(
                                str(route.get("provider") or ""),
                                str(route.get("model") or ""),
                                base_url,
                            ),
                        )
                    },
                },
            ],
        }
    ]

    last_exc: Optional[Exception] = None
    for attempt in range(max(1, VISION_ATTEMPTS)):
        try:
            text = _post_chat(base_url, key, str(route.get("model") or ""), messages, headers)
            if text:
                return text
            last_exc = RuntimeError(f"{label} returned no text")
        except _UpstreamError as exc:
            if _rejects_images(exc.status, exc.detail):
                raise _NeedsVisionModel(
                    route.get("provider", ""),
                    route.get("model", ""),
                    f"the endpoint refused image input (HTTP {exc.status})",
                ) from exc
            last_exc = RuntimeError(f"{label} HTTP {exc.status}: {exc.detail}")
        except Exception as exc:  # 504/429/5xx and network blips are worth one more try
            last_exc = exc
        if attempt + 1 < max(1, VISION_ATTEMPTS):
            time.sleep(1.5)
    raise last_exc if last_exc else RuntimeError(f"{label}: no response")


def _describe(png: bytes) -> str:
    """Describe a frame with the active model, falling back to the pinned one once.

    Raises :class:`_NeedsVisionModel` when nothing available can accept images, so the
    caller surfaces a prompt instead of quietly using someone else's endpoint.
    """
    global _LAST_DESCRIBER
    route = _route()
    if not route.get("model"):
        raise RuntimeError("no model configured — set one in Hermes (model.default)")
    if route.get("supports_vision") is False:
        raise _NeedsVisionModel(
            route.get("provider", ""), route.get("model", ""), "the model is listed as text-only for image input"
        )
    try:
        text = _describe_with_model(png, route)
        label = f"{route.get('provider')}/{route.get('model')}"
    except _NeedsVisionModel:
        # The active model refused the image at runtime: the pinned model gets one shot.
        pin_route = None if route.get("source") == "pinned" else _pin_route()
        if pin_route is None:
            raise
        text = _describe_with_model(png, pin_route)  # may refuse too -> prompt
        label = f"{pin_route.get('provider')}/{pin_route.get('model')}"
    _LAST_DESCRIBER = label
    return text

def _consume_stop_request() -> bool:
    """True once per outstanding stop request, consuming it.

    The request is a file because the half that raises it (``__init__.py``) lives in another
    process; see ``STOP_REQUEST_PATH``."""
    try:
        STOP_REQUEST_PATH.unlink()
    except FileNotFoundError:
        return False
    except OSError:  # pragma: no cover - a locked file just defers the stop to the next tick
        return False
    return True


def _write_text_atomic(path: Path, text: str) -> None:
    """Write a state file atomically, with a per-writer temp name.

    A fixed ``<name>.tmp`` is a collision: two writers (the engine thread plus a probe, a CLI call or
    a second start) both target it, one gets PermissionError and its tick dies. The temp name carries
    pid + a random suffix, so writers only ever contend on the final atomic swap.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        for attempt in range(6):  # Windows: a reader holding the target can briefly refuse the swap
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.005 * (attempt + 1))  # ~75ms of slack, then surface the failure
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically — see ``_write_text_atomic`` for the collision story."""
    _write_text_atomic(path, json.dumps(data, indent=1))


def _append_log(entry: dict[str, Any]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_LOCK:
        lines: list[str] = []
        if LOG_PATH.exists():
            try:
                lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
        lines.append(json.dumps(entry, ensure_ascii=False))
        LOG_PATH.write_text("\n".join(lines[-LOG_KEEP:]) + "\n", encoding="utf-8")


# ── Engine ────────────────────────────────────────────────────────────────
class _Engine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.monitor: Optional[dict[str, Any]] = None
        self.interval_ms = DEFAULT_INTERVAL_MS
        self.threshold = DEFAULT_THRESHOLD
        self.count = 0
        self.skipped = 0
        self.last_description = ""
        self.last_at: Optional[float] = None
        self.last_seconds: Optional[float] = None
        self.last_error = ""
        self.needs_vision = False  # the active model cannot see images: the pane must prompt
        self.vision_error = ""
        self.source_failures = 0  # consecutive capture failures on the chosen source
        self.source_missing = False  # the chosen window is closed/gone
        self.waiting = ""  # chosen window is minimized/covered: frames resume by themselves
        self.grab_method = ""  # "screen" (on top) or "printwindow" (background)
        self.started_at: Optional[float] = None
        self.last_png: Optional[bytes] = None
        self.frame_seq = 0  # bumps whenever last_png is replaced: the /preview cache identity
        self._last_thumb: Optional[bytes] = None  # 64x64 fingerprint of the previous frame
        self._published_at = 0.0  # when last_png was last refreshed
        self.heartbeat_at: Optional[float] = None  # refreshed every tick: see _publish

    # -- status -----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        running = self._thread is not None and self._thread.is_alive()
        route = _route()
        return {
            "running": running,
            "heartbeat_at": self.heartbeat_at,
            "monitor": self.monitor,
            "source": self.monitor,
            "source_kind": (self.monitor or {}).get("kind") or "monitor",
            "source_label": (self.monitor or {}).get("label"),
            "source_missing": bool(self.source_missing),
            "waiting": self.waiting or None,
            "grab_method": self.grab_method or None,
            "interval_ms": self.interval_ms,
            "threshold": self.threshold,
            "count": self.count,
            "skipped": self.skipped,
            "last_description": self.last_description,
            "last_at": self.last_at,
            "last_seconds": self.last_seconds,
            "describer": _LAST_DESCRIBER or None,
            "vision": {
                "provider": route.get("provider"),
                "model": route.get("model"),
                "source": route.get("source"),
                "active_provider": route.get("active_provider"),
                "active_model": route.get("active_model"),
                "active_source": route.get("active_source"),
                "session_id": route.get("session_id"),
                "active_supports_vision": route.get("active_supports_vision"),
                "supports_vision": route.get("supports_vision"),
                "fallback_model": route.get("fallback_model"),
                "needs_vision_model": bool(self.needs_vision),
                "detail": self.vision_error,
            },
            "started_at": self.started_at,
            "error": self.last_error,
            "recent": _recent(12),
            "log_path": str(LOG_PATH),
        }

    # -- lifecycle --------------------------------------------------------
    def start(
        self, source: dict[str, Any], interval_ms: int, threshold: float, session_id: str = ""
    ) -> dict[str, Any]:
        """Begin watching ``source`` — a display, an application window, or a camera.

        ``session_id`` is the live chat the pane is looking at: frames are described
        by THAT session's model, not by the profile default in ``config.yaml``.
        """
        # A snapshot session holding another camera would fight this watch over the
        # module's single _CAM_HANDLE; end it first (an open overlay reports the
        # session expired on its next frame). Backends without the feature: no-op.
        _snap_reset()
        self.stop()
        _WATCH_SESSION["id"] = str(session_id or "")
        route = _route()
        _append_log(
            {
                "ts": time.time(),
                "event": "watch_start",
                "source": source.get("id"),
                "source_kind": source.get("kind"),
                "session_id": route.get("session_id"),
                "provider": route.get("provider"),
                "model": route.get("model"),
                "model_source": route.get("source"),
                "active_source": route.get("active_source"),
            }
        )
        self._stop = threading.Event()
        with self._lock:
            self.monitor = source
            requested = int(interval_ms)
            # 0 = manual: the loop idles (heartbeat only) and the pane's snapshot
            # buttons are the capture path; anything else is a checking rhythm.
            self.interval_ms = max(500, requested) if requested > 0 else 0
            self.threshold = min(100.0, max(0.0, float(threshold)))
            self.count = 0
            self.skipped = 0
            self.last_error = ""
            self.last_description = ""
            self.last_at = None
            self.last_seconds = None
            self.last_png = None
            self.frame_seq = 0
            self._last_thumb = None
            self._published_at = 0.0
            self.needs_vision = False
            self.vision_error = ""
            self.source_failures = 0
            self.source_missing = False
            self.grab_method = ""
            self.started_at = time.time()
        self._thread = threading.Thread(target=self._run_guarded, name="peripheral-vision", daemon=True)
        self._thread.start()
        _write_json(STATUS_PATH, {**self.status(), "v": 1})
        return self.status()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        # Let the device go before joining: the running read fails fast, the LED goes off promptly,
        # and the join is not left waiting on a native call that can take seconds.
        release_camera()  # a watched camera must not keep its LED on
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        self._thread = None
        self.started_at = None
        release_camera()  # in case the loop re-opened it between the release and the join
        _write_json(STATUS_PATH, {**self.status(), "v": 1})
        return self.status()

    def set_interval(self, interval_ms: int) -> int:
        """Apply a rhythm (0 = manual) to a running loop; the route persists it.

        The loop re-reads ``interval_ms`` every iteration and _run's manual branch
        takes over the moment it reaches 0, so a pick lands within one tick — no
        restart of anything, and harmless when this is called while stopped.
        """
        requested = int(interval_ms)
        with self._lock:
            self.interval_ms = max(500, requested) if requested > 0 else 0
        self._publish()
        return self.interval_ms

    # -- loop -------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            if _consume_stop_request():
                # The plugin can be unloaded in the OTHER process (its agent half), which
                # cannot reach this engine and cannot stop it directly. Honoring the request
                # here is what actually ends the watch and releases the camera.
                self.last_error = "stopped: the plugin was unloaded"
                _append_log({"ts": time.time(), "event": "stop_requested"})
                self._stop.set()
                break
            if self.interval_ms <= 0:
                # Manual: no checking rhythm — the pane's snapshot buttons capture on
                # demand. The loop breathes so /status stays live (heartbeat) and the
                # unload request above is never missed.
                if self.heartbeat_at is None or (time.time() - self.heartbeat_at) >= 2.0:
                    self._publish()
                self._stop.wait(0.25)
                continue
            started = time.time()
            try:
                try:
                    img = _grab(self.monitor or {})
                except _SourceMinimized as exc:  # waiting on you, not failing: keep the watch alive
                    self.waiting = str(exc)[:300]
                    self.last_error = self.waiting
                    self.source_failures = 0
                    self._publish()
                    self._sleep(started)
                    continue
                except Exception as exc:  # the source itself: report, retry, never substitute
                    self._on_source_failure(exc)
                    if self._stop.is_set():
                        break
                    self._publish()
                    self._sleep(started)
                    continue
                self.source_failures = 0
                self.source_missing = False
                self.waiting = _grab_waiting()  # e.g. "minimized — showing its last frame"
                self.grab_method = _grab_method() or self.grab_method
                # Diff on a 64x64 fingerprint of a cheap box-reduced copy. The intake-grade resize
                # plus PNG encode below costs ~100 ms and most ticks change nothing, so they are paid
                # only when a frame actually moves — or when the preview copy has gone stale.
                thumb = _thumb_bytes(_fast_small(img))
                previous_thumb = self._last_thumb
                self._last_thumb = thumb
                unchanged = previous_thumb is not None and (
                    _similarity_bytes(previous_thumb, thumb) >= self.threshold
                )
                stale = (
                    self.last_png is None
                    or (time.time() - self._published_at) >= PREVIEW_MAX_AGE_S
                )
                if unchanged and not stale:
                    self.skipped += 1
                    self._publish()
                    self._sleep(started)
                    continue
                png = _png_bytes(_publish_copy(img, _watch_intake()))
                self.last_png = png
                self.frame_seq += 1
                self._published_at = time.time()
                if unchanged:  # preview refresh only: nothing new to describe
                    self._publish()
                    self._sleep(started)
                    continue
                description = _describe(png)
                self.count += 1
                self.last_description = description
                self.last_at = time.time()
                self.last_seconds = round(self.last_at - started, 1)
                self.last_error = ""
                self.needs_vision = False
                self.vision_error = ""
                _append_log(
                    {
                        "ts": self.last_at,
                        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.last_at)),
                        "source": (self.monitor or {}).get("label"),
                        "seconds": self.last_seconds,
                        "describer": _LAST_DESCRIBER or None,
                        "description": description,
                    }
                )
            except _NeedsVisionModel as exc:  # capability, not a transient failure: prompt
                self.needs_vision = True
                self.vision_error = str(exc)[:400]
                self.last_error = str(exc)[:400]
            except Exception as exc:  # keep the loop alive, surface the reason
                self.needs_vision = False
                self.vision_error = ""
                self.last_error = f"{type(exc).__name__}: {exc}"[:400]
            self._publish()
            self._sleep(started)

        # The loop is over — stop event, unload request, or repeated capture failure. Releasing the
        # source and publishing the TERMINAL state belongs to _run_guarded, which does it on every
        # exit path, including one this loop cannot catch.

    def _run_guarded(self) -> None:
        """Run the loop, and publish the terminal state however it ends — including a crash.

        The agent half runs in a different process and gates context injection on
        status["running"], so a loop that dies unexpectedly must not leave `running: true` behind:
        before this wrapper existed, a crash left the last live reading on disk and the flag with
        it. Writing `running: false` plus a final heartbeat covers every exit — clean stop, unload
        request, and the loop dying on an exception it did not catch. Readers still verify the
        heartbeat (see STATUS_HEARTBEAT_MAX_AGE_S); this is the writer's half of that one rule.
        """
        try:
            self._run()
        except BaseException as exc:  # incl. SystemExit: the file must not outlive the loop
            self.last_error = f"{type(exc).__name__}: {exc}"[:400]
            import traceback  # noqa: PLC0415 - only needed on the crash path

            _append_log(
                {
                    "ts": time.time(),
                    "event": "loop_crashed",
                    "error": self.last_error,
                    "trace": traceback.format_exc()[:800],
                }
            )
        finally:
            try:
                release_camera()
            except Exception:  # best-effort: releasing must never eat the terminal write
                pass
            self.heartbeat_at = time.time()
            _write_json(STATUS_PATH, {**self.status(), "running": False, "v": 1})

    def _on_source_failure(self, exc: Exception) -> None:
        """A capture failed. Retry a few times, then stop with the reason (no substitution)."""
        self.source_failures += 1
        self.source_missing = isinstance(exc, _SourceGone)
        detail = f"{type(exc).__name__}: {exc}"[:380]
        if self.source_failures >= SOURCE_FAILURE_LIMIT:
            self.last_error = (
                f"stopped after {self.source_failures} failed captures — {detail}"
            )[:400]
            self._stop.set()
        else:
            self.last_error = detail

    def _sleep(self, started: float) -> None:
        elapsed = (time.time() - started) * 1000
        self._stop.wait(max(0.1, (self.interval_ms - elapsed) / 1000.0))

    def _publish(self) -> None:
        # Every tick refreshes the heartbeat, including ticks that describe nothing (unchanged
        # frame, failing source, minimized window). Liveness is the one thing this file must never
        # lie about — see reconcile_stale_status() and STATUS_HEARTBEAT_MAX_AGE_S.
        self.heartbeat_at = time.time()
        _write_json(STATUS_PATH, {**self.status(), "v": 1})


def _recent(limit: int = 12) -> list[dict[str, Any]]:
    with _LOG_LOCK:
        try:
            raw = LOG_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return []
    out: list[dict[str, Any]] = []
    for line in raw:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return list(reversed(out))


ENGINE = _Engine()


# ── Manual snapshots ─────────────────────────────────────────────────────
# The pane's "manual" rhythm grows two snapshot buttons: one hands a camera feed
# to a drag-crop overlay, the other any display/window. A camera session keeps live
# frames coming; a display/window session captures once at full resolution and crops
# THAT still (WYSIWYG). /snap saves the crop as a PNG AND returns it base64, and the
# pane stages it into the chat input through the app's own paste route — the whole
# feature is plugin-side (no desktop change, no clipboard takeover).
SNAP_TTL_S = 120.0  # an idle session this long is gone (its camera must not stay held)
SNAP_PREVIEW_WIDTH = 960  # the overlay's feed; the SNAP itself is full resolution
SNAP_DIR = STATE_DIR / "snaps"

_SNAP: dict[str, Any] = {
    "id": "",
    "source": None,
    "at": 0.0,
    "opened_camera": False,
    "frames": 0,
    "still": None,  # a display/window session's full-resolution capture — the crop base
    "still_payload": None,  # its encoded wire form, replayed on every overlay poll
}


def _snap_reset(release: bool = True) -> None:
    """End any live snapshot session; release the camera only when IT opened the device.

    The shared _CAM_HANDLE may belong to the watch (same camera — reads serialize
    under _CAM_LOCK); releasing that would blind the watch mid-frame, so the release
    is conditional on this session having opened the device itself.
    """
    held = bool(_SNAP.get("opened_camera"))
    _SNAP.update(
        {
            "id": "",
            "source": None,
            "at": 0.0,
            "opened_camera": False,
            "frames": 0,
            "still": None,
            "still_payload": None,
        }
    )
    if release and held:
        release_camera()


def _reap_snap_if_stale() -> None:
    """Stop holding a camera for a pane that went away (its /snap/stop never came).

    Woken by the pane/chip poll: a killed overlay leaves no other trace, and an
    unreleased camera means the LED stays on. Runs on a thread.
    """
    if _SNAP.get("id") and (time.time() - float(_SNAP.get("at") or 0)) > SNAP_TTL_S + 15:
        _snap_reset()


def _snap_camera_busy_reason(source: dict[str, Any]) -> str:
    """Why a snapshot may not open this camera right now ('' = allowed).

    There is ONE _CAM_HANDLE: while the watch holds a DIFFERENT camera, a snapshot
    that opened this one would swap the handle back and forth every frame (each swap
    is a device reset). Sharing the SAME camera is safe — every read is inside
    _CAM_LOCK.
    """
    thread = getattr(ENGINE, "_thread", None)
    if thread is None or not thread.is_alive():
        return ""
    watched = ENGINE.monitor or {}
    if watched.get("kind") != "camera":
        return ""
    if int(watched.get("index") or 0) == int(source.get("index") or 0):
        return ""
    return (
        f"camera {source.get('index')} cannot be opened while camera {watched.get('index')} "
        "is being watched — snapshot from the watched camera, or stop the watch"
    )


def _snap_source_summary(source: dict[str, Any]) -> dict[str, Any]:
    """The bits of a source the pane's overlay needs (label for the dialog title)."""
    return {
        "id": source.get("id"),
        "label": source.get("label"),
        "kind": source.get("kind"),
        "width": source.get("width"),
        "height": source.get("height"),
    }


def _snap_kind(source: dict[str, Any]) -> str:
    """A source's kind, with the same fallbacks _grab uses for sources that predate ``kind``."""
    return source.get("kind") or ("window" if source.get("hwnd") else "monitor")


def _snap_payload_for(img: Any, still: bool = False) -> dict[str, Any]:
    """The wire form of one overlay frame: a JPEG preview of ``img``."""
    shot, out_w, out_h = _wire_jpeg(img, SNAP_PREVIEW_WIDTH)
    payload = {
        "ok": True,
        "width": out_w,
        "height": out_h,
        "method": _grab_method() or None,
        "waiting": _grab_waiting() or None,
        "data_url": "data:image/jpeg;base64," + base64.b64encode(shot).decode("ascii"),
    }
    if still:
        payload["still"] = True
        payload["captured_at"] = time.time()
    return payload


def _snap_still_payload() -> dict[str, Any]:
    """The stored still's wire form, encoded once and replayed on every poll."""
    payload = _SNAP.get("still_payload")
    if not isinstance(payload, dict):
        payload = _snap_payload_for(_SNAP.get("still"), still=True)
        _SNAP["still_payload"] = payload
    return payload


def _snap_frame_payload() -> dict[str, Any]:
    """One frame of the active session for the crop overlay (thread caller).

    A camera keeps a LIVE feed — every poll is a fresh frame, because timing the shot is
    the point. A display or window captures ONCE at full resolution and hands the SAME
    still back on every poll: the crop base is exactly the image the selection was drawn
    on, never a fresh grab that drifted from it.
    """
    if not _SNAP.get("id"):
        return {"ok": False, "error": "no snapshot session — reopen the snapshot"}
    source = _SNAP.get("source") or {}
    _SNAP["at"] = time.time()  # the idle TTL counts from the last frame the pane asked for
    kind = _snap_kind(source)
    if kind != "camera" and _SNAP.get("still") is not None:
        return _snap_still_payload()  # already captured: same bytes, no device work
    had_camera = _CAM_HANDLE.get("cap") is not None
    try:
        img = _grab(source)
    except (_SourceGone, _SourceMinimized) as exc:  # waiting states, not failures
        return {"ok": False, "error": str(exc)[:300], "waiting": True}
    except Exception as exc:  # the pane shows the reason verbatim
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    if source.get("kind") == "camera" and not had_camera and _CAM_HANDLE.get("cap") is not None:
        _SNAP["opened_camera"] = True  # this session opened the device: it must release it
    _SNAP["frames"] = int(_SNAP.get("frames") or 0) + 1
    if kind != "camera":
        # The display/window still IS the session's frame: full resolution, kept for the crop.
        _SNAP["still"] = img
        payload = _snap_payload_for(img, still=True)
        _SNAP["still_payload"] = payload
        return payload
    return _snap_payload_for(img, still=False)


def _crop_normalized(img: Any, rect: Any) -> Any:
    """Crop ``img`` by the pane's selection — {x,y,w,h} as fractions of the frame.

    Anything unusable (missing rect, degenerate size) crops nothing: the whole frame
    is the sane default, never a crash.
    """
    if not isinstance(rect, dict):
        return img
    width, height = img.size[:2]
    if not width or not height:
        return img
    try:
        x = min(max(0.0, float(rect.get("x"))), 1.0)
        y = min(max(0.0, float(rect.get("y"))), 1.0)
        w = min(max(0.0, float(rect.get("w"))), 1.0)
        h = min(max(0.0, float(rect.get("h"))), 1.0)
    except (TypeError, ValueError):
        return img
    if w < 0.005 or h < 0.005:
        return img
    left = min(int(round(x * width)), width - 1)
    top = min(int(round(y * height)), height - 1)
    right = max(left + 1, min(int(round((x + w) * width)), width))
    bottom = max(top + 1, min(int(round((y + h) * height)), height))
    return img.crop((left, top, right, bottom))


def _snap_save(rect: Any) -> dict[str, Any]:
    """The session's capture, cropped by ``rect``, saved as PNG (thread caller).

    A camera hands out a fresh full-resolution frame (it is live by design). A display or
    window crops the session's stored still — the exact full-resolution image the overlay
    showed — so the saved crop can never point at pixels the user never saw. The base64
    twin rides back to the pane so it can stage the image into the chat input; the file on
    disk is the same pixels, there for dragging in or keeping.
    """
    source = _SNAP.get("source") or {}
    img = _SNAP.get("still") if _snap_kind(source) != "camera" else None
    if img is None:
        img = _grab(source)
    img = _crop_normalized(img, rect)
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"snap-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}.png"
    path = SNAP_DIR / name
    data = _png_bytes(img)
    tmp = path.with_name(f"{name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return {
        "ok": True,
        "name": name,
        "path": str(path),
        "width": img.size[0],
        "height": img.size[1],
        "bytes": len(data),
        "png_b64": base64.b64encode(data).decode("ascii"),
    }


# ── Routes ────────────────────────────────────────────────────────────────
@router.get("/monitors")
async def get_monitors() -> dict[str, Any]:
    """Compatibility route: displays plus application windows."""
    return await get_sources()


@router.get("/sources")
async def get_sources(refresh: bool = False) -> dict[str, Any]:
    """Every selectable source: displays, application windows and cameras.

    Enumeration touches Win32 and camera drivers, so it runs off the event loop and
    a cold camera probe continues in the background instead of holding the request.
    """
    if refresh:
        await asyncio.to_thread(list_cameras, True)
    sources = await asyncio.to_thread(list_sources)
    return {
        **sources,
        "requires_selection": True,
        "cameras_probing": cameras_probing(),
        "refreshed_at": time.time(),
    }


def reconcile_stale_status() -> Optional[dict[str, Any]]:
    """Correct a status file left behind by an engine that is gone.

    A killed backend never reaches the terminal write at the end of ``_run``, so its last reading
    keeps claiming ``running: true`` — and the plugin's agent half runs in ANOTHER process and has
    only this file to go on. This is the backend's half of the rule: when it is woken (the pane's
    /status poll), a claim whose heartbeat has gone cold is rewritten as stopped. A live watch
    refreshes its heartbeat every tick, so this can never clobber a running one.
    """
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("running"):
        return None
    beat = data.get("heartbeat_at")
    if beat is None:  # a writer too old to heartbeat: the file's own mtime is the next evidence
        try:
            beat = STATUS_PATH.stat().st_mtime
        except OSError:
            beat = None
    if beat is not None:
        try:
            if (time.time() - float(beat)) <= STATUS_HEARTBEAT_MAX_AGE_S:
                return None
        except (TypeError, ValueError):
            pass  # an unreadable heartbeat is not evidence of life: fall through and correct it
    data.update(
        {
            "running": False,
            "stale": True,
            "heartbeat_at": beat,
            "error": data.get("error") or "the capture loop is gone (its heartbeat went cold)",
        }
    )
    _write_json(STATUS_PATH, {**data, "v": 1})
    _append_log({"ts": time.time(), "event": "stale_status_corrected"})
    return data


@router.get("/status")
async def get_status() -> dict[str, Any]:
    """Engine state, the intake, the inject-mode pick, and the checking rhythm."""
    engine_status = ENGINE.status()
    # A snapshot request is also the moment to heal a file left by a backend that died: in-process
    # `running` is authoritative here, but whatever reads the file from outside is not.
    if not engine_status.get("running"):
        reconcile_stale_status()
    # The snapshot session's janitor: a killed overlay leaves no /snap/stop, and an
    # unreleased camera means the LED stays on — this poll is the waking call.
    await asyncio.to_thread(_reap_snap_if_stale)
    return {
        **engine_status,
        "intake": _watch_intake(),
        "inject_mode": _inject_mode_state(),
        "interval": _interval_state(engine_status.get("running")),
        "snap": True,
    }


@router.get("/vision")
async def get_vision() -> dict[str, Any]:
    """Which model describes frames, whether it can see, and what else could."""
    cfg = _hermes_cfg()
    route = _route()
    return {
        "route": route,
        "needs_vision_model": bool(ENGINE.needs_vision) or route.get("supports_vision") is False,
        "detail": ENGINE.vision_error,
        "candidates": _vision_candidates(cfg, route),
    }


@router.post("/vision")
async def post_vision(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Pin the model used for descriptions; an empty ``model`` clears the pin."""
    provider = str(payload.get("provider") or "").strip()
    model = str(payload.get("model") or "").strip()
    if model and not provider:
        provider = _active_model(_hermes_cfg(), str(_WATCH_SESSION.get("id") or ""))[0]
    _save_pin(provider if model else "", model)
    if model:
        ENGINE.needs_vision = False
        ENGINE.vision_error = ""
        ENGINE.last_error = ""
    return {"ok": True, "vision": await get_vision()}


@router.post("/start")
async def post_start(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Start the loop. ``source_id`` is mandatory: this never picks a default."""
    source_id = str(payload.get("source_id") or payload.get("monitor_id") or "").strip()
    if not source_id:
        return {
            "ok": False,
            "error": "source_id is required — pick a display or app explicitly (the picker must prompt).",
        }
    # A pick resolves without enumerating: a camera id carries its own index (and displays are cheap
    # ctypes). Probing here opened the very devices the engine was about to need — seconds per pick —
    # and a flaky probe answered "unknown source_id" for a webcam that was plainly there, so the pane
    # retried until the desktop app's 30s RPC timeout fired: the reported "massive lag switching to
    # the camera". Windows still need the full listing (Win32 enumeration), so that stays on a thread.
    chosen = _source_from_id(source_id)
    sources = None
    if chosen is None and not source_id.startswith(("camera-", "default-camera")):
        sources = await asyncio.to_thread(list_sources)
        available = sources["monitors"] + sources["windows"] + sources["cameras"]
        chosen = next((s for s in available if s["id"] == source_id), None)
        if chosen is None and source_id.isdigit():
            idx = int(source_id)
            chosen = next((s for s in available if s.get("index") == idx), None)
    if chosen is None:
        if sources is None:
            sources = await asyncio.to_thread(list_sources)
        return {"ok": False, "error": f"unknown source_id {source_id!r}", "sources": sources}
    if (chosen.get("kind") or "") == "camera":
        abort_camera_probe()  # free the device before the watch takes it
    interval = int(payload.get("interval_ms") or DEFAULT_INTERVAL_MS)
    threshold = float(payload.get("threshold") or DEFAULT_THRESHOLD)
    session_id = str(payload.get("session_id") or payload.get("session") or "").strip()
    # Starting a watch can join the previous watch thread, which may be in the middle of a native
    # camera read — that join must never happen on the event loop, or the pane freezes and the app
    # restarts the backend.
    status = await asyncio.to_thread(ENGINE.start, chosen, interval, threshold, session_id)
    return {"ok": True, "status": status}


@router.post("/stop")
async def post_stop() -> dict[str, Any]:
    # Same reason as /start: stopping joins the watch thread and releases the camera device.
    return {"ok": True, "status": await asyncio.to_thread(ENGINE.stop)}


@router.post("/inject_mode")
async def post_inject_mode(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Pick when fresh readings ride turns; an empty ``mode`` clears the pane's pick.

    The pick is written where the agent half reads it on its next turn, so it applies
    without restarting anything. A value that is not one of the modes is refused rather
    than stored — a typo must not silently configure nothing.
    """
    requested = str(payload.get("mode") or "")
    mode = _normalize_inject_mode(requested)
    if requested.strip() and not mode:
        return {"ok": False, "error": f"mode must be one of {', '.join(INJECT_MODES)}"}
    try:
        _save_inject_mode(mode)
    except OSError as exc:
        return {"ok": False, "error": f"could not persist the inject mode: {exc}"}
    return {"ok": True, "inject_mode": _inject_mode_state()}


@router.post("/interval")
async def post_interval(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Pick how often the watch checks for changes; 0 is manual (snapshot only).

    Persisted for the next watch and applied to a running one within a tick — the
    loop re-reads ``interval_ms`` every iteration. Manual stops the checking rhythm
    but not the watch: the pane's snapshot buttons capture on demand.
    """
    ms = _normalize_interval(payload.get("interval_ms"))
    if ms < 0:
        return {"ok": False, "error": "interval_ms must be 0 (manual) or 500–600000"}
    try:
        _save_interval(ms)
    except OSError as exc:
        return {"ok": False, "error": f"could not persist the interval: {exc}"}
    await asyncio.to_thread(ENGINE.set_interval, ms)
    return {"ok": True, "interval": _interval_state(ENGINE.status().get("running"))}


@router.post("/snap/start")
async def post_snap_start(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Open a snapshot session for ONE source: the pane's drag-crop overlay base.

    A camera gets a live feed; a display or window is captured once, at full resolution,
    and that still is both what the overlay shows and what /snap crops. Resolution mirrors
    /start (no enumeration for a camera id); a camera already watched by the engine may be
    shared, but a DIFFERENT one is refused rather than thrashing the single device handle.
    ``source_id`` is mandatory.
    """
    source_id = str(payload.get("source_id") or "").strip()
    if not source_id:
        return {"ok": False, "error": "source_id is required — pick a source to snapshot."}
    chosen = _source_from_id(source_id)
    if chosen is None and not source_id.startswith(("camera-", "default-camera")):
        sources = await asyncio.to_thread(list_sources)
        available = sources["monitors"] + sources["windows"] + sources["cameras"]
        chosen = next((s for s in available if s["id"] == source_id), None)
        if chosen is None and source_id.isdigit():
            idx = int(source_id)
            chosen = next((s for s in available if s.get("index") == idx), None)
    if chosen is None:
        return {"ok": False, "error": f"unknown source_id {source_id!r} — refresh the list."}
    if (chosen.get("kind") or "") == "camera":
        busy = _snap_camera_busy_reason(chosen)
        if busy:
            return {"ok": False, "error": busy}
        abort_camera_probe()  # the pick outranks a probe, same as /start
    _snap_reset()  # one session at a time; ends any previous one cleanly
    _SNAP.update(
        {"id": uuid.uuid4().hex[:12], "source": chosen, "at": time.time(), "opened_camera": False, "frames": 0}
    )
    frame = await asyncio.to_thread(_snap_frame_payload)
    if not frame.get("ok"):
        _snap_reset()
        return {"ok": False, "error": frame.get("error") or "could not grab a frame"}
    return {"ok": True, "session_id": _SNAP["id"], "frame": frame, "source": _snap_source_summary(chosen)}


@router.get("/snap/frame")
async def get_snap_frame(session_id: str = "") -> dict[str, Any]:
    """One frame of an open snapshot session (the overlay polls this).

    A camera re-grabs per poll; a display/window session replays its stored still.
    """
    if not session_id or session_id != _SNAP.get("id"):
        return {"ok": False, "error": "snapshot session not found — reopen the snapshot"}
    if time.time() - float(_SNAP.get("at") or 0) > SNAP_TTL_S:
        await asyncio.to_thread(_snap_reset)
        return {"ok": False, "error": "snapshot session expired"}
    return await asyncio.to_thread(_snap_frame_payload)


@router.post("/snap")
async def post_snap(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Save the session's capture — cropped when ``rect`` is given — as a PNG.

    The crop runs at FULL resolution (the overlay's feed is only a preview). A still
    session crops the very capture the overlay showed; a camera crops a fresh frame.
    The base64 twin in the response is what the pane stages into the chat input, and
    the session stays open so one view can yield several crops.
    """
    session_id = str(payload.get("session_id") or "")
    if not session_id or session_id != _SNAP.get("id"):
        return {"ok": False, "error": "snapshot session not found — reopen the snapshot"}
    if time.time() - float(_SNAP.get("at") or 0) > SNAP_TTL_S:
        await asyncio.to_thread(_snap_reset)
        return {"ok": False, "error": "snapshot session expired"}
    try:
        return await asyncio.to_thread(_snap_save, payload.get("rect"))
    except (_SourceGone, _SourceMinimized) as exc:
        return {"ok": False, "error": str(exc)[:300], "waiting": True}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


@router.post("/snap/stop")
async def post_snap_stop(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """End a snapshot session and let its camera go (lights off) if it opened one.

    A stale id is a no-op — a dying overlay must not close a session that replaced it.
    """
    session_id = str(payload.get("session_id") or "")
    if session_id and _SNAP.get("id") and session_id != _SNAP.get("id"):
        return {"ok": True, "ended": False}
    await asyncio.to_thread(_snap_reset)
    return {"ok": True, "ended": True}


@router.get("/preview")
async def get_preview(width: int = 640, hwnd: int = 0) -> dict[str, Any]:
    width = max(1, width)  # guard: 0 or negative → resize((0,0)) crash
    """The pane's thumbnail: the frame being watched, or one program on request.

    With ``hwnd`` it previews a listed program instead of the watch — that is how the picker shows
    what each window would actually contribute, including a minimized window's last frame. The watch
    loop's status is untouched: grab state is per thread.
    """
    method = ENGINE.grab_method
    waiting = ENGINE.waiting
    if hwnd:

        def _capture() -> tuple[Optional[bytes], int, int, str, str, str]:
            source = next((w for w in list_windows() if int(w.get("hwnd") or 0) == int(hwnd)), None)
            if not source:
                return None, 0, 0, "", "", "that program is no longer open"
            # Resize inside the capture thread, at the width the pane asked for: encoding a full-size
            # window PNG only to shrink it on the way out is the bulk of a picker row's cost.
            shot, out_w, out_h = _wire_jpeg(_grab(source), width)
            return shot, out_w, out_h, _grab_method(), _grab_waiting(), ""

        try:
            shot, out_w, out_h, method, waiting, failure = await asyncio.to_thread(_capture)
        except (_SourceGone, _SourceMinimized) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if failure:
            return {"ok": False, "error": failure}
        return {
            "ok": True,
            "width": out_w,
            "height": out_h,
            "method": method or None,
            "waiting": waiting or None,
            "data_url": "data:image/jpeg;base64," + base64.b64encode(shot).decode("ascii"),
        }
    cached = _preview_get(ENGINE.frame_seq, width)
    if cached is not None:
        return cached
    png = ENGINE.last_png
    if not png:
        return {"ok": False, "error": "no frame captured yet"}
    try:
        from PIL import Image

        with Image.open(io.BytesIO(png)) as img:
            data, out_w, out_h = _wire_jpeg(img, width)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    payload = {
        "ok": True,
        "width": out_w,
        "height": out_h,
        "method": ENGINE.grab_method or None,
        "waiting": ENGINE.waiting or None,
        "data_url": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii"),
    }
    _preview_put(ENGINE.frame_seq, width, payload)
    return payload
