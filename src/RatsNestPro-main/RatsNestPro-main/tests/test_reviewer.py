"""Task 7: Independent Reviewer — report, triage, severity preservation."""

from __future__ import annotations

import json

import pytest

from ratsnestpro.agents import LlmError, Reviewer
from ratsnestpro.families import Atmega328Params, build_ir, expectations_for
from ratsnestpro.verification import verify_design


def _clean_report():
    p = Atmega328Params()
    return verify_design(build_ir(p), expectations_for(p))


def _blocked_report():
    ir = build_ir(Atmega328Params(decoupling_count=4))
    exp = expectations_for(Atmega328Params(decoupling_count=6))
    return verify_design(ir, exp)


class FakeClient:
    def __init__(self, payload=None, raise_exc=None):
        self._payload = payload
        self._raise = raise_exc
        self.calls = 0

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        self.calls += 1
        if self._raise:
            raise self._raise
        return json.dumps(self._payload)


def test_no_findings_gives_empty_review_without_llm_call() -> None:
    client = FakeClient({"narrative": "x", "triage": []})
    result = Reviewer().review(_clean_report(), mode="auto", client=client)
    assert client.calls == 0
    assert result.triage == []
    assert result.blocked is False
    assert "# Design Review" in result.markdown


def test_offline_review_of_blocked_design() -> None:
    result = Reviewer().review(_blocked_report(), mode="offline")
    assert result.blocked is True
    assert result.source == "deterministic"
    assert any(t.severity == "error" for t in result.triage)
    md = result.markdown
    assert "BLOCKED" in md and "DEC-001" in md


def test_llm_cannot_change_severity() -> None:
    report = _blocked_report()
    fids = [f.finding_id for f in report.findings]
    # Malicious LLM tries to mark the error as info and a false positive.
    payload = {
        "narrative": "looks fine to me",
        "triage": [
            {"finding_id": fids[0], "severity": "info",
             "suspected_false_positive": True, "priority": "low", "note": "ignore"}
        ],
    }
    result = Reviewer().review(report, mode="auto", client=FakeClient(payload))
    # Severity is preserved from the original finding, not the LLM.
    first = next(t for t in result.triage if t.finding_id == fids[0])
    assert first.severity == "error"
    # Advisory fields may reflect the LLM, but the design is still blocked.
    assert result.blocked is True


def test_required_review_fails_closed_on_bad_output() -> None:
    with pytest.raises(LlmError):
        Reviewer().review(_blocked_report(), mode="required", client=FakeClient("not json"))


def test_auto_falls_back_when_llm_unavailable() -> None:
    result = Reviewer().review(
        _blocked_report(), mode="auto", client=FakeClient(raise_exc=LlmError("down"))
    )
    assert result.source == "deterministic"
    assert result.triage  # still has deterministic triage
