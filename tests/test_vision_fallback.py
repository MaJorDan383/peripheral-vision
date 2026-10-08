"""The describe ladder: an unusable route falls through, and only a blind ladder raises.

The rule these pin: when the configured ``auxiliary.vision`` pick is **not available** — a refused
key or permission, a missing model, an endpoint that is not answering — descriptions continue on
the model the active session is already using, **silently**, whenever that model can see. The
error exists only for the case the user has to act on: nothing on the ladder can process an image,
and then it names both the model that blocks them and the pick that forced the question.

Live edges are stubbed (route resolution, endpoint lookup, config, the model call, the log), so
this runs with no config, no keys and no network.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import NoReturn

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

import plugin_api as cv  # noqa: E402

AUX_PICK = ("om", "auto/best-vision")
SESSION = ("commandcode", "deepseek-v4.1-flash-fast")
FRAME = b"\x89PNG\r\n\x1a\n"  # the bytes are never parsed: _describe_with_model is stubbed


class APIConnectionError(Exception):
    """Stands in for openai's: the ladder classifies an unusable route by exception name."""


class APITimeoutError(Exception):
    """Also stands in for openai's — a timeout is transient and must keep its old path."""


def _refused(body: str = 'Combo "auto/best-vision" is not allowed for this API key') -> Exception:
    return cv._UpstreamError(403, body)


def _raise(exc: BaseException) -> NoReturn:
    """A stub that refuses, as a one-liner for ``monkeypatch.setattr``."""
    raise exc


def _aux_route(*, session_sees: bool = True, pinned: str = "") -> dict:
    return {
        "provider": AUX_PICK[0],
        "model": AUX_PICK[1],
        "source": "auxiliary",
        "base_url": "http://127.0.0.1:20128/v1",
        "active_provider": SESSION[0],
        "active_model": SESSION[1],
        "active_source": "session",
        "active_supports_vision": session_sees,
        "session_id": "sess-1",
        "supports_vision": None,
        "fallback_model": pinned,
    }


@pytest.fixture(autouse=True)
def _no_live_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    """No config, no endpoint lookup, no pin, no log writes, no parked-route carry-over."""
    monkeypatch.setattr(cv, "_hermes_cfg", lambda ttl=5.0: {})
    monkeypatch.setattr(cv, "_endpoint", lambda cfg, provider, sid="": (f"http://{provider}/v1", "key"))
    monkeypatch.setattr(cv, "_pin_route", lambda: None)
    monkeypatch.setattr(cv, "_append_log", lambda entry: None)
    monkeypatch.setattr(cv, "_recent", lambda n=12: [])
    monkeypatch.setattr(cv, "_ROUTE_DEAD", {})
    monkeypatch.setattr(cv, "_ROUTE_WARNED", set())
    monkeypatch.setattr(cv, "_LAST_ROUTE", None)
    monkeypatch.setattr(cv, "_LAST_DESCRIBER", "")


def test_a_pick_that_works_describes_and_the_session_is_never_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: nothing about the ladder changes the happy path."""
    monkeypatch.setattr(cv, "_route", lambda session_id="": _aux_route())
    called: list[str] = []

    def fake(png: bytes, route: dict) -> str:
        del png
        called.append(f"{route['provider']}/{route['model']}")
        return "A terminal running pytest."

    monkeypatch.setattr(cv, "_describe_with_model", fake)
    assert cv._describe(FRAME) == "A terminal running pytest."
    assert called == ["om/auto/best-vision"]
    assert cv._LAST_DESCRIBER == "om/auto/best-vision"


def test_a_refused_pick_falls_through_to_the_session_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reported bug: a 403 on the pick must not cost the user their descriptions."""
    monkeypatch.setattr(cv, "_route", lambda session_id="": _aux_route())
    called: list[str] = []

    def fake(png: bytes, route: dict) -> str:
        del png
        called.append(f"{route['provider']}/{route['model']}")
        if route["provider"] == AUX_PICK[0]:
            raise _refused()
        return "A code editor with a diff open."

    monkeypatch.setattr(cv, "_describe_with_model", fake)
    # no exception: the refusal is not surfaced while another route can see
    assert cv._describe(FRAME) == "A code editor with a diff open."
    assert called == ["om/auto/best-vision", "commandcode/deepseek-v4.1-flash-fast"]
    assert cv._LAST_DESCRIBER == "commandcode/deepseek-v4.1-flash-fast"


