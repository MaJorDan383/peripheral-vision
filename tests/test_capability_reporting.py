"""Capability reporting: whose verdict is it, and what does a refusal actually mean?

Three defects made the pane claim a working model could not see:

1. ``_rejects_images`` treated ANY error body containing "image"/"vision"/"media" as a capability
   verdict. An unavailable model id containing "vision", an image-GENERATION model on the chat
   path, an embedding endpoint — all reported "this model cannot process images", which pauses
   the watch and tells the user to go find another model.
2. The session model was read with ``_sessions.get(desktop_session_id)``, but ``_sessions`` is
   keyed by a short runtime id and keeps the desktop's stored id in ``session_key``. The lookup
   missed, so every resolution fell back to the config default.
3. A pane pin used to outrank the session model, so the route the user configured in the pane
   could disagree with the model they were talking to. The pin is gone: the describe route is
   the session model unless Hermes says it cannot see, and then the ``auxiliary.vision`` pick.

These tests pin the corrected contract. Live edges are stubbed; no config, keys or network.
"""
from __future__ import annotations

import sys
import threading
import types
from pathlib import Path
from typing import NoReturn

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

import plugin_api as cv  # noqa: E402

STORED_ID = "local-" + "a" * 64  # what the desktop pane sends
RUNTIME_ID = "0f3a9c1e"  # what _sessions is keyed by


def _png() -> bytes:
    """A real PNG: the route encodes the frame before it is posted."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


# ── 1. A refusal is a modality refusal, not any 4xx that says "image" ─────────────────────────


@pytest.mark.parametrize(
    "status, body",
    [
        (400, "This API supports text input only. This conversation contains an image."),
        (400, "This model does not support image input."),
        (400, '{"error":{"message":"Unsupported content type: image_url"}}'),
        (404, "gpt-3.5-turbo is not a vision model"),
        (422, "image inputs are not supported for this model"),
    ],
)
def test_a_real_modality_refusal_is_a_capability_verdict(status: int, body: str) -> None:
    assert cv._rejects_images(status, body) is True


@pytest.mark.parametrize(
    "status, body, why",
    [
        (
            400,
            "Model 'llama-3.2-90b-vision-instruct' is not available in the active live "
            "catalog for provider 'nvidia'.",
            "the model id contains 'vision' but the complaint is availability",
        ),
        (
            400,
            "Model 'voyage-multimodal-3.5' is not a chat model.",
            "an embedding model is the wrong route, not a blind one",
        ),
        (
            400,
            "Model 'codex/gpt-image-2.5-flare' is an image-generation model and cannot be "
            "used on this path.",
            "image generation is not image understanding",
        ),
        (404, '{"error":{"message":"Model not found","code":"model_not_found"}}', "missing model"),
        (401, "No active credentials for provider: kilo-gateway", "no key"),
        (400, "max_tokens is too large: 400", "a request-shape problem"),
        (400, '{"error":{"message":"Model \\"Kimi-K2.7-Code\\" is not supported on this endpoint."}}',
         "the model is not served here"),
        (503, "upstream unavailable", "transient"),
        (400, "", "no body at all"),
    ],
)
def test_a_route_problem_is_not_a_capability_verdict(status: int, body: str, why: str) -> None:
    """The regression: every one of these was reported as "cannot process images"."""
    assert cv._rejects_images(status, body) is False, why


def test_a_refused_frame_does_not_pause_the_watch_as_a_capability_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable model must fall through the ladder, not raise _NeedsVisionModel."""

    def describe(png: bytes, route: dict) -> str:
        del png
        if route["model"] == "gone-vision-model":
            raise cv._UpstreamError(
                400,
                "Model 'gone-vision-model' is not available in the active live catalog.",
            )
        return "a described frame"

    monkeypatch.setattr(cv, "_route", lambda session_id="": _route(model="gone-vision-model"))
    monkeypatch.setattr(cv, "_endpoint", lambda cfg, provider, sid="": ("http://stub/v1", "k"))
    monkeypatch.setattr(cv, "_append_log", lambda entry: None)
    monkeypatch.setattr(cv, "_ROUTE_DEAD", {})
    monkeypatch.setattr(cv, "_ROUTE_WARNED", set())
    monkeypatch.setattr(cv, "_hermes_cfg", lambda ttl=5.0: {})
    monkeypatch.setattr(cv, "_describe_with_model", describe)

    # The ladder has no other rung, so the route error surfaces as itself — NOT as the
    # capability verdict that would tell the user to go find a vision model.
    with pytest.raises(cv._UpstreamError):
        cv._describe(b"\x89PNG\r\n\x1a\n")


