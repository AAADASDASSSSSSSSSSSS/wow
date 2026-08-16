"""Task 8: repair loop + root-cause diagnosis + whitelist enforcement."""

from __future__ import annotations

import json

import pytest

from ratsnestpro.agents import Coder, LlmError, apply_actions
from ratsnestpro.domain.contracts import RepairAction
from ratsnestpro.families import Atmega328Params, build_ir, expectations_for
from ratsnestpro.orchestration import run_repair
from ratsnestpro.verification import verify_design


class FakeClient:
    def __init__(self, payload=None, raise_exc=None):
        self._payload = payload
        self._raise = raise_exc

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        if self._raise:
            raise self._raise
        return json.dumps(self._payload)


def test_apply_actions_rejects_illegal_operation() -> None:
    with pytest.raises(LlmError):
        apply_actions(Atmega328Params(), [RepairAction(operation="run_shell", arguments={})])


def test_apply_actions_rejects_non_whitelisted_param() -> None:
    with pytest.raises(LlmError):
        apply_actions(
            Atmega328Params(),
            [RepairAction(operation="set_param", arguments={"name": "backdoor", "value": 1})],
        )


def test_repair_converges_on_decoupling_mismatch() -> None:
    # Start with 4 decouplers, target expects 6 → loop should converge.
    start = Atmega328Params(crystal_mhz=8, ldo_output_v=3.3, decoupling_count=4)
    target = expectations_for(Atmega328Params(crystal_mhz=8, ldo_output_v=3.3, decoupling_count=6))
    result = run_repair(start, target, max_iter=5, mode="offline")
    assert result.success is True
    assert result.params.decoupling_count == 6
    assert result.iterations >= 1


def test_repair_converges_on_crystal_and_voltage() -> None:
    # 8 MHz / 3.3 V but target is 16 MHz / 5 V — needs coupled param change.
    start = Atmega328Params(crystal_mhz=8, ldo_output_v=3.3)
    target = expectations_for(Atmega328Params(crystal_mhz=16, ldo_output_v=5.0))
    result = run_repair(start, target, max_iter=5, mode="offline")
    assert result.success is True
    assert result.params.crystal_mhz == 16 and result.params.ldo_output_v == 5.0


def test_repair_already_satisfied_is_zero_iterations() -> None:
    p = Atmega328Params()
    result = run_repair(p, expectations_for(p), max_iter=5, mode="offline")
    assert result.success and result.iterations == 0


def test_repair_fails_closed_when_coder_gives_up() -> None:
    # Force a blocked report whose failing gate the deterministic Coder can't map.
    # A catalog failure isn't mapped to any param → give up.
    start = Atmega328Params()
    ir = build_ir(start)
    ir.components[0].catalog_id = ""  # break catalog → CAT-001
    target = expectations_for(start)
    # Verify to confirm it is blocked, then run the loop with this scenario.
    assert verify_design(ir, target).blocked

    # Coder.diagnose over a report with only a catalog failure yields give_up.
    coder = Coder()
    report = verify_design(ir, target)
    decision = coder.diagnose(report, start, target, mode="offline")
    assert decision.give_up is True


def test_live_coder_whitelist_rejects_illegal_action() -> None:
    payload = {
        "diagnosis": "malicious",
        "give_up": False,
        "actions": [{"operation": "run_shell", "arguments": {"cmd": "rm -rf /"}}],
    }
    start = Atmega328Params(decoupling_count=4)
    target = expectations_for(Atmega328Params(decoupling_count=6))
    report = verify_design(build_ir(start), target)
    # required mode must fail closed on the illegal action.
    with pytest.raises(LlmError):
        Coder().diagnose(report, start, target, mode="required", client=FakeClient(payload))


def test_semi_auto_stop_declines_repair() -> None:
    start = Atmega328Params(decoupling_count=4)
    target = expectations_for(Atmega328Params(decoupling_count=6))
    result = run_repair(
        start, target, max_iter=5, mode="offline", on_step=lambda i, d, p: False
    )
    assert result.success is False
    assert "stopped" in result.reason
