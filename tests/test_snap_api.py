"""Manual snapshots: /interval, the snapshot routes, and what /status publishes.

Runs the backend half's real handlers against throwaway state — no engine, no network,
no device: _grab is stubbed with a synthetic PIL frame, so crop math, session lifecycle
and the camera-release bookkeeping are all exercised for real. Nothing here may touch
the live plugin's files: STATUS/LOG/INTERVAL/SNAP paths are all redirected.
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

PLUGIN_DIR = Path(__file__).parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load("peripheral_vision_backend_half_snap", PLUGIN_DIR / "dashboard" / "plugin_api.py")


def _frame(width: int = 200, height: int = 100) -> Image.Image:
    return Image.new("RGB", (width, height), (12, 34, 56))


def _monitor(source_id: str = "monitor-1") -> dict:
    return {"id": source_id, "label": "Display 1", "kind": "monitor", "width": 1920, "height": 1080}


def _camera(index: int) -> dict:
    return {
        "id": f"camera-{index}",
        "label": f"Camera {index}",
        "kind": "camera",
        "index": index,
        "width": 1280,
        "height": 720,
    }


class _Count:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Throwaway state + a synthetic grab; nothing reaches the live plugin's files."""
    monkeypatch.setattr(api, "INTERVAL_PATH", tmp_path / "interval")
    monkeypatch.setattr(api, "SNAP_DIR", tmp_path / "snaps")
    monkeypatch.setattr(api, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(api, "LOG_PATH", tmp_path / "log.jsonl")
    monkeypatch.setattr(api, "_grab", lambda source: _frame())
    monkeypatch.setattr(api, "_grab_method", lambda: "screen")
    monkeypatch.setattr(api, "_grab_waiting", lambda: "")
    release = _Count()
    monkeypatch.setattr(api, "release_camera", release)
    abort = _Count()
    monkeypatch.setattr(api, "abort_camera_probe", abort)
    api._SNAP.update(
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
    yield SimpleNamespace(tmp=tmp_path, release=release, abort=abort)
    api._SNAP.update(
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


# ── the interval pick ────────────────────────────────────────────────────────


def test_interval_route_persists_a_pane_pick(sandbox) -> None:
    result = asyncio.run(api.post_interval({"interval_ms": 5000}))
    assert result["ok"] is True
    assert (sandbox.tmp / "interval").read_text(encoding="utf-8").strip() == "5000"
    assert api._interval_state(False) == {"ms": 5000, "source": "pane"}


def test_manual_is_zero_and_junk_is_refused(sandbox) -> None:
    ok = asyncio.run(api.post_interval({"interval_ms": 0}))
    assert ok["ok"] is True
    assert ok["interval"]["ms"] == 0
    assert (sandbox.tmp / "interval").read_text(encoding="utf-8").strip() == "0"
    for bad in (123, -5, "abc", None, 700_000):
        refused = asyncio.run(api.post_interval({"interval_ms": bad}))
        assert refused["ok"] is False, bad
    assert (sandbox.tmp / "interval").read_text(encoding="utf-8").strip() == "0", "refusals must not touch the file"


def test_interval_falls_back_to_default_without_a_file(sandbox) -> None:
    assert api._interval_state(False) == {"ms": api.DEFAULT_INTERVAL_MS, "source": "default"}
    (sandbox.tmp / "interval").write_text("0\n", encoding="utf-8")
    assert api._interval_state(False) == {"ms": 0, "source": "pane"}


# ── snapshot sessions ────────────────────────────────────────────────────────


def test_snap_start_opens_a_session_with_a_frame(sandbox, monkeypatch) -> None:
    monkeypatch.setattr(api, "_source_from_id", lambda sid: _monitor(sid))
    started = asyncio.run(api.post_snap_start({"source_id": "monitor-1"}))
    assert started["ok"] is True
    assert len(started["session_id"]) == 12
    assert started["source"] == {
        "id": "monitor-1",
        "label": "Display 1",
        "kind": "monitor",
        "width": 1920,
        "height": 1080,
    }
    frame = started["frame"]
    assert frame["ok"] is True
    assert frame["data_url"].startswith("data:image/jpeg;base64,")
    assert (frame["width"], frame["height"]) == (200, 100)
    again = asyncio.run(api.get_snap_frame(started["session_id"]))
    assert again["ok"] is True


def test_snap_crop_saves_the_selection(sandbox, monkeypatch) -> None:
    monkeypatch.setattr(api, "_source_from_id", lambda sid: _monitor(sid))
    started = asyncio.run(api.post_snap_start({"source_id": "monitor-1"}))
    session_id = started["session_id"]
    result = asyncio.run(
        api.post_snap({"session_id": session_id, "rect": {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}})
    )
    assert result["ok"] is True
    assert (result["width"], result["height"]) == (100, 50)
    path = Path(result["path"])
    assert path.parent == sandbox.tmp / "snaps"
    assert path.exists() and path.suffix == ".png"
    raw = base64.b64decode(result["png_b64"])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    assert raw == path.read_bytes(), "the base64 twin and the saved file are the same pixels"
    full = asyncio.run(api.post_snap({"session_id": session_id}))
    assert (full["width"], full["height"]) == (200, 100), "no rect = the whole frame"
    assert api._SNAP["id"] == session_id, "snapping keeps the session open for another crop"


def test_snap_sessions_expire_and_unknown_ids_are_refused(sandbox) -> None:
    assert asyncio.run(api.get_snap_frame("nope"))["ok"] is False
    assert asyncio.run(api.post_snap({"session_id": "nope"}))["ok"] is False
    api._SNAP.update(
        {
            "id": "abc",
            "source": _monitor(),
            "at": time.time() - api.SNAP_TTL_S - 1,
            "opened_camera": False,
            "frames": 0,
            "still": None,
            "still_payload": None,
        }
    )
    expired = asyncio.run(api.get_snap_frame("abc"))
    assert expired["ok"] is False and "expired" in expired["error"]
    assert api._SNAP["id"] == ""


def test_unknown_sources_are_refused(sandbox, monkeypatch) -> None:
    monkeypatch.setattr(api, "_source_from_id", lambda sid: None)
    monkeypatch.setattr(api, "list_sources", lambda: {"monitors": [], "windows": [], "cameras": []})
    refused = asyncio.run(api.post_snap_start({"source_id": "monitor-9"}))
    assert refused["ok"] is False and "unknown source_id" in refused["error"]
    assert asyncio.run(api.post_snap_start({}))["ok"] is False


# ── the camera device hand-off ───────────────────────────────────────────────


def test_camera_release_follows_who_opened_the_device(sandbox, monkeypatch) -> None:
    monkeypatch.setattr(api, "_source_from_id", lambda sid: _camera(1))
    # (a) the engine already holds the device: sharing must NOT release it on stop.
    monkeypatch.setitem(api._CAM_HANDLE, "cap", object())
    started = asyncio.run(api.post_snap_start({"source_id": "camera-1"}))
    assert started["ok"] is True
    assert api._SNAP["opened_camera"] is False
    asyncio.run(api.post_snap_stop({"session_id": started["session_id"]}))
    assert sandbox.release.calls == 0
    # (b) the session opens the device itself: stopping must release it (LED off).
    monkeypatch.setitem(api._CAM_HANDLE, "cap", None)

    def opening_grab(source):
        api._CAM_HANDLE["cap"] = object()
        return _frame()

    monkeypatch.setattr(api, "_grab", opening_grab)
    started = asyncio.run(api.post_snap_start({"source_id": "camera-1"}))
    assert started["ok"] is True
    assert api._SNAP["opened_camera"] is True
    asyncio.run(api.post_snap_stop({"session_id": started["session_id"]}))
    assert sandbox.release.calls == 1
    assert api._SNAP["id"] == ""


def test_a_different_watched_camera_is_refused(sandbox, monkeypatch) -> None:
    class _FakeThread:
        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(api, "ENGINE", SimpleNamespace(_thread=_FakeThread(), monitor={"kind": "camera", "index": 0}))
    monkeypatch.setattr(api, "_source_from_id", lambda sid: _camera(1))
    refused = asyncio.run(api.post_snap_start({"source_id": "camera-1"}))
    assert refused["ok"] is False and "cannot be opened" in refused["error"]
    # The watched camera itself may be shared — reads serialize under _CAM_LOCK.
    monkeypatch.setattr(api, "_source_from_id", lambda sid: _camera(0))
    allowed = asyncio.run(api.post_snap_start({"source_id": "camera-0"}))
    assert allowed["ok"] is True
    assert sandbox.abort.calls == 1, "a camera pick still aborts an in-flight probe"


# ── /status ──────────────────────────────────────────────────────────────────


def test_status_carries_interval_and_the_snap_marker(sandbox, monkeypatch) -> None:
    class _Running:
        interval_ms = 0

        @staticmethod
        def status() -> dict:
            return {"running": True}

    monkeypatch.setattr(api, "ENGINE", _Running())
    monkeypatch.setattr(api, "_watch_intake", lambda: {})
    payload = asyncio.run(api.get_status())
    assert payload["snap"] is True
    assert payload["interval"] == {"ms": 0, "source": "engine"}


def test_status_reaps_an_idle_session(sandbox, monkeypatch) -> None:
    class _Stopped:
        @staticmethod
        def status() -> dict:
            return {"running": False}

    monkeypatch.setattr(api, "ENGINE", _Stopped())
    monkeypatch.setattr(api, "_watch_intake", lambda: {})
    api._SNAP.update(
        {
            "id": "old",
            "source": _monitor(),
            "at": time.time() - api.SNAP_TTL_S - 20,
            "opened_camera": True,
            "frames": 0,
            "still": None,
            "still_payload": None,
        }
    )
    asyncio.run(api.get_status())
    assert api._SNAP["id"] == "", "a killed overlay's session must not outlive the poll"
    assert sandbox.release.calls == 1, "and its camera must be let go"


# ── stills vs live feeds ─────────────────────────────────────────────────────


def test_a_display_session_is_one_still_replayed_for_every_poll(sandbox, monkeypatch) -> None:
    """The crop base is the exact capture the overlay showed — never a re-grab that drifted."""
    monkeypatch.setattr(api, "_source_from_id", lambda sid: _monitor(sid))
    grabs = _Count()
    colors = [(10, 0, 0), (20, 0, 0), (30, 0, 0)]

    def counted(source):
        grabs()
        return Image.new("RGB", (200, 100), colors[min(grabs.calls - 1, 2)])

    monkeypatch.setattr(api, "_grab", counted)
    started = asyncio.run(api.post_snap_start({"source_id": "monitor-1"}))
    assert started["ok"] is True
    assert started["frame"].get("still") is True, "a display session is a still, not a live feed"
    assert grabs.calls == 1
    for _ in range(3):
        frame = asyncio.run(api.get_snap_frame(started["session_id"]))
        assert frame["ok"] is True and frame.get("still") is True
    assert grabs.calls == 1, "polling a still never re-grabs"
    result = asyncio.run(
        api.post_snap({"session_id": started["session_id"], "rect": {"x": 0, "y": 0, "w": 0.5, "h": 0.5}})
    )
    assert result["ok"] is True and (result["width"], result["height"]) == (100, 50)
    assert grabs.calls == 1, "the crop runs on the stored still — the pixels the user circled"
    with Image.open(io.BytesIO(base64.b64decode(result["png_b64"]))) as crop:
        assert crop.getpixel((5, 5)) == (10, 0, 0)


def test_a_window_session_uses_the_last_capture_and_a_retake_recaptures(sandbox, monkeypatch) -> None:
    window = {"id": "window-3", "label": "Notepad", "kind": "window", "hwnd": 1234, "width": 800, "height": 600}
    monkeypatch.setattr(api, "_source_from_id", lambda sid: window)
    grabs = _Count()

    def counted(source):
        grabs()
        return _frame()

    monkeypatch.setattr(api, "_grab", counted)
    started = asyncio.run(api.post_snap_start({"source_id": "window-3"}))
    assert started["ok"] is True and started["frame"].get("still") is True
    again = asyncio.run(api.get_snap_frame(started["session_id"]))
    assert again.get("still") is True and grabs.calls == 1
    # Retake is a second /snap/start for the same id: exactly one fresh capture, new session.
    retaken = asyncio.run(api.post_snap_start({"source_id": "window-3"}))
    assert retaken["ok"] is True and grabs.calls == 2
    assert retaken["session_id"] != started["session_id"]


def test_a_camera_stays_live_and_its_snap_re_grabs(sandbox, monkeypatch) -> None:
    monkeypatch.setattr(api, "_source_from_id", lambda sid: _camera(1))
    grabs = _Count()

    def counted(source):
        grabs()
        return _frame()

    monkeypatch.setattr(api, "_grab", counted)
    started = asyncio.run(api.post_snap_start({"source_id": "camera-1"}))
    assert started["ok"] is True
    assert "still" not in started["frame"], "a camera session is live, not a still"
    assert grabs.calls == 1
    for _ in range(2):
        asyncio.run(api.get_snap_frame(started["session_id"]))
    assert grabs.calls == 3, "every camera poll is a fresh frame"
    result = asyncio.run(api.post_snap({"session_id": started["session_id"]}))
    assert result["ok"] is True and grabs.calls == 4, "a camera snap grabs its own fresh frame"


# ── the last-frame fallback ──────────────────────────────────────────────────


def _window_source(source_id: str = "window-777") -> dict:
    return {
        "id": source_id,
        "label": "Notepad — notes.txt",
        "kind": "window",
        "hwnd": 777,
        "x": 0,
        "y": 0,
        "width": 800,
        "height": 600,
    }


def _clear_frames() -> None:
    with api._LAST_FRAME_LOCK:
        api._LAST_FRAMES.clear()


@pytest.fixture(autouse=True)
def _fresh_frame_cache():
    _clear_frames()
    yield
    _clear_frames()


def test_failed_live_grab_falls_back_to_the_last_frame(sandbox, monkeypatch) -> None:
    src = _window_source()
    api._remember_frame(src, _frame(320, 120))

    def blank(source):
        raise RuntimeError("window comes back blank in the background (GPU-composited or hidden)")

    monkeypatch.setattr(api, "_grab", blank)
    monkeypatch.setattr(api, "_source_from_id", lambda sid: src)
    started = asyncio.run(api.post_snap_start({"source_id": "window-777"}))
    assert started["ok"] is True, started
    frame = started["frame"]
    assert frame["ok"] is True and frame.get("last_frame") is True
    assert "last frame captured" in (frame.get("waiting") or "")
    assert (frame["width"], frame["height"]) == (320, 120), "the feed shows the remembered frame"
    saved = asyncio.run(
        api.post_snap({"session_id": started["session_id"], "rect": {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}})
    )
    assert saved["ok"] is True and (saved["width"], saved["height"]) == (160, 60)
    assert "stale" not in saved, "the still came from the session, not a fresh fallback grab"


def test_minimized_wait_state_also_uses_the_last_frame(sandbox, monkeypatch) -> None:
    src = _window_source()
    api._remember_frame(src, _frame(64, 48))

    def gone(source):
        raise api._SourceMinimized("DWM returned no frame for Notepad")

    monkeypatch.setattr(api, "_grab", gone)
    monkeypatch.setattr(api, "_source_from_id", lambda sid: src)
    started = asyncio.run(api.post_snap_start({"source_id": "window-777"}))
    assert started["ok"] is True
    assert started["frame"].get("last_frame") is True
    assert "DWM returned no frame" in started["frame"]["waiting"]


def test_without_a_last_frame_the_original_error_stands(sandbox, monkeypatch) -> None:
    src = _window_source()
    monkeypatch.setattr(api, "_source_from_id", lambda sid: src)

    def blank(source):
        raise RuntimeError("window comes back blank")

    monkeypatch.setattr(api, "_grab", blank)
    started = asyncio.run(api.post_snap_start({"source_id": "window-777"}))
    assert started["ok"] is False and "blank" in started["error"]


def test_camera_save_falls_back_and_flags_stale(sandbox, monkeypatch) -> None:
    src = _camera(1)
    api._remember_frame(src, _frame(64, 48))
    monkeypatch.setattr(api, "_source_from_id", lambda sid: src)
    monkeypatch.setattr(api, "_grab", lambda source: _frame())
    started = asyncio.run(api.post_snap_start({"source_id": "camera-1"}))
    assert started["ok"] is True

    def dead(source):
        raise RuntimeError("camera 1 returned no frame")

    monkeypatch.setattr(api, "_grab", dead)
    saved = asyncio.run(api.post_snap({"session_id": started["session_id"]}))
    assert saved["ok"] is True and saved.get("stale") is True
    assert (saved["width"], saved["height"]) == (64, 48), "the PNG is the remembered frame"


def test_frames_remembered_per_source_with_a_cap() -> None:
    a, b = _window_source("window-1"), _window_source("window-2")
    api._remember_frame(a, _frame(10, 10))
    api._remember_frame(a, _frame(20, 20))  # newest wins for the same source
    api._remember_frame(b, _frame(30, 30))
    got = api._recall_frame(a)
    assert got is not None and got["img"].size == (20, 20)
    assert api._recall_frame(_window_source("window-3")) is None
    for i in range(api._LAST_FRAMES_MAX + 3):
        api._remember_frame(_window_source(f"window-{i}"), _frame(5, 5))
    with api._LAST_FRAME_LOCK:
        assert len(api._LAST_FRAMES) == api._LAST_FRAMES_MAX, "the cache is bounded"


def test_thumbnail_captures_run_on_their_own_thread(monkeypatch) -> None:
    seen = {}

    def capture(source):
        seen["thread"] = threading.current_thread().name
        return _frame(12, 12)

    monkeypatch.setattr(api, "_thumb_capture", capture)
    img = api._grab_window_thumbnail(
        {"kind": "window", "hwnd": 5, "label": "x", "x": 0, "y": 0, "width": 10, "height": 10}
    )
    assert img.size == (12, 12)
    assert seen["thread"].startswith("pv-thumb"), "the host window's thread owns every capture"
    assert api._grab_method() == "thumbnail", "the grab state lands on the caller's thread-local"


def test_preview_falls_back_to_the_last_frame(sandbox, monkeypatch) -> None:
    src = _window_source("window-777")
    api._remember_frame(src, _frame(64, 48))
    monkeypatch.setattr(api, "list_windows", lambda: [src])

    def gone(source):
        raise api._SourceMinimized("DWM returned no frame for Notepad")

    monkeypatch.setattr(api, "_grab", gone)
    got = asyncio.run(api.get_preview(width=64, hwnd=777))
    assert got["ok"] is True and got.get("last_frame") is True
    assert got["data_url"].startswith("data:image/jpeg;base64,")
    assert "last frame captured" in (got.get("waiting") or "")
    assert got["method"] == "last-frame"


def test_preview_without_a_last_frame_reports_the_reason(sandbox, monkeypatch) -> None:
    src = _window_source("window-777")
    monkeypatch.setattr(api, "list_windows", lambda: [src])

    def gone(source):
        raise api._SourceMinimized("DWM returned no frame for Notepad")

    monkeypatch.setattr(api, "_grab", gone)
    got = asyncio.run(api.get_preview(width=64, hwnd=777))
    assert got["ok"] is False and "no frame" in got["error"]


# ── the minimized → restored upgrade ────────────────────────────────────────


def _prime_still(session: str = "s1") -> dict:
    """A window session parked on a small non-live still, as if captured while minimized."""
    src = _window_source("window-777")
    thumb = _frame(320, 240)
    api._SNAP.update({
        "id": session, "source": src, "at": time.time(), "opened_camera": False,
        "frames": 1, "still": thumb, "still_payload": None, "non_live_still": True,
    })
    return api._snap_still_payload()


def test_still_upgrades_once_the_window_is_restored(sandbox, monkeypatch) -> None:
    _prime_still()
    monkeypatch.setattr(api, "_window_iconic", lambda hwnd: False)
    big = _frame(640, 480)
    monkeypatch.setattr(api, "_grab", lambda src: big)

    payload = api._snap_frame_payload()
    assert api._SNAP["still"] is big, "the session still is the fresh full grab"
    assert api._SNAP["non_live_still"] is False
    assert (payload["width"], payload["height"]) == (640, 480), "the wire form is re-encoded"
    assert payload.get("waiting") is None
    assert payload.get("still") is True
    assert api._snap_frame_payload()["width"] == 640, "and it replays from then on"


def test_no_upgrade_while_the_window_is_still_minimized(sandbox, monkeypatch) -> None:
    _prime_still()
    monkeypatch.setattr(api, "_window_iconic", lambda hwnd: True)

    def boom(src):
        raise AssertionError("must not grab while minimized")

    monkeypatch.setattr(api, "_grab", boom)
    payload = api._snap_frame_payload()
    assert (payload["width"], payload["height"]) == (320, 240), "the DWM still replays untouched"
    assert api._SNAP["non_live_still"] is True


def test_failed_upgrade_keeps_the_old_still(sandbox, monkeypatch) -> None:
    _prime_still()
    monkeypatch.setattr(api, "_window_iconic", lambda hwnd: False)

    def gone(src):
        raise api._SourceMinimized("DWM returned no frame")

    monkeypatch.setattr(api, "_grab", gone)
    payload = api._snap_frame_payload()
    assert payload["ok"] is True and (payload["width"], payload["height"]) == (320, 240)
    assert api._SNAP["non_live_still"] is True, "a failed upgrade retries on the next poll"
