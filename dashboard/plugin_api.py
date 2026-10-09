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
- ``POST /select``    → the pane publishes its live selection here; this is the
                         gate every content route below reads (45s TTL, the pane
                         re-publishes every 10s while it is open)
- ``POST /start``     → begin the loop on one explicitly selected source
                         (never defaults; the UI must prompt for a choice)
- ``POST /stop``      → stop the loop
- ``GET  /status``    → running state + ring buffer of recent descriptions
                         (the description text itself is gated with the content)
- ``POST /inject_mode`` → when fresh readings ride turns (pane pick beats the env var)
- ``GET  /preview``   → last captured frame (downscaled data URL) for the pane,
                         gated: refused while the pane holds no selected source

State is persisted to ``$HERMES_HOME/cache/peripheral-vision/`` so the
``pre_llm_call`` hook (which runs in the agent/gateway process, not this web
process) can inject the freshest descriptions as conversation context.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re

# Cross-platform capture abstraction.
# Use an absolute import for test compatibility (tests load via importlib without a package).
import sys
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request

sys.path.insert(0, str(Path(__file__).parent))
from capture import (
    abort_camera_probe as capture_abort_camera_probe,
    camera_cache as capture_camera_cache,
    camera_is_quiet as capture_camera_is_quiet,
    camera_open as capture_camera_open,
    camera_source as capture_camera_source,
    cameras_probing as capture_cameras_probing,
    get_window_rect as capture_get_window_rect,
    grab as capture_grab,
    grab_window_thumbnail as capture_grab_window_thumbnail,
    list_cameras as capture_list_cameras,
    list_monitors as capture_list_monitors,
    list_sources as capture_list_sources,
    list_windows as capture_list_windows,
    probe_cameras_now as capture_probe_cameras_now,
    release_camera as capture_release_camera,
    window_exe as capture_window_exe,
    window_iconic as capture_window_iconic,
)

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
DEFAULT_INTERVAL_MS = 5000
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
DEFAULT_INJECT_MODE = "on_mention"

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
_LAST_ROUTE: Optional[dict[str, Any]] = None  # the route that actually served the last frame

# A route that answers 400/401/403/404/405/422 — or refuses the connection outright — will not
# work on the next tick either: the key, the permission or the model is wrong, or its endpoint is
# down. Those move the ladder on instead of surfacing an error the user cannot act on. 429, 5xx and
# timeouts stay transient and keep their existing retry-then-report path.
_ROUTE_DEAD_STATUSES = frozenset({400, 401, 403, 404, 405, 422})
_ROUTE_DEAD_ERRORS = frozenset(
    {
        "APIConnectionError",
        "ConnectError",
        "ConnectTimeout",
        "URLError",
        "ConnectionError",
        "ConnectionRefusedError",
        "NewConnectionError",
        "RemoteProtocolError",
    }
)
# Long enough to stop paying for a dead route on every described frame, short enough that a fixed
# pick recovers by itself without a restart.
_ROUTE_COOLDOWN_S = 300.0
_ROUTE_DEAD: dict[str, tuple[float, BaseException]] = {}  # route key -> (when parked, failure)
_ROUTE_WARNED: set[str] = set()  # one log line per parked route, not one per tick
_WATCH_SESSION: dict[str, str] = {"id": ""}  # live chat whose model describes frames


# ── Monitor enumeration ───────────────────────────────────────────────────

# ── Cross-platform capture (delegates to the OS backend via capture.py) ──
# The thin wrappers below keep this module's call-sites unchanged; all
# Win32/GDI/DWM/OpenCV machinery lives in dashboard/capture_windows.py.

def list_monitors() -> list[dict[str, Any]]:
    """Enumerate connected displays (index, name, bounds, primary)."""
    return capture_list_monitors()


def list_windows(limit: int = 150) -> list[dict[str, Any]]:
    """Enumerate top-level windows with titles and geometry."""
    return capture_list_windows(limit)


def list_cameras(force: bool = False) -> list[dict[str, Any]]:
    """Enumerate attached cameras (cached; the probe runs in the background)."""
    return capture_list_cameras(force)


def probe_cameras_now() -> list[dict[str, Any]]:
    """Force a camera re-probe and return the device list."""
    return capture_probe_cameras_now()


def cameras_probing() -> bool:
    """Whether a camera probe is in flight."""
    return capture_cameras_probing()


def abort_camera_probe() -> None:
    """Signal an in-flight camera probe to stop."""
    capture_abort_camera_probe()
def _source_from_id(source_id: str) -> Optional[dict[str, Any]]:
    """The source a pick names, resolved without enumerating devices.

    Cameras and displays resolve by index arithmetic; windows fall back to the full listing.
    Enumerating on a pick is what made it slow enough for the desktop app's 30s RPC timeout to fire.
    """
    match = re.fullmatch(r"camera-(\d+)", source_id)
    if match:
        return capture_camera_source(int(match.group(1)))
    if source_id in ("camera-default", "default-camera"):
        cached = capture_camera_cache().get("devices") or []
        best = next((c for c in cached if c.get("default")), cached[0] if cached else None)
        return capture_camera_source(int(best.get("index") or 0) if best else 0)
    match = re.fullmatch(r"(?:monitor|display)-(\d+)", source_id)
    if match:
        index = int(match.group(1))
        return next((m for m in capture_list_monitors() if int(m.get("index", -1)) == index), None)
    match = re.fullmatch(r"window-(\d+)", source_id)
    if match:
        hwnd = int(match.group(1))
        return next((w for w in list_windows() if int(w.get("hwnd") or 0) == hwnd), None)
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



