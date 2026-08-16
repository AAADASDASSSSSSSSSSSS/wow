"""Task 6: EricAI-driven Architect (mocked client — no network)."""

from __future__ import annotations

import json

import pytest

from ratsnestpro.agents import Architect, LlmError, LlmMode, parse_mode
from ratsnestpro.agents.llm import resolve_client


class FakeClient:
    """Stand-in for EricAIClient returning canned JSON (or raising)."""

    def __init__(self, payload: dict | None = None, raise_exc: Exception | None = None) -> None:
        self._payload = payload
        self._raise = raise_exc
        self.calls = 0

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return json.dumps(self._payload)


_VALID = {
    "qualified": True,
    "family": "atmega328-dev-board",
    "mandatory_features_present": True,
    "missing_features": [],
    "clarifying_questions": [],
    "rationale": "matches the ATmega328 dev-board family",
    "params": {
        "crystal_mhz": 16,
        "ldo_output_v": 5.0,
        "decoupling_count": 6,
        "power_led": True,
        "breakout_rows": 2,
        "breakout_pins_per_row": 8,
        "mounting_holes": 4,
    },
}


def test_parse_mode_aliases() -> None:
    assert parse_mode("off") == LlmMode.OFFLINE
    assert parse_mode("live") == LlmMode.REQUIRED
    assert parse_mode(None) == LlmMode.OFFLINE
    assert parse_mode("auto") == LlmMode.AUTO


def test_offline_is_deterministic_and_makes_no_call() -> None:
    client = FakeClient(_VALID)
    res = Architect().plan("ATmega328 8MHz 3.3V no LED", mode="offline", client=client)
    assert res.source == "deterministic"
    assert client.calls == 0
    assert res.ready and res.params is not None
    assert res.params.crystal_mhz == 8 and res.params.power_led is False


def test_offline_rejects_non_family() -> None:
    res = Architect().plan("Design an STM32 dev board", mode="offline")
    assert res.decision.qualified is False
    assert res.params is None
    assert res.decision.clarifying_questions


def test_auto_uses_llm_when_client_valid() -> None:
    client = FakeClient(_VALID)
    res = Architect().plan("build me an atmega board", mode="auto", client=client)
    assert res.source == "ericai" and client.calls == 1
    assert res.ready and res.params.crystal_mhz == 16


def test_auto_falls_back_on_llm_failure() -> None:
    client = FakeClient(raise_exc=LlmError("network down"))
    res = Architect().plan("atmega328 8MHz 3.3V", mode="auto", client=client)
    assert res.source == "deterministic"
    assert res.ready and res.params.crystal_mhz == 8


def test_required_fails_closed_on_invalid_params() -> None:
    bad = dict(_VALID)
    bad["params"] = {**_VALID["params"], "crystal_mhz": 16, "ldo_output_v": 3.3}  # illegal
    client = FakeClient(bad)
    with pytest.raises(LlmError):
        Architect().plan("atmega328 board", mode="required", client=client)


def test_required_fails_closed_when_no_client_available() -> None:
    # No injected client and ericai is not installed → resolve_client raises.
    with pytest.raises(LlmError):
        resolve_client(LlmMode.REQUIRED, None)


def test_llm_wrong_family_is_not_qualified() -> None:
    payload = dict(_VALID)
    payload["family"] = "stm32-dev-board"
    client = FakeClient(payload)
    res = Architect().plan("something", mode="auto", client=client)
    # Wrong family → not qualified, no params (auto keeps the ericai verdict).
    assert res.decision.qualified is False
    assert res.params is None


def test_llm_clarifying_questions_surface() -> None:
    payload = {
        "qualified": False,
        "family": "",
        "mandatory_features_present": False,
        "missing_features": ["ldo regulator"],
        "clarifying_questions": ["Which supply voltage do you need?"],
        "rationale": "ambiguous",
        "params": None,
    }
    client = FakeClient(payload)
    res = Architect().plan("a board", mode="required", client=client)
    assert res.decision.qualified is False
    assert res.decision.clarifying_questions == ["Which supply voltage do you need?"]
