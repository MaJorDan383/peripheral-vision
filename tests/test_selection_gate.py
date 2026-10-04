"""The pane's selection gate: screen content stays invisible until the plugin pane picks a source.

The dashboard mounts this router for EVERY caller its auth admits (token console, tailnet
web session, local process). Without the gate any of those could pull live frames from
/preview or open a watch with /start while the pane never noticed. So /preview, /start,
/snap/start, /snap/frame and /snap refuse until the pane has published a selection over
POST /select, and /status blinds its descriptions to the same condition — while /sources,
/interval, /inject_mode and /stop stay open (metadata and teardown must never be gateable).
The pane re-publishes every 10s while open and "" on close; a crashed pane expires through
the TTL, so the gate cannot be left open by a pane that is gone.
"""
from __future__ import annotations

import asyncio
import importlib.util
import time
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load("peripheral_vision_backend_half_selection_gate", PLUGIN_DIR / "dashboard" / "plugin_api.py")


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Throwaway state: /status's helpers and the reaper must touch no live file, and
    every test starts from a closed gate (also cleared afterwards for the next file)."""
    monkeypatch.setattr(api, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(api, "LOG_PATH", tmp_path / "log.jsonl")
    monkeypatch.setattr(api, "INTERVAL_PATH", tmp_path / "interval")
    monkeypatch.setattr(api, "SNAP_DIR", tmp_path / "snaps")
    monkeypatch.setattr(api, "_watch_intake", lambda: {})
    api._SNAP.update(
        {"id": "", "source": None, "at": 0.0, "opened_camera": False, "frames": 0,
         "still": None, "still_payload": None}
    )
    api._SELECTION.update({"source_id": "", "at": 0.0})
    yield
    api._SELECTION.update({"source_id": "", "at": 0.0})


def _refusal(payload: dict) -> None:
    assert payload["ok"] is False, f"content route answered without a selection: {payload!r}"
    assert payload.get("selection_required") is True, "callers must be able to tell WHY"
    assert "selected source" in payload["error"]


# ── the gate lifecycle ───────────────────────────────────────────────────────


def test_a_publish_opens_the_gate_and_an_empty_one_closes_it(paths):
    assert api._selection_fresh() is False, "no pane selection yet"
    ok = asyncio.run(api.post_select({"source_id": "monitor-1"}))
    assert ok == {"ok": True, "source_id": "monitor-1", "ttl_s": api._SELECT_TTL_S}
    assert api._selection_fresh() is True
    closed = asyncio.run(api.post_select({"source_id": ""}))
    assert closed["ok"] is True and closed["source_id"] == ""
    assert api._selection_fresh() is False, "the pane closing must close the gate"


def test_a_dead_pane_stops_being_trusted(paths):
    asyncio.run(api.post_select({"source_id": "window-3"}))
    api._SELECTION["at"] = time.time() - api._SELECT_TTL_S - 1
    assert api._selection_fresh() is False, "a crashed pane must not leave content open"
    _refusal(asyncio.run(api.get_preview()))


def test_publishing_is_allowed_without_an_auth_layer(paths):
    """Tests and the loopback dev server mount the router bare: no app.state.auth_required
    exists there, so publishing must still work (auth lives in hermes_cli's own mount)."""
    result = asyncio.run(api.post_select({"source_id": "monitor-1"}))
    assert result["ok"] is True and api._selection_fresh() is True


# ── every content route refuses while closed ─────────────────────────────────


def test_content_routes_refuse_without_a_selection(paths):
    _refusal(asyncio.run(api.get_preview()))
    _refusal(asyncio.run(api.post_start({"source_id": "monitor-1"})))
    _refusal(asyncio.run(api.post_snap_start({"source_id": "monitor-1"})))
    _refusal(asyncio.run(api.get_snap_frame("anything")))
    _refusal(asyncio.run(api.post_snap({"session_id": "anything"})))


def test_a_published_selection_gets_past_the_gate(paths, monkeypatch):
    """Open gate → the refusal is gone; the route fails (or succeeds) on its OWN terms.
    The source lookup is stubbed to miss so nothing actually captures."""
    monkeypatch.setattr(api, "_source_from_id", lambda sid: None)
    asyncio.run(api.post_select({"source_id": "monitor-1"}))
    snap = asyncio.run(api.post_snap_start({"source_id": "monitor-1"}))
    assert "selection_required" not in snap, "the gate no longer refuses"
    assert snap["ok"] is False and "unknown source_id" in snap["error"], "it failed later, on the source"


def test_metadata_routes_stay_open(paths):
    """/inject_mode carries no pixels: the picker's own controls must not break just
    because nothing is selected yet."""
    asyncio.run(api.post_select({"source_id": ""}))
    asyncio.run(api.post_inject_mode({"mode": "always"}))
    assert api._inject_mode_state() == {"mode": "always", "source": "pane"}


# ── /status: the descriptions are content too ────────────────────────────────


def _engine_with_recent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    recent = [{"description": "A terminal showing private notes"}]

    class _Running:
        @staticmethod
        def status() -> dict:
            return {
                "running": True,
                "recent": recent,
                "last_description": "A terminal showing private notes",
            }

    monkeypatch.setattr(api, "ENGINE", _Running())
    return recent


def test_status_blinds_the_descriptions_until_a_selection(paths, monkeypatch):
    recent = _engine_with_recent(monkeypatch)
    blind = asyncio.run(api.get_status())
    assert blind["running"] is True, "state and cadence stay readable — only the content blinds"
    assert blind["recent"] == [], "the ring buffer is a transcript of the screen"
    assert blind["last_description"] is None

    asyncio.run(api.post_select({"source_id": "monitor-1"}))
    seen = asyncio.run(api.get_status())
    assert seen["recent"] == recent
    assert seen["last_description"] == "A terminal showing private notes"
