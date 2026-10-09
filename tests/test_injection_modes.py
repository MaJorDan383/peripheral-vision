"""Injection modes: how often a fresh reading is allowed to ride a turn.

The capture loop lives in the backend process; this half only reads the status file it
writes, so every case runs with no camera, no cv2, no model and no config.
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

DESC = "A browser window with several tabs is open."
OTHER = "A terminal window showing pytest output."


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the agent half at a throwaway state directory and a clean injection memory."""
    monkeypatch.setattr(cv, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cv, "_STATUS", tmp_path / "status.json")
    monkeypatch.setattr(cv, "_STOP_REQUEST", tmp_path / "stop_request")
    monkeypatch.delenv(cv.INJECT_MODE_ENV, raising=False)
    cv.reset_injection_state()
    return tmp_path


def _status(**over: object) -> dict:
    payload = {
        "running": True,
        "last_at": time.time(),
        "source_label": "Monitor 1",
        "recent": [{"description": DESC}],
    }
    payload.update(over)
    return payload


def _write(state: Path, payload: object) -> None:
    (state / "status.json").write_text(json.dumps(payload), encoding="utf-8")


def test_always_injects_on_every_turn(state: Path) -> None:
    """The original behaviour, kept for users who want it unthrottled."""
    _write(state, _status())
    first = cv.build_context(mode="always")
    second = cv.build_context(mode="always")
    assert first and second, "always must not throttle"
    assert DESC in first and DESC in second


def test_on_change_suppresses_an_identical_reading(state: Path) -> None:
    """The point of the mode: a screen that has not moved must not cost tokens again."""
    _write(state, _status())
    assert cv.build_context(mode="on_change", session_id="s1") is not None
    assert cv.build_context(mode="on_change", session_id="s1") is None


def test_on_change_injects_a_new_reading(state: Path) -> None:
    _write(state, _status())
    assert cv.build_context(mode="on_change", session_id="s1") is not None
    _write(state, _status(recent=[{"description": OTHER}]))
    text = cv.build_context(mode="on_change", session_id="s1")
    assert text and OTHER in text


def test_the_frame_age_alone_is_not_a_change(state: Path) -> None:
    """The header carries the frame age, which differs every turn; keying on the rendered
    text would dedupe nothing. Only the descriptions count as a change."""
    _write(state, _status(last_at=time.time() - 5))
    assert cv.build_context(mode="on_change", session_id="s1") is not None
    _write(state, _status(last_at=time.time()))
    assert cv.build_context(mode="on_change", session_id="s1") is None


def test_on_change_re_anchors_after_the_recharge_window(state: Path) -> None:
    """An unchanged screen still has to be re-described eventually, or a long session
    silently loses its eyes."""
    _write(state, _status())
    base = time.time()
    assert cv.build_context(base, mode="on_change", session_id="s1") is not None
    assert cv.build_context(base + 1, mode="on_change", session_id="s1") is None
    later = base + cv.RECHARGE_SECONDS + 1
    assert cv.build_context(later, mode="on_change", session_id="s1") is not None


def test_on_change_is_per_session(state: Path) -> None:
    _write(state, _status())
    assert cv.build_context(mode="on_change", session_id="s1") is not None
    assert cv.build_context(mode="on_change", session_id="s2") is not None


def test_on_mention_waits_for_a_turn_about_the_screen(state: Path) -> None:
    _write(state, _status())
    assert cv.build_context(mode="on_mention", user_message="write me a merge sort") is None
    text = cv.build_context(mode="on_mention", user_message="look at my screen and tell me what broke")
    assert text and DESC in text


def test_on_mention_reads_multimodal_turns(state: Path) -> None:
    """Provider payloads arrive as a str or a list of parts; both must be searched."""
    _write(state, _status())
    parts = [{"type": "text", "text": "can you see this window?"}]
    assert cv.build_context(mode="on_mention", user_message=parts) is not None


def test_on_mention_still_needs_a_fresh_reading(state: Path) -> None:
    _write(state, _status(last_at=time.time() - cv.FRESH_SECONDS - 1))
    assert cv.build_context(mode="on_mention", user_message="what's on my screen?") is None


