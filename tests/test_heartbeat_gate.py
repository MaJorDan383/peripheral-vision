"""The injection gate must not believe a `running` flag that nothing is refreshing.

`running` is written by the capture loop and only corrected when the loop stops cleanly. When the
backend is killed (or crashes), the last file it wrote keeps saying `running: true` forever — and
this half, in a different process, cannot tell the difference by itself. The heartbeat is that
difference, so a cold heartbeat has to read as "not running".

Driven against a real status file in a temp dir; no backend, camera or model involved.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).parent.parent

_spec = importlib.util.spec_from_file_location("peripheral_vision_agent_half", PLUGIN_DIR / "__init__.py")
cv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cv)

sys.path.insert(0, str(PLUGIN_DIR / "dashboard"))
import plugin_api  # noqa: E402

DESCRIPTION = "A terminal showing pytest output."


@pytest.fixture
def status_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "status.json"
    monkeypatch.setattr(cv, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cv, "_STATUS", path)
    monkeypatch.setattr(cv, "_STOP_REQUEST", tmp_path / "stop_request")
    monkeypatch.delenv(cv.INJECT_MODE_ENV, raising=False)
    cv.reset_injection_state()
    return path


def _claim(path: Path, *, running: bool = True, heartbeat=..., now: float | None = None) -> None:
    payload = {
        "running": running,
        "last_at": (now or time.time()),
        "source_label": "Monitor 1",
        "recent": [{"description": DESCRIPTION}],
    }
    if heartbeat is not ...:
        payload["heartbeat_at"] = heartbeat
    path.write_text(json.dumps(payload), encoding="utf-8")
    if heartbeat is not ... and isinstance(heartbeat, (int, float)):
        stamp = time.time() if now is None else now
        age = stamp - float(heartbeat)
        os.utime(path, (time.time() - age, time.time() - age))


def test_a_live_loop_with_a_fresh_heartbeat_injects(status_file: Path) -> None:
    _claim(status_file, heartbeat=time.time())
    assert cv.build_context(mode="always") is not None


def test_a_cold_heartbeat_is_refused_even_though_the_flag_says_running(status_file: Path) -> None:
    """The bug: the backend was killed, and the file still claims `running: true`."""
    _claim(status_file, heartbeat=time.time() - (cv.HEARTBEAT_MAX_AGE_S + 30))
    assert cv.build_context(mode="always") is None


def test_a_writer_too_old_to_heartbeat_is_still_honoured_when_the_file_is_fresh(status_file: Path) -> None:
    _claim(status_file)  # no heartbeat_at at all, file just written
    assert cv.build_context(mode="always") is not None


def test_the_same_writer_is_refused_once_the_file_itself_goes_cold(status_file: Path) -> None:
    _claim(status_file)
    old = time.time() - (cv.HEARTBEAT_MAX_AGE_S + 60)
    os.utime(status_file, (old, old))
    assert cv.build_context(mode="always") is None


def test_a_stopped_loop_never_injects_however_fresh_the_heartbeat(status_file: Path) -> None:
    _claim(status_file, running=False, heartbeat=time.time())
    assert cv.build_context(mode="always") is None


def test_an_unreadable_heartbeat_fails_closed(status_file: Path) -> None:
    _claim(status_file, heartbeat="not-a-timestamp")
    assert cv.build_context(mode="always") is None


def test_both_halves_agree_on_the_tolerance() -> None:
    """One rule, two processes: the reader's window must match the writer's."""
    assert cv.HEARTBEAT_MAX_AGE_S == plugin_api.STATUS_HEARTBEAT_MAX_AGE_S
