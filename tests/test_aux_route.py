"""Route precedence: an explicit auxiliary.vision pick IS the describe route.

Hermes' own rule (``agent.image_routing._explicit_aux_vision_override``) routes image
work to a picked ``auxiliary.vision`` model even when the main model can see. The
plugin's describe pipeline follows the same rule; these tests pin the precedence and
the session/pin fallbacks it must not disturb.

All live edges of ``_route()`` are stubbed (active model, capability lookup, pin file,
endpoint resolution, config) so this runs with no config, no keys, no network.
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
    monkeypatch.setattr(api, "_pin", lambda: {})
    monkeypatch.setattr(api, "_endpoint", lambda cfg, p, sid="": (f"http://resolved/{p}/v1", "key"))
    return api


def _use_cfg(monkeypatch: pytest.MonkeyPatch, cfg: dict, specified=None) -> None:
    monkeypatch.setattr(api, "_hermes_cfg", lambda ttl=5.0: cfg)
    if specified is not None:
        monkeypatch.setattr(api, "_specified_vision", specified)


def test_aux_vision_pick_wins_even_when_session_can_see(stubbed, monkeypatch):
    calls: list[tuple[str, str]] = []
    _use_cfg(monkeypatch, _cfg(K_AUX), specified=lambda cfg, p, m: calls.append((p, m)) or True)
    route = api._route("sess-1")
    assert route["source"] == "auxiliary"
    assert (route["provider"], route["model"]) == ("omni", "auto/best-vision")
    assert route["base_url"] == "http://localhost:20128/v1"
    # Capability is resolved for the ROUTE's model, not the session's.
    assert ("omni", "auto/best-vision") in calls
    assert route["active_source"] == "session"  # the session is still reported, just not used


def test_placeholder_aux_block_is_not_a_pick(stubbed, monkeypatch):
    _use_cfg(monkeypatch, _cfg({"provider": "auto", "model": "", "base_url": ""}))
    route = api._route("sess-1")
    assert route["source"] == "session"
    assert route["provider"] == "commandcode"


def test_no_aux_block_uses_session(stubbed, monkeypatch):
    _use_cfg(monkeypatch, _cfg())
    route = api._route("sess-1")
    assert route["source"] == "session"
    assert route["model"] == "deepseek-v4.1-flash"


def test_aux_without_base_url_resolves_the_endpoint(stubbed, monkeypatch):
    _use_cfg(monkeypatch, _cfg({"provider": "omni", "model": "auto/best-vision", "base_url": ""}))
    route = api._route("sess-1")
    assert route["source"] == "auxiliary"
    assert route["base_url"] == "http://resolved/omni/v1"


def test_text_only_session_still_falls_back_to_pin_without_aux(stubbed, monkeypatch):
    monkeypatch.setattr(api, "_pin", lambda: {"provider": "omni", "model": "pinned-vision"})
    _use_cfg(monkeypatch, _cfg(), specified=lambda cfg, p, m: False)
    route = api._route("sess-1")
    assert route["source"] == "pinned"
    assert route["model"] == "pinned-vision"
    assert route["fallback_model"] == "pinned-vision"


def test_aux_outranks_the_text_only_pin(stubbed, monkeypatch):
    monkeypatch.setattr(api, "_pin", lambda: {"provider": "omni", "model": "pinned-vision"})
    _use_cfg(monkeypatch, _cfg(K_AUX), specified=lambda cfg, p, m: False)
    route = api._route("sess-1")
    assert route["source"] == "auxiliary"
    assert route["model"] == "auto/best-vision"
    assert route["fallback_model"] == "pinned-vision"  # still the refusal fallback