def test_the_status_names_the_model_that_really_described(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pane must not credit the pick for frames another model described."""
    monkeypatch.setattr(cv, "_route", lambda session_id="": _aux_route())
    monkeypatch.setattr(
        cv,
        "_describe_with_model",
        lambda png, route: _raise(_refused()) if route["provider"] == AUX_PICK[0] else "A browser window.",
    )
    cv._describe(FRAME)
    vision = cv._Engine().status()["vision"]
    assert (vision["provider"], vision["model"], vision["source"]) == (*SESSION, "session")
    assert vision["needs_vision_model"] is False, "a working fallback is not a paused pane"
    assert not vision["detail"], "a working fallback shows no error"


def test_a_dead_endpoint_falls_through_like_a_refused_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other reported symptom: the local endpoint is not answering at all."""
    monkeypatch.setattr(cv, "_route", lambda session_id="": _aux_route())

    def fake(png: bytes, route: dict) -> str:
        del png
        if route["provider"] == AUX_PICK[0]:
            raise APIConnectionError("Connection error.")
        return "A spreadsheet."

    monkeypatch.setattr(cv, "_describe_with_model", fake)
    assert cv._describe(FRAME) == "A spreadsheet."


def test_a_parked_pick_is_not_retried_on_every_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """A route that cannot serve is paid for once, then skipped until the cooldown lapses."""
    monkeypatch.setattr(cv, "_route", lambda session_id="": _aux_route())
    attempts: list[str] = []

    def fake(png: bytes, route: dict) -> str:
        del png
        attempts.append(route["provider"])
        if route["provider"] == AUX_PICK[0]:
            raise _refused()
        return "A chat window."

    monkeypatch.setattr(cv, "_describe_with_model", fake)
    cv._describe(FRAME)
    cv._describe(FRAME)
    assert attempts.count(AUX_PICK[0]) == 1, "the refused pick was called again"
    assert attempts == [AUX_PICK[0], SESSION[0], SESSION[0]]


def test_the_pinned_model_is_the_last_rung(monkeypatch: pytest.MonkeyPatch) -> None:
    """A text-only session model still leaves the pin a chance — unchanged behaviour."""
    monkeypatch.setattr(cv, "_route", lambda session_id="": _aux_route(session_sees=False))
    monkeypatch.setattr(
        cv,
        "_pin_route",
        lambda: {"provider": "local", "model": "qwen-vl", "source": "pinned", "supports_vision": True},
    )
    called: list[str] = []

    def fake(png: bytes, route: dict) -> str:
        del png
        called.append(f"{route['provider']}/{route['model']}")
        if route["model"] != "qwen-vl":
            raise _refused()
        return "A diagram."

    monkeypatch.setattr(cv, "_describe_with_model", fake)
    assert cv._describe(FRAME) == "A diagram."
    assert called == ["om/auto/best-vision", "local/qwen-vl"], "the blind session was attempted"
    assert cv._LAST_ROUTE is not None and cv._LAST_ROUTE["source"] == "pinned"


def test_a_blind_session_with_an_unavailable_pick_explains_both(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing can see: the error names the blocking model *and* the pick that forced the question."""
    monkeypatch.setattr(cv, "_route", lambda session_id="": _aux_route(session_sees=False))
    monkeypatch.setattr(cv, "_describe_with_model", lambda png, route: _raise(_refused()))
    with pytest.raises(cv._NeedsVisionModel) as excinfo:
        cv._describe(FRAME)
    text = str(excinfo.value)
    assert "commandcode/deepseek-v4.1-flash-fast" in text, "does not say what blocks the user"
    assert "om/auto/best-vision" in text and "unavailable" in text, "does not say which setting to fix"
    assert "switch this chat to a vision-capable model" in text, "does not say how to resume"
    assert "(HTTP 403)" in text and "{" not in text, "the reason should be a status, not an error body"


def test_a_refusal_keeps_the_status_the_ladder_classifies_on() -> None:
    """The live regression: an HTTP status flattened into a string made a 403 look transient."""
    exc = cv._UpstreamError(403, "Combo is not allowed", "om/auto/best-vision")
    assert str(exc) == "om/auto/best-vision HTTP 403: Combo is not allowed", "error text changed shape"
    assert cv._route_is_dead(exc), "a 403 was not read as an unusable route"
    assert not cv._route_is_dead(cv._UpstreamError(503, "busy", "om/auto/best-vision"))


def test_a_transient_failure_is_still_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 5xx or a timeout is not an unusable route: it keeps today's retry-then-report path."""
    monkeypatch.setattr(cv, "_route", lambda session_id="": _aux_route())
    tried: list[str] = []

    def fake(png: bytes, route: dict) -> str:
        del png
        tried.append(route["provider"])
        raise cv._UpstreamError(503, "upstream is busy")

    monkeypatch.setattr(cv, "_describe_with_model", fake)
    with pytest.raises(cv._UpstreamError):
        cv._describe(FRAME)
    assert tried == [AUX_PICK[0]], "a transient failure silently moved to another model"

    tried.clear()

    def fake_timeout(png: bytes, route: dict) -> str:
        del png
        tried.append(route["provider"])
        raise APITimeoutError("timed out")

    monkeypatch.setattr(cv, "_describe_with_model", fake_timeout)
    with pytest.raises(APITimeoutError):
        cv._describe(FRAME)
    assert tried == [AUX_PICK[0]], "a timeout was classified as an unusable route"
