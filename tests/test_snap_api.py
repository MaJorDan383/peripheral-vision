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