def test_tool_only_injects_nothing(state: Path) -> None:
    """No ambient context by design — and no exception when the mode is useless here."""
    _write(state, _status())
    assert cv.build_context(mode="tool_only") is None


@pytest.mark.parametrize("mode", cv.INJECT_MODES)
def test_every_mode_respects_running_and_freshness(state: Path, mode: str) -> None:
    _write(state, _status(running=False))
    assert cv.build_context(mode=mode, user_message="look at my screen") is None
    _write(state, _status(last_at=time.time() - cv.FRESH_SECONDS - 1))
    assert cv.build_context(mode=mode, user_message="look at my screen") is None


def test_an_unknown_mode_falls_back_to_the_default(
    state: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A typo must not silence the feature it configures."""
    monkeypatch.setenv(cv.INJECT_MODE_ENV, "somtimes")
    assert cv.inject_mode() == cv.DEFAULT_INJECT_MODE
    _write(state, _status())
    assert cv.build_context(session_id="s1", user_message="look at my screen") is not None


def test_the_mode_comes_from_the_environment(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cv.INJECT_MODE_ENV, "always")
    assert cv.inject_mode() == "always"
    _write(state, _status())
    assert cv.build_context(session_id="s1") is not None
    assert cv.build_context(session_id="s1") is not None, "always must not throttle"


def test_the_environment_accepts_hyphenated_modes(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``on-change`` reads like ``on_change`` — nobody should have to guess the separator."""
    monkeypatch.setenv(cv.INJECT_MODE_ENV, "on-change")
    assert cv.inject_mode() == "on_change"


def test_the_hook_honours_the_mode_from_the_environment(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through the registered hook body, not just build_context()."""
    monkeypatch.setenv(cv.INJECT_MODE_ENV, "tool_only")
    _write(state, _status())
    assert cv.on_pre_llm_call(session_id="s1", user_message="look at my screen") is None


def test_the_tracked_sessions_stay_bounded(state: Path) -> None:
    """A long-lived process serving many sessions must not leak one entry per session."""
    _write(state, _status())
    for index in range(cv.MAX_TRACKED_SESSIONS + 20):
        cv.build_context(mode="on_change", session_id=f"session-{index}")
    assert len(cv._sent_index) <= cv.MAX_TRACKED_SESSIONS


# --- the pane's pick (the inject_mode file) ---------------------------------------------------


def _pick(state: Path, mode: str) -> None:
    """What POST /inject_mode writes: the mode name, one line, in the state dir."""
    (state / "inject_mode").write_text(mode + "\n", encoding="utf-8")


def test_the_pane_pick_beats_the_environment(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pick in the desktop pane is the user's latest explicit intent: its file outranks the
    environment variable, which only sets the value for setups nobody ever picked in."""
    monkeypatch.setenv(cv.INJECT_MODE_ENV, "on_mention")
    _pick(state, "always")
    assert cv.inject_mode() == "always"
    _write(state, _status())
    assert cv.build_context(session_id="s1", user_message="no screen words here") is not None


def test_the_pane_pick_accepts_hyphenated_modes(state: Path) -> None:
    _pick(state, "on-mention")
    assert cv.inject_mode() == "on_mention"


def test_a_broken_pane_pick_falls_back_to_the_environment(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-edited or half-written file must not take the feature down with it."""
    monkeypatch.setenv(cv.INJECT_MODE_ENV, "always")
    _pick(state, "banana")
    assert cv.inject_mode() == "always"


def test_clearing_the_pane_pick_restores_the_environment(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(cv.INJECT_MODE_ENV, "always")
    _pick(state, "tool_only")
    assert cv.inject_mode() == "tool_only"
    (state / "inject_mode").unlink()
    assert cv.inject_mode() == "always"


def test_the_hook_reads_the_pane_pick_end_to_end(state: Path) -> None:
    """Through the registered hook body: a pick of tool_only silences injection with no
    environment variable, no restart, and no test-only override."""
    _write(state, _status())
    _pick(state, "tool_only")
    assert cv.on_pre_llm_call(session_id="s1", user_message="look at my screen") is None
