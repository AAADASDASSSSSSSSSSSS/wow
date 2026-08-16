"""Structured incremental repair patches for the AHE loop.

A blocked selection must not be answered by rewriting the whole BOM from step 1.
This module expresses a repair as a bounded, versioned delta: which scope it
targets, which state version it was computed against, which typed actions it
performs, and which pipeline steps must therefore re-run.

The patch is a *plan*: applying component changes to a real ``SelectionPlan``
goes through the existing :class:`ratsnestpro.orchestration.pipeline_contracts.
SelectionPatch` so the pipeline's own uniqueness validation still applies.

See ``docs/Intent_Routing_and_AHE_EHE.md`` sections 4.8 and 7.1.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.ratsnestpro.diagnosis import DiagnosisReport, RepairStrategy

# Canonical 17-step order; a patch re-runs its scope and everything downstream.
PIPELINE_ORDER: tuple[str, ...] = (
    "requirements",
    "topology",
    "selection",
    "schematic_connections",
    "schematic_pinmap",
    "schematic_layout",
    "schematic_materialize",
    "erc",
    "layout_partition",
    "layout_critical",
    "layout_general",
    "layout_write",
    "route_plan",
    "route_planes",
    "route_signals",
    "route_fab",
    "manufacture",
)

RepairActionType = Literal[
    "add_support_network",
    "implement_topology_block",
    "replace_symbol",
    "reassign_pins",
    "adjust_placement",
    "adjust_routing_constraints",
    "fix_connectivity",
    "fix_manufacturing_violation",
    "retry_evidence_source",
]

# Which action types a diagnosis strategy may emit. Keeps a repair from silently
# widening its authority (e.g. a pin conflict cannot rewrite the BOM).
_ALLOWED_ACTIONS: dict[RepairStrategy, tuple[RepairActionType, ...]] = {
    "extend_selection": ("add_support_network", "implement_topology_block"),
    "acquire_symbol": ("replace_symbol",),
    "reassign_pins": ("reassign_pins",),
    "adjust_layout_or_routing": ("adjust_placement", "adjust_routing_constraints"),
    "fix_flagged_object": ("fix_manufacturing_violation", "fix_connectivity"),
    "retry_with_alternate_source": ("retry_evidence_source",),
    "record_capability_gap": (),
    "block_honestly": (),
}

# A failure class may need a more specific action than its strategy's default,
# e.g. an ERC error is repaired by changing connectivity, not fab geometry.
_CLASS_ACTION: dict[str, RepairActionType] = {
    "erc_violation": "fix_connectivity",
}


class RepairAction(BaseModel):
    """One typed, bounded change requested by a repair patch."""

    model_config = ConfigDict(extra="forbid")

    type: RepairActionType
    target: str = Field(default="", max_length=120)
    roles: list[str] = Field(default_factory=list, max_length=64)
    block: str = Field(default="", max_length=200)
    detail: str = Field(default="", max_length=2_000)


class RepairPreconditions(BaseModel):
    """State the patch was computed against; a stale patch must be recomputed."""

    model_config = ConfigDict(extra="forbid")

    state_version: int = Field(default=0, ge=0)
    selection_version: int = Field(default=0, ge=0)
    completed_steps: int = Field(default=0, ge=0)


class RepairPatch(BaseModel):
    """A bounded repair delta scoped to one pipeline step.

    Carries the four fields the reference harness requires of every change:
    failure evidence (``failure_classes`` + each action's ``detail``), root cause
    (``rationale``), targeted fix (``actions``), and predicted impact
    (``predicted_fixes``/``risk_objects``) so the next attempt can falsify it.
    """

    model_config = ConfigDict(extra="forbid")

    repair_scope: str = Field(min_length=1, max_length=64)
    preconditions: RepairPreconditions = Field(default_factory=RepairPreconditions)
    actions: list[RepairAction] = Field(default_factory=list, max_length=64)
    failure_classes: list[str] = Field(default_factory=list, max_length=32)
    predicted_fixes: list[str] = Field(default_factory=list, max_length=512)
    risk_objects: list[str] = Field(default_factory=list, max_length=256)
    rationale: str = Field(default="", max_length=4_000)

    @model_validator(mode="after")
    def _known_scope(self) -> RepairPatch:
        if self.repair_scope not in PIPELINE_ORDER:
            raise ValueError(f"unknown repair scope: {self.repair_scope}")
        return self

    @property
    def affected_steps(self) -> tuple[str, ...]:
        """The scope plus every downstream step, which must all re-run."""
        index = PIPELINE_ORDER.index(self.repair_scope)
        return PIPELINE_ORDER[index:]

    def is_applicable(self, current: RepairPreconditions) -> bool:
        """Whether this patch is still fresh enough to apply.

        Section 7.1: two repairs computed from the same old version must not
        silently overwrite each other — the later one re-reads state instead.
        """
        return (
            self.preconditions.state_version == current.state_version
            and self.preconditions.selection_version == current.selection_version
        )


class RepairOutcome(BaseModel):
    """Result of one repair round, retained for traceability."""

    model_config = ConfigDict(extra="forbid")

    patch: RepairPatch
    applied: bool = False
    resumed_from: str = ""
    state_version_before: int = 0
    state_version_after: int = 0
    blockers_before: list[str] = Field(default_factory=list)
    blockers_after: list[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=2_000)

    @property
    def improved(self) -> bool:
        return len(self.blockers_after) < len(self.blockers_before)


def attempt_failure_score(result: dict[str, object]) -> tuple[int, int, int]:
    """Rank one hardware attempt: lower is better.

    Coarse fallback for when no verification report exists to compare object by
    object (:func:`evaluate_change` is the primary signal). Compares how far the
    pipeline got, then remaining release blockers, then ERC error count.
    """
    raw_completed = result.get("completed_steps")
    completed = raw_completed if isinstance(raw_completed, int) else 0
    blockers = result.get("release_blockers")
    blocker_count = len(blockers) if isinstance(blockers, list) else 99
    verification = result.get("verification")
    erc_errors = 0
    if isinstance(verification, dict):
        erc = verification.get("erc")
        if isinstance(erc, dict) and isinstance(erc.get("errors"), int):
            erc_errors = int(erc["errors"])
    return (-completed, blocker_count, erc_errors)


def repair_regressed(previous: dict[str, object], candidate: dict[str, object]) -> bool:
    """True when ``candidate`` is strictly worse than ``previous``."""
    return attempt_failure_score(candidate) > attempt_failure_score(previous)


RepairVerdict = Literal[
    "EFFECTIVE",
    "PARTIALLY_EFFECTIVE",
    "MIXED",
    "INEFFECTIVE",
    "HARMFUL",
]

# What the loop is allowed to do with an attempt, per verdict. Mirrors the
# reference harness's policy: HARMFUL must roll back, INEFFECTIVE is rolled back
# or redesigned, MIXED keeps the effective part.
_VERDICT_KEEPS_RESULT: dict[RepairVerdict, bool] = {
    "EFFECTIVE": True,
    "PARTIALLY_EFFECTIVE": True,
    "MIXED": True,
    "INEFFECTIVE": False,
    "HARMFUL": False,
}


class ChangeEvaluation(BaseModel):
    """Prediction-versus-reality scoring of one repair attempt.

    Object-level counterpart of the reference harness's ``change_evaluation``:
    a repair declares which violation signatures it should remove and which
    objects it risks, and the next verification run falsifies that claim.
    """

    model_config = ConfigDict(extra="forbid")

    repair_scope: str = ""
    predicted_fixes: list[str] = Field(default_factory=list)
    actually_fixed: list[str] = Field(default_factory=list)
    still_failed: list[str] = Field(default_factory=list)
    unpredicted_fixes: list[str] = Field(default_factory=list)
    introduced: list[str] = Field(default_factory=list)
    risk_realized: list[str] = Field(default_factory=list)
    unattributed_regressions: list[str] = Field(default_factory=list)
    hit_rate: str = "0/0"
    verdict: RepairVerdict = "INEFFECTIVE"

    @property
    def keeps_result(self) -> bool:
        """Whether the attempt's output may stay authoritative."""
        return _VERDICT_KEEPS_RESULT[self.verdict]

    @property
    def should_continue(self) -> bool:
        """Whether another repair round is worth attempting."""
        return self.verdict in {"EFFECTIVE", "PARTIALLY_EFFECTIVE", "MIXED"}

    def summary(self) -> str:
        return (
            f"{self.verdict} (hit {self.hit_rate}; "
            f"+{len(self.introduced)} introduced, "
            f"{len(self.risk_realized)} predicted risk realised)"
        )


def evaluate_change(
    patch: RepairPatch | None,
    flips: dict[str, list[str]],
) -> ChangeEvaluation:
    """Score a repair attempt from its predictions and the observed flips.

    ``flips`` is :func:`agents.ratsnestpro.evidence.compare_digests` output:
    ``fixed`` signatures disappeared, ``introduced`` are new, ``persisted``
    survived. Verdict thresholds follow the reference implementation, so a
    change that realises risk without fixing anything is HARMFUL, and one that
    fixes nothing at all is INEFFECTIVE.
    """
    predicted = list(patch.predicted_fixes) if patch else []
    risks = list(patch.risk_objects) if patch else []
    fixed = set(flips.get("fixed", []))
    introduced = set(flips.get("introduced", []))

    actually_fixed = [item for item in predicted if item in fixed]
    still_failed = [item for item in predicted if item not in fixed]
    unpredicted_fixes = sorted(fixed - set(predicted))
    risk_realized = [item for item in risks if item in introduced]
    unattributed = sorted(introduced - set(risks))

    n_fixed = len(actually_fixed) + len(unpredicted_fixes)
    n_predicted = len(predicted)
    n_introduced = len(introduced)

    if n_introduced > n_fixed:
        verdict: RepairVerdict = "HARMFUL"
    elif n_introduced and n_fixed:
        verdict = "MIXED"
    elif n_fixed == 0:
        verdict = "INEFFECTIVE"
    elif n_predicted and len(actually_fixed) == n_predicted:
        verdict = "EFFECTIVE"
    else:
        verdict = "PARTIALLY_EFFECTIVE"

    return ChangeEvaluation(
        repair_scope=patch.repair_scope if patch else "",
        predicted_fixes=predicted,
        actually_fixed=actually_fixed,
        still_failed=still_failed,
        unpredicted_fixes=unpredicted_fixes,
        introduced=sorted(introduced),
        risk_realized=risk_realized,
        unattributed_regressions=unattributed,
        hit_rate=f"{len(actually_fixed)}/{n_predicted}" if n_predicted else f"{n_fixed}/0",
        verdict=verdict,
    )


def _obligation_roles(report: DiagnosisReport) -> list[str]:
    roles: list[str] = []
    for diagnosis in report.recoverable:
        roles.extend(diagnosis.targets)
    return list(dict.fromkeys(roles))


def plan_repair(
    report: DiagnosisReport,
    *,
    preconditions: RepairPreconditions,
    missing_roles: list[str] | None = None,
    missing_blocks: list[str] | None = None,
    max_scope: str = "",
    predicted_fixes: list[str] | None = None,
    risk_objects: list[str] | None = None,
) -> RepairPatch | None:
    """Build a scoped patch from a diagnosis report, or None when not repairable.

    ``missing_roles``/``missing_blocks`` come from the capability obligation graph
    so a selection repair asks for exactly the absent support roles instead of
    regenerating the BOM. ``max_scope`` is the step that actually blocked:
    resuming after it would skip the failure instead of fixing it, since derived
    blockers ("Freerouting did not complete") name later steps that were never
    reached.
    """
    if not report.should_attempt_repair:
        return None
    scope = report.primary_scope()
    if not scope:
        return None
    if max_scope in PIPELINE_ORDER and PIPELINE_ORDER.index(scope) > PIPELINE_ORDER.index(
        max_scope
    ):
        scope = max_scope
    actions: list[RepairAction] = []
    seen: set[tuple[str, str]] = set()
    for diagnosis in report.recoverable:
        if diagnosis.repair_scope != scope:
            continue
        allowed = _ALLOWED_ACTIONS.get(diagnosis.strategy, ())
        if not allowed:
            continue
        action_type = _CLASS_ACTION.get(diagnosis.failure_class, allowed[0])
        if action_type not in allowed:
            action_type = allowed[0]
        if action_type == "add_support_network":
            roles = list(missing_roles or []) or _obligation_roles(report)
            target = diagnosis.targets[0] if diagnosis.targets else ""
            key = (action_type, target)
            if key in seen:
                continue
            seen.add(key)
            actions.append(
                RepairAction(
                    type=action_type,
                    target=target,
                    roles=roles[:64],
                    detail=diagnosis.summary,
                )
            )
            continue
        for block in missing_blocks or [""]:
            key = (action_type, block or diagnosis.summary[:40])
            if key in seen:
                continue
            seen.add(key)
            actions.append(
                RepairAction(
                    type=action_type,
                    target=diagnosis.targets[0] if diagnosis.targets else "",
                    block=block,
                    detail=diagnosis.summary,
                )
            )
    if not actions:
        return None
    return RepairPatch(
        repair_scope=scope,
        preconditions=preconditions,
        actions=actions,
        failure_classes=[d.failure_class for d in report.recoverable],
        predicted_fixes=list(predicted_fixes or []),
        risk_objects=list(risk_objects or []),
        rationale=(
            f"AHE repair for {len(actions)} action(s) at scope {scope}; "
            "downstream steps re-run from there."
        ),
    )


def to_selection_patch(patch: RepairPatch) -> object | None:
    """Translate selection-scope actions into the pipeline's SelectionPatch.

    Returns ``None`` when the patch has nothing the pipeline contract can carry;
    the pipeline's own validation remains authoritative for what it accepts.
    """
    if patch.repair_scope != "selection":
        return None
    try:
        from ratsnestpro.orchestration.pipeline_contracts import SelectionPatch
    except Exception:
        return None
    removals = [
        action.target
        for action in patch.actions
        if action.type == "replace_symbol" and action.target
    ]
    if not removals:
        return None
    return SelectionPatch(remove_refs=list(dict.fromkeys(removals)), rationale=patch.rationale)


def resume_plan(patch: RepairPatch, completed_steps: list[str]) -> dict[str, object]:
    """Steps to keep vs discard when resuming from the patched scope.

    Feeds the existing ``pipeline_state.json`` checkpoint: artifacts before the
    scope stay valid, the scope and everything after it are recomputed.
    """
    affected = set(patch.affected_steps)
    keep = [step for step in completed_steps if step not in affected]
    discard = [step for step in completed_steps if step in affected]
    return {
        "resume_from": patch.repair_scope,
        "keep_steps": keep,
        "discard_steps": discard,
        "rerun_steps": list(patch.affected_steps),
    }