# ── 2. The session model is found by any of its three identities ─────────────────────────────


def _fake_gateway(monkeypatch: pytest.MonkeyPatch, session: dict | None) -> None:
    """Install a stand-in ``tui_gateway.server`` with one live session record."""

    class _Server(types.ModuleType):
        _sessions_lock = threading.RLock()

        def __init__(self) -> None:
            super().__init__("tui_gateway.server")
            self._sessions = {RUNTIME_ID: session} if session is not None else {}

        def _session_info(self, agent, session=None):  # noqa: ANN001, ANN201 - host shape
            del session
            return {
                "provider": getattr(agent, "provider", ""),
                "model": getattr(agent, "model", ""),
            }

    package = types.ModuleType("tui_gateway")
    package.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tui_gateway", package)
    monkeypatch.setitem(sys.modules, "tui_gateway.server", _Server())


def _agent() -> object:
    return types.SimpleNamespace(
        provider="commandcode",
        model="deepseek/deepseek-v4.1-flash",
        session_id=STORED_ID,
    )


def test_the_stored_session_id_resolves_to_the_live_runtime_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression: the pane sends the stored id, ``_sessions`` is keyed by a runtime id."""
    _fake_gateway(
        monkeypatch, {"agent": _agent(), "session_key": STORED_ID}
    )
    assert cv._live_session_model(STORED_ID) == ("commandcode", "deepseek/deepseek-v4.1-flash")


def test_the_runtime_id_still_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_gateway(monkeypatch, {"agent": _agent(), "session_key": STORED_ID})
    assert cv._live_session_model(RUNTIME_ID) == ("commandcode", "deepseek/deepseek-v4.1-flash")


def test_an_unbuilt_session_reports_its_own_override_not_the_config_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed chat has no agent yet; its picked model still describes its frames."""
    _fake_gateway(
        monkeypatch,
        {
            "agent": None,
            "session_key": STORED_ID,
            "model_override": {"provider": "om", "model": "gemini/gemini-flash-lite-latest"},
        },
    )
    assert cv._live_session_model(STORED_ID) == ("om", "gemini/gemini-flash-lite-latest")


