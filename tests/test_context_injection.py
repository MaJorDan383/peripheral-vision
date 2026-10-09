"""Agent-side injection gates: what reaches a turn, and what must never reach one.

The capture loop lives in the backend process; this half only reads the status file it
writes, so every case here runs with no camera, no cv2, no model and no config.

Every call here names its `mode`, so a case tests the gate it is about instead of silently
inheriting whatever mode happens to be the default.
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("peripheral_vision_agent_half", PLUGIN_DIR / "__init__.py")
cv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cv)


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the agent half at a throwaway state directory."""
    monkeypatch.setattr(cv, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cv, "_STATUS", tmp_path / "status.json")
    monkeypatch.setattr(cv, "_STOP_REQUEST", tmp_path / "stop_request")
    monkeypatch.delenv(cv.INJECT_MODE_ENV, raising=False)
    # The default mode remembers what it last sent — module state that must not leak between
    # tests, or two cases injecting the same description would look like a repeat to the second.
    cv.reset_injection_state()
    return tmp_path


def _status(**over: object) -> dict:
    payload = {
        "running": True,
        "last_at": time.time(),
        "source_label": "Monitor 1",
        "recent": [{"description": "A browser window with several tabs is open."}],
    }
    payload.update(over)
    return payload


def _write(state: Path, payload: object) -> None:
    (state / "status.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )


def test_injects_a_fresh_description(state: Path) -> None:
    _write(state, _status())
    text = cv.build_context(mode="always")
    assert text, "a running loop with a fresh description must inject"
    assert "A browser window with several tabs is open." in text
    assert "Monitor 1" in text
    assert len(text) <= cv.MAX_CONTEXT_CHARS


def test_no_injection_while_the_loop_is_not_running(state: Path) -> None:
    _write(state, _status(running=False))
    assert cv.build_context(mode="always") is None


def test_no_injection_when_the_newest_frame_is_stale(state: Path) -> None:
    _write(state, _status(last_at=time.time() - cv.FRESH_SECONDS - 1))
    assert cv.build_context(mode="always") is None


def test_no_injection_without_any_description(state: Path) -> None:
    _write(state, _status(recent=[], last_description=""))
    assert cv.build_context(mode="always") is None


def test_no_injection_when_the_state_file_is_missing(state: Path) -> None:
    assert cv.build_context(mode="always") is None


@pytest.mark.parametrize("payload", ['{"running": true, "last_at":', "null", "[]", "", "0"])
def test_a_broken_state_file_never_raises(state: Path, payload: str) -> None:
    """A corrupt or foreign file must not slow down or break a turn."""
    _write(state, payload)
    assert cv.build_context(mode="always") is None
    assert cv.on_pre_llm_call() is None


def test_a_long_description_is_capped(state: Path) -> None:
    _write(state, _status(recent=[{"description": "x" * 5000}]))
    text = cv.build_context(mode="always")
    assert text is not None
    assert len(text) <= cv.MAX_CONTEXT_CHARS


def test_millisecond_timestamps_are_tolerated(state: Path) -> None:
    """Both halves must agree on the clock even if one writes ms."""
    _write(state, _status(last_at=time.time() * 1000))
    assert cv.build_context(mode="always") is not None


def test_the_hook_returns_a_context_key(state: Path) -> None:
    _write(state, _status())
    result = cv.on_pre_llm_call(user_message="look at my screen")
    assert result and "context" in result


def test_unload_writes_a_stop_request(state: Path) -> None:
    """The unload callback is the only thing that asks the backend to stop."""
    assert not (state / "stop_request").exists()
    cv.on_unload()
    assert (state / "stop_request").exists()
