"""Shared mutable state between plugin_api.py and capture_windows.py."""

import threading
from typing import Any, Optional

# Thumbnail host window (off-screen, used for DWM thumbnails of minimized windows)
_THUMB_HOST: dict[str, Any] = {"hwnd": 0, "size": (0, 0)}
_THUMB_PROC: Any = None
_THUMB_LOCK = threading.Lock()
_THUMB_EXECUTOR: Optional[threading.Thread] = None
_THUMB_MAX_EDGE = 1280

# Camera state
_CAM_LOCK = threading.Lock()
_CAM_HANDLE: dict[str, Any] = {"cap": None, "index": None}
_CAM_PROBE_ABORT = threading.Event()
_CAMERA_CACHE: dict[str, Any] = {"at": 0.0, "devices": [], "probing": False, "thread": None}

# Per-thread grab state
_GRAB_STATE = threading.local()

# Last frames cache (for dedup)
_LAST_FRAMES: dict[str, tuple[float, bytes]] = {}
_LAST_FRAME_LOCK = threading.Lock()

# Export the actual mutable objects so all modules share them
# (not copies - the dicts and locks must be the same object)
THUMB_HOST = _THUMB_HOST
THUMB_PROC = _THUMB_PROC
THUMB_LOCK = _THUMB_LOCK
THUMB_EXECUTOR = _THUMB_EXECUTOR
THUMB_MAX_EDGE = _THUMB_MAX_EDGE

CAM_LOCK = _CAM_LOCK
CAM_HANDLE = _CAM_HANDLE
CAM_PROBE_ABORT = _CAM_PROBE_ABORT
CAMERA_CACHE = _CAMERA_CACHE

GRAB_STATE = _GRAB_STATE

LAST_FRAMES = _LAST_FRAMES
LAST_FRAME_LOCK = _LAST_FRAME_LOCK
