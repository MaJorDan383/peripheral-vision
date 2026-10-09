"""The pane's inject-mode pick: POST /inject_mode and the state /status publishes.

Runs the backend half's real handlers against a throwaway state file — no engine, no
network, no camera. This route is how a pick in the pane reaches the agent half: the
backend writes the file, the hook in __init__.py reads it on the next turn. The
cross-half test near the top exercises exactly that handoff.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cv = _load("peripheral_vision_agent_half", PLUGIN_DIR / "__init__.py")
api = _load("peripheral_vision_backend_half", PLUGIN_DIR / "dashboard" / "plugin_api.py")


@pytest.fixture
def pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Route the backend half's pick file at a throwaway path and clear the env tier."""
    monkeypatch.setattr(api, "INJECT_MODE_PATH", tmp_path / "inject_mode")
    monkeypatch.delenv(api.INJECT_MODE_ENV, raising=False)
    return tmp_path / "inject_mode"


def test_the_two_halves_agree_on_the_modes_and_the_file() -> None:
    """The constants are deliberately duplicated across the two halves (neither process may
    import the other); this is the guard that a future edit cannot drift them apart."""
    assert api.INJECT_MODES == cv.INJECT_MODES
    assert api.DEFAULT_INJECT_MODE == cv.DEFAULT_INJECT_MODE
    assert api.INJECT_MODE_ENV == cv.INJECT_MODE_ENV
    assert api.INJECT_MODE_PATH.name == cv.INJECT_MODE_FILE


def test_the_agent_half_reads_what_the_route_wrote(
    pinned: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The money path: POST the pick, then evaluate the mode the agent half's hook uses."""
    result = asyncio.run(api.post_inject_mode({"mode": "on_mention"}))
    assert result["ok"] is True
    monkeypatch.setattr(cv, "_STATE_DIR", tmp_path)
    assert cv.inject_mode() == "on_mention"


def test_posting_a_mode_persists_it(pinned: Path) -> None:
    result = asyncio.run(api.post_inject_mode({"mode": "always"}))
    assert result["inject_mode"] == {"mode": "always", "source": "pane"}
    # The exact bytes the agent half will read.
    assert pinned.read_text(encoding="utf-8").strip() == "always"
    assert api._inject_mode_state() == {"mode": "always", "source": "pane"}


def test_an_unknown_mode_is_refused_not_stored(pinned: Path) -> None:
    result = asyncio.run(api.post_inject_mode({"mode": "sometimes"}))
    assert result["ok"] is False
    assert "mode must be one of" in result["error"]
    assert not pinned.exists(), "a refused mode must not reach the file the hook reads"


def test_hyphens_and_case_normalize(pinned: Path) -> None:
    result = asyncio.run(api.post_inject_mode({"mode": "On-Mention"}))
    assert result["ok"] is True
    assert result["inject_mode"]["mode"] == "on_mention"
    assert pinned.read_text(encoding="utf-8").strip() == "on_mention"


def test_clearing_falls_back_and_removes_the_file(
    pinned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(api.post_inject_mode({"mode": "always"}))
    assert pinned.exists()
    monkeypatch.setenv(api.INJECT_MODE_ENV, "on_mention")
    result = asyncio.run(api.post_inject_mode({"mode": ""}))
    assert result["inject_mode"] == {"mode": "on_mention", "source": "environment"}
    assert not pinned.exists()


def test_the_pane_pick_outranks_the_environment(
    pinned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(api.INJECT_MODE_ENV, "on_mention")
    assert api._inject_mode_state() == {"mode": "on_mention", "source": "environment"}
    asyncio.run(api.post_inject_mode({"mode": "always"}))
    assert api._inject_mode_state() == {"mode": "always", "source": "pane"}


def test_status_reports_the_mode_and_its_source(
    pinned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pane's select reads /status: the payload must carry the pick and where it came
    from, without touching the engine or the real state directory."""

    class _Running:
        @staticmethod
        def status() -> dict:
            return {"running": True}

    monkeypatch.setattr(api, "ENGINE", _Running())
    monkeypatch.setattr(api, "_watch_intake", lambda: {})
    asyncio.run(api.post_inject_mode({"mode": "tool_only"}))
    payload = asyncio.run(api.get_status())
    assert payload["inject_mode"] == {"mode": "tool_only", "source": "pane"}


def test_the_shipped_defaults_are_on_mention_and_five_seconds() -> None:
    """What a fresh install — or a cleared state directory — starts from.

    The two halves' constants are only compared to *each other* elsewhere, so a matching pair
    could still drift away from the shipped values without a red test. This pins the values
    themselves, and the pane's own fallbacks (used until /status answers) with them.
    """
    assert api.DEFAULT_INJECT_MODE == cv.DEFAULT_INJECT_MODE == "on_mention"
    assert api.DEFAULT_INTERVAL_MS == 5000
    pane = (PLUGIN_DIR / "desktop" / "plugin.js").read_text(encoding="utf-8")
    assert "|| 'on_mention'" in pane, "the pane's mode fallback drifted from the backend's"
    assert ": '5000'" in pane, "the pane's blink-rate fallback drifted from the backend's"