def test_a_finalized_session_is_not_treated_as_live(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_gateway(
        monkeypatch, {"agent": _agent(), "session_key": STORED_ID, "_finalized": True}
    )
    assert cv._live_session_model(STORED_ID) == ("", "")


def test_no_session_at_all_reports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_gateway(monkeypatch, None)
    assert cv._live_session_model(STORED_ID) == ("", "")


# ── 3. An explicit pin outranks an UNKNOWN capability verdict ───────────────────────────────


def _route(model: str = "reflection/beam:free", provider: str = "commandcode") -> dict:
    return {
        "provider": provider,
        "model": model,
        "source": "config",
        "base_url": f"http://{provider}/v1",
        "active_provider": provider,
        "active_model": model,
        "active_source": "config",
        "active_supports_vision": None,
        "session_id": "",
        "supports_vision": None,
    }


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No live state dir, config, keys or log: this file tests decisions, not I/O."""
    monkeypatch.setattr(cv, "_hermes_cfg", lambda ttl=5.0: {})
    monkeypatch.setattr(cv, "_endpoint", lambda cfg, provider, sid="": (f"http://{provider}/v1", "k"))
    monkeypatch.setattr(cv, "_append_log", lambda entry: None)
    monkeypatch.setattr(cv, "_ROUTE_DEAD", {})
    monkeypatch.setattr(cv, "_ROUTE_WARNED", set())
    monkeypatch.setattr(cv, "OBSERVED_CAPS_PATH", tmp_path / "observed_caps.json")
    monkeypatch.setattr(cv, "_OBSERVED_CAPS", {})
    monkeypatch.setattr(cv, "_CAP_CACHE", {})
    monkeypatch.setattr(cv, "_WATCH_SESSION", {"id": ""})


def test_an_unknown_verdict_does_not_detour_to_the_aux_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The relay case: a model the catalog cannot classify is tried, not routed around."""
    monkeypatch.setattr(cv, "_active_model", lambda cfg, sid="": ("commandcode", "relay-alias", "config"))
    monkeypatch.setattr(cv, "_specified_vision", lambda cfg, p, m: None)  # unknown, not False
    monkeypatch.setattr(
        cv,
        "_aux_vision_target",
        lambda cfg: {"provider": "om", "model": "aux-vision", "base_url": "http://om/v1"},
    )

    route = cv._route()
    assert (route["provider"], route["model"]) == ("commandcode", "relay-alias")
    assert route["source"] == "config"


def test_a_seeing_session_model_ignores_the_aux_pick(monkeypatch: pytest.MonkeyPatch) -> None:
    """One place configures vision: a model that can see describes its own frames."""
    monkeypatch.setattr(cv, "_active_model", lambda cfg, sid="": ("openai", "gpt-5", "session"))
    monkeypatch.setattr(cv, "_specified_vision", lambda cfg, p, m: True)
    monkeypatch.setattr(
        cv,
        "_aux_vision_target",
        lambda cfg: {"provider": "om", "model": "aux-vision", "base_url": "http://om/v1"},
    )

    route = cv._route()
    assert (route["provider"], route["model"]) == ("openai", "gpt-5")
    assert route["source"] == "session"
    assert "fallback_model" not in route, "the pane pin is gone from the payload"


def test_the_aux_pick_leads_the_ladder_and_the_session_stays_under_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pick that stops working must not take the session model down with it."""
    monkeypatch.setattr(cv, "_route", lambda session_id="": {
        **_route(model="aux-vision", provider="om"),
        "source": "auxiliary",
        "active_model": "session-model",
        "active_provider": "commandcode",
    })

    ladder = cv._describe_ladder()
    assert [(r["provider"], r["model"]) for r in ladder] == [
        ("om", "aux-vision"),
        ("commandcode", "session-model"),
    ]


def test_a_session_route_has_no_second_rung_without_an_aux_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No aux pick configured (the default placeholder is not one): exactly one rung."""
    monkeypatch.setattr(cv, "_route", lambda session_id="": _route(model="session-model"))

    ladder = cv._describe_ladder()
    assert [(r["provider"], r["model"]) for r in ladder] == [("commandcode", "session-model")]


def test_a_session_route_gains_the_aux_pick_as_its_second_rung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown-verdict session model leads, with the pick underneath for a refusal."""
    monkeypatch.setattr(cv, "_route", lambda session_id="": _route(model="relay-alias"))
    monkeypatch.setattr(
        cv,
        "_aux_vision_target",
        lambda cfg: {"provider": "om", "model": "aux-vision", "base_url": "http://om/v1"},
    )

    ladder = cv._describe_ladder()
    assert [(r["provider"], r["model"], r["source"]) for r in ladder] == [
        ("commandcode", "relay-alias", "config"),
        ("om", "aux-vision", "auxiliary"),
    ]


# ── 4. An observed verdict outranks a catalog that does not list the model ──────────────────


def test_an_observation_beats_an_unknown_catalog() -> None:
    cv._remember_capability("om", "relay-alias", True)
    assert cv._specified_vision({}, "om", "relay-alias") is True
    cv._remember_capability("om", "text-alias", False)
    assert cv._specified_vision({}, "om", "text-alias") is False


def test_an_observation_survives_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cv, "OBSERVED_CAPS_PATH", tmp_path / "observed_caps.json")
    cv._remember_capability("om", "relay-alias", True)

    monkeypatch.setattr(cv, "_OBSERVED_CAPS", {})  # simulate a fresh process
    assert cv._specified_vision({}, "om", "relay-alias") is True


def test_a_described_frame_is_recorded_as_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cv, "_describe_with_model", lambda png, route: "a described frame")
    monkeypatch.setattr(cv, "_route", lambda session_id="": _route(model="relay-alias", provider="om"))

    cv._describe(b"\x89PNG\r\n\x1a\n")
    assert cv._OBSERVED_CAPS[("om", "relay-alias")] is True


def test_a_proven_model_leads_the_candidate_list(monkeypatch: pytest.MonkeyPatch) -> None:
    cv._remember_capability("om", "proven-sees", True)
    candidates = cv._vision_candidates({}, _route(model="configured", provider="commandcode"))
    assert candidates[0]["model"] == "configured"  # the active model still leads
    assert {"provider": "om", "model": "proven-sees", "label": "om · proven-sees (described a frame)"} in candidates


def test_a_refused_route_is_recorded_as_blind(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuses(*args, **kwargs) -> NoReturn:
        del args, kwargs
        raise cv._UpstreamError(400, "This API supports text input only.")

    monkeypatch.setattr(cv, "_post_chat", refuses)
    route = {**_route(model="blind-alias", provider="om"), "base_url": "http://om/v1"}
    with pytest.raises(cv._NeedsVisionModel):
        cv._describe_with_model(_png(), route)
    assert cv._OBSERVED_CAPS[("om", "blind-alias")] is False


# ── 5. A reasoning model that spends its budget on thinking is not a broken model ───────────


@pytest.mark.parametrize(
    "payload, exhausted",
    [
        ({"choices": [{"finish_reason": "length", "message": {"content": ""}}]}, True),
        ({"choices": [{"finish_reason": "length", "message": {"content": "a blue screen"}}]}, False),
        ({"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}, False),
        ({"choices": [{"finish_reason": "stop", "message": {"content": "blue"}}]}, False),
        ({"choices": []}, False),
        ({}, False),
    ],
)
def test_a_capped_completion_with_no_text_is_recognised(payload: dict, exhausted: bool) -> None:
    """``finish_reason: length`` with empty content means the think block ate the budget."""
    assert cv._budget_exhausted(payload) is exhausted


def test_a_starved_first_attempt_is_retried_with_the_full_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression: 400 tokens went entirely to ``reasoning_content``, so a seeing model
    reported "returned no text" and the pane lost the frame."""
    budgets: list[int] = []

    def capped(base_url, key, model, messages, headers, max_tokens=None) -> str:
        del base_url, key, model, messages, headers
        budgets.append(int(max_tokens or cv.VISION_MAX_TOKENS))
        if len(budgets) == 1:
            raise cv._TokenStarved(budgets[0])
        return "a described frame"

    monkeypatch.setattr(cv, "_post_chat", capped)
    route = {**_route(model="reasoner", provider="om"), "base_url": "http://om/v1"}
    assert cv._describe_with_model(_png(), route) == "a described frame"
    assert budgets == [400, cv.VISION_MAX_TOKENS], "the retry must use the full budget"


def test_the_default_budget_leaves_room_for_a_think_block() -> None:
    """400 tokens was not enough for the reasoning alone; the default must exceed it."""
    assert cv.VISION_MAX_TOKENS >= 1000


# ── 6. A polite refusal in a 200 OK is not proof of vision ──────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "I\u2019m sorry, but I can\u2019t see the contents of your computer screen.",
        "Sorry \u2014 I can't see the image you're referring to.",
        "I cannot view the screenshot you attached.",
        "As an AI language model, I cannot see images.",
        "No image was provided in your message.",
        "There is no image here.",
        "The image is not attached to this conversation.",
        "I don't have access to the screenshot.",
    ],
)
def test_an_answer_that_disclaims_the_image_is_not_evidence_of_vision(text: str) -> None:
    assert cv._answer_without_seeing(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "The image shows a solid blue screen with no visible apps.",
        "A dark editor window is visible with several tabs.",
        "I can see the Hermes desktop app with a chat pane open.",
        "The screenshot shows a spreadsheet with three columns of numbers.",
        "No visible apps are running; the desktop shows a wallpaper.",
        "The window is maximized on a 3840x2069 display.",
    ],
)
def test_a_real_description_is_never_mistaken_for_a_refusal(text: str) -> None:
    assert cv._answer_without_seeing(text) is False


def test_a_blind_answer_is_recorded_as_blind_and_pauses_the_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A text-only relay answers 200 with an apology; that must not rank it first in the pane."""
    monkeypatch.setattr(
        cv,
        "_post_chat",
        lambda *a, **k: "I'm sorry, but I can't see the contents of your computer screen.",
    )
    route = {**_route(model="polite-but-blind", provider="om"), "base_url": "http://om/v1"}
    with pytest.raises(cv._NeedsVisionModel):
        cv._describe_with_model(_png(), route)
    assert cv._OBSERVED_CAPS[("om", "polite-but-blind")] is False