# ── Grab dispatcher (cross-platform; pixels come from the backend) ───────
# Thumbnail + grab state live in shared_state so the capture backends can report
# how a frame was obtained (``method``) and why it is not live (``waiting``)
# through the same per-thread object this module reads.
from shared_state import CAM_HANDLE, GRAB_STATE, THUMB_HOST, THUMB_PROC

_GRAB_STATE = GRAB_STATE  # per-thread: the watcher loop and the pane's previews must not mix

# Backwards-compatible alias for tests/older code.
# Tests monkeypatch api._CAM_HANDLE["cap"].
_CAM_HANDLE = CAM_HANDLE


_THUMB_HOST = THUMB_HOST
_THUMB_PROC = THUMB_PROC


def _get_capture_backend():
    import capture
    return capture._backend()


def _grab_method() -> str:
    """How this thread's last frame was obtained (printwindow / screen / thumbnail / camera)."""
    return getattr(_GRAB_STATE, "method", "") or ""


def _grab_waiting() -> str:
    """Why this thread's last frame is not live, or an empty string when it is live."""
    return getattr(_GRAB_STATE, "waiting", "") or ""

_LOG_LOCK = threading.Lock()    # serialises log ring-buffer reads + writes so concurrent engine ticks can't corrupt log.jsonl
_PREVIEW_LOCK = threading.Lock()
_PREVIEW_CACHE: dict[tuple[int, int], dict[str, Any]] = {}  # (frame_seq, width) -> /preview payload
_PREVIEW_CACHE_MAX = 32  # the pane asks for a handful of widths; the rest is the picker's rows

# ── Last-frame cache ──────────────────────────────────────────────────────
# Every successful capture is remembered per source, so a later capture that fails — a minimized
# window whose DWM surface came back blank, a camera that hands out no frame — can answer with the
# last real frame instead of an error. A handful of full-resolution images: newest wins.
_LAST_FRAME_LOCK = threading.Lock()
_LAST_FRAMES: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_LAST_FRAMES_MAX = 6


def _frame_key(source: dict[str, Any]) -> str:
    kind = source.get("kind") or ("window" if source.get("hwnd") else "monitor")
    ident = source.get("id") or source.get("hwnd") or source.get("index")
    return f"{kind}:{ident}"


def _remember_frame(source: dict[str, Any], img: Any) -> None:
    """Keep ``img`` as that source's last good frame. Best-effort: never fail a capture over it."""
    try:
        if img is None:
            return
        key = _frame_key(source)
        with _LAST_FRAME_LOCK:
            _LAST_FRAMES[key] = {"img": img, "at": time.time(), "label": source.get("label") or key}
            _LAST_FRAMES.move_to_end(key)
            while len(_LAST_FRAMES) > _LAST_FRAMES_MAX:
                _LAST_FRAMES.popitem(last=False)
    except Exception:
        pass


def _recall_frame(source: dict[str, Any]) -> Optional[dict[str, Any]]:
    """That source's last good frame — ``{"img", "at", "label"}`` — or None."""
    try:
        key = _frame_key(source)
    except Exception:
        return None
    with _LAST_FRAME_LOCK:
        entry = _LAST_FRAMES.get(key)
        if entry is None:
            return None
        _LAST_FRAMES.move_to_end(key)
        return dict(entry)


def _fallback_note(source: dict[str, Any], entry: dict[str, Any], reason: str) -> str:
    """The waiting-style note a served fallback frame carries (the pane shows it verbatim)."""
    age = max(0.0, time.time() - float(entry.get("at") or 0.0))
    ago = f"{int(age)}s ago" if age < 120 else f"{int(age // 60)}m ago"
    label = source.get("label") or entry.get("label") or "that source"
    return f"live capture failed ({reason[:120]}); showing the last frame captured for {label} — {ago}"


def _grab(source: dict[str, Any]):
    """PIL image of the chosen source: an application window, a display, or a camera.

    A successful grab is remembered as that source's last frame, which is exactly what a later
    failed grab falls back to (see _recall_frame) instead of erroring.
    """
    _GRAB_STATE.waiting = ""  # only the backend's window path can report a non-live frame
    img = capture_grab(source)
    _remember_frame(source, img)
    return img


def release_camera() -> None:
    """Close the camera device so its LED turns off when watching stops."""
    capture_release_camera()
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


_THUMB_MAX_EDGE = 1920  # a published copy never travels larger than this (full-res stills are local)


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


def _aux_vision_target(cfg: dict[str, Any]) -> dict[str, str]:
    """The user's explicit ``auxiliary.vision`` pick (provider/model/base_url), or {}.

    Mirrors Hermes' own predicate (``agent.image_routing._explicit_aux_vision_override``):
    an ``auto``/empty provider with no model and no base_url is a placeholder, not a pick.
    A real pick IS the vision route — Hermes sends image work there even when the main
    model can see, and so does this plugin.
    """
    aux = cfg.get("auxiliary") if isinstance(cfg.get("auxiliary"), dict) else {}
    vision = aux.get("vision") if isinstance(aux.get("vision"), dict) else {}
    provider = str(vision.get("provider") or "").strip()
    model = str(vision.get("model") or "").strip()
    base_url = str(vision.get("base_url") or "").strip()
    if not vision or (provider.lower() in {"", "auto"} and not model and not base_url):
        return {}
    return {"provider": provider, "model": model, "base_url": base_url}


