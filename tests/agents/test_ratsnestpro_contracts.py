"""Frozen-interface contract tests (Day 0 of docs/Production_Readiness_Plan.md).

Two engineers work in parallel on disjoint paths and meet at these APIs and state
fields. Freezing them in prose is not enforceable, so this pins them here: a
unilateral change fails CI instead of silently breaking the other track. Changing
anything asserted below requires agreement from both owners, and this file is
updated in the same commit.

Ownership:
  A  src/RatsNestPro-main/**, agents/ratsnestpro/evidence.py, diagnosis.py
  B  agents/ratsnestpro/tools.py, ratsnestpro_agent.py, core/, service/,
     scripts/, docker/, .github/workflows/
  shared contract module (needs both owners): agents/ratsnestpro/repair.py
"""

import inspect

from agents.ratsnestpro import evidence, repair
from agents.ratsnestpro.ratsnestpro_agent import RatsNestWorkflowState


def test_digest_prompt_signature_is_frozen() -> None:
    """A produces the digest; B decides how much prompt budget to give it."""
    params = inspect.signature(evidence.ViolationDigest.to_prompt).parameters

    assert list(params) == ["self", "max_rules", "max_objects", "max_pins"]
    assert all(
        params[name].default is not inspect.Parameter.empty
        for name in ("max_rules", "max_objects", "max_pins")
    )


def test_error_signatures_is_a_set_of_rule_object_strings() -> None:
    """The rule:object signature is the unit both tracks compare and score."""
    digest = evidence.ViolationDigest(
        findings=[
            evidence.ViolationFinding(
                kind="erc",
                rule_type="pin_not_connected",
                severity="error",
                description="Pin not connected",
                ref="U1",
                pin="7",
            )
        ]
    )

    assert digest.error_signatures == {"pin_not_connected:U1:7"}


def test_compare_signatures_keys_are_frozen() -> None:
    """B persists these keys and reports on them; A must keep producing them."""
    flips = evidence.compare_signatures(["a:1"], ["b:2"])

    assert set(flips) == {"fixed", "introduced", "persisted"}
    assert flips["fixed"] == ["a:1"]
    assert flips["introduced"] == ["b:2"]


def test_verdict_vocabulary_is_frozen() -> None:
    """The verdict drives keep/rollback in B's loop and A's redesign strategy."""
    from typing import get_args

    assert set(get_args(repair.RepairVerdict)) == {
        "EFFECTIVE",
        "PARTIALLY_EFFECTIVE",
        "MIXED",
        "INEFFECTIVE",
        "HARMFUL",
    }


def test_verdict_keep_and_continue_policy_is_frozen() -> None:
    """Only these verdicts may keep a result or authorise another round."""
    keeps = {
        verdict: repair.ChangeEvaluation(verdict=verdict).keeps_result
        for verdict in ("EFFECTIVE", "PARTIALLY_EFFECTIVE", "MIXED", "INEFFECTIVE", "HARMFUL")
    }

    assert keeps == {
        "EFFECTIVE": True,
        "PARTIALLY_EFFECTIVE": True,
        "MIXED": True,
        "INEFFECTIVE": False,
        "HARMFUL": False,
    }
    assert repair.ChangeEvaluation(verdict="HARMFUL").should_continue is False


def test_change_evaluation_report_fields_are_frozen() -> None:
    """B's benchmark report reads exactly these fields."""
    fields = set(repair.ChangeEvaluation.model_fields)

    assert {
        "repair_scope",
        "predicted_fixes",
        "actually_fixed",
        "still_failed",
        "unpredicted_fixes",
        "introduced",
        "risk_realized",
        "unattributed_regressions",
        "hit_rate",
        "verdict",
    } <= fields


def test_repair_patch_carries_the_four_field_change_contract() -> None:
    """Evidence, root cause, targeted fix and predicted impact."""
    fields = set(repair.RepairPatch.model_fields)

    assert {"failure_classes", "rationale", "actions", "predicted_fixes", "risk_objects"} <= fields


def test_shared_state_fields_are_frozen() -> None:
    """B owns writes, A only consumes. Renaming one breaks the other track."""
    annotations = set(RatsNestWorkflowState.__annotations__)

    assert {
        "verification_digest",
        "verification_signatures",
        "change_evaluations",
        "repair_patches",
        "diagnosis",
        "component_constraints",
    } <= annotations
