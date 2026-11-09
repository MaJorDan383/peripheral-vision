"""Route precedence: descriptions follow Hermes' own model, with the aux pick as the fallback.

There is no plugin-level pin any more. Frames go to the model the live session is running
whenever Hermes does not positively report that it cannot see; only a text-only session model
(the exact case Hermes itself detours around) sends them to the ``auxiliary.vision`` model
configured in Hermes' settings. Unknown capability keeps the session model, because a relay
alias the catalog has never heard of is decided by the first described frame, not by a guess.

All live edges of ``_route()`` are stubbed (active model, capability lookup, endpoint resolution,
config) so this runs with no config, no keys, no network.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load("peripheral_vision_backend_half_aux", PLUGIN_DIR / "dashboard" / "plugin_api.py")

K_AUX = {"provider": "omni", "model": "auto/best-vision", "base_url": "http://localhost:20128/v1"}


def _cfg(vision=None) -> dict:
    cfg: dict = {"model": {"provider": "commandcode", "default": "deepseek-v4.1-flash"}}
    if vision is not None:
        cfg["auxiliary"] = {"vision": vision}
    return cfg


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch):
    """Every live edge of _route() replaced; the real precedence logic stays."""
    monkeypatch.setattr(api, "_active_model", lambda cfg, sid="": ("commandcode", "deepseek-v4.1-flash", "session"))
    monkeypatch.setattr(api, "_specified_vision", lambda cfg, p, m: True)
    monkeypatch.setattr(api, "_endpoint", lambda cfg, p, sid="": (f"http://resolved/{p}/v1", "key"))
    return api


def _use_cfg(monkeypatch: pytest.MonkeyPatch, cfg: dict, specified=None) -> None:
    monkeypatch.setattr(api, "_hermes_cfg", lambda ttl=5.0: cfg)
    if specified is not None:
        monkeypatch.setattr(api, "_specified_vision", specified)


def test_a_seeing_session_model_describes_its_own_frames(stubbed, monkeypatch):
    """An aux pick cannot hijack a session model that can see: the chat's model leads."""
    calls: list[tuple[str, str]] = []
    _use_cfg(monkeypatch, _cfg(K_AUX), specified=lambda cfg, p, m: calls.append((p, m)) or True)
    route = api._route("sess-1")
    assert route["source"] == "session"
    assert (route["provider"], route["model"]) == ("commandcode", "deepseek-v4.1-flash")
    # Capability is resolved for the ROUTE's model, and the session is named as the active one.
    assert ("commandcode", "deepseek-v4.1-flash") in calls
    assert route["active_source"] == "session"
    assert route["active_supports_vision"] is True


def test_unknown_capability_keeps_the_session_model(stubbed, monkeypatch):
    """A relay alias the catalog cannot classify must be tried, not detoured around."""
    _use_cfg(monkeypatch, _cfg(K_AUX), specified=lambda cfg, p, m: None)
    route = api._route("sess-1")
    assert route["source"] == "session"
    assert route["model"] == "deepseek-v4.1-flash"


def test_placeholder_aux_block_is_not_a_pick(stubbed, monkeypatch):
    _use_cfg(
        monkeypatch,
        _cfg({"provider": "auto", "model": "", "base_url": ""}),
        specified=lambda cfg, p, m: False,
    )
    route = api._route("sess-1")
    assert route["source"] == "session"
    assert route["provider"] == "commandcode"


def test_no_aux_block_uses_session(stubbed, monkeypatch):
    _use_cfg(monkeypatch, _cfg())
    route = api._route("sess-1")
    assert route["source"] == "session"
    assert route["model"] == "deepseek-v4.1-flash"


def test_aux_without_base_url_resolves_the_endpoint(stubbed, monkeypatch):
    _use_cfg(
        monkeypatch,
        _cfg({"provider": "omni", "model": "auto/best-vision", "base_url": ""}),
        specified=lambda cfg, p, m: False,
    )
    route = api._route("sess-1")
    assert route["source"] == "auxiliary"
    assert route["base_url"] == "http://resolved/omni/v1"


def test_a_text_only_session_model_falls_back_to_the_aux_pick(stubbed, monkeypatch):
    """The one branch the aux pick exists for: the chat's model cannot read a frame."""
    _use_cfg(monkeypatch, _cfg(K_AUX), specified=lambda cfg, p, m: False)
    route = api._route("sess-1")
    assert route["source"] == "auxiliary"
    assert (route["provider"], route["model"]) == ("omni", "auto/best-vision")
    assert route["base_url"] == "http://localhost:20128/v1"
    assert route["active_model"] == "deepseek-v4.1-flash", "the blocked model is still reported"


def test_a_text_only_session_without_an_aux_pick_stays_on_the_session_model(stubbed, monkeypatch):
    """Nothing else is configured: name the model that blocks the user instead of inventing one."""
    _use_cfg(monkeypatch, _cfg(), specified=lambda cfg, p, m: False)
    route = api._route("sess-1")
    assert route["source"] == "session"
    assert route["model"] == "deepseek-v4.1-flash"
    assert route["supports_vision"] is False


def test_an_aux_pick_is_used_when_no_model_is_known_at_all(stubbed, monkeypatch):
    """No live session and no config default: the configured vision backend is all there is."""
    monkeypatch.setattr(api, "_active_model", lambda cfg, sid="": ("", "", "config"))
    _use_cfg(monkeypatch, _cfg(K_AUX))
    route = api._route("sess-1")
    assert route["source"] == "auxiliary"
    assert (route["provider"], route["model"]) == ("omni", "auto/best-vision")


def test_a_pin_left_on_disk_is_ignored_and_swept(stubbed, monkeypatch, tmp_path):
    """An upgrading user keeps a `vision_model.json`; it must not route a single frame."""
    pin = tmp_path / "vision_model.json"
    pin.write_text('{"provider": "omnicorp", "model": "pinned-vision"}', encoding="utf-8")
    monkeypatch.setattr(api, "STATE_DIR", tmp_path)
    _use_cfg(monkeypatch, _cfg(), specified=lambda cfg, p, m: True)

    route = api._route("sess-1")
    assert route["model"] == "deepseek-v4.1-flash"
    assert "fallback_model" not in route, "the pin is gone from the payload, not just ignored"
    assert pin.exists(), "resolution must not delete files as a side effect"

    api._drop_legacy_pin()
    assert not pin.exists()