def _route(session_id: str = "") -> dict[str, Any]:
    """Resolve the model that describes frames.

    Precedence:
    1. an explicit ``auxiliary.vision`` pick in Hermes' config — the user named the model
       that does vision work, so frames go there (the same rule Hermes applies to images
       attached to a turn, even when the session model can see);
    2. otherwise, the model the CURRENT SESSION is running while it can see — frames go
       to whatever model the conversation is on right now;
    3. otherwise (a text-only session), a model pinned from the pane, which also stays
       in ``fallback_*`` so a runtime refusal can fall back to it exactly once.
    """
    sid = session_id or str(_WATCH_SESSION.get("id") or "")
    cfg = _hermes_cfg()
    active_provider, active_model, active_source = _active_model(cfg, sid)
    active_caps = (
        _specified_vision(cfg, active_provider, active_model)
        if active_provider and active_model
        else None
    )
    aux = _aux_vision_target(cfg)
    pinned = _pin()
    if aux:
        # The user picked a vision model in Hermes' own settings: descriptions go there.
        provider = aux["provider"] or active_provider
        model = aux["model"] or active_model
        source = "auxiliary"
        base_url = aux["base_url"] or _endpoint(cfg, provider, sid)[0]
    else:
        use_pin = bool(pinned.get("model")) and active_caps is False
        provider = pinned.get("provider") if use_pin else active_provider
        model = pinned.get("model") if use_pin else active_model
        source = "pinned" if use_pin else active_source
        base_url = _endpoint(cfg, provider, sid)[0]
    return {
        "provider": provider,
        "model": model,
        "source": source,
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
        "base_url": base_url,
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
    """An HTTP error from the model endpoint, with status code and body preserved.

    ``label`` names the route it came from (``provider/model``) and leads the message, so the
    text reads the way it always has while ``status`` stays a fact callers can act on — which is
    how the describe ladder tells "this route cannot serve" from "this route blipped".
    """

    def __init__(self, status: int, detail: str, label: str = ""):
        self.status = status
        self.detail = detail
        self.label = label
        super().__init__(f"{f'{label} ' if label else ''}HTTP {status}: {detail}")


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
            last_exc = _UpstreamError(exc.status, exc.detail, label)  # status stays classifiable
        except Exception as exc:  # 504/429/5xx and network blips are worth one more try
            last_exc = exc
        if attempt + 1 < max(1, VISION_ATTEMPTS):
            time.sleep(1.5)
    raise last_exc if last_exc else RuntimeError(f"{label}: no response")


def _brief_error(exc: BaseException) -> str:
    """One clause naming a failure, trimmed — never an upstream body verbatim.

    A status is the actionable fact, so an HTTP failure is reported as just that rather than as a
    developer's error string; a route that could not be reached says so in words, because the
    exception class name means nothing to the person reading the pane.
    """
    if isinstance(exc, _UpstreamError):
        return f"HTTP {exc.status}"
    if type(exc).__name__ in _ROUTE_DEAD_ERRORS:
        return "it could not be reached"
    text = " ".join(str(exc).split())[:120]
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _route_key(route: dict[str, Any]) -> str:
    return f"{route.get('provider')}/{route.get('model')}@{route.get('base_url') or ''}"


def _route_is_dead(exc: BaseException) -> bool:
    """True when the failure says the ROUTE cannot serve images, not that it blipped."""
    if isinstance(exc, _NeedsVisionModel):
        return True
    if isinstance(exc, _UpstreamError):
        return exc.status in _ROUTE_DEAD_STATUSES
    return type(exc).__name__ in _ROUTE_DEAD_ERRORS


def _route_died(route: dict[str, Any], exc: BaseException) -> None:
    """Park a route that cannot serve, and say so once in the log."""
    key = _route_key(route)
    _ROUTE_DEAD[key] = (time.time(), exc)
    if key not in _ROUTE_WARNED:
        _ROUTE_WARNED.add(key)
        _append_log(
            {
                "ts": time.time(),
                "event": "describe_route_unavailable",
                "route": key,
                "source": route.get("source"),
                "reason": _brief_error(exc)[:300],
            }
        )


def _session_route(primary: dict[str, Any]) -> dict[str, Any]:
    """The active session model as a describe route — the rung under an unusable aux pick."""
    provider = str(primary.get("active_provider") or "")
    model = str(primary.get("active_model") or "")
    sid = str(primary.get("session_id") or "")
    return {
        "provider": provider,
        "model": model,
        "source": str(primary.get("active_source") or "session"),
        "active_provider": provider,
        "active_model": model,
        "active_source": str(primary.get("active_source") or "session"),
        "session_id": sid,
        "active_supports_vision": primary.get("active_supports_vision"),
        "supports_vision": primary.get("active_supports_vision"),
        "base_url": _endpoint(_hermes_cfg(), provider, sid)[0],
    }


def _describe_ladder() -> list[dict[str, Any]]:
    """The routes to try, best first: what the user configured, then what can actually see.

    1. the resolved route — the ``auxiliary.vision`` pick, else the session/pinned model;
    2. when that was an aux pick: the ACTIVE SESSION model. A pick whose key, permission or
       endpoint is unavailable must not cost the user their descriptions while the model they are
       already talking to can read a frame perfectly well — and when that model is text-only, it
       still belongs on the ladder, because it is the thing the error has to name;
    3. the model pinned in the pane (the existing fallback for a model that cannot see).
    """
    primary = _route()
    routes = [primary]
    if primary.get("source") == "auxiliary" and primary.get("active_model"):
        routes.append(_session_route(primary))
    if primary.get("source") != "pinned":
        pinned = _pin_route()
        if pinned:
            routes.append(pinned)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for route in routes:
        key = _route_key(route)
        if route.get("model") and key not in seen:
            seen.add(key)
            out.append(route)
    return out


def _vision_give_up(dead: list[tuple[dict[str, Any], BaseException]]) -> Exception:
    """The error when no rung could see: name the model that blocks the user, and the failed pick.

    A text-only model is the thing the user has to change, so it leads; the unavailable pick that
    forced the question goes in the reason clause, where it tells them which setting to fix.
    """
    blind = [(route, exc) for route, exc in dead if isinstance(exc, _NeedsVisionModel)]
    gone = [(route, exc) for route, exc in dead if route.get("source") == "auxiliary"]
    if not blind:
        return dead[-1][1] if dead else RuntimeError("no route could describe a frame")
    route, exc = blind[0]
    note = exc.raw
    if gone:
        pick, pick_exc = gone[0]
        note = (
            f"the configured auxiliary vision model {pick.get('provider')}/{pick.get('model')} "
            f"is unavailable ({_brief_error(pick_exc)})"
        )
    return _NeedsVisionModel(str(route.get("provider") or ""), str(route.get("model") or ""), note)


def _describe(png: bytes) -> str:
    """Describe a frame, walking the ladder until a route can see.

    An unusable route (refused key, permission or model — or an endpoint that is not answering)
    falls through **silently**: it is parked for ``_ROUTE_COOLDOWN_S`` so later frames do not pay
    for it again, and retried once the cooldown lapses, which is how a fixed pick recovers on its
    own. Only when nothing on the ladder can accept images does this raise, via
    :class:`_NeedsVisionModel`, naming what blocks the user and what to do about it.
    """
    global _LAST_DESCRIBER, _LAST_ROUTE
    routes = _describe_ladder()
    if not routes:
        raise RuntimeError("no model configured — set one in Hermes (model.default)")

    dead: list[tuple[dict[str, Any], BaseException]] = []
    for index, route in enumerate(routes):
        last = index == len(routes) - 1
        if route.get("supports_vision") is False:
            exc: Exception = _NeedsVisionModel(
                str(route.get("provider") or ""),
                str(route.get("model") or ""),
                "the model is listed as text-only for image input",
            )
            dead.append((route, exc))
            if last:
                break
            continue
        parked = _ROUTE_DEAD.get(_route_key(route))
        if not last and parked and (time.time() - parked[0]) < _ROUTE_COOLDOWN_S:
            dead.append((route, parked[1]))  # parked moments ago: skip the wire call
            continue
        try:
            text = _describe_with_model(png, route)
        except Exception as exc:  # noqa: BLE001 - classified here, re-raised otherwise
            if not _route_is_dead(exc):
                raise  # 429/5xx/timeout: transient, keep today's retry-then-report behaviour
            if not last:
                _route_died(route, exc)  # park it and let the next rung serve this frame
            dead.append((route, exc))
            continue
        if not text:
            raise RuntimeError(f"{route.get('provider')}/{route.get('model')} returned no text")
        _LAST_DESCRIBER = f"{route.get('provider')}/{route.get('model')}"
        _LAST_ROUTE = route
        return text

    raise _vision_give_up(dead)

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
        # The pane names the model that describes frames, so it has to name the one that really
        # did: when the configured pick was unavailable the ladder served from another rung, and
        # crediting the pick would tell the user a model is reading their screen when it is not.
        vision_route = _LAST_ROUTE or route
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
                "provider": vision_route.get("provider"),
                "model": vision_route.get("model"),
                "source": vision_route.get("source"),
                "active_provider": route.get("active_provider"),
                "active_model": route.get("active_model"),
                "active_source": route.get("active_source"),
                "session_id": route.get("session_id"),
                "active_supports_vision": route.get("active_supports_vision"),
                "supports_vision": vision_route.get("supports_vision"),
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
# The overlay's feed: the crop dialog renders up to ~1500 CSS px wide, so a 4K screen's feed is
# sized to it and never upscaled on the user's display; the SNAP itself stays full resolution.
SNAP_PREVIEW_WIDTH = 1920
# A grab that overruns this is stuck (a window that stopped pumping its queue); answer the pane
# instead of holding its request open. The still-crop path never grabs, so it is unaffected.
SNAP_GRAB_TIMEOUT_S = 25.0
SNAP_DIR = STATE_DIR / "snaps"
# Snapshots are the only thing this plugin writes that a user can pile up: one PNG per
# crop, megabytes each at 4K, and a session of retakes writes one per retake. Retention
# is deliberately blunt — the crop just written and the one before it, so the previous
# crop is still on disk to drag back in, but no run can grow the folder without limit.
# The pane stages a crop from the response's own bytes, never from this folder, so
# pruning can never take away the image the user is looking at.
SNAP_KEEP = 2

_SNAP: dict[str, Any] = {
    "id": "",
    "source": None,
    "at": 0.0,
    "opened_camera": False,
    "frames": 0,
    "still": None,  # a display/window session's full-resolution capture — the crop base
    "still_payload": None,  # its encoded wire form, replayed on every overlay poll
    "non_live_still": False,  # the still came from a minimized/failed grab — upgrade when live
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
            "non_live_still": False,
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


def _snap_camera_busy_reason(source: dict[str, Any], action: str = "snapshot from") -> str:
    """Why this camera may not be opened right now ('' = allowed).

    There is ONE _CAM_HANDLE: while the watch holds a DIFFERENT camera, opening this
    one would swap the handle back and forth every frame (each swap is a device reset).
    Sharing the SAME camera is safe — every read is inside _CAM_LOCK.
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
        f"is being watched — {action} the watched camera, or stop the watch"
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


def _is_cloaked(hwnd: int) -> bool:
    """UWP/ghost windows that exist but render nothing."""
    try:
        import ctypes
        value = ctypes.c_int(0)
        ok = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd), ctypes.c_uint(14), ctypes.byref(value), ctypes.sizeof(value)
        )  # DWMWA_CLOAKED
        return bool(ok == 0 and value.value)
    except Exception:
        return False


def _thumb_capture(source: dict[str, Any]):
    """Facade seam for tests: capture a thumbnail frame (worker thread only)."""
    # Tests monkeypatch api._thumb_capture(source)-> PIL image.
    # Production uses the backend's thumbnail capture for minimized windows.
    return capture_grab_window_thumbnail(int(source.get("hwnd") or 0))


def _grab_window_thumbnail(source: dict[str, Any]):
    """Capture a window thumbnail on its dedicated worker thread.

    Tests assert the worker owns the capture (thread name prefix "pv-thumb").
    """
    # Grab state for the caller's thread-local (tests assert this).
    _GRAB_STATE.method = "thumbnail"

    result: list[Any] = [None]
    exc: list[BaseException] = []

    def _worker() -> None:
        try:
            # Make the TLS visible to the capture implementation.
            _GRAB_STATE.method = "thumbnail"
            result[0] = _thumb_capture(source)
        except BaseException as e:  # pragma: no cover
            exc.append(e)

    t = threading.Thread(target=_worker, name="pv-thumb-worker", daemon=True)
    t.start()
    t.join()

    if exc:
        raise exc[0]
    return result[0]


def _window_rect(hwnd: int):
    """DWM frame bounds when available, else the window rect.

    Backend seam expects a source dict for get_window_rect.
    (Tests stub api._window_rect directly.)
    """
    return capture_get_window_rect({"kind": "window", "hwnd": int(hwnd)})


def _window_exe(pid: int) -> str:
    """Executable name of a window's process."""
    return capture_window_exe(int(pid))


def _restored_rect(hwnd: int):
    """The rectangle a minimized window restores to."""
    be = _get_capture_backend()
    fn = getattr(be, "_restored_rect", None)
    if fn is not None:
        return fn(int(hwnd))
    return None


def _window_iconic(hwnd: int) -> bool:
    """Is this window minimized right now? (its own seam — tests stub it, no Win32 in CI)"""
    return capture_window_iconic(int(hwnd))


def _window_rested(hwnd: int) -> bool:
    """True once a window's DWM frame bounds stop changing (a restore animation has settled).

    Restoring a window runs a system animation, and a maximized one grows top-down in full-width
    bands: a capture taken then is sized from the partial bounds, so it comes back as a clipped
    top band of the window. Two reads a beat apart tell a settling window from a settled one.
    """
    first = _window_rect(int(hwnd))
    if not first:
        return True  # unknown geometry: let the post-grab size check decide
    if len(first) == 2:
        # Some tests/backends stub _window_rect with a legacy (x1, y1) tuple.
        return True
    time.sleep(0.12)
    second = _window_rect(int(hwnd))
    return second == first


def _snap_maybe_upgrade_still() -> None:
    """Re-capture a still that came from a minimized (or failing) window once it is live again.

    The overlay can sit open on a minimized window showing its DWM last frame; restoring the
    window would otherwise leave that frozen frame on screen until a manual Retake. The first
    poll after a restore can land mid-animation, so the window must be rested and the capture
    must match its live frame bounds — otherwise the DWM frame keeps replaying and this retries
    on the next poll. Best-effort: any failure keeps the still this session already has.
    """
    src = _SNAP.get("source") or {}
    if not _SNAP.get("non_live_still") or _snap_kind(src) != "window":
        return
    hwnd = int(src.get("hwnd") or 0)
    if hwnd and _window_iconic(hwnd):
        return  # still minimized — the DWM frame is the honest frame
    if hwnd and not _window_rested(hwnd):
        return  # mid restore animation; a capture now would freeze a clipped band
    fresh = _window_rect(int(hwnd)) if hwnd else None
    if fresh:
        # the source dict still carries the placement rect from snap time; a window restored to
        # a different geometry (maximized, resized, moved) must be grabbed where it IS now
        src = {
            **src,
            "x": fresh[0],
            "y": fresh[1],
            "width": max(1, fresh[2] - fresh[0]),
            "height": max(1, fresh[3] - fresh[1]),
        }
    try:
        img = _grab(src)
    except Exception:
        return  # keep replaying what we have; Retake is still there
    if fresh:
        want_w, want_h = fresh[2] - fresh[0], fresh[3] - fresh[1]
        got_w, got_h = img.size
        if want_w > 0 and want_h > 0 and (
            abs(got_w - want_w) > 0.08 * want_w or abs(got_h - want_h) > 0.08 * want_h
        ):
            return  # the window moved during the grab; keep the DWM frame, retry next poll
    _SNAP["still"] = img
    _SNAP["still_payload"] = None  # next _snap_still_payload re-encodes from the sharper still
    _SNAP["non_live_still"] = False


def _snap_frame_payload() -> dict[str, Any]:
    """One frame of the active session for the crop overlay (thread caller).

    A camera keeps a LIVE feed — every poll is a fresh frame, because timing the shot is
    the point. A display or window captures ONCE at full resolution and hands the SAME
    still back on every poll: the crop base is exactly the image the selection was drawn
    on, never a fresh grab that drifted from it. A still that started non-live (a minimized
    window's DWM frame) upgrades itself once that window is restored — see
    _snap_maybe_upgrade_still.
    """
    if not _SNAP.get("id"):
        return {"ok": False, "error": "no snapshot session — reopen the snapshot"}
    source = _SNAP.get("source") or {}
    _SNAP["at"] = time.time()  # the idle TTL counts from the last frame the pane asked for
    kind = _snap_kind(source)
    if kind != "camera" and _SNAP.get("still") is not None:
        _snap_maybe_upgrade_still()  # a restored window sharpens the feed without a Retake
        return _snap_still_payload()  # already captured: same bytes, no device work
    had_camera = capture_camera_open()
    note = ""
    try:
        img = _grab(source)
    except (_SourceGone, _SourceMinimized) as exc:  # waiting states, not failures
        entry = _recall_frame(source)
        if entry is None:
            return {"ok": False, "error": str(exc)[:300], "waiting": True}
        img, note = entry["img"], _fallback_note(source, entry, str(exc))
    except Exception as exc:  # the pane shows the reason verbatim
        entry = _recall_frame(source)
        if entry is None:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        img, note = entry["img"], _fallback_note(source, entry, f"{type(exc).__name__}: {exc}")
    if source.get("kind") == "camera" and not had_camera and capture_camera_open():
        _SNAP["opened_camera"] = True  # this session opened the device: it must release it
    _SNAP["frames"] = int(_SNAP.get("frames") or 0) + 1
    if kind != "camera":
        # The display/window still IS the session's frame: full resolution, kept for the crop.
        _SNAP["still"] = img
        _SNAP["non_live_still"] = bool(note) or _grab_method() == "thumbnail"
        payload = _snap_payload_for(img, still=True)
        if note:
            payload["waiting"] = note
            payload["last_frame"] = True
        _SNAP["still_payload"] = payload
        return payload
    payload = _snap_payload_for(img, still=False)
    if note:
        payload["waiting"] = note
        payload["last_frame"] = True
    return payload


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


def _prune_snaps(keep: int = SNAP_KEEP) -> None:
    """Leave only the newest ``keep`` snapshots in SNAP_DIR, and no half-written temp.

    Runs on the snapshot thread right after a save. The name carries the timestamp (the
    four hex chars only break a same-second tie), so a name sort is newest-last and
    ``[:-keep]`` is exactly the superseded crops. A ``.tmp`` is a write that died between
    its bytes and the replace — never a snapshot, always swept. Deleting is best-effort:
    a stale file another process still holds open must not fail the save just asked for.
    """
    try:
        saved = sorted(p for p in SNAP_DIR.glob("snap-*") if not p.name.endswith(".tmp"))
        stale = list(SNAP_DIR.glob("snap-*.tmp"))
    except OSError:
        return
    stale += saved[:-keep] if keep > 0 else saved
    for path in stale:
        try:
            path.unlink()
        except OSError:
            pass


def _snap_save(rect: Any) -> dict[str, Any]:
    """The session's capture, cropped by ``rect``, saved as PNG (thread caller).

    A camera hands out a fresh full-resolution frame (it is live by design). A display or
    window crops the session's stored still — the exact full-resolution image the overlay
    showed — so the saved crop can never point at pixels the user never saw. The base64
    twin rides back to the pane so it can stage the image into the chat input; the file on
    disk is the same pixels, there for dragging in or keeping. SNAP_DIR keeps only this
    crop and the previous one (see ``_prune_snaps``), so the response's bytes — not a
    returning visit to the folder — are what the pane hands to the chat.
    """
    source = _SNAP.get("source") or {}
    img = _SNAP.get("still") if _snap_kind(source) != "camera" else None
    stale = False
    if img is None:
        try:
            img = _grab(source)
        except Exception:  # a live frame that fails falls back to the source's last capture
            entry = _recall_frame(source)
            if entry is None:
                raise
            img = entry["img"]
            stale = True
    img = _crop_normalized(img, rect)
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"snap-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}.png"
    path = SNAP_DIR / name
    data = _png_bytes(img)
    tmp = path.with_name(f"{name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    _prune_snaps()  # keep this crop and the one before it; nothing accumulates here
    result = {
        "ok": True,
        "name": name,
        "path": str(path),
        "width": img.size[0],
        "height": img.size[1],
        "bytes": len(data),
        "png_b64": base64.b64encode(data).decode("ascii"),
    }
    if stale:
        result["stale"] = True  # the live grab failed; this PNG is the source's last frame
    return result


# ── Routes ────────────────────────────────────────────────────────────────
# ── the pane's selection gate ────────────────────────────────────────────────────────
# Screen content may flow only while the PLUGIN PANE holds a selected source. The router is
# mounted at /api/plugins/peripheral-vision/* for EVERY caller the dashboard's auth admits —
# a token-holding console, a web session on the tailnet, or a local process — and without
# this gate any of them could silently pull live frames via /preview or open a watch via
# /start without the pane ever being asked. The pane publishes its selection over POST /select
# and re-publishes it every 10s while it is open; a closed pane stops publishing, and the
# gate fades after _SELECT_TTL_S even if the pane crashed mid-flight. Gated: /preview,
# /start, /snap/start, /snap/frame, /snap, and the description text inside /status.
# /stop, /sources, /monitors, /interval, /inject_mode, /vision stay open: metadata and
# teardown only (an attacker must never be able to hold the watch OFF).
_SELECT_TTL_S = 45.0
_SELECTION: dict[str, Any] = {"source_id": "", "at": 0.0}


def _selection_fresh() -> bool:
    """True while the pane's published selection has not gone stale."""
    if not _SELECTION.get("source_id"):
        return False
    return (time.time() - float(_SELECTION.get("at") or 0.0)) <= _SELECT_TTL_S


def _selection_refusal() -> Optional[dict[str, Any]]:
    """None when the gate is open; the standard ``ok: False`` payload when it is not."""
    if _selection_fresh():
        return None
    return {
        "ok": False,
        "error": "the plugin pane has no selected source — pick one there first (screen content stays gated until it is)",
        "selection_required": True,
    }


def _selection_write_allowed(request: Optional[Request]) -> bool:
    """Who may publish a selection: only a caller the dashboard's own auth let through.

    On the mounted router that is the session token (loopback mode) or the gated cookie
    session — the same bar every other /api/ route clears. The router also gets mounted
    with NO auth layer at all (tests, the standalone dev server): there
    ``app.state.auth_required`` never exists, the server binds loopback only, and
    publishing stays open so the dev flow keeps working.
    """
    app = getattr(request, "app", None) if request is not None else None
    if app is None or not hasattr(app.state, "auth_required"):
        return True
    try:
        from hermes_cli.web_server import _require_token

        _require_token(request)  # raises 401 when the caller carries no session
        return True
    except HTTPException:
        return False
    except Exception:
        # The auth layer exists but can't be verified from here; auth_middleware has
        # already gated every non-public /api/ route, so anything that reached this
        # handler is a session the dashboard accepted.
        return True


@router.post("/select")
async def post_select(payload: dict[str, Any] = Body(default={}), request: Request = None) -> dict[str, Any]:
    """Publish the pane's current selection: an id opens the gate, ``""`` closes it.

    The pane fires this on every pick, every 10s while open, and once on close — so a
    closed (or dead) pane can never leave content routes unlocked.
    """
    source_id = str(payload.get("source_id") or "").strip()
    if not _selection_write_allowed(request):
        return {"ok": False, "error": "publishing a selection requires the dashboard session"}
    _SELECTION["source_id"] = source_id
    _SELECTION["at"] = time.time() if source_id else 0.0
    return {"ok": True, "source_id": source_id, "ttl_s": _SELECT_TTL_S}


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
        await asyncio.to_thread(capture_list_cameras, True)
    sources = await asyncio.to_thread(capture_list_sources)
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
    payload = {
        **engine_status,
        "intake": _watch_intake(),
        "inject_mode": _inject_mode_state(),
        "interval": _interval_state(engine_status.get("running")),
        "snap": True,
    }
    # The descriptions ARE screen content in text form: a dashboard caller must not learn
    # what is on the screen from a status poll the pane never opened. Everything else here
    # is metadata (state, cadence, model) and stays readable.
    if not _selection_fresh():
        if "recent" in payload:
            payload["recent"] = []
        if "last_description" in payload:
            payload["last_description"] = None
    return payload


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
    refused = _selection_refusal()
    if refused:
        return refused
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
        sources = await asyncio.to_thread(capture_list_sources)
        available = sources["monitors"] + sources["windows"] + sources["cameras"]
        chosen = next((s for s in available if s["id"] == source_id), None)
        if chosen is None and source_id.isdigit():
            idx = int(source_id)
            chosen = next((s for s in available if s.get("index") == idx), None)
    if chosen is None:
        if sources is None:
            sources = await asyncio.to_thread(capture_list_sources)
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
    refused = _selection_refusal()
    if refused:
        return refused
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
    try:
        frame = await asyncio.wait_for(asyncio.to_thread(_snap_frame_payload), timeout=SNAP_GRAB_TIMEOUT_S)
    except asyncio.TimeoutError:
        _snap_reset()
        return {"ok": False, "error": "the capture timed out — the source did not return a frame"}
    if not frame.get("ok"):
        _snap_reset()
        return {"ok": False, "error": frame.get("error") or "could not grab a frame"}
    return {"ok": True, "session_id": _SNAP["id"], "frame": frame, "source": _snap_source_summary(chosen)}


@router.get("/snap/frame")
async def get_snap_frame(session_id: str = "") -> dict[str, Any]:
    """One frame of an open snapshot session (the overlay polls this).

    A camera re-grabs per poll; a display/window session replays its stored still.
    """
    refused = _selection_refusal()
    if refused:
        return refused
    if not session_id or session_id != _SNAP.get("id"):
        return {"ok": False, "error": "snapshot session not found — reopen the snapshot"}
    if time.time() - float(_SNAP.get("at") or 0) > SNAP_TTL_S:
        await asyncio.to_thread(_snap_reset)
        return {"ok": False, "error": "snapshot session expired"}
    try:
        return await asyncio.wait_for(asyncio.to_thread(_snap_frame_payload), timeout=SNAP_GRAB_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "the capture timed out — the source did not return a frame"}


@router.post("/snap")
async def post_snap(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Save the session's capture — cropped when ``rect`` is given — as a PNG.

    The crop runs at FULL resolution (the overlay's feed is only a preview). A still
    session crops the very capture the overlay showed; a camera crops a fresh frame.
    The base64 twin in the response is what the pane stages into the chat input, and
    the session stays open so one view can yield several crops.
    """
    refused = _selection_refusal()
    if refused:
        return refused
    session_id = str(payload.get("session_id") or "")
    if not session_id or session_id != _SNAP.get("id"):
        return {"ok": False, "error": "snapshot session not found — reopen the snapshot"}
    if time.time() - float(_SNAP.get("at") or 0) > SNAP_TTL_S:
        await asyncio.to_thread(_snap_reset)
        return {"ok": False, "error": "snapshot session expired"}
    try:
        return await asyncio.wait_for(asyncio.to_thread(_snap_save, payload.get("rect")), timeout=SNAP_GRAB_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "the capture timed out — the source did not return a frame"}
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
async def get_preview(
    width: int = 640, hwnd: int = 0, source_id: str = ""
) -> dict[str, Any]:
    width = max(1, width)  # guard: 0 or negative → resize((0,0)) crash
    """The pane's thumbnail: the frame being watched, or one source on request.

    With ``source_id`` (or the older ``hwnd``) it previews a listed source instead of the watch —
    that is how the picker shows what each row would actually contribute, including a minimized
    window's last frame (the very DWM surface its taskbar preview shows). When that surface is gone,
    the source's last captured frame answers instead, flagged ``last_frame``. The watch loop's
    status is untouched: grab state is per thread.

    Picker previews (``source_id`` / ``hwnd``) bypass the selection gate — they are metadata, not
    the watched frame, and the picker must show them before any source is chosen.
    """
    picker_preview = bool(source_id) or bool(hwnd)
    if not picker_preview:
        refused = _selection_refusal()
        if refused:
            return refused
    method = ENGINE.grab_method
    waiting = ENGINE.waiting
    if picker_preview:
        # Resolve the source: source_id covers every kind; hwnd is the older window-only form.
        source = None
        if source_id:
            source = _source_from_id(source_id)
        if source is None and hwnd:
            source = next(
                (w for w in list_windows() if int(w.get("hwnd") or 0) == int(hwnd)), None
            )
        if not source:
            return {"ok": False, "error": "that source is no longer available"}
        # Camera previews get two guards before anything is opened: a virtual camera is never
        # opened unprompted (Phone Link's pops its stream window — the rule the probe obeys),
        # and a camera other than the watched one must not swap the single shared handle
        # under a running watch (each swap resets both devices).
        release_after = False
        if source.get("kind") == "camera":
            if capture_camera_is_quiet(int(source.get("index") or -1)):
                return {
                    "ok": False,
                    "error": "that camera is never previewed — pick it and start the watch to open it",
                }
            busy = _snap_camera_busy_reason(source, action="preview")
            if busy:
                return {"ok": False, "error": busy}
            # Release only what THIS preview opened: a handle the watch (or a snapshot
            # overlay) already held is theirs — mirroring _snap_reset's rule.
            held = _CAM_HANDLE.get("cap")
            held_index = _CAM_HANDLE.get("index")
            release_after = (
                held is None
                or held_index is None
                or source.get("index") is None
                or int(held_index) != int(source.get("index"))
            )

        def _capture() -> tuple[Optional[bytes], int, int, str, str, str, bool]:
            # A minimized window previews as its DWM last frame (the surface taskbar previews show);
            # when even that is gone, the source's last captured frame answers instead.
            last = False
            try:
                img = _grab(source)
                note = _grab_waiting()
            except Exception as exc:
                entry = _recall_frame(source)
                if entry is None:
                    raise
                img, last = entry["img"], True
                note = _fallback_note(source, entry, f"{type(exc).__name__}: {exc}")
            # Resize inside the capture thread, at the width the pane asked for: encoding a full-size
            # window PNG only to shrink it on the way out is the bulk of a picker row's cost.
            shot, out_w, out_h = _wire_jpeg(img, width)
            return shot, out_w, out_h, (_grab_method() if not last else "last-frame"), note, "", last

        try:
            try:
                shot, out_w, out_h, method, waiting, failure, last = await asyncio.to_thread(_capture)
            except (_SourceGone, _SourceMinimized) as exc:
                return {"ok": False, "error": str(exc)}
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if failure:
                return {"ok": False, "error": failure}
            result = {
                "ok": True,
                "width": out_w,
                "height": out_h,
                "method": method or None,
                "waiting": waiting or None,
                "data_url": "data:image/jpeg;base64," + base64.b64encode(shot).decode("ascii"),
            }
            if last:
                result["last_frame"] = True
            return result
        finally:
            # A thumbnail must not leave the camera open — nothing else would ever close it
            # (the LED would stay on after the picker is gone). Every failure path lands here.
            # Off the event loop: the release waits on _CAM_LOCK, which a cold camera open
            # holds for a second or two, and the loop must not stall with it.
            if release_after:
                await asyncio.to_thread(release_camera)
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
