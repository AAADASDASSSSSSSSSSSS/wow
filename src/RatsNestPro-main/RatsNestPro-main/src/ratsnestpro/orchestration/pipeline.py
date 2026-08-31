"""The fixed, industry-standard PCB pipeline framework.

The process is *pinned*: a fixed, ordered sequence of steps that cannot be
skipped or reordered. Every step has the same shape —

    inject knowledge  ->  LLM structured proposal  ->  bottom-line check

The LLM makes the design decisions (fed the relevant knowledge for that step);
a small, cheap "anti-board-burn" check validates the proposal against real
libraries and fab values — it never encodes business rules, it only catches
things that would ruin a board (missing pins, single-pin nets, sub-fab widths).

Design stance:
* ``offline`` mode uses each step's deterministic fallback (no model calls).
* ``auto`` uses the LLM and falls back to the deterministic path on failure.
* ``required`` must use the LLM; a failure or invalid output fails closed.

This module provides the framework plus the concrete steps
(requirements, topology, selection, component preparation). Each one
subclasses :class:`PipelineStepBase` and is registered in :data:`ALL_STEPS`.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

from pydantic import BaseModel

from ratsnestpro import config
from ratsnestpro.agents.llm import LlmError, LlmMode
from ratsnestpro.domain.contracts import ContractModel, RequirementSpec, Severity
from ratsnestpro.eda import factbrief, factclaim, factgate, footprints, grounding, symbols
from ratsnestpro.eda import factsheet as factsheet_module
from ratsnestpro.eda.adapter import kicad_cli_available, run_erc
from ratsnestpro.eda.factsheet import DeviceClass, FactSheetBase, all_fact_sheets
from ratsnestpro.eda.materialize import materialize_pinmapped
from ratsnestpro.knowledge import KnowledgeBase, build_default_kb
from ratsnestpro.orchestration import check_classes
from ratsnestpro.orchestration.pipeline_contracts import (
    COMPONENT_RELEASE_MANIFEST_SCHEMA,
    COMPONENT_RELEASE_POLICY,
    BoardPartition,
    BoardZone,
    ComponentPrepareResult,
    ComponentRoleSpec,
    ErcSummary,
    FabAudit,
    LogicalPin,
    ManufactureResult,
    MappedNet,
    MappedPin,
    MaterializeResult,
    NetClass,
    NetIntent,
    NetlistIntent,
    NetlistPatch,
    PcbPlacement,
    PcbPlacementPlan,
    PcbWriteResult,
    PinMapPlan,
    PlanePlan,
    PreparedComponent,
    RoutePlan,
    RouteResult,
    SchLayoutPlan,
    SelectedPart,
    SelectionPatch,
    SelectionPlan,
    SheetPlacement,
    TopologyBlock,
    TopologyPlan,
)

# --------------------------------------------------------------------------- #
# The pinned step sequence
# --------------------------------------------------------------------------- #


class PipelineStep(StrEnum):
    """The fixed industry-standard flow. Order is authoritative and enforced."""

    REQUIREMENTS = "requirements"
    TOPOLOGY = "topology"
    SELECTION = "selection"
    COMPONENT_PREPARE = "component_prepare"
    SCH_CONNECTIONS = "schematic_connections"
    SCH_PINMAP = "schematic_pinmap"
    SCH_LAYOUT = "schematic_layout"
    SCH_MATERIALIZE = "schematic_materialize"
    ERC = "erc"
    LAYOUT_PARTITION = "layout_partition"
    LAYOUT_CRITICAL = "layout_critical"
    LAYOUT_GENERAL = "layout_general"
    LAYOUT_WRITE = "layout_write"
    ROUTE_PLAN = "route_plan"
    ROUTE_PLANES = "route_planes"
    ROUTE_SIGNALS = "route_signals"
    ROUTE_FAB = "route_fab"
    MANUFACTURE = "manufacture"


# Canonical order (StrEnum preserves definition order).
CANONICAL_ORDER: list[PipelineStep] = list(PipelineStep)
PIPELINE_TOTAL_STEPS = len(CANONICAL_ORDER)
_ORDER_INDEX: dict[PipelineStep, int] = {s: i for i, s in enumerate(CANONICAL_ORDER)}


# --------------------------------------------------------------------------- #
# Results and state
# --------------------------------------------------------------------------- #


class CheckResult(ContractModel):
    """One bottom-line check outcome. An unmet ERROR check blocks the pipeline."""

    name: str
    ok: bool
    severity: Severity = Severity.ERROR
    message: str = ""
    # The specific objects this check is about: component references, net names,
    # ``ref:pin`` pairs. Declared by the check rather than recovered from the
    # message downstream, because that recovery is a regex over prose — it reads
    # single-letter-prefixed references only (so ``FB1``, ``MH1`` and ``TP1`` are
    # invisible), never reads net names, and cannot distinguish a real reference
    # from a token that merely looks like one. Empty means "not declared", and
    # the diagnosis layer then falls back to the regex as before.
    targets: list[str] = []

    @property
    def failure_class(self) -> str | None:
        """The class declared for this check, or None when it is unmapped.

        A property rather than a stored field: ``check_classes`` is where the
        mapping is maintained and reviewed, and a copy on every instance could
        disagree with it. ``None`` means "no declaration" and must be treated as
        "fall back to inference" — never as a class in its own right.
        """
        from ratsnestpro.orchestration.check_classes import failure_class_for

        return failure_class_for(self.name)


class StepResult(ContractModel):
    """Per-step outcome. The artifact itself is stored on the state object."""

    step: PipelineStep
    used_llm: bool = False
    knowledge_used: list[str] = []
    # ``device:slot`` for every datasheet fact shown to this step. Recorded so a
    # finished board can answer "which manual figures did this design see?" —
    # ``knowledge_used`` already does that for soft knowledge, and a cited hard
    # fact deserves at least the same traceability.
    facts_used: list[str] = []
    # Deterministic corrections this step applied on its own, one line each.
    # Recording them is not cosmetic: AHE attributes a change in check outcomes
    # to whatever it believes acted, so a silent correction lands in
    # ``unattributed_regressions`` and can flip a verdict from HARMFUL to
    # EFFECTIVE — and HARMFUL is what forces a rollback.
    auto_fixes: list[str] = []
    checks: list[CheckResult] = []
    blocked: bool = False
    summary: str = ""

    @property
    def error_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok and c.severity == Severity.ERROR]


@dataclass
class PipelineState:
    """Mutable state threaded through the pipeline."""

    requirement_text: str
    project_name: str = "generated_board"
    artifacts: dict[PipelineStep, BaseModel] = field(default_factory=dict)
    results: list[StepResult] = field(default_factory=list)
    # Verdicts on the values the USER asked for, computed once by
    # ``RequirementsStep.propose`` and read by its ``check``. Held here rather
    # than on ``RequirementSpec`` on purpose: that model is what the LLM produces,
    # and a conflict verdict the model could rewrite would be a gate the model
    # could open. Later steps read it too, to report accepted risks.
    claim_verdicts: list[factclaim.ClaimVerdict] = field(default_factory=list)
    # Deterministic corrections made while producing an artifact, keyed by step.
    # Whatever makes the correction records it here; ``PipelineStepBase.run``
    # moves the entries onto the ``StepResult`` and truncates the ones a
    # discarded repair round made, so a recorded fix is one that actually shipped.
    auto_fixes: dict[PipelineStep, list[str]] = field(default_factory=dict)

    def record_auto_fix(self, step: PipelineStep, description: str) -> None:
        """Record one deterministic correction, in the words a reviewer needs.

        Not optional bookkeeping. AHE attributes a change in check outcomes to
        whatever it believes acted; a correction made silently lands in
        ``unattributed_regressions``, which distorts the verdict and can read
        HARMFUL as EFFECTIVE — and HARMFUL is what forces a rollback. So a
        correction that is not recorded is worse than one not made.
        """
        self.auto_fixes.setdefault(step, []).append(description)

    def accepted_risks(self) -> list[factclaim.ClaimVerdict]:
        """Conflicts the user was warned about and chose to accept."""
        return [v for v in self.claim_verdicts if not v.ok and v.acknowledged]

    def artifact(self, step: PipelineStep) -> BaseModel | None:
        return self.artifacts.get(step)

    @property
    def completed(self) -> list[PipelineStep]:
        return [r.step for r in self.results]

    @property
    def blocked(self) -> bool:
        return any(r.blocked for r in self.results)


@dataclass
class PipelineContext:
    """Shared services for steps: LLM mode/client and the knowledge base."""

    mode: LlmMode = LlmMode.OFFLINE
    client: object | None = None  # LLMClient | None (kept loose to avoid import cycle)
    kb: KnowledgeBase = field(default_factory=build_default_kb)
    out_dir: str | None = None  # where materialized artifacts (.kicad_sch) are written
    repair_feedback: str = ""  # bottom-line check failures fed back for LLM self-repair
    # Datasheet facts for the step currently proposing, rendered by
    # ``eda.factbrief``. Set by ``PipelineStepBase.run`` immediately before
    # ``propose`` and cleared afterwards, exactly like ``repair_feedback`` — this
    # avoids threading a new argument through every ``propose``/``repair``
    # signature in the file. Kept SEPARATE from the retrieved ``knowledge``
    # string on purpose: ``knowledge.store`` states that corpus text is "never
    # treated as fact", and merging cited datasheet limits into advisory prose
    # would erase exactly that distinction.
    fact_brief: str = ""
    repair_attempts: int = 0  # how many times a blocked LLM step may re-propose (opt-in)
    # Safe default for every caller, including the direct Python API. Planning
    # and diagnostic callers may opt out explicitly, but an omitted flag must
    # never turn an unrouted placement into a successful build.
    require_freerouting: bool = True
    capture_step_errors: bool = False
    # Diagnostic only: keep running past a blocked step to see how far the flow
    # reaches. Artifacts produced after a block are NOT trustworthy.
    continue_on_blocked: bool = False
    on_step_completed: Callable[[PipelineState, StepResult], None] | None = None
    # A single step can run for minutes, so a frontend that only sees completions
    # cannot tell a working run from a hung one.
    on_step_started: Callable[[PipelineState, PipelineStep], None] | None = None


_MAX_REPAIR_ARTIFACT_CHARS = 80_000

# Fallback board rectangle in mm, used only when no proposal supplies an
# outline. Deliberately generic: a step must not inherit the dimensions of one
# particular reference board.
_DEFAULT_OUTLINE_MM = (70.0, 50.0)

# Regulated rails a requirement may name, checked most specific first. Used only
# to label the fallback topology's rails; a proposal normally states the rail.
# Replaces a family-specific params extractor that only knew 3.3 V and 5.0 V.
_TEXT_RAILS = ("1.8", "2.5", "3.3", "5")


def _requested_rail_v(text: str) -> str:
    """The regulated rail voltage named in free text, defaulting to 3.3 V."""
    t = text.lower().replace(" ", "")
    for rail in _TEXT_RAILS:
        if f"{rail}v" in t or rail.replace(".", "v") in t:
            return rail
    return "3.3"


# --------------------------------------------------------------------------- #
# LLM proposal helper (structured, fail-closed in required mode)
# --------------------------------------------------------------------------- #

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> str:
    """Pull the first JSON object out of a model response (tolerates fences)."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        # drop an optional leading language tag like ``json``
        stripped = re.sub(r"^json\s*", "", stripped, flags=re.IGNORECASE)
    m = _JSON_RE.search(stripped)
    return m.group(0) if m else stripped


def _close_truncated_json(text: str) -> str | None:
    """Close containers when a model response ends after a complete JSON value.

    Providers can stop at their output limit after the final complete item in a
    long array. In that narrow case the existing values are still valid model
    output; adding only the missing ``]``/``}`` delimiters is deterministic.
    Responses cut inside a string, with mismatched delimiters, or after a
    dangling comma are not repaired.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    pairs = {"}": "{", "]": "["}
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack or stack.pop() != pairs[char]:
                return None
    if in_string or not stack or text.rstrip().endswith((",", ":")):
        return None
    closers = {"{": "}", "[": "]"}
    return text + "".join(closers[opener] for opener in reversed(stack))


def propose_structured[T: BaseModel](
    ctx: PipelineContext,
    *,
    model: type[T],
    system: str,
    user: str,
    fallback: Callable[[], T],
) -> tuple[T, bool]:
    """Get a validated ``model`` instance: LLM proposal or deterministic fallback.

    Returns ``(artifact, used_llm)``. In ``required`` mode a missing client,
    request failure, or invalid/unparseable output raises :class:`LlmError`
    (fail closed). In ``auto`` mode any such failure falls back deterministically.
    In ``offline`` mode the fallback is used directly.
    """
    client = ctx.client
    if ctx.mode == LlmMode.OFFLINE or client is None:
        if ctx.mode == LlmMode.REQUIRED:
            raise LlmError("required LLM mode but no client is available")
        return fallback(), False
    # LLMs occasionally emit truncated or slightly malformed JSON. Retry a few
    # times, tightening the instruction each round, before deciding.
    attempts = 3
    last_exc: Exception | None = None
    for i in range(attempts):
        prompt = user
        if ctx.repair_feedback:
            prompt = (
                f"{prompt}\n\nYour previous proposal was rejected by a bottom-line "
                f"check:\n{ctx.repair_feedback}\nFix exactly these problems and return "
                "corrected JSON. Do not reintroduce them."
            )
        if i:
            schema = json.dumps(
                model.model_json_schema(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            prompt = (
                f"{prompt}\n\nIMPORTANT: your previous reply was not valid JSON "
                f"({last_exc}). Reply with a SINGLE minified JSON object only — "
                "no prose, no markdown fences, no trailing commas. The reply may "
                "have failed schema validation even when it was valid JSON; correct "
                "the reported field types and constraints. Required JSON schema: "
                f"{schema[:12_000]}"
            )
        try:
            raw = client.complete(system, prompt)  # type: ignore[attr-defined]
            candidate = _extract_json(raw)
            try:
                artifact = model.model_validate_json(candidate)
            except Exception:
                repaired = _close_truncated_json(candidate)
                if repaired is None:
                    raise
                artifact = model.model_validate_json(repaired)
            return artifact, True
        except Exception as exc:  # parse or validation failure
            last_exc = exc
    if ctx.mode == LlmMode.REQUIRED:
        raise LlmError(f"{model.__name__} proposal failed: {last_exc}") from last_exc
    return fallback(), False


# --------------------------------------------------------------------------- #
# Step base class
# --------------------------------------------------------------------------- #


def _repair_failure_line(check: CheckResult) -> str:
    """One failure, with whatever structure the check declared about it.

    The class tells the model what kind of edit is wanted and the target list
    bounds where to make it. Both are omitted when the check declared neither,
    so a line never claims more structure than exists.
    """
    head = f"- {check.name}"
    if check.failure_class:
        head += f" [{check.failure_class}]"
    if check.targets:
        head += f" targets: {', '.join(check.targets)}"
    return f"{head}: {check.message}"


class PipelineStepBase(ABC):
    """A single pipeline step: knowledge -> proposal -> bottom-line check."""

    step: ClassVar[PipelineStep]
    knowledge_role: ClassVar[str | None] = None

    def knowledge_query(self, state: PipelineState) -> str | None:
        """Query used to retrieve step knowledge; ``None`` skips retrieval."""
        return None

    def fact_sheets_for_step(
        self, state: PipelineState
    ) -> list[tuple[str, FactSheetBase]] | None:
        """Devices whose datasheet facts this step should be shown.

        ``None`` — the default — means this step gets no fact brief. Overriding it
        is the whole opt-in: a step that does not call a model has nothing to do
        with a prompt, and a step that has not yet resolved any device has nothing
        to look up. See ``factbrief.BRIEFED_STEPS`` for which steps do.

        Return ``factbrief.resolve_sheets(parts)`` once a selection exists, or
        ``factbrief.sheets_mentioned(state.requirement_text)`` before it does.
        """
        return None

    def uncovered_parts_for_step(self, state: PipelineState) -> list[str]:
        """Names of this step's parts that no fact sheet answers for.

        Reported alongside the brief so an absent entry cannot be read as an
        absent constraint — the same reason a ``not_asserted`` slot is rendered
        rather than dropped, applied at the level of a whole device.
        """
        return []

    @abstractmethod
    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        """Produce the (validated) artifact + used_llm flag. Sets no state."""

    @abstractmethod
    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        """Cheap bottom-line checks against real libraries / fab values."""

    def repair(
        self,
        state: PipelineState,
        ctx: PipelineContext,
        knowledge: str,
        artifact: BaseModel,
        checks: list[CheckResult],
    ) -> tuple[BaseModel, bool]:
        """Repair a rejected proposal; subclasses may return a bounded delta."""
        return self.propose(state, ctx, knowledge)

    def run(self, state: PipelineState, ctx: PipelineContext) -> StepResult:
        knowledge = ""
        knowledge_ids: list[str] = []
        query = self.knowledge_query(state)
        if query and self.knowledge_role is not None:
            hits = ctx.kb.retrieve(query, top_k=3, role=self.knowledge_role)
            knowledge = "\n\n".join(f"[{h.doc.id}]\n{h.doc.text.strip()}" for h in hits)
            knowledge_ids = [h.doc.id for h in hits]
        # Datasheet facts, held on the context for the whole propose/repair
        # lifecycle: a repair re-proposes and needs the same grounding as the
        # first attempt. Cleared in the `finally` at the end so a step can never
        # read another step's brief.
        facts_used = self._install_fact_brief(state, ctx)
        try:
            return self._run_proposal(
                state, ctx, knowledge, knowledge_ids, facts_used
            )
        finally:
            ctx.fact_brief = ""

    def _install_fact_brief(
        self, state: PipelineState, ctx: PipelineContext
    ) -> list[str]:
        """Render this step's brief onto ``ctx`` and return what it contained."""
        ctx.fact_brief = ""
        sheets = self.fact_sheets_for_step(state)
        if not sheets:
            return []
        step_name = type(self).__name__
        ctx.fact_brief = factbrief.brief(
            step_name, sheets, uncovered=self.uncovered_parts_for_step(state)
        )
        if not ctx.fact_brief:
            return []
        return [
            f"{sheet.device}:{slot_name}"
            for sheet in factbrief.unique_devices(sheets)
            for slot_name in factbrief.slots_for_step(step_name, sheet)
            if (slot := sheet.slot(slot_name)) is not None and slot.asserted
        ]

    def _run_proposal(
        self,
        state: PipelineState,
        ctx: PipelineContext,
        knowledge: str,
        knowledge_ids: list[str],
        facts_used: list[str],
    ) -> StepResult:
        artifact, used_llm = self.propose(state, ctx, knowledge)
        checks = self.check(state, artifact)
        blocked = any(not c.ok and c.severity == Severity.ERROR for c in checks)
        best_artifact = artifact
        best_checks = checks

        def failure_score(results: list[CheckResult]) -> tuple[int, int, int]:
            failed = [check for check in results if not check.ok]
            return (
                sum(
                    not check.ok and check.severity == Severity.ERROR
                    for check in results
                ),
                len(failed),
                sum(len(check.message) for check in failed),
            )

        # Check-repair loop: when the bottom-line check blocks an LLM proposal,
        # feed the failures back and let the model correct itself (bounded).
        # Deterministic (offline) proposals are not retried — they are fixed.
        if blocked and used_llm and ctx.mode != LlmMode.OFFLINE:
            repair_budget = max(0, ctx.repair_attempts)
            adaptive_limit = min(10, repair_budget + 2)
            attempt = 0
            while attempt < repair_budget:
                attempt += 1
                # Corrections recorded past this point belong to this round, and
                # are dropped again if the round's candidate loses.
                fixes_before = len(state.auto_fixes.get(self.step, ()))
                fails = [
                    check
                    for check in best_checks
                    if not check.ok and check.severity == Severity.ERROR
                ]
                failure_text = "\n".join(
                    _repair_failure_line(c)
                    for c in fails
                )
                directives = check_classes.repair_directives(c.name for c in fails)
                rejected_json = best_artifact.model_dump_json(
                    exclude={"rationale"},
                )
                if len(rejected_json) > _MAX_REPAIR_ARTIFACT_CHARS:
                    rejected_json = (
                        f"<omitted: {len(rejected_json)} characters exceeds "
                        f"{_MAX_REPAIR_ARTIFACT_CHARS}>"
                    )
                ctx.repair_feedback = (
                    "Rejected proposal JSON:\n"
                    f"{rejected_json}\n"
                    "Failed checks:\n"
                    f"{failure_text}"
                    + (
                        "\nWhat kind of edit each class needs:\n"
                        + "\n".join(f"- {d}" for d in directives)
                        if directives
                        else ""
                    )
                )
                try:
                    candidate, candidate_used_llm = self.repair(
                        state,
                        ctx,
                        knowledge,
                        best_artifact,
                        best_checks,
                    )
                except LlmError:
                    # The already validated, blocked proposal and its deterministic
                    # check evidence are more useful than losing the entire step
                    # because one repair response could not be parsed.
                    break
                finally:
                    ctx.repair_feedback = ""
                candidate_checks = self.check(state, candidate)
                if failure_score(candidate_checks) < failure_score(best_checks):
                    best_artifact = candidate
                    best_checks = candidate_checks
                    used_llm = candidate_used_llm
                    # Complex artifacts often need a sequence of small safe
                    # deltas. Reward actual convergence with up to two extra
                    # rounds, while never extending a stagnant retry loop.
                    if repair_budget < adaptive_limit:
                        repair_budget += 1
                else:
                    # This candidate is discarded, so any correction made while
                    # building it did not ship. Recording it anyway would make
                    # AHE attribute an effect to a change that is not in the
                    # artifact.
                    del state.auto_fixes.setdefault(self.step, [])[fixes_before:]
                if failure_score(best_checks)[0] == 0:
                    break
            artifact = best_artifact
            checks = best_checks
            blocked = failure_score(checks)[0] > 0

        state.artifacts[self.step] = artifact
        result = StepResult(
            step=self.step,
            used_llm=used_llm,
            knowledge_used=knowledge_ids,
            facts_used=facts_used,
            auto_fixes=list(state.auto_fixes.get(self.step, ())),
            checks=checks,
            blocked=blocked,
            summary=self.summarize(artifact),
        )
        state.results.append(result)
        return result

    def summarize(self, artifact: BaseModel) -> str:
        return type(artifact).__name__


# --------------------------------------------------------------------------- #
# Concrete steps (Task 4 seeds the first two; later tasks add the rest)
# --------------------------------------------------------------------------- #


# Prompt wording shared by every step that is shown datasheet facts. Kept in one
# place because the two sentences carry the whole contract: the facts outrank the
# model's recollection, and an explicit "NOT STATED" is a statement of ignorance
# rather than a grant of freedom. A step that copied only the first sentence would
# be inviting the model to fill the gap itself.
_FACT_AUTHORITY = (
    "The datasheet facts supplied below are AUTHORITATIVE and carry page-level "
    "citations: prefer them over any value you recall. A line marked NOT STATED "
    "means no consulted source gives that figure - treat it as UNKNOWN, never as "
    "unlimited, and do not substitute a number of your own. A part listed as "
    "having no datasheet facts is missing evidence, not free of constraints."
)


def _facts_block(ctx: PipelineContext) -> str:
    """The fact brief as its own labelled prompt section, or ``""``.

    Deliberately a SEPARATE block from ``Knowledge:``.
    :mod:`ratsnestpro.knowledge.store` states that retrieved corpus text is
    "never treated as fact"; concatenating cited datasheet limits into that same
    section would erase the distinction the two-tier knowledge stance exists to
    maintain.
    """
    if not ctx.fact_brief:
        return ""
    return f"Datasheet facts (authoritative, cited):\n{ctx.fact_brief}\n\n"


def _preflight_checks() -> list[CheckResult]:
    """Environment probes as checks: never blocking, never a silent pass.

    Severity is WARNING for all of them by decision: a missing dependency must
    not stop a run that can still produce useful artifacts. What it must not do
    is look like success — an unavailable library means the checks downstream of
    it stopped verifying anything, and the message says exactly that.

    The names carry the ``tool_unavailable`` failure class, whose repair strategy
    is ``record_capability_gap``: nothing in the design can fix a tool that is
    not installed, so classifying it as a design defect would send a repair round
    after an unfixable target.
    """
    from ratsnestpro.eda import preflight as preflight_module

    report = preflight_module.preflight()
    return [
        CheckResult(
            name=f"tool_unavailable.{probe.name}",
            ok=probe.available,
            severity=Severity.WARNING,
            message=probe.message(),
        )
        for probe in report.probes
    ]


class RequirementsStep(PipelineStepBase):
    step = PipelineStep.REQUIREMENTS
    knowledge_role = "general"

    def knowledge_query(self, state: PipelineState) -> str | None:
        """Retrieved only to ground the EXPERIENCE check, not the normalization.

        The soft corpus is what a value outside every datasheet is judged
        against, so the query names the values rather than the board.
        """
        claims = factclaim.extract_claims(state.requirement_text)
        if not claims:
            return None
        return "typical values and design practice for: " + ", ".join(
            f"{claim.value:g} {claim.unit} {claim.slots[0]}" for claim in claims
        )

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        def fallback() -> RequirementSpec:
            return RequirementSpec(
                raw_text=state.requirement_text, project_name=state.project_name
            )

        system = (
            "You normalize a hardware requirement into JSON with fields "
            "raw_text, project_name, constraints[], acceptance_criteria[]."
        )
        spec, used_llm = propose_structured(
            ctx, model=RequirementSpec, system=system, user=state.requirement_text,
            fallback=fallback,
        )
        # Arbitration runs here, in propose, because Tier 2 may consult a model
        # and `check` must stay deterministic. The result is stored on the state
        # so `check` only reads a settled conclusion.
        state.claim_verdicts = list(self._arbitrate(state, ctx, knowledge).verdicts)
        return spec, used_llm

    def _arbitrate(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> factclaim.Arbitration:
        # A claim is something the USER asked for, so it is read from the original
        # requirement only. ``state.requirement_text`` grows during a run —
        # grounded architect evidence, datasheet excerpts — and those texts are
        # dense with numbers that are facts rather than requests. A shipped run
        # stopped at step 1 demanding acknowledgement of "clock_external = 36 MHz"
        # quoted out of "...APB domain is 36 MHz. See Figure 2...", a sentence
        # about the chip's internal bus, asking the user to accept a risk they had
        # never taken and could not find in anything they wrote.
        #
        # Acks and fact-sheet matching still read the full text. An
        # acknowledgement is masked before claims are extracted either way, and a
        # device named only in the evidence is still a device on this board.
        claims = factclaim.extract_claims(_original_requirement(state.requirement_text))
        if not claims:
            return factclaim.Arbitration()
        acks = factclaim.parse_acks(state.requirement_text)
        sheets = list(factsheet_module.fact_sheets_named(state.requirement_text))
        hard = factclaim.arbitrate(claims, sheets, acks=acks)
        if not hard.unresolved:
            return hard
        return factclaim.judge_by_experience(
            hard,
            ask=self._experience_asker(ctx, knowledge),
            acks=acks,
            corpus_ids=self._corpus_ids(state, ctx),
        )

    def _corpus_ids(self, state: PipelineState, ctx: PipelineContext) -> list[str]:
        query = self.knowledge_query(state)
        if not query or self.knowledge_role is None:
            return []
        return [
            hit.doc.id for hit in ctx.kb.retrieve(query, top_k=3, role=self.knowledge_role)
        ]

    def _experience_asker(
        self, ctx: PipelineContext, knowledge: str
    ) -> factclaim.ExperienceAsk:
        """An asker that returns ``None`` rather than raising.

        ``propose_structured`` fails CLOSED in required mode, which is right for
        an artifact and wrong for an advisory opinion: a board must not be
        stopped because a soft second opinion was unavailable. The exception is
        therefore swallowed here, at the boundary, where the reason is visible.
        """

        def ask(verdict: factclaim.ClaimVerdict) -> factclaim.ExperienceOpinion | None:
            if ctx.mode == LlmMode.OFFLINE or ctx.client is None:
                return None
            try:
                opinion, _ = propose_structured(
                    ctx,
                    model=factclaim.ExperienceOpinion,
                    system=factclaim.EXPERIENCE_SYSTEM,
                    user=factclaim.experience_prompt(verdict, knowledge),
                    fallback=lambda: factclaim.ExperienceOpinion(within_norm=True),
                )
            except LlmError:
                return None
            return opinion

        return ask

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, RequirementSpec)
        checks = [
            CheckResult(
                name="requirement_text_present",
                ok=bool(artifact.raw_text.strip()),
                message="requirement text must not be empty",
            )
        ]
        checks.extend(self._claim_checks(state))
        checks.extend(_preflight_checks())
        return checks

    def _claim_checks(self, state: PipelineState) -> list[CheckResult]:
        """One check per requested value that a datasheet or experience disputes.

        The severity rule is the whole policy in three lines:

        * unacknowledged HARD conflict -> ERROR. The design stops and the user is
          asked, because a cited datasheet limit is being broken.
        * acknowledged conflict -> WARNING, never removed. The user decided; the
          finding stays so the accepted risk is on the record and reaches the
          final report. A deleted check would make an accepted risk
          indistinguishable from an absent one.
        * advisory conflict -> whatever ``judge_by_experience`` set, which is
          never ERROR.
        """
        out: list[CheckResult] = []
        for verdict in state.claim_verdicts:
            if verdict.ok:
                continue
            # Named by slot AND value: one requirement can dispute the same slot
            # twice ("feed the regulator from 24 V ... power the MCU from 5 V"
            # both land on a voltage slot), and two checks sharing a name would
            # let a repair or a report silently drop one of them.
            name = f"user_claim_conflict:{verdict.ack_token or verdict.slot}"
            if verdict.acknowledged:
                out.append(CheckResult(
                    name=name,
                    ok=True,
                    severity=Severity.WARNING,
                    message=(
                        f"ACCEPTED RISK — {verdict.message} The user acknowledged "
                        f"this and chose to proceed with their own value "
                        f"({factclaim.ACK_PREFIX} {verdict.ack_token})."
                        + (f" Source: {verdict.citation}" if verdict.citation else "")
                    ),
                ))
                continue
            out.append(CheckResult(
                name=name,
                ok=False,
                severity=verdict.severity,
                message=(
                    f"{verdict.message}"
                    + (f" Source: {verdict.citation}." if verdict.citation else "")
                    + f" To proceed with this value anyway, add this line to the "
                    f"requirement: {factclaim.ACK_PREFIX} {verdict.ack_token}"
                ),
            ))
        return out

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, RequirementSpec)
        return f"requirement '{artifact.project_name}' ({len(artifact.raw_text)} chars)"


class TopologyStep(PipelineStepBase):
    step = PipelineStep.TOPOLOGY
    knowledge_role = "topology"

    def knowledge_query(self, state: PipelineState) -> str | None:
        return f"power tree and block topology for: {state.requirement_text}"

    def fact_sheets_for_step(
        self, state: PipelineState
    ) -> list[tuple[str, FactSheetBase]] | None:
        """Devices named in the requirement — no part has been selected yet.

        Topology runs before selection, so the requirement text is the only place
        a device can be resolved from. That is enough for the facts this step
        actually needs: an MCU's ``supply_rails`` is what says the RP2040's 1.1 V
        core comes from an on-chip LDO and must not be fed by the board, which is
        a power-tree decision made here and nowhere else.
        """
        return factbrief.sheets_mentioned(state.requirement_text) or None

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        def fallback() -> TopologyPlan:
            rail = _requested_rail_v(state.requirement_text)
            # A requested 5 V logic rail collides with the 5 V input rail, and
            # ``TopologyPlan`` requires unique rails. Deduplicate instead of
            # declaring the same rail twice: the fallback runs inside
            # ``propose_structured``, which does not catch construction errors,
            # so a duplicate raised ValidationError out of the pipeline as an
            # uncaught crash rather than a reportable gate failure. Every
            # ATmega328 16 MHz request lands here, since that speed grade
            # forces the 5 V rail.
            rails = list(dict.fromkeys(["5V", f"{rail}V"]))
            blocks = [
                TopologyBlock(name="power_input", kind="power_input",
                              description="external supply input + protection"),
                TopologyBlock(name="regulator", kind="regulator",
                              description=f"LDO to {rail} V rail"),
                TopologyBlock(name="mcu", kind="mcu", description="microcontroller"),
                TopologyBlock(name="oscillator", kind="oscillator",
                              description="crystal + load caps"),
                TopologyBlock(name="reset", kind="reset", description="reset pull-up + button"),
                TopologyBlock(name="headers", kind="header", description="breakout headers"),
            ]
            return TopologyPlan(
                blocks=blocks, rails=rails, ground_net="GND",
                component_roles=[
                    ComponentRoleSpec(
                        role="power_input", description="input protection and connector"
                    ),
                    ComponentRoleSpec(role="regulator", description=f"regulator for {rail} V rail"),
                    ComponentRoleSpec(role="mcu", description="required microcontroller"),
                    ComponentRoleSpec(
                        role="oscillator", description="clock source and load capacitors"
                    ),
                    ComponentRoleSpec(role="reset", description="reset network"),
                    ComponentRoleSpec(role="headers", description="user-facing headers"),
                ],
                rationale="deterministic baseline topology",
            )

        system = (
            "You design a PCB block-level topology. Return JSON with blocks[] "
            "(name, kind, description), component_roles[] (role, description, "
            "value, package, selection_mode, required_capabilities, selection_basis, "
            "quantity, required, min_stock, max_price, max_lead_days, "
            "hard_constraints, soft_preferences), rails[] (supply rail names), "
            "ground_net, rationale. Freeze functional roles and hard constraints "
            "before any manufacturer part number is selected. Use "
            "selection_mode='capability_only' unless the user explicitly fixed an "
            "exact order code. Do not choose a manufacturer, symbol, footprint, MCU "
            "family, or exact MPN for a capability-only role. Record interfaces, "
            "memory, performance, power, package and lifecycle needs in "
            "required_capabilities/hard_constraints so the next step can compare "
            "real candidates. Use the provided design knowledge. "
            f"{_FACT_AUTHORITY}"
        )
        user = (
            f"Requirement:\n{state.requirement_text}\n\n"
            f"{_facts_block(ctx)}"
            f"Knowledge:\n{knowledge}"
        )
        plan, used_llm = propose_structured(
            ctx, model=TopologyPlan, system=system, user=user, fallback=fallback
        )
        _normalize_topology_identity(plan, state.requirement_text)
        return plan, used_llm

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, TopologyPlan)
        return [
            CheckResult(
                name="has_blocks", ok=bool(artifact.blocks),
                message="topology must define at least one functional block",
            ),
            CheckResult(
                name="has_supply_rail", ok=bool(artifact.rails),
                message="topology must define at least one supply rail",
            ),
            CheckResult(
                name="has_ground", ok=bool(artifact.ground_net.strip()),
                message="topology must define a ground net",
            ),
        ]

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, TopologyPlan)
        return f"{len(artifact.blocks)} blocks, rails={artifact.rails}"


_CATALOG_DECISION_RE = re.compile(
    r"candidate for\s+([^:;]+):\s*MPN=([^;]+)",
    re.IGNORECASE,
)


def _user_selected_mpn(requirement: str, value: str) -> str:
    wanted = value.strip().casefold()
    for query, mpn in _CATALOG_DECISION_RE.findall(requirement):
        if query.strip().casefold() == wanted:
            return mpn.strip()
    return ""


def _ground_mpns(
    parts: list[SelectedPart],
    requirement: str = "",
    component_roles: list[ComponentRoleSpec] | None = None,
) -> list[str]:
    """Fill procurement identity from configured providers without fabrication."""
    try:
        from ratsnestpro.parts import PartConstraint, ProcurementContext
        from ratsnestpro.parts.selector import PartSelector

        sel = PartSelector()
        issues: list[str] = []
        role_specs = {
            spec.role.strip().casefold(): spec
            for spec in component_roles or []
            if spec.role.strip()
        }
        for p in parts:
            if p.role == "mounting_hole" or not p.value:
                continue
            spec = role_specs.get(p.role.strip().casefold())
            requested_mpn = _user_selected_mpn(requirement, p.value)
            exact_mpn = requested_mpn or (spec.exact_mpn if spec else "")
            quantity = max(1, p.quantity, spec.quantity if spec else 1)
            constraint = PartConstraint(
                role=p.role,
                value=(spec.value if spec and spec.value else p.value),
                footprint=(spec.footprint if spec and spec.footprint else p.footprint),
                package=(spec.package if spec else ""),
                manufacturer=(spec.manufacturer if spec else ""),
                exact_mpn=exact_mpn,
                min_stock=(spec.min_stock if spec else 0),
                max_price=(spec.max_price if spec else None),
                max_lead_days=(spec.max_lead_days if spec else None),
                required=(spec.required if spec else True),
                quantity=quantity,
                hard_constraints=tuple(spec.hard_constraints if spec else ()),
                soft_preferences=tuple(spec.soft_preferences if spec else ()),
            )
            context = ProcurementContext(quantity=quantity)
            if exact_mpn:
                cands, provider_issues = sel.search_catalog(
                    constraint,
                    context=context,
                    limit=10,
                )
                cands = [
                    candidate
                    for candidate in cands
                    if candidate.mpn.casefold() == exact_mpn.casefold()
                ]
            else:
                cands, provider_issues = sel.search_catalog(
                    constraint,
                    context=context,
                    limit=3,
                )
            issues.extend(
                f"{issue.provider}:{issue.code}:{issue.message}"
                for issue in provider_issues
            )
            if cands:
                candidate = cands[0]
                p.mpn = candidate.mpn
                p.lcsc = candidate.lcsc
                p.catalog_provider = candidate.provider
                p.provider_part_id = candidate.provider_part_id
                p.manufacturer = candidate.manufacturer
                p.package_match = candidate.package_match
                p.asset_status = candidate.asset_status
                p.lifecycle = candidate.lifecycle
                p.rohs = candidate.rohs
                p.lead_days = candidate.lead_days
                p.unit_price = candidate.price
                p.price_currency = candidate.currency
                p.catalog_snapshot_id = candidate.snapshot_id
                p.datasheet = candidate.datasheet
                p.catalog_source_url = candidate.source_url
                p.constraint_gaps = list(candidate.constraint_gaps)
                p.selection_confidence = (
                    0.95
                    if candidate.package_match == "exact" and not candidate.constraint_gaps
                    else 0.85
                    if candidate.package_match == "compatible" and not candidate.constraint_gaps
                    else 0.55
                )
                p.selection_reason = (
                    "manufacturability/package evidence first; then JLC/basic, "
                    "provider preference, stock, lead time, and price"
                )
                issues.extend(
                    f"selection:constraint_unverified:{p.ref}:{gap}"
                    for gap in candidate.constraint_gaps
                )
        return issues
    except Exception:
        # Provider failures are explicit evidence gaps, not design crashes.
        return ["catalog:query_failed:provider query failed"]


_FREQ_RE = re.compile(r"(\d+(?:\.\d+)?)\s*MHz", re.IGNORECASE)


def _parse_voltage(token: str) -> float | None:
    """Parse a rail/part token into volts: '3V3'->3.3, '3.3V'/'5V'->3.3/5.0."""
    t = token.upper()
    m = re.match(r"(\d)V(\d)\b", t)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"(\d+(?:\.\d+)?)\s*V", t)
    return float(m.group(1)) if m else None


_MCU_MODEL_RE = re.compile(
    r"\b(?:RP\d{4}|ATMEGA\d+[A-Z0-9-]*|ATTINY\d+[A-Z0-9-]*|ESP32[A-Z0-9-]*|"
    r"STM32[A-Z]{1,2}\d{3,4}[A-Z0-9]*(?![A-Z0-9-])|"
    r"NRF\d+[A-Z0-9-]*|SAMD\d+[A-Z0-9-]*|"
    r"PIC\d+[A-Z0-9-]*|CH32[A-Z0-9-]*)\b",
    re.IGNORECASE,
)
_MCU_NEGATION_RE = re.compile(
    r"\b(?:not|never|without|instead\s+of|rather\s+than|"
    r"do\s+not|don't|must\s+not|forbid(?:den)?)"
    r"(?:\s+(?:use|using|choose|select|replace))?\b|"
    r"(?:不要|不是|而非|禁止|不得|不能|不用|不允许)"
    r"(?:使用|采用|选用|替换(?:为)?)?",
    re.IGNORECASE,
)
_MCU_POSITIVE_RE = re.compile(
    r"\b(?:use|using|choose|select|must\s+be|required|replace\s+with)\b|"
    r"(?:主控(?:必须)?是|使用|采用|选用|改为)",
    re.IGNORECASE,
)
_MODEL_LIKE_TOKEN_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9_.-]{2,}\d[A-Za-z0-9_.-]*\b"
)
_FIXED_IDENTITY_CUE_RE = re.compile(
    r"(?:必须(?:使用|采用|选用|是|为)?|固定(?:使用|采用|选用|是|为|主控)?|"
    r"指定(?:使用|采用|选用|是|为)?|不得替换|禁止替换|"
    r"\bmust\s+(?:use|be)\b|\bshall\s+(?:use|be)\b|"
    r"\buse\s+exactly\b|\bexact\s+(?:part|mpn)\b|"
    r"\bno\s+substitution\b|\bdo\s+not\s+substitute\b|"
    r"\buse\b|\busing\b|\bdesign\b|使用|采用|选用)",
    re.IGNORECASE,
)
_IDENTITY_ALTERNATIVE_RE = re.compile(
    r"(?:\bor\b|\beither\b|或者|或是|均可|都可以|任选|二选一)",
    re.IGNORECASE,
)
_IDENTITY_EXAMPLE_RE = re.compile(
    r"(?:\be\.g\.\b|\bfor\s+example\b|\bsuch\s+as\b|\blike\b|比如|例如|类似|参考)",
    re.IGNORECASE,
)
_GENERIC_MCU_FAMILY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "STM32",
        re.compile(r"(?<![A-Za-z0-9])STM32(?![A-Za-z0-9-])", re.IGNORECASE),
    ),
    (
        "ESP32",
        re.compile(r"(?<![A-Za-z0-9])ESP32(?![A-Za-z0-9-])", re.IGNORECASE),
    ),
)
_NON_MCU_IDENTITY_PREFIXES = (
    "usb", "uart", "usart", "spi", "i2c", "i2s", "can", "gpio", "adc",
    "dac", "pwm", "sdio", "sdmmc", "qspi", "rmii", "jtag", "swd", "wifi",
)

_MCU_CAPABILITY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("wifi", ("wi-fi", "wifi", "无线局域网")),
    ("bluetooth_le", ("bluetooth le", "ble", "蓝牙低功耗", "低功耗蓝牙")),
    ("bluetooth", ("bluetooth", "蓝牙")),
    ("ethernet_mac", ("ethernet", "以太网", "rmii", "rgmii")),
    ("can_fd", ("can-fd", "can fd", "canfd")),
    ("can", ("can总线", "can bus", "can接口")),
    ("usb_host", ("usb host", "usb主机")),
    ("usb_device", ("usb device", "usb设备")),
    ("sdio", ("sdio", "sdmmc", "sdhc")),
    ("ota", ("ota", "空中升级", "远程升级")),
    ("low_power", ("low power", "low-power", "低功耗", "电池供电")),
    ("hardware_crypto", ("hardware crypto", "硬件加密", "secure boot", "安全启动")),
)


def _intent_clause(text: str, start: int) -> str:
    separators = ".!?。！？;；\n"
    begin = max((text.rfind(separator, 0, start) for separator in separators), default=-1) + 1
    ends = [text.find(separator, start) for separator in separators]
    end = min((position for position in ends if position >= 0), default=len(text))
    return text[begin:end]


def _looks_like_mcu_order_code(token: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", token.casefold())
    if normalized in {"stm32", "esp32"}:
        return False
    if len(normalized) < 5 or not any(char.isdigit() for char in normalized):
        return False
    if normalized.startswith(_NON_MCU_IDENTITY_PREFIXES):
        return False
    if _MCU_MODEL_RE.fullmatch(token):
        return True
    return bool(_library_mcu_models(token)) or normalized.startswith(
        ("atsam", "same", "samd", "lpc", "efm", "gd32", "msp", "ra", "psoc", "pic")
    )


def _mcu_family_options(text: str) -> list[str]:
    """Broad MCU families offered or required by the user.

    These constrain the later selection candidate set but never pretend that a
    family label such as ``STM32`` is an exact manufacturer order code.
    """
    source = _original_requirement(text)
    options: list[str] = []
    for family, pattern in _GENERIC_MCU_FAMILY_PATTERNS:
        for match in pattern.finditer(source):
            if _model_mention_is_negated(source, match.start()):
                continue
            clause = _intent_clause(source, match.start())
            if _IDENTITY_EXAMPLE_RE.search(clause):
                continue
            options.append(family)
            break
    return list(dict.fromkeys(options))


def _fixed_mcu_tokens(text: str) -> tuple[str, ...]:
    """Exact MCU identities fixed by user intent, not merely mentioned.

    A list such as "STM32 or ESP32 are both acceptable" deliberately returns
    nothing: those names are candidate preferences, not two mandatory MCUs.
    """
    source = _original_requirement(text)
    matches = [
        match for match in _MODEL_LIKE_TOKEN_RE.finditer(source)
        if _looks_like_mcu_order_code(match.group(0))
        and not _model_mention_is_negated(source, match.start())
    ]
    fixed: list[str] = []
    for match in matches:
        clause = _intent_clause(source, match.start())
        clause_models = [
            candidate.group(0)
            for candidate in _MODEL_LIKE_TOKEN_RE.finditer(clause)
            if _looks_like_mcu_order_code(candidate.group(0))
        ]
        if len(clause_models) > 1 and _IDENTITY_ALTERNATIVE_RE.search(clause):
            continue
        sole_specific_model = (
            len(matches) == 1
            and not _IDENTITY_ALTERNATIVE_RE.search(clause)
            and not _IDENTITY_EXAMPLE_RE.search(clause)
        )
        if _FIXED_IDENTITY_CUE_RE.search(clause) or sole_specific_model:
            fixed.append(match.group(0))
    return tuple(dict.fromkeys(fixed))


def _fixed_mcu_models(text: str) -> set[str]:
    return {
        re.sub(r"[^a-z0-9]", "", token.casefold())
        for token in _fixed_mcu_tokens(text)
    }


def _mcu_capability_requirements(text: str) -> list[str]:
    """Normalize user-facing MCU needs without choosing a device family."""
    source = _original_requirement(text)
    lowered = source.casefold()
    capabilities = ["mcu_core"]
    for capability, keywords in _MCU_CAPABILITY_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            capabilities.append(capability)
    for count, interface in re.findall(
        r"(\d+)\s*(?:路|个|x\s*)?\s*(uart|usart|spi|i2c|gpio|adc|dac|pwm)s?\b",
        lowered,
    ):
        capabilities.append(f"{interface}>={count}")
    flash_patterns = (
        r"(?:flash|闪存)[^\n。.;；]{0,40}?(?:至少|>=|不低于)\s*(\d+(?:\.\d+)?)\s*(kb|mb|gb|kbit|mbit|gbit)",
        r"(?:至少|>=|不低于)\s*(\d+(?:\.\d+)?)\s*(kb|mb|gb|kbit|mbit|gbit)[^\n。.;；]{0,20}?(?:flash|闪存)",
    )
    for pattern in flash_patterns:
        match = re.search(pattern, lowered)
        if match:
            capabilities.append(f"flash>={match.group(1)} {match.group(2).upper()}")
            break
    family_options = _mcu_family_options(source)
    if family_options:
        capabilities.append(f"mcu_family_any_of={'|'.join(family_options)}")
    return list(dict.fromkeys(capabilities))


def _identity_is_explicitly_fixed(requirement: str, identity: str) -> bool:
    if not identity:
        return False
    source = _original_requirement(requirement)
    match = re.search(re.escape(identity), source, re.IGNORECASE)
    if match is None:
        return False
    clause = _intent_clause(source, match.start())
    return bool(
        _FIXED_IDENTITY_CUE_RE.search(clause)
        and not _IDENTITY_ALTERNATIVE_RE.search(clause)
    )


def _normalize_topology_identity(plan: TopologyPlan, requirement: str) -> None:
    """Keep topology procurement-neutral unless the user fixed an identity."""
    fixed_mcus = _fixed_mcu_tokens(requirement)
    mcu_capabilities = _mcu_capability_requirements(requirement)
    for role in plan.component_roles:
        role_name = role.role.strip().casefold()
        catalog_choice = _user_selected_mpn(requirement, role.value)
        if role_name in {"mcu", "microcontroller", "soc", "controller"}:
            role.required_capabilities = list(dict.fromkeys([
                *role.required_capabilities,
                *mcu_capabilities,
            ]))
            if len(fixed_mcus) == 1:
                role.selection_mode = "fixed_exact"
                role.exact_mpn = fixed_mcus[0]
                role.selection_basis = "exact MCU identity fixed by the user"
                continue
            role.selection_mode = "capability_only"
            role.exact_mpn = ""
            role.manufacturer = ""
            role.symbol = ""
            role.footprint = ""
            role.value = ""
            role.selection_basis = (
                "choose the MCU/SoC in the selection step from frozen capabilities"
            )
            continue
        fixed_identity = catalog_choice or (
            role.exact_mpn
            if _identity_is_explicitly_fixed(requirement, role.exact_mpn)
            else ""
        )
        if fixed_identity:
            role.selection_mode = "fixed_exact"
            role.exact_mpn = fixed_identity
            role.selection_basis = "exact component identity fixed by the user"
        else:
            role.selection_mode = "capability_only"
            role.exact_mpn = ""
            role.manufacturer = ""
            role.symbol = ""
            role.footprint = ""


_ARCHITECT_EVIDENCE_MARKER = "GROUNDED ARCHITECT EVIDENCE"
_VCAP_CEXT_RE = re.compile(
    r"C\s*EXT\s+Capacitance[^0-9]{0,120}"
    r"(\d+(?:\.\d+)?)\s*[uµμ]F",
    re.IGNORECASE,
)
_VCAP_VALUE_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*[uµμ]F?\s*$",
    re.IGNORECASE,
)
_VCAP_ENGINEERING_RE = re.compile(r"^\s*(\d+)[uµμ](\d+)\s*$", re.IGNORECASE)


def _original_requirement(text: str) -> str:
    """Exclude downstream evidence from user-intent parsers."""
    return text.partition(_ARCHITECT_EVIDENCE_MARKER)[0]


def _model_mention_is_negated(text: str, start: int) -> bool:
    clause_start = max(
        (text.rfind(separator, 0, start) for separator in ".!?。！？;\n"),
        default=-1,
    )
    prefix = text[clause_start + 1:start]
    negations = list(_MCU_NEGATION_RE.finditer(prefix))
    positives = list(_MCU_POSITIVE_RE.finditer(prefix))
    return bool(
        negations
        and not any(
            positive.start() >= negations[-1].end()
            for positive in positives
        )
    )


def _mcu_models(text: str) -> set[str]:
    text = _original_requirement(text)
    text = re.sub(
        r"\b(?:run_name|project_name)\b\s*(?:=|:)\s*[\"']?"
        r"[a-zA-Z0-9_.-]+[\"']?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    models: set[str] = set()
    for match in _MCU_MODEL_RE.finditer(text):
        if _model_mention_is_negated(text, match.start()):
            continue
        models.add(re.sub(r"[^a-z0-9]", "", match.group(0).lower()))
    models.update(_library_mcu_models(text))
    return models


def _mcu_model_matches(first: str, second: str) -> bool:
    """Match normalized MCU order codes, treating KiCad's ``x`` as a wildcard."""
    if first == second:
        return True

    def pattern(value: str) -> str:
        return re.escape(value).replace("x", "[a-z0-9]")

    return bool(
        re.fullmatch(pattern(first), second)
        or re.fullmatch(pattern(second), first)
    )


def _library_mcu_models(text: str) -> set[str]:
    """Recognize order codes through installed KiCad MCU libraries."""
    try:
        library_models = {
            re.sub(r"[^a-z0-9]", "", lib_id.partition(":")[2].lower())
            for lib_id in grounding.symbol_index()
            if lib_id.partition(":")[0].upper().startswith("MCU_")
        }
    except Exception:
        return set()

    requested: set[str] = set()
    for match in _MODEL_LIKE_TOKEN_RE.finditer(text):
        if _model_mention_is_negated(text, match.start()):
            continue
        model = re.sub(r"[^a-z0-9]", "", match.group(0).lower())
        if any(_mcu_model_matches(model, candidate) for candidate in library_models):
            requested.add(model)
    return requested


def _requested_mcu_symbols(requirement: str) -> list[dict[str, str]]:
    """Find exact KiCad MCU symbols and their library-defined footprints."""
    requested = _mcu_models(requirement)
    if not requested:
        return []
    matches: list[dict[str, str]] = []
    index = grounding.symbol_index()
    for model in sorted(requested):
        compatible = []
        for lib_id in index:
            symbol_name = re.sub(
                r"[^a-z0-9]", "", lib_id.partition(":")[2].lower()
            )
            if (
                model in symbol_name
                or symbol_name in model
                or _mcu_model_matches(model, symbol_name)
            ):
                compatible.append((lib_id, symbol_name))
        exact = [item for item in compatible if item[1] == model]
        for lib_id, _symbol_name in exact or compatible:
            props = symbols.symbol_properties(lib_id)
            matches.append({
                "symbol": lib_id,
                "footprint": props.get("Footprint", ""),
            })
    return matches[:20]


def _capability_mcu_symbols() -> list[dict[str, str]]:
    """Grounded, known MCU candidates for a capability-driven selection.

    Families appear here only as candidate metadata after the requirement has
    been normalized. They are never used to route or classify the request.
    """
    try:
        index = grounding.symbol_index()
    except Exception:
        return []
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for sheet in all_fact_sheets():
        if DeviceClass(sheet.device_class) is not DeviceClass.MCU:
            continue
        keys = {
            re.sub(r"[^a-z0-9]", "", key.casefold())
            for key in sheet.match_keys()
            if key
        }
        for lib_id in index:
            if not lib_id.partition(":")[0].upper().startswith("MCU_"):
                continue
            symbol_name = re.sub(
                r"[^a-z0-9]", "", lib_id.partition(":")[2].casefold()
            )
            if not any(
                key == symbol_name or _mcu_model_matches(key, symbol_name)
                for key in keys
            ):
                continue
            if lib_id in seen:
                continue
            seen.add(lib_id)
            properties = symbols.symbol_properties(lib_id)
            candidates.append({
                "device": sheet.device,
                "family_metadata": sheet.family,
                "symbol": lib_id,
                "footprint": properties.get("Footprint", ""),
                "datasheet": sheet.source.url,
            })
    return candidates[:24]


_LIBRARY_HINT_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+_.-]{2,}\b")
_GENERIC_LIBRARY_TOKENS = {
    "PCB", "MCU", "USB", "GPIO", "QFN", "LQFP", "TQFP", "LED", "LEDS",
}
_SEMANTIC_LIBRARY_TOKENS = {"crystal"}
_COMPATIBLE_FOOTPRINT_HINTS: dict[str, tuple[str, ...]] = {
    "Device:L_Coupled": (
        "Inductor_SMD:L_CommonModeChoke_Coilcraft_1812CAN",
    ),
}


def _compatible_footprint_hints(lib_id: str) -> list[str]:
    """Return installed footprints whose electrical pads match ``lib_id``."""
    symbol_pins = symbols.symbol_pins(lib_id) or []
    pin_numbers = {
        str(pin["number"]) for pin in symbol_pins if pin.get("number")
    }
    if not pin_numbers:
        return []
    matches: list[str] = []
    for footprint in _COMPATIBLE_FOOTPRINT_HINTS.get(lib_id, ()):
        pads = footprints.footprint_pads(footprint) or []
        pad_numbers = {
            str(pad["number"]) for pad in pads if pad.get("number")
        }
        if pad_numbers == pin_numbers:
            matches.append(footprint)
    return matches


def _component_symbol_hints(
    requirement: str,
) -> dict[str, list[dict[str, object]]]:
    """Return bounded real-library hints for part-like names in a requirement."""
    requirement = _original_requirement(requirement)
    tokens = {
        token
        for token in _LIBRARY_HINT_TOKEN_RE.findall(requirement)
        if (any(ch.isdigit() for ch in token) or token.isupper())
        or token.lower() in _SEMANTIC_LIBRARY_TOKENS
        if token.upper() not in _GENERIC_LIBRARY_TOKENS
    }
    requested_mcus = _mcu_models(requirement)
    tokens = {
        token for token in tokens
        if not any(
            model in re.sub(r"[^a-z0-9]", "", token.lower())
            or re.sub(r"[^a-z0-9]", "", token.lower()) in model
            for model in requested_mcus
        )
    }
    index = grounding.symbol_index()
    hints: dict[str, list[dict[str, object]]] = {}
    for token in sorted(tokens):
        normalized = re.sub(r"[^a-z0-9]", "", token.lower())
        if len(normalized) < 3:
            continue
        candidates = [
            lib_id for lib_id in index
            if normalized in re.sub(
                r"[^a-z0-9]", "", lib_id.partition(":")[2].lower()
            )
        ]
        candidates.sort(key=lambda lib_id: (
            0 if re.sub(
                r"[^a-z0-9]", "", lib_id.partition(":")[2].lower()
            ) == normalized else 1,
            len(lib_id),
            lib_id,
        ))
        if candidates:
            hints[token] = []
            for lib_id in candidates[:12]:
                pins = symbols.symbol_pins(lib_id) or []
                properties = symbols.symbol_properties(lib_id)
                hints[token].append({
                    "symbol": lib_id,
                    "pins": sorted({
                        str(pin["number"]) for pin in pins if pin["number"]
                    }),
                    "value": properties.get("Value", ""),
                    "description": properties.get("Description", ""),
                    "datasheet": properties.get("Datasheet", ""),
                    "default_footprint": properties.get("Footprint", ""),
                })

    lower = requirement.lower()
    semantic_candidates: list[tuple[str, str]] = []
    if "accelerometer" in lower or "加速度计" in requirement:
        semantic_candidates.append(
            ("3-axis accelerometer", "Sensor_Motion:LIS3DH")
        )
    if "spi nor" in lower or "w25q" in lower:
        semantic_candidates.append(
            ("SPI NOR Flash (>=64 Mbit)", "Memory_Flash:W25Q128JVS")
        )
    if "led" in lower:
        semantic_candidates.append(("indicator LED", "Device:LED"))
    if (
        ("24 v" in lower or "24v" in lower)
        and ("5 v" in lower or "5v" in lower)
    ):
        semantic_candidates.append(
            ("7-24V industrial 5V buck", "Regulator_Switching:TPS54360DDA")
        )
        semantic_candidates.append(
            ("24V reverse-polarity diode", "Diode:SS34")
        )
    if (
        "外部直流输入优先" in requirement
        or "priority" in lower
        or "不得反向灌电" in requirement
        or "backfeed" in lower
    ):
        semantic_candidates.append(
            ("5V source-priority power mux", "Power_Management:TPS2116DRL")
        )
        if "5 v" in lower or "5v" in lower:
            semantic_candidates.append(
                ("5V reverse-blocking P-MOSFET", "Transistor_FET:AO3401A")
            )
    if "microsd" in lower:
        semantic_candidates.append(
            ("microSD socket", "Connector:Micro_SD_Card")
        )
    if "can" in lower and (
        "共模" in requirement
        or "common-mode" in lower
        or "common mode" in lower
    ):
        semantic_candidates.append(
            ("CAN common-mode choke", "Device:L_Coupled")
        )
    if "can" in lower and (
        "gnd" in lower
        or "ground" in lower
        or "接地" in requirement
    ):
        semantic_candidates.append(
            ("CANH/CANL/GND connector", "Connector_Generic:Conn_01x03")
        )
    if "swd" in lower and (
        "10-pin" in lower
        or "10 pin" in lower
        or "10pin" in lower
    ):
        semantic_candidates.append(
            (
                "10-pin Cortex SWD connector",
                "Connector_Generic:Conn_02x05_Odd_Even",
            )
        )

    for label, lib_id in semantic_candidates:
        if lib_id not in index:
            continue
        pins = symbols.symbol_pins(lib_id) or []
        properties = symbols.symbol_properties(lib_id)
        hints[label] = [{
            "symbol": lib_id,
            "pins": sorted({
                str(pin["number"]) for pin in pins if pin["number"]
            }),
            "value": properties.get("Value", ""),
            "description": properties.get("Description", ""),
            "datasheet": properties.get("Datasheet", ""),
            "default_footprint": properties.get("Footprint", ""),
            "compatible_footprints": _compatible_footprint_hints(lib_id),
        }]
    return hints


def _normalize_symbol_for_footprint(part: SelectedPart) -> str | None:
    """Choose a verified numbering-compatible generic symbol when unambiguous."""
    pads = footprints.footprint_pads(part.footprint) or []
    pad_numbers = {str(pad["number"]) for pad in pads if pad["number"]}
    if not pad_numbers:
        return None
    current_pins = symbols.symbol_pins(part.symbol) or []
    current_numbers = {
        str(pin["number"]) for pin in current_pins if pin["number"]
    }
    if current_numbers == pad_numbers:
        return None

    footprint_name = part.footprint.partition(":")[2]
    connector = re.search(
        r"(?:PinHeader|PinSocket)_(\d+)x(\d+)",
        footprint_name,
        re.IGNORECASE,
    )
    candidates: list[str] = []
    if connector:
        rows, columns = int(connector.group(1)), int(connector.group(2))
        candidates.append(
            f"Connector_Generic:Conn_01x{columns:02d}"
            if rows == 1
            else f"Connector_Generic:Conn_{rows:02d}x{columns:02d}_Odd_Even"
        )
    dip_switch = re.search(r"SW_DIP_SPSTx(\d+)", footprint_name, re.IGNORECASE)
    if dip_switch:
        candidates.append(f"Switch:SW_DIP_x{int(dip_switch.group(1)):02d}")
    if "Crystal" in part.symbol and "4Pin" in footprint_name:
        candidates.append("Device:Crystal_GND24")

    for candidate in candidates:
        candidate_pins = symbols.symbol_pins(candidate) or []
        candidate_numbers = {
            str(pin["number"]) for pin in candidate_pins if pin["number"]
        }
        if candidate_numbers == pad_numbers:
            return candidate
    return None


def _grounded_vcap_uf(requirement: str) -> float | None:
    evidence = requirement.partition(_ARCHITECT_EVIDENCE_MARKER)[2]
    match = _VCAP_CEXT_RE.search(evidence)
    return float(match.group(1)) if match else None


def _capacitance_uf(value: str) -> float | None:
    match = _VCAP_VALUE_RE.fullmatch(value)
    if match:
        return float(match.group(1))
    engineering = _VCAP_ENGINEERING_RE.fullmatch(value)
    if engineering:
        return float(f"{engineering.group(1)}.{engineering.group(2)}")
    return None


def _symbol_power_pin_counts(lib_id: str) -> dict[str, int]:
    """Count real MCU supply pins by library pin name."""
    counts = {"VDD": 0, "VDDA": 0, "VBAT": 0, "VCAP": 0}
    for pin in symbols.symbol_pins(lib_id) or []:
        name = str(pin.get("name", "")).upper()
        if name == "VBAT":
            counts[name] += 1
        elif name == "VDDA" or "AVDD" in name:
            counts["VDDA"] += 1
        elif name == "VDD" or name.startswith("VDD") or name.endswith("VDD"):
            counts["VDD"] += 1
        elif name.startswith("VCAP"):
            counts["VCAP"] += 1
    return counts


def _requires_per_supply_pin_decoupling(requirement: str) -> bool:
    original = _original_requirement(requirement)
    lower = original.lower()
    names_each_power_pin = (
        "每个电源引脚" in original
        or "each power pin" in lower
        or "each supply pin" in lower
    )
    specifies_100_nf = (
        "100 nf" in lower
        or "100nf" in lower
        or "0.1 uf" in lower
        or "0.1uf" in lower
    )
    return names_each_power_pin and specifies_100_nf


def _functional_connector_pin_requirement(
    requirement: str,
    part: SelectedPart,
) -> tuple[int, str] | None:
    """Translate an explicit interface role into its minimum real pin count."""
    original = _original_requirement(requirement)
    lower = original.lower()
    role = part.role.lower()
    is_connector = part.ref.upper().startswith("J")
    if (
        is_connector
        and "microsd" in lower
        and "microsd" in role
        and ("connector" in role or "socket" in role)
    ):
        return 9, "microSD socket"
    if (
        is_connector
        and "can" in lower
        and "can" in role
        and ("interface" in role or "connector" in role)
        and ("gnd" in lower or "ground" in lower or "接地" in original)
    ):
        return 3, "CANH/CANL/GND interface"
    if (
        is_connector
        and "swd" in lower
        and "swd" in role
        and (
            "10-pin" in lower
            or "10 pin" in lower
            or "10pin" in lower
        )
    ):
        return 10, "10-pin Cortex SWD interface"
    return None


def _normalize_grounded_values(
    parts: list[SelectedPart],
    requirement: str,
) -> None:
    expected_vcap = _grounded_vcap_uf(requirement)
    if expected_vcap is None:
        return
    for part in parts:
        if "vcap" in part.role.lower():
            part.value = f"{expected_vcap:g}uF"


def _identity_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _required_flash_mbit(requirement: str) -> float | None:
    requirement = _original_requirement(requirement)
    matches = re.findall(
        r"(?:至少|at\s+least|>=?)\s*(\d+(?:\.\d+)?)\s*Mbit",
        requirement,
        re.IGNORECASE,
    )
    return max((float(value) for value in matches), default=None)


def _flash_capacity_mbit(properties: dict[str, str]) -> float | None:
    description = properties.get("Description", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*Mbit", description, re.IGNORECASE)
    return float(match.group(1)) if match else None


def _specific_component_identity_error(
    part: SelectedPart,
    requirement: str,
) -> str | None:
    """Reject a display-value relabel of a different installed library device."""
    properties = symbols.symbol_properties(part.symbol)
    library_value = properties.get("Value", "")
    description = properties.get("Description", "")
    role = part.role.lower()
    is_ic = part.ref.upper().startswith("U")

    expected_library = ""
    expected_description = ""
    if is_ic and "flash" in role:
        expected_library = "Memory_Flash:"
        expected_description = "flash"
    elif is_ic and "accelerometer" in role:
        expected_library = "Sensor_Motion:"
        expected_description = "accelerometer"
    elif is_ic and (
        "regulator" in role or "dc_dc" in role or role.startswith("ldo")
    ):
        expected_library = "Regulator_"
    elif is_ic and "transceiver" in role and "can" in role:
        expected_library = "Interface_CAN_LIN:"
        expected_description = "can"
    elif is_ic and (
        "power_mux" in role or "power_path" in role
    ):
        expected_library = "Power_Management:"

    if expected_library and not part.symbol.startswith(expected_library):
        return (
            f"{part.ref} role {part.role!r} requires a {expected_library} "
            f"library device, but {part.symbol!r} is {description or 'unclassified'}"
        )
    if (
        expected_description
        and expected_description not in description.lower()
    ):
        return (
            f"{part.ref} role {part.role!r} is not supported by the real "
            f"library description for {part.symbol!r}: {description!r}"
        )

    is_indicator_led = (
        role.endswith("_led")
        and "current_limit" not in role
        and "resistor" not in role
    )
    is_critical_input_device = "reverse_polarity" in role
    generic_led = part.symbol == "Device:LED"
    identity_required = bool(expected_library) or (
        is_indicator_led and not generic_led
    ) or is_critical_input_device
    if (
        identity_required
        and library_value
        and _identity_token(library_value) != _identity_token(part.value)
    ):
        return (
            f"{part.ref} displays value {part.value!r}, but the installed "
            f"symbol {part.symbol!r} is the different device "
            f"{library_value!r}; select the real device instead of relabeling it"
        )

    if is_ic and "flash" in role:
        required_mbit = _required_flash_mbit(requirement)
        actual_mbit = _flash_capacity_mbit(properties)
        if required_mbit is not None and (
            actual_mbit is None or actual_mbit < required_mbit
        ):
            return (
                f"{part.ref} must provide at least {required_mbit:g} Mbit, "
                f"but {part.symbol!r} proves "
                f"{actual_mbit if actual_mbit is not None else 'no'} Mbit "
                "in its real KiCad library description"
            )
    return None


def _required_input_rating_v(requirement: str) -> float | None:
    original = _original_requirement(requirement)
    ranges = re.findall(
        r"(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*"
        r"(\d+(?:\.\d+)?)\s*V\b",
        original,
        re.IGNORECASE,
    )
    if not ranges:
        return None
    maximum = max(float(high) for _low, high in ranges)
    if "浪涌" in original or "surge" in original.lower():
        return maximum * 1.5
    return maximum


def _library_voltage_rating_v(part: SelectedPart) -> float | None:
    description = symbols.symbol_properties(part.symbol).get("Description", "")
    values = re.findall(r"(\d+(?:\.\d+)?)\s*V\b", description, re.IGNORECASE)
    return max((float(value) for value in values), default=None)


def _selection_requirement_checks(
    requirement: str,
    parts: list[SelectedPart],
) -> list[CheckResult]:
    """Check explicit board requirements that cannot be inferred from pin count."""
    original = _original_requirement(requirement)
    lower = original.lower()
    roles = [part.role.lower() for part in parts]
    checks: list[CheckResult] = []

    for converter in (
        part
        for part in parts
        if part.ref.upper().startswith("U")
        and (
            "regulator_switching:" in part.symbol.lower()
            or "buck" in part.role.lower()
            or "switching_regulator" in part.role.lower()
        )
    ):
        pin_names = {
            str(pin.get("name", "")).upper()
            for pin in (symbols.symbol_pins(converter.symbol) or [])
        }
        related = [
            part
            for part in parts
            if part.ref != converter.ref
            and (
                "buck" in part.role.lower()
                or converter.ref.lower() in part.role.lower()
            )
        ]

        def roles_for(
            *tokens: str,
            candidates: list[SelectedPart] = related,
        ) -> list[SelectedPart]:
            return [
                part
                for part in candidates
                if all(token in part.role.lower() for token in tokens)
            ]

        missing_support: list[str] = []
        if not roles_for("input", "capacitor"):
            missing_support.append("input capacitor")
        if not roles_for("output", "capacitor"):
            missing_support.append("output capacitor")
        if not roles_for("inductor"):
            missing_support.append("inductor")
        bootstrap_capacitors = [
            part
            for part in related
            if "bootstrap" in part.role.lower()
            and (
                part.ref.upper().startswith("C")
                or "capacitor" in part.role.lower()
            )
        ]
        if any("BOOT" in name for name in pin_names) and not bootstrap_capacitors:
            missing_support.append("bootstrap capacitor")
        if any(name == "FB" or name.endswith("/FB") for name in pin_names):
            feedback_resistors = [
                part
                for part in related
                if "feedback" in part.role.lower()
                and (
                    part.ref.upper().startswith("R")
                    or "resistor" in part.role.lower()
                )
            ]
            if len(feedback_resistors) < 2:
                missing_support.append("two feedback resistors")
        if any("RT" in name for name in pin_names) and not [
            part
            for part in related
            if any(
                token in part.role.lower()
                for token in ("timing", "rt_", "rtclk")
            )
            and (
                part.ref.upper().startswith("R")
                or "resistor" in part.role.lower()
            )
        ]:
            missing_support.append("RT/CLK timing resistor")
        if any("COMP" in name for name in pin_names):
            compensation = [
                part
                for part in related
                if "compensation" in part.role.lower()
                or re.search(
                    r"(?:^|_)comp(?:_|$)",
                    part.role.lower(),
                )
            ]
            has_comp_r = any(
                part.ref.upper().startswith("R")
                or "resistor" in part.role.lower()
                for part in compensation
            )
            has_comp_c = any(
                part.ref.upper().startswith("C")
                or "capacitor" in part.role.lower()
                for part in compensation
            )
            if not (has_comp_r and has_comp_c):
                missing_support.append("COMP resistor and capacitor")
        checks.append(CheckResult(
            name=f"switching_regulator_support_parts:{converter.ref}",
            ok=not missing_support,
            message=(
                f"{converter.ref} real symbol pins require explicit selected "
                f"support parts; missing={missing_support}"
            ),
        ))

    needs_sdio4 = "microsd" in lower and (
        "sdio" in lower or "4-bit" in lower or "4 bit" in lower
    )
    if needs_sdio4:
        required_pullups = {
            "sdio_cmd_pullup",
            "sdio_dat0_pullup",
            "sdio_dat1_pullup",
            "sdio_dat2_pullup",
            "sdio_dat3_pullup",
        }
        missing = sorted(required_pullups - set(roles))
        checks.append(CheckResult(
            name="microsd_sdio4_pullups",
            ok=not missing,
            message=f"missing required SDIO 4-bit pull-up roles: {missing}",
        ))
        has_esd = any(
            part.ref.upper().startswith(("D", "U"))
            and "microsd" in part.role.lower()
            and (
                "esd" in part.role.lower()
                or "tvs" in part.role.lower()
            )
            for part in parts
        )
        checks.append(CheckResult(
            name="microsd_esd_protection",
            ok=has_esd,
            message="microSD requires a dedicated ESD/TVS protection part",
        ))

    needs_can_common_mode = "can" in lower and (
        "共模" in original or "common-mode" in lower or "common mode" in lower
    )
    if needs_can_common_mode:
        has_can_filter = any(
            "can" in part.role.lower()
            and (
                "common_mode" in part.role.lower()
                or "commonmode" in part.role.lower()
                or "choke" in part.role.lower()
                or "cmc" in part.role.lower()
            )
            and len(symbols.symbol_pins(part.symbol) or []) >= 4
            and (
                part.symbol == "Device:L_Coupled"
                or "commonmode" in part.symbol.lower().replace("_", "")
                or "coupled" in part.symbol.lower()
            )
            for part in parts
        )
        checks.append(CheckResult(
            name="can_common_mode_protection",
            ok=has_can_filter,
            message="CANH/CANL require a dedicated common-mode filter/choke part",
        ))

    needs_can_tvs = (
        "canh" in lower
        and "canl" in lower
        and any(token in lower for token in ("tvs", "esd", "surge"))
    )
    if needs_can_tvs:
        can_tvs_channels = 0
        for part in parts:
            role = part.role.lower()
            if "can" not in role or not any(
                token in role for token in ("tvs", "esd", "protection")
            ):
                continue
            pin_count = len(symbols.symbol_pins(part.symbol) or [])
            can_tvs_channels += 1 if pin_count <= 2 else 2
        checks.append(CheckResult(
            name="can_differential_tvs_channels",
            ok=can_tvs_channels >= 2,
            message=(
                "CANH and CANL each require a real TVS/ESD protection channel; "
                f"grounded selection provides {can_tvs_channels}"
            ),
        ))

    needs_selectable_can_termination = (
        "can" in lower
        and re.search(r"\b120\s*(?:ohm|r)\b|120\s*[ΩΩ]", lower) is not None
        and any(
            token in lower
            for token in ("jumper", "selectable", "switchable", "跳线", "可选择")
        )
    )
    if needs_selectable_can_termination:
        termination_parts = [
            part
            for part in parts
            if "can" in part.role.lower()
            and "termination" in part.role.lower()
        ]
        has_resistor = any(
            part.ref.upper().startswith("R")
            or "resistor" in part.role.lower()
            for part in termination_parts
        )
        has_selector = any(
            part.ref.upper().startswith(("JP", "SW"))
            or any(
                token in part.role.lower()
                for token in ("jumper", "switch", "selector", "enable")
            )
            for part in termination_parts
        )
        checks.append(CheckResult(
            name="can_selectable_termination_parts",
            ok=has_resistor and has_selector,
            message=(
                "selectable CAN termination requires both a 120-ohm resistor "
                "and a real two-terminal jumper/switch, each with a CAN "
                f"termination role; selected refs={[p.ref for p in termination_parts]}"
            ),
        ))

    needs_two_analog_inputs = (
        re.search(r"0\s*[-–—]\s*10\s*V", original, re.IGNORECASE)
        is not None
        or (
            any(token in original for token in ("模拟输入", "模拟量输入"))
            and any(token in original for token in ("两路", "2路"))
        )
    )
    if needs_two_analog_inputs:
        analog_connectors = [
            part
            for part in parts
            if "analog" in part.role.lower()
            and any(
                token in part.role.lower()
                for token in ("connector", "terminal", "interface")
            )
        ]
        connector_pin_counts = [
            len(symbols.symbol_pins(part.symbol) or [])
            for part in analog_connectors
        ]
        has_analog_connector = (
            any(count >= 3 for count in connector_pin_counts)
            or sum(count >= 2 for count in connector_pin_counts) >= 2
        )
        checks.append(CheckResult(
            name="analog_input_external_connector",
            ok=has_analog_connector,
            message=(
                "two external analog channels require either one >=3-pin "
                "connector (AI1, AI2, GND) or two >=2-pin connectors; "
                "selected="
                f"{[(part.ref, count) for part, count in zip(
                    analog_connectors,
                    connector_pin_counts,
                    strict=True,
                )]}"
            ),
        ))
        missing_channels: list[int] = []
        for channel in (1, 2):
            if not any(
                part.ref.upper().startswith(("D", "U"))
                and "analog" in part.role.lower()
                and str(channel) in part.role.lower()
                and any(
                    token in part.role.lower()
                    for token in ("overvoltage", "clamp", "protection", "tvs")
                )
                for part in parts
            ):
                missing_channels.append(channel)
        checks.append(CheckResult(
            name="analog_input_overvoltage_protection",
            ok=not missing_channels,
            message=(
                "missing explicit overvoltage/clamp protection for analog "
                f"input channels: {missing_channels}"
            ),
        ))

    needs_power_priority = (
        "外部直流输入优先" in original
        or "不得反向灌电" in original
        or "priority" in lower
        or "backfeed" in lower
    )
    if needs_power_priority:
        has_power_path = any(
            part.ref.upper().startswith(("D", "Q", "U"))
            and any(
                token in part.role.lower()
                for token in (
                    "power_path",
                    "power_mux",
                    "source_priority",
                    "ideal_diode",
                    "reverse_blocking",
                    "oring",
                )
            )
            for part in parts
        )
        checks.append(CheckResult(
            name="dual_input_priority_and_backfeed",
            ok=has_power_path,
            message=(
                "dual-input design requires an explicit priority/backfeed-"
                "blocking power-path component"
            ),
        ))
    return checks


_TOPOLOGY_COVERAGE_STOPWORDS = {
    "block",
    "board",
    "connector",
    "external",
    "header",
    "interface",
    "passive",
    "physical",
    "using",
}


def _role_slug(text: str) -> str:
    """``"Power Input"`` / ``"power-input"`` -> ``"power_input"``."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _uncovered_topology_blocks(
    state: PipelineState,
    parts: list[SelectedPart],
) -> list[str]:
    """Find functional blocks with no semantic evidence in the selected BOM."""
    topology = state.artifact(PipelineStep.TOPOLOGY)
    if not isinstance(topology, TopologyPlan):
        return []
    # A part that declares the block's own name or kind as its role *is* the
    # implementation. Token matching alone missed this: "power_input" reduces to
    # no usable token (power/input/protection are stopwords), so a USB-C power
    # connector never covered a "power_input" block. That miss was invisible
    # while this check only warned for the deterministic fallback.
    part_roles = {_role_slug(part.role) for part in parts} - {""}
    part_tokens = [
        _semantic_role_tokens(
            f"{part.role} {part.value} {part.symbol} {part.footprint}"
        )
        for part in parts
    ]
    uncovered: list[str] = []
    for block in topology.blocks:
        if {_role_slug(block.name), _role_slug(block.kind)} & part_roles:
            continue
        tokens = _semantic_role_tokens(
            f"{block.name} {block.kind} {block.description}"
        ) - _TOPOLOGY_COVERAGE_STOPWORDS
        if tokens and not any(tokens & selected for selected in part_tokens):
            uncovered.append(block.name)
    return uncovered


def _normalize_footprint_for_symbol(part: SelectedPart) -> str | None:
    """Choose a grounded compatible footprint for known semantic parts/connectors."""
    semantic_candidates = _compatible_footprint_hints(part.symbol)
    if (
        semantic_candidates
        and "common_mode" in part.role.lower()
        and part.footprint not in semantic_candidates
    ):
        return semantic_candidates[0]

    symbol_name = part.symbol.partition(":")[2]
    connector = re.match(r"Conn_(\d+)x(\d+)", symbol_name, re.IGNORECASE)
    if not connector:
        return None
    rows, columns = int(connector.group(1)), int(connector.group(2))
    symbol_pins = symbols.symbol_pins(part.symbol) or []
    pin_numbers = {str(pin["number"]) for pin in symbol_pins if pin["number"]}
    # LLMs often emit the old shorthand ``Conn_02x08`` while current KiCad
    # libraries expose numbered variants such as ``Conn_02x08_Odd_Even``.
    # The connector dimensions still provide an unambiguous electrical pad
    # set; the following symbol-normalization pass replaces the shorthand
    # with the verified installed symbol.
    if not pin_numbers:
        pin_numbers = {str(number) for number in range(1, rows * columns + 1)}
    current_pads = footprints.footprint_pads(part.footprint) or []
    current_numbers = {
        str(pad["number"]) for pad in current_pads if pad["number"]
    }
    if pin_numbers == current_numbers:
        return None

    proposed_name = part.footprint.partition(":")[2]
    pitch_match = re.search(r"P(\d+(?:\.\d+)?)mm", proposed_name, re.IGNORECASE)
    pitch = pitch_match.group(1) if pitch_match else "2.54"
    styles = (
        ["PinSocket", "PinHeader"]
        if "PinSocket" in proposed_name
        else ["PinHeader", "PinSocket"]
    )
    for style in styles:
        library = f"Connector_{style}_{pitch}mm"
        candidate = (
            f"{library}:{style}_{rows}x{columns:02d}_P{pitch}mm_Vertical"
        )
        pads = footprints.footprint_pads(candidate) or []
        pad_numbers = {str(pad["number"]) for pad in pads if pad["number"]}
        if pad_numbers == pin_numbers:
            return candidate
    return None


_MAX_SELECTION_PARTS = 128


def _ground_selected_parts(
    parts: list[SelectedPart],
    requirement: str,
    fixes: list[str] | None = None,
    component_roles: list[ComponentRoleSpec] | None = None,
) -> list[str]:
    """Ground selected parts to installed libraries and trusted catalog data.

    ``fixes`` collects the corrections that changed an electrical decision, for
    the caller to record as ``auto_fixes``. Only the symbol/footprint numbering
    swaps are reported: replacing a name with its installed spelling, or filling
    in procurement data, leaves the design the same, while swapping
    ``Device:Crystal`` for ``Device:Crystal_GND24`` changes which pads exist and
    therefore what the connectivity checks are judging.

    Left as ``None`` the corrections still happen and simply go unreported. That
    is only safe for a caller whose result is discarded — an attributed run needs
    the trail, or AHE credits the change to whatever else moved in the same
    round.
    """
    symbol_library_available = config.symbol_dir() is not None
    footprint_library_available = config.footprint_dir() is not None
    for part in parts:
        if symbol_library_available:
            grounded_symbol = grounding.ground_symbol(part.symbol)
            if grounded_symbol:
                part.symbol = grounded_symbol
        if footprint_library_available:
            grounded_footprint = grounding.ground_footprint(part.footprint)
            if grounded_footprint is not None:
                part.footprint = grounded_footprint
        if symbol_library_available and footprint_library_available:
            default_footprint = symbols.symbol_properties(part.symbol).get(
                "Footprint",
                "",
            )
            if (
                default_footprint
                and footprints.footprint_pads(default_footprint) is not None
            ):
                part.footprint = default_footprint
            compatible_footprint = _normalize_footprint_for_symbol(part)
            if compatible_footprint:
                if fixes is not None and compatible_footprint != part.footprint:
                    fixes.append(
                        f"{part.ref}: footprint {part.footprint} -> "
                        f"{compatible_footprint} (pad numbers had to match the "
                        f"pins of {part.symbol})"
                    )
                part.footprint = compatible_footprint
            compatible_symbol = _normalize_symbol_for_footprint(part)
            if compatible_symbol:
                if fixes is not None and compatible_symbol != part.symbol:
                    fixes.append(
                        f"{part.ref}: symbol {part.symbol} -> {compatible_symbol} "
                        f"(pin numbers had to match the pads of {part.footprint})"
                    )
                part.symbol = compatible_symbol
    _normalize_grounded_values(parts, requirement)
    for part in parts:
        part.mpn = ""
        part.lcsc = ""
        part.catalog_provider = ""
        part.provider_part_id = ""
        part.manufacturer = ""
        part.package_match = "unknown"
        part.asset_status = "unverified"
        part.lifecycle = ""
        part.rohs = ""
        part.lead_days = None
        part.unit_price = 0.0
        part.price_currency = "CNY"
        part.catalog_snapshot_id = ""
        part.selection_confidence = 0.0
        part.selection_reason = ""
        part.datasheet = ""
        part.catalog_source_url = ""
        part.constraint_gaps = []
    try:
        issues = _ground_mpns(parts, requirement, component_roles)
    except TypeError as exc:
        # Keep compatibility with integrations that monkeypatch the legacy
        # one-argument grounding hook.
        if "positional argument" not in str(exc):
            raise
        issues = _ground_mpns(parts)
    return list(issues or [])


def _apply_selection_patch(
    plan: SelectionPlan,
    patch: SelectionPatch,
) -> SelectionPlan:
    """Merge a bounded part delta while preserving every unrelated selection."""
    removals = {ref.upper() for ref in patch.remove_refs}
    upserts = {
        part.ref.upper(): part.model_copy(deep=True)
        for part in patch.upsert_parts
    }
    parts: list[SelectedPart] = []
    for existing in plan.parts:
        key = existing.ref.upper()
        if key in removals:
            continue
        replacement = upserts.pop(key, None)
        parts.append(
            replacement
            if replacement is not None
            else existing.model_copy(deep=True)
        )
    parts.extend(upserts.values())
    return SelectionPlan(
        parts=parts,
        rationale=patch.rationale or plan.rationale,
        catalog_issues=list(plan.catalog_issues),
    )


def _apply_selection_identity_policy(
    plan: SelectionPlan,
    requirement: str,
    topology: TopologyPlan | None,
) -> None:
    """Stamp the identity policy decided before catalog grounding.

    Repair proposals are LLM deltas and may omit policy metadata, so the same
    deterministic stamp is applied after both initial selection and repair.
    """
    fixed_tokens = list(_fixed_mcu_tokens(requirement))
    family_options = _mcu_family_options(requirement)
    capabilities = _mcu_capability_requirements(requirement)
    if topology is not None:
        capabilities = next(
            (
                role.required_capabilities
                for role in topology.component_roles
                if role.role.strip().casefold()
                in {"mcu", "microcontroller", "soc", "controller"}
            ),
            capabilities,
        )
    for part in plan.parts:
        if part.role.strip().casefold() not in {
            "mcu", "microcontroller", "soc", "controller"
        }:
            continue
        if len(fixed_tokens) == 1:
            part.identity_mode = "fixed_exact"
            part.requested_identity = fixed_tokens[0][:200]
        elif family_options:
            part.identity_mode = "family_variant"
            part.requested_identity = " | ".join(family_options)[:200]
        else:
            part.identity_mode = "capability_only"
            part.requested_identity = ", ".join(capabilities)[:200]
        selected_sheet = factsheet_module.fact_sheet(
            " ".join((part.mpn, part.value, part.symbol)),
            device_class=DeviceClass.MCU,
        )
        if selected_sheet is not None:
            part.device_family = selected_sheet.family
            part.validation_profile = selected_sheet.device
        if not part.selection_reason:
            part.selection_reason = plan.rationale[:1_000]


class SelectionStep(PipelineStepBase):
    step = PipelineStep.SELECTION
    knowledge_role = "selection"

    def knowledge_query(self, state: PipelineState) -> str | None:
        return f"component selection and packages for: {state.requirement_text}"

    def fact_sheets_for_step(
        self, state: PipelineState
    ) -> list[tuple[str, FactSheetBase]] | None:
        """Devices named in the requirement, because this step CREATES the parts.

        There is no selection to resolve against yet — producing one is the job.
        The requirement is nonetheless a sound source here: the system prompt
        already requires every MCU it names to appear as a selected part, so the
        facts shown are the facts of parts that will exist. This is what lets
        supply_range and clock_external arrive BEFORE the choice rather than as a
        ``datasheet_limits`` rejection afterwards.
        """
        mentioned = factbrief.sheets_mentioned(state.requirement_text)
        if _fixed_mcu_models(state.requirement_text):
            return mentioned or None
        topology = state.artifact(PipelineStep.TOPOLOGY)
        capability_mcu_role = isinstance(topology, TopologyPlan) and any(
            role.role.strip().casefold() in {"mcu", "microcontroller", "soc", "controller"}
            and role.selection_mode == "capability_only"
            for role in topology.component_roles
        )
        if not capability_mcu_role:
            return mentioned or None
        candidate_sheets = [
            ("", sheet)
            for sheet in all_fact_sheets()
            if DeviceClass(sheet.device_class) is DeviceClass.MCU
        ]
        return factbrief.dedupe([*mentioned, *candidate_sheets]) or None

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        def fallback() -> SelectionPlan:
            """No proposal means no BOM — never a different board's BOM.

            This used to return the hard-coded ATmega328 reference BOM, so a
            failed model call on an STM32 requirement silently produced an
            ATmega dev board: wrong MCU, a nonexistent AP2112K-5.0 regulator,
            and every downstream check green because that board is internally
            consistent. An empty selection is allowed to construct precisely so
            the bottom-line checks below report it instead (fail closed).
            """
            return SelectionPlan(parts=[], rationale="no proposal: no usable LLM output")

        fixed_mcus = sorted(_fixed_mcu_models(state.requirement_text))
        fixed_mcu_tokens = list(_fixed_mcu_tokens(state.requirement_text))
        needs_llm_library_hints = (
            ctx.mode != LlmMode.OFFLINE
            and ctx.client is not None
            and config.symbol_dir() is not None
        )
        exact_mcu_symbols = (
            _requested_mcu_symbols(" ".join(fixed_mcu_tokens))
            if needs_llm_library_hints
            else []
        )
        capability_mcu_symbols = (
            _capability_mcu_symbols()
            if needs_llm_library_hints and not fixed_mcus
            else []
        )
        mcu_symbol_candidates = exact_mcu_symbols or capability_mcu_symbols
        mcu_power_pin_counts = [
            {
                "symbol": candidate["symbol"],
                "counts": _symbol_power_pin_counts(candidate["symbol"]),
            }
            for candidate in mcu_symbol_candidates
        ]
        component_hints = (
            _component_symbol_hints(state.requirement_text)
            if needs_llm_library_hints
            else {}
        )
        topology = state.artifact(PipelineStep.TOPOLOGY)
        topology_blocks = (
            [block.model_dump() for block in topology.blocks]
            if isinstance(topology, TopologyPlan)
            else []
        )
        role_specs = (
            [role.model_dump() for role in topology.component_roles]
            if isinstance(topology, TopologyPlan)
            else []
        )
        mcu_capabilities = next(
            (
                role.required_capabilities
                for role in topology.component_roles
                if role.role.strip().casefold()
                in {"mcu", "microcontroller", "soc", "controller"}
            ),
            _mcu_capability_requirements(state.requirement_text),
        ) if isinstance(topology, TopologyPlan) else _mcu_capability_requirements(
            state.requirement_text
        )
        system = (
            "You choose real components for the design. Return JSON with parts[] "
            "(ref, symbol as 'Lib:Name', value, footprint as 'Lib:Name', role, "
            "identity_mode, requested_identity, selection_reason) and rationale. "
            "Use only real KiCad symbols/footprints; do not invent MPNs. "
            "For a capability_only MCU role, compare candidates against every "
            "frozen capability and choose one primary MCU/SoC here; an MCU family "
            "is candidate metadata, never an input category. For fixed_exact, use "
            "the exact user identity and do not substitute it. "
            "If the requirement contains a USER-SELECTED CATALOG CANDIDATE, honor "
            "that exact MPN/provider decision when it exists in the supplied catalog "
            "evidence; never silently substitute a different candidate. "
            f"Keep the response bounded: select at most {_MAX_SELECTION_PARTS} "
            "physical parts, include "
            "exactly ref/symbol/value/footprint/role per part, and keep rationale "
            "under 200 characters. "
            "Every MCU explicitly FIXED by the user MUST appear as a selected part "
            "with role='mcu'. A family merely mentioned as an acceptable option is "
            "not mandatory and must not create a second MCU. Select "
            "enough protection channels for every protected signal; a two-pin TVS "
            "protects only one signal and cannot be shared across different nets. "
            "A displayed part value MUST identify the actual installed KiCad symbol "
            "device; never relabel a lower-capacity flash, optical receiver, regulator, "
            "or high-power LED as another component. For an SDIO 4-bit microSD bus use "
            "roles sdio_cmd_pullup and sdio_dat0_pullup through "
            "sdio_dat3_pullup plus microsd_esd. For CAN common-mode protection use "
            "can_common_mode_choke. For each protected analog channel use "
            "analog_input_overvoltage_protection_N. For dual-source priority and "
            "backfeed blocking use an explicit power_mux, power_path_controller, "
            "ideal_diode, or reverse_blocking role. "
            "For every switching regulator, inspect its real symbol pins and select "
            "the complete support network. Use explicit buck_* roles for input and "
            "output capacitors and the inductor. When the symbol exposes BOOT, FB, "
            "RT/CLK, or COMP, also select a bootstrap capacitor, two feedback "
            "resistors, a timing resistor, and both a compensation resistor and "
            "capacitor. Do not defer these physical parts to the connection step. "
            "For external analog inputs select a real connector with enough pins "
            "for every channel and its ground return, using an "
            "analog_input_connector role: one shared >=3-pin connector or separate "
            ">=2-pin connectors. "
            "For an explicitly required 10-pin Cortex SWD interface use a real "
            "10-pin symbol. A CAN connector that exposes CANH, CANL, and GND needs "
            "at least three electrical pins. Selectable CAN termination requires "
            "both a 120-ohm resistor with role can_termination_resistor and a real "
            "two-terminal jumper/switch with role can_termination_jumper. "
            "Use the real Micro_SD_Card socket "
            "symbol for microSD and a real four-pin coupled-inductor symbol for a "
            "CAN common-mode choke. Use the supplied installed-symbol power-pin "
            "counts as authoritative: create one mcu_vdd_decoupling_N part per "
            "real digital VDD pin and one mcu_vdda_decoupling_N part per real "
            "analog VDD pin. If those counts are unavailable, select only a small "
            "design-justified set; never fill the part budget with duplicates. "
            "Every topology block listed below must have a physical implementation "
            "in parts[]. "
            "Prefer the installed symbol candidates listed below over an unlisted "
            "alternative, and pair every symbol with a footprint having exactly the "
            "same electrical pin/pad numbers. Connector footprints may additionally "
            "contain mechanical or shield pads. "
            f"{_FACT_AUTHORITY}"
        )
        user = (
            f"Requirement:\n{state.requirement_text}\n\n"
            f"User-fixed MCU models: {fixed_mcus or 'none; select from capabilities'}\n"
            f"Frozen MCU capability requirements: {mcu_capabilities}\n"
            "Grounded MCU symbol candidates available in the installed KiCad library: "
            f"{mcu_symbol_candidates or 'none found'}\n"
            "For a fixed identity, use its exact symbol and non-empty library-defined "
            "footprint. For capability-only selection, candidates are preferred but "
            "must still satisfy the frozen capabilities.\n"
            "Power-pin counts read from those installed MCU symbols: "
            f"{mcu_power_pin_counts or 'none'}\n"
            f"Required topology blocks: {topology_blocks or 'none'}\n"
            "Frozen component role specifications (honor these before choosing "
            f"MPNs): {role_specs or 'none'}\n"
            "Other installed KiCad symbol candidates matching named components: "
            f"{component_hints or 'none'}\n\n"
            f"{_facts_block(ctx)}"
            f"Knowledge:\n{knowledge}"
        )
        plan, used = propose_structured(
            ctx, model=SelectionPlan, system=system, user=user, fallback=fallback
        )
        # Ground names and procurement data exactly as later selection deltas
        # are grounded before they can be merged into this plan.
        fixes: list[str] = []
        catalog_issues = _ground_selected_parts(
            plan.parts,
            state.requirement_text,
            fixes,
            topology.component_roles if isinstance(topology, TopologyPlan) else None,
        )
        plan.catalog_issues = list(dict.fromkeys(catalog_issues))[:100]
        _apply_selection_identity_policy(
            plan,
            state.requirement_text,
            topology if isinstance(topology, TopologyPlan) else None,
        )
        for fix in fixes:
            state.record_auto_fix(self.step, fix)
        return plan, used

    def repair(
        self,
        state: PipelineState,
        ctx: PipelineContext,
        knowledge: str,
        artifact: BaseModel,
        checks: list[CheckResult],
    ) -> tuple[BaseModel, bool]:
        """Repair only failed component choices instead of regenerating the BOM."""
        assert isinstance(artifact, SelectionPlan)
        failed = "\n".join(
            f"- {check.name}: {check.message}"
            for check in checks
            if not check.ok and check.severity == Severity.ERROR
        )
        current_parts = "\n".join(
            f"{part.ref}: symbol={part.symbol!r}, value={part.value!r}, "
            f"footprint={part.footprint!r}, role={part.role!r}"
            for part in artifact.parts
        )
        system = (
            "You repair a grounded PCB component selection using a bounded JSON "
            "delta. Return upsert_parts[], remove_refs[], and rationale. Upsert "
            "only missing or invalid physical parts and replace an existing part "
            "by reusing its ref. Remove a ref only when the failed check proves "
            "that physical part is invalid. Preserve all unrelated parts, refs, "
            "roles, symbols, footprints, and values. Use only real installed "
            "KiCad symbol and footprint IDs, keep numeric symbol pins compatible "
            "with footprint pads, and never invent MPN/LCSC/stock data. Do not "
            "delete a required circuit merely to silence a check. "
            f"{_FACT_AUTHORITY}"
        )
        user = (
            f"Requirement:\n{state.requirement_text}\n\n"
            f"Failed bottom-line checks:\n{failed}\n\n"
            f"Current grounded selection:\n{current_parts}\n\n"
            "Relevant installed symbol candidates:\n"
            f"{_component_symbol_hints(state.requirement_text) or 'none'}\n\n"
            f"{_facts_block(ctx)}"
            f"Knowledge:\n{knowledge}"
        )
        # The bounded prompt above already contains the failed checks and the
        # current selection. Do not append PipelineStepBase's full rejected JSON
        # a second time.
        base_feedback = ctx.repair_feedback
        ctx.repair_feedback = ""
        try:
            patch, used = propose_structured(
                ctx,
                model=SelectionPatch,
                system=system,
                user=user,
                fallback=SelectionPatch,
            )
        finally:
            ctx.repair_feedback = base_feedback
        fixes: list[str] = []
        topology = state.artifact(PipelineStep.TOPOLOGY)
        catalog_issues = _ground_selected_parts(
            patch.upsert_parts,
            state.requirement_text,
            fixes,
            topology.component_roles if isinstance(topology, TopologyPlan) else None,
        )
        for fix in fixes:
            state.record_auto_fix(self.step, fix)
        repaired = _apply_selection_patch(artifact, patch)
        _apply_selection_identity_policy(
            repaired,
            state.requirement_text,
            topology if isinstance(topology, TopologyPlan) else None,
        )
        repaired.catalog_issues = list(dict.fromkeys(
            [*artifact.catalog_issues, *catalog_issues]
        ))[:100]
        return repaired, used

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, SelectionPlan)
        checks: list[CheckResult] = [
            CheckResult(
                name="has_parts", ok=bool(artifact.parts),
                message="selection must contain at least one part",
            ),
            CheckResult(
                name="compact_part_count",
                ok=len(artifact.parts) <= _MAX_SELECTION_PARTS,
                severity=Severity.WARNING,
                message=(
                    f"selection has {len(artifact.parts)} parts; {_MAX_SELECTION_PARTS} "
                    "is a context-efficiency target, not an electrical release gate"
                ),
            ),
        ]
        uncovered_blocks = _uncovered_topology_blocks(state, artifact.parts)
        checks.append(CheckResult(
            name="topology_blocks_covered",
            ok=not uncovered_blocks,
            # Formerly downgraded to WARNING for the deterministic ATmega
            # fallback, whose fixed BOM could not cover an arbitrary topology.
            # With no substitute BOM left, uncovered blocks always block.
            severity=Severity.ERROR,
            message=(
                "selected BOM has no semantic physical implementation for "
                f"topology blocks: {uncovered_blocks}"
            ),
        ))
        requested_mcus = _fixed_mcu_models(state.requirement_text)
        requested_mcu_families = _mcu_family_options(state.requirement_text)
        matched_mcu_parts: list[SelectedPart] = [
            part
            for part in artifact.parts
            if part.role.strip().casefold() in {
                "mcu", "microcontroller", "soc", "controller"
            }
        ] if not requested_mcus else []
        if requested_mcus:
            selected_models = _mcu_models(
                " ".join(f"{p.value} {p.symbol}" for p in artifact.parts)
            )
            family_matches = all(
                any(
                    requested in selected
                    or selected in requested
                    or _mcu_model_matches(requested, selected)
                    for selected in selected_models
                )
                for requested in requested_mcus
            )
            checks.append(CheckResult(
                name="requested_mcu_selected",
                ok=family_matches,
                message=(
                    f"requested MCU {sorted(requested_mcus)} is not present; "
                    f"selected MCU models: {sorted(selected_models)}"
                ),
            ))
            for p in artifact.parts:
                part_models = _mcu_models(f"{p.value} {p.symbol}")
                if not any(
                    requested in selected
                    or selected in requested
                    or _mcu_model_matches(requested, selected)
                    for requested in requested_mcus
                    for selected in part_models
                ):
                    continue
                matched_mcu_parts.append(p)
                expected_footprint = symbols.symbol_properties(p.symbol).get(
                    "Footprint", ""
                )
                if expected_footprint:
                    checks.append(CheckResult(
                        name=f"mcu_footprint:{p.ref}",
                        ok=p.footprint == expected_footprint,
                        message=(
                            f"{p.symbol} requires library footprint "
                            f"{expected_footprint!r}, got {p.footprint!r}"
                        ),
                    ))
        elif requested_mcu_families:
            selected_identities = [
                re.sub(
                    r"[^a-z0-9]",
                    "",
                    " ".join(
                        (
                            part.mpn,
                            part.value,
                            part.symbol,
                            part.device_family,
                            part.validation_profile,
                        )
                    ).casefold(),
                )
                for part in matched_mcu_parts
            ]
            family_matches = any(
                family.casefold() in selected
                for family in requested_mcu_families
                for selected in selected_identities
            )
            checks.append(CheckResult(
                name="requested_mcu_family_selected",
                ok=family_matches,
                message=(
                    "selected MCU does not satisfy allowed family options "
                    f"{requested_mcu_families}; selected identities: "
                    f"{selected_identities}"
                ),
            ))
        if matched_mcu_parts:
            expected_vdd = sum(
                _symbol_power_pin_counts(part.symbol)["VDD"]
                for part in matched_mcu_parts
            )
            expected_vdda = sum(
                _symbol_power_pin_counts(part.symbol)["VDDA"]
                for part in matched_mcu_parts
            )
            vdd_parts = [
                part for part in artifact.parts
                if re.fullmatch(
                    r"mcu_vdd_decoupling(?:_\d+)?",
                    part.role.lower(),
                )
            ]
            vdda_parts = [
                part for part in artifact.parts
                if re.fullmatch(
                    r"mcu_vdda_decoupling(?:_\d+)?",
                    part.role.lower(),
                )
            ]
            numbered_vdd_parts = [
                part for part in vdd_parts
                if re.fullmatch(r"mcu_vdd_decoupling_\d+", part.role.lower())
            ]
            numbered_vdda_parts = [
                part for part in vdda_parts
                if re.fullmatch(r"mcu_vdda_decoupling_\d+", part.role.lower())
            ]
            if expected_vdd or expected_vdda:
                checks.append(CheckResult(
                    name="mcu_supply_decoupling_not_excessive",
                    ok=(
                        len(numbered_vdd_parts) <= expected_vdd
                        and len(numbered_vdda_parts) <= expected_vdda
                    ),
                    message=(
                        "numbered per-pin decoupling roles exceed real MCU supply "
                        f"pins: VDD expected at most {expected_vdd}, found "
                        f"{len(numbered_vdd_parts)}; VDDA expected at most "
                        f"{expected_vdda}, found {len(numbered_vdda_parts)}"
                    ),
                ))
            if _requires_per_supply_pin_decoupling(state.requirement_text):
                checks.append(CheckResult(
                    name="mcu_supply_decoupling_count",
                    ok=(
                        len(vdd_parts) == expected_vdd
                        and len(vdda_parts) == expected_vdda
                    ),
                    message=(
                        "real MCU symbol supply pins require one 100nF capacitor "
                        f"each: VDD expected {expected_vdd}, found {len(vdd_parts)}; "
                        f"VDDA expected {expected_vdda}, found {len(vdda_parts)}"
                    ),
                ))
        expected_vcap = _grounded_vcap_uf(state.requirement_text)
        if expected_vcap is not None:
            vcap_parts = [
                part for part in artifact.parts
                if "vcap" in part.role.lower()
            ]
            invalid_vcap = [
                f"{part.ref}={part.value}"
                for part in vcap_parts
                if _capacitance_uf(part.value) != expected_vcap
            ]
            checks.append(CheckResult(
                name="grounded_vcap_capacitance",
                ok=len(vcap_parts) >= 2 and not invalid_vcap,
                message=(
                    f"official architect evidence requires two "
                    f"{expected_vcap:g}uF VCAP capacitors; found "
                    f"{[f'{part.ref}={part.value}' for part in vcap_parts]}"
                ),
            ))
        checks.extend(
            _selection_requirement_checks(
                state.requirement_text,
                artifact.parts,
            )
        )
        sym_root = config.symbol_dir()
        fp_root = config.footprint_dir()
        if sym_root is None:
            checks.append(CheckResult(
                name="tool_unavailable.symbol_library", ok=False, severity=Severity.WARNING,
                message=(
                    "no symbol library found in KICAD_SYMBOL_DIR or any KiCad install; "
                    "symbol pins not verified"
                ),
            ))
        if fp_root is None:
            checks.append(CheckResult(
                name="tool_unavailable.footprint_library", ok=False, severity=Severity.WARNING,
                message=(
                    "no footprint library found in KICAD_FOOTPRINT_DIR or any KiCad install; "
                    "pads not verified"
                ),
            ))
        # Per-part existence checks only when the relevant library is configured.
        # Selection verifies the symbol/footprint EXIST; pin-level verification
        # is the job of the pin-mapping step. Zero-pin symbols (e.g. a mounting
        # hole) are valid, so we resolve existence rather than requiring pins.
        for p in artifact.parts:
            resolved = None
            if sym_root is not None:
                resolved = symbols.resolve_symbol(p.symbol)
                checks.append(CheckResult(
                    name=f"symbol:{p.ref}", ok=resolved is not None,
                    message=f"symbol {p.symbol!r} for {p.ref} not found in library",
                ))
                if resolved is not None:
                    identity_error = _specific_component_identity_error(
                        p,
                        state.requirement_text,
                    )
                    checks.append(CheckResult(
                        name=f"component_identity:{p.ref}",
                        ok=identity_error is None,
                        message=identity_error or (
                            f"{p.ref} value and role match real library device "
                            f"{p.symbol!r}"
                        ),
                    ))
                    functional_requirement = (
                        _functional_connector_pin_requirement(
                            state.requirement_text,
                            p,
                        )
                    )
                    if functional_requirement is not None:
                        required_pins, interface_name = functional_requirement
                        real_pin_count = len({
                            str(pin["number"])
                            for pin in (symbols.symbol_pins(p.symbol) or [])
                            if pin["number"]
                        })
                        checks.append(CheckResult(
                            name=f"functional_pin_count:{p.ref}",
                            ok=real_pin_count >= required_pins,
                            message=(
                                f"{p.ref} role {p.role!r} is the "
                                f"{interface_name} and requires at least "
                                f"{required_pins} real electrical pins; "
                                f"{p.symbol!r} provides {real_pin_count}"
                            ),
                        ))
                    role = p.role.lower()
                    input_facing = (
                        "reverse_polarity" in role
                        or (
                            p.ref.upper().startswith("U")
                            and "dc_dc" in role
                            and "5v" in role
                        )
                    )
                    required_rating = _required_input_rating_v(
                        state.requirement_text
                    )
                    if input_facing and required_rating is not None:
                        actual_rating = _library_voltage_rating_v(p)
                        checks.append(CheckResult(
                            name=f"input_voltage_rating:{p.ref}",
                            ok=(
                                actual_rating is not None
                                and actual_rating >= required_rating
                            ),
                            message=(
                                f"{p.ref} on the protected industrial input "
                                f"requires at least {required_rating:g}V rating; "
                                f"real library description for {p.symbol!r} "
                                f"proves {actual_rating if actual_rating is not None else 'no'}V"
                            ),
                        ))
            if fp_root is not None and p.footprint:
                pads = footprints.footprint_pads(p.footprint)
                checks.append(CheckResult(
                    name=f"footprint:{p.ref}", ok=pads is not None,
                    message=f"footprint {p.footprint!r} for {p.ref} not found in library",
                ))
                symbol_pins = symbols.symbol_pins(p.symbol) if sym_root is not None else None
                if symbol_pins and pads:
                    pin_numbers = {
                        str(pin["number"]) for pin in symbol_pins if pin["number"]
                    }
                    pad_numbers = {
                        str(pad["number"]) for pad in pads if pad["number"]
                    }
                    connector_with_extra_pads = (
                        p.symbol.startswith(("Connector:", "Connector_Generic:"))
                        and pin_numbers.issubset(pad_numbers)
                    )
                    compatible_numbers = (
                        pin_numbers == pad_numbers or connector_with_extra_pads
                    )
                    footprint_hints = _compatible_footprint_hints(p.symbol)
                    checks.append(CheckResult(
                        name=f"pin_pad_compatibility:{p.ref}",
                        ok=compatible_numbers,
                        message=(
                            f"{p.ref} ({p.symbol}) symbol pins "
                            f"{sorted(pin_numbers)} do not match "
                            f"footprint pads {sorted(pad_numbers)}. Select a real "
                            "installed device symbol whose numeric pins match the "
                            "footprint. Grounded compatible footprint candidates: "
                            f"{footprint_hints or 'none found'}."
                        ),
                    ))
        # Datasheet-limit bottom line (anti-board-burn), driven by the fact
        # sheets rather than three hard-coded MCU fields. EVERY selected part
        # that has a sheet is checked against every slot the design can be
        # observed for, so an LDO's absolute maximum input, a TVS standoff and a
        # crystal's ESR are reachable here - not just the MCU's clock. Each
        # finding is its own check so a repair can target one violation, carries
        # the severity its slot's CONSEQUENCE implies (burn and malfunction
        # block, margin reports), and cites the page-level source of the
        # specific fact it violated. Fails open: unknown parts and
        # undeterminable values produce no finding, never a fabricated limit.
        topo_plan = state.artifact(PipelineStep.TOPOLOGY)
        findings = factgate.gate_findings(
            artifact.parts,
            rails=list(getattr(topo_plan, "rails", None) or []),
            requirement_text=state.requirement_text,
        )
        for finding in findings:
            checks.append(CheckResult(
                name=f"datasheet_limits:{finding.ref}:{finding.slot}",
                ok=False,
                severity=finding.severity,
                message=finding.as_text(),
                targets=list(finding.all_targets()),
            ))
        if not findings:
            checks.append(CheckResult(
                name="datasheet_limits", ok=True,
                message=(
                    "every selected part with a fact sheet is within its "
                    "datasheet limits"
                ),
            ))
        # A part with NO fact sheet passes the block above without being checked
        # at all, and a skipped part is indistinguishable from a clean one. That
        # fail-open behaviour is deliberate (an open-world catalog must not be
        # limited to the 17-sheet roster, and a fabricated limit is worse than
        # none), so this does not block — it announces. WARNING, because the
        # honest statement is "no evidence either way", not "this is wrong".
        for gap in factgate.coverage_gaps(artifact.parts):
            checks.append(CheckResult(
                name=f"datasheet_coverage:{gap.ref}",
                ok=False,
                severity=Severity.WARNING,
                message=gap.as_text(),
            ))
        return checks

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, SelectionPlan)
        grounded = sum(1 for p in artifact.parts if p.lcsc)
        return f"{len(artifact.parts)} parts ({grounded} grounded to a catalog MPN)"


class ComponentPrepareStep(PipelineStepBase):
    """Prepare selected symbols, footprints and procurement identities.

    This stage records what is ready for release without making catalog freshness
    or missing remote credentials an electrical design blocker. Schematic work can
    continue with an explicit release gap; production export consumes this report.
    """

    step = PipelineStep.COMPONENT_PREPARE
    knowledge_role = "selection"

    def knowledge_query(self, state: PipelineState) -> str | None:
        return f"component preparation and release evidence for: {state.requirement_text}"

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        selection = state.artifact(PipelineStep.SELECTION)
        if not isinstance(selection, SelectionPlan):
            return ComponentPrepareResult(
                notes=["selection artifact is unavailable; preparation deferred"]
            ), False

        symbol_root = config.symbol_dir()
        footprint_root = config.footprint_dir()
        prepared: list[PreparedComponent] = []
        unresolved: list[str] = []
        external: list[str] = []
        release_blockers: list[str] = []
        catalog_issues = list(selection.catalog_issues)
        for part in selection.parts:
            notes: list[str] = []
            blockers: list[str] = []
            evidence: list[str] = []
            if symbol_root is None:
                symbol_status = "unverified"
                notes.append("symbol library unavailable")
                blockers.append("symbol library evidence is unavailable")
            else:
                symbol_status = "verified" if symbols.resolve_symbol(part.symbol) else "missing"
                if symbol_status == "verified":
                    evidence.append(f"installed-symbol:{part.symbol}")
            if footprint_root is None:
                footprint_status = "unverified"
                notes.append("footprint library unavailable")
                blockers.append("footprint library evidence is unavailable")
            else:
                footprint_status = (
                    "verified" if part.footprint and footprints.footprint_pads(part.footprint)
                    is not None else "missing"
                )
                if footprint_status == "verified":
                    evidence.append(f"installed-footprint:{part.footprint}")
            if symbol_status == "missing" or footprint_status == "missing":
                asset_status = "missing"
                blockers.append("symbol/footprint asset is missing")
            elif symbol_status == "verified" and footprint_status == "verified":
                asset_status = "verified"
            else:
                asset_status = "unverified"

            mechanical = part.role.lower() in {"mounting_hole", "test_point", "fiducial"}
            if not part.mpn and not mechanical:
                unresolved.append(part.ref)
                blockers.append("no grounded manufacturer part number")
            if part.mpn:
                evidence.append(f"mpn:{part.mpn}")
            if not mechanical and part.package_match not in {"exact", "compatible"}:
                blockers.append(
                    f"catalog package match is {part.package_match!r}, not exact/compatible"
                )
            try:
                datasheet_parts = urlsplit(part.datasheet)
                datasheet_https = bool(
                    datasheet_parts.scheme == "https" and datasheet_parts.netloc
                )
            except ValueError:
                datasheet_https = False
            if mechanical:
                datasheet_status = "not_applicable"
            elif not part.datasheet:
                datasheet_status = "missing"
                blockers.append("datasheet evidence is missing")
            elif not datasheet_https:
                datasheet_status = "invalid_url"
                blockers.append("datasheet must be an absolute HTTPS URL")
            else:
                datasheet_status = "verified_https"
                evidence.append(f"datasheet:{part.datasheet}")
            if not mechanical and not part.catalog_provider:
                blockers.append("catalog provider provenance is missing")
            if not mechanical and not part.catalog_snapshot_id:
                blockers.append("catalog query snapshot is missing")
            elif part.catalog_snapshot_id:
                evidence.append(f"catalog-snapshot:{part.catalog_snapshot_id}")
            blockers.extend(part.constraint_gaps)
            if part.lifecycle.casefold() in {
                "obsolete",
                "eol",
                "end of life",
                "not recommended for new designs",
                "nrnd",
            }:
                blockers.append(f"lifecycle status is {part.lifecycle!r}")
            if part.dnp:
                blockers.append("component is marked DNP")
            if part.asset_status == "unverified_external":
                external.append(part.ref)
                blockers.append("external asset is not validated")
            blockers = list(dict.fromkeys(blockers))
            component_release_ready = not blockers
            status = (
                "installed_exact"
                if component_release_ready and part.package_match == "exact"
                else "installed_qualified_validated"
                if component_release_ready
                else "unresolved"
            )
            part.release_ready = component_release_ready
            part.unresolved = not component_release_ready
            part.resolution_status = status
            part.resolution_detail = "; ".join(blockers)
            part.identity_provenance = part.catalog_snapshot_id
            release_blockers.extend(f"{part.ref}: {blocker}" for blocker in blockers)
            prepared.append(
                PreparedComponent(
                    ref=part.ref,
                    role=part.role,
                    symbol=part.symbol,
                    footprint=part.footprint,
                    mpn=part.mpn,
                    lcsc=part.lcsc,
                    provider=part.catalog_provider,
                    datasheet=part.datasheet if hasattr(part, "datasheet") else "",
                    package_match=part.package_match,
                    datasheet_status=datasheet_status,
                    symbol_status=symbol_status,
                    footprint_status=footprint_status,
                    asset_status=(
                        "unverified_external" if part.asset_status == "unverified_external"
                        else asset_status
                    ),
                    status=status,
                    release_ready=component_release_ready,
                    dnp=part.dnp,
                    unresolved=not component_release_ready,
                    quantity=part.quantity,
                    notes=notes,
                    blockers=blockers,
                    evidence=evidence,
                )
            )
        return (
            ComponentPrepareResult(
                components=prepared,
                unresolved_refs=unresolved,
                external_asset_refs=external,
                catalog_issues=catalog_issues,
                release_ready=(
                    bool(prepared)
                    and all(component.release_ready for component in prepared)
                    and not release_blockers
                ),
                release_blockers=release_blockers,
                notes=[
                    "design may continue; release blockers are evaluated at manufacturing",
                    "remote provider credentials are optional",
                ],
            ),
            False,
        )

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, ComponentPrepareResult)
        selection = state.artifact(PipelineStep.SELECTION)
        expected = len(selection.parts) if isinstance(selection, SelectionPlan) else 0
        checks = [
            CheckResult(
                name="prepared_components_accounted",
                ok=len(artifact.components) == expected,
                message=(
                    f"prepared {len(artifact.components)} component(s), expected {expected}"
                ),
            ),
            CheckResult(
                name="component_release_ready",
                ok=artifact.release_ready,
                severity=Severity.WARNING,
                message=(
                    "component preparation is not release-ready: "
                    f"{artifact.release_blockers or ['unverified evidence']}"
                ),
            ),
        ]
        if artifact.external_asset_refs:
            checks.append(CheckResult(
                name="external_assets_validated",
                ok=False,
                severity=Severity.WARNING,
                message=(
                    "external assets require validation before production release: "
                    f"{artifact.external_asset_refs}"
                ),
            ))
        if artifact.catalog_issues:
            checks.append(CheckResult(
                name="catalog_evidence_available",
                ok=False,
                severity=Severity.WARNING,
                message=f"catalog provider evidence gaps: {artifact.catalog_issues}",
            ))
        return checks

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, ComponentPrepareResult)
        ready = sum(c.asset_status == "verified" for c in artifact.components)
        return f"{len(artifact.components)} components prepared; {ready} assets verified"


_POWER_NET_HINTS = ("VBUS", "VCC", "VDD", "3V3", "5V", "3.3V", "VIN", "VBAT")


def _classify_net(name: str) -> str:
    upper = name.upper()
    if upper in {"GND", "GROUND", "VSS"}:
        return "ground"
    if any(h in upper for h in _POWER_NET_HINTS):
        return "power"
    if "XTAL" in upper or "CLK" in upper or "CLOCK" in upper:
        return "clock"
    return "signal"


def _selected_parts_pin_block(selection: SelectionPlan | None) -> str:
    if selection is None:
        return ""
    lines: list[str] = []
    for part in selection.parts:
        pins = symbols.symbol_pins(part.symbol) or []
        shown = [
            (
                f"{pin['number']}={pin['name']}"
                if str(pin["name"]) not in ("", "~")
                else str(pin["number"])
            )
            for pin in pins
            if pin["number"]
        ]
        lines.append(
            f"{part.ref} role={part.role!r} value={part.value!r} "
            f"symbol={part.symbol!r} footprint={part.footprint!r} "
            f"pins=[{', '.join(shown) if shown else '(no pins)'}]"
        )
    return "\n".join(lines)


def _apply_netlist_patch(plan: NetlistIntent, patch: NetlistPatch) -> NetlistIntent:
    """Apply a bounded connection repair without regenerating valid nets."""
    removed_nets = {name.lower() for name in patch.remove_nets}
    nets = [
        net.model_copy(deep=True)
        for net in plan.nets
        if net.name.lower() not in removed_nets
    ]
    remove_keys = {pin.key().lower() for pin in patch.remove_pins}
    for net in nets:
        net.pins = [
            pin for pin in net.pins if pin.key().lower() not in remove_keys
        ]

    remove_no_connect_keys = {
        pin.key().lower() for pin in patch.remove_no_connect_pins
    }
    no_connect = {
        pin.key().lower(): pin.model_copy(deep=True)
        for pin in plan.no_connect_pins
        if pin.key().lower() not in remove_no_connect_keys
    }
    by_name = {net.name.lower(): net for net in nets}
    for update in patch.upsert_nets:
        target = by_name.get(update.name.lower())
        if target is None:
            target = NetIntent(
                name=update.name,
                kind=update.kind,
                pins=[],
                purpose=update.purpose,
            )
            nets.append(target)
            by_name[update.name.lower()] = target
        elif update.purpose:
            target.purpose = update.purpose
        for pin in update.pins:
            key = pin.key().lower()
            for net in nets:
                net.pins = [
                    existing
                    for existing in net.pins
                    if existing.key().lower() != key
                ]
            no_connect.pop(key, None)
            target.pins.append(pin.model_copy(deep=True))

    for pin in patch.add_no_connect_pins:
        key = pin.key().lower()
        for net in nets:
            net.pins = [
                existing
                for existing in net.pins
                if existing.key().lower() != key
            ]
        no_connect[key] = pin.model_copy(deep=True)

    nets = [net for net in nets if net.pins]
    net_names = {net.name for net in nets}
    supply_nets = [
        name
        for name in plan.supply_nets
        if name in net_names and name.lower() != plan.ground_net.lower()
    ]
    for update in patch.upsert_nets:
        if (
            update.kind == "power"
            and update.name.lower() != plan.ground_net.lower()
            and update.name not in supply_nets
        ):
            supply_nets.append(update.name)
    return NetlistIntent(
        additional_parts=patch.additional_parts,
        nets=nets,
        no_connect_pins=list(no_connect.values()),
        supply_nets=supply_nets,
        ground_net=plan.ground_net,
        rationale=plan.rationale,
    )


def _remove_unknown_netlist_refs(
    plan: NetlistIntent,
    selection: SelectionPlan | None,
) -> NetlistIntent:
    """Drop hallucinated pin references after a repair proposal.

    These references are not physical components: they occur in neither the
    grounded selection nor the connection step's explicit additional-parts
    delta. Removing only those invalid pin mentions lets the normal connectivity
    checks expose any net that now needs a real peer.
    """
    known_refs = {
        *(
            part.ref
            for part in (
                selection.parts if selection is not None else []
            )
        ),
        *(part.ref for part in plan.additional_parts),
    }
    return plan.model_copy(
        update={
            "nets": [
                net.model_copy(
                    update={
                        "pins": [
                            pin for pin in net.pins
                            if pin.ref in known_refs
                        ]
                    }
                )
                for net in plan.nets
            ],
            "no_connect_pins": [
                pin for pin in plan.no_connect_pins
                if pin.ref in known_refs
            ],
        },
        deep=True,
    )


def _remove_invalid_no_connect_pins(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Drop advisory NC entries absent from an otherwise grounded symbol."""
    parts = [
        *(selection.parts if selection is not None else []),
        *plan.additional_parts,
    ]
    by_ref = {part.ref: part for part in parts}
    valid: list[LogicalPin] = []
    for logical in plan.no_connect_pins:
        part = by_ref.get(logical.ref)
        if part is None:
            continue
        part_pins = symbols.symbol_pins(part.symbol) or []
        if not part_pins or _resolve_logical_pin(part_pins, logical.pin) is not None:
            valid.append(logical)
    return plan.model_copy(update={"no_connect_pins": valid}, deep=True)


def _normalize_additional_parts(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Make the connection-step part delta a case-insensitive ref upsert."""
    selected_refs = {
        part.ref.upper()
        for part in (selection.parts if selection is not None else [])
    }
    additions: dict[str, SelectedPart] = {}
    for part in plan.additional_parts:
        key = part.ref.upper()
        if key not in selected_refs:
            additions[key] = part.model_copy(deep=True)
    return plan.model_copy(
        update={"additional_parts": list(additions.values())},
        deep=True,
    )


def _normalize_bridged_capacitor_return(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
    fixes: list[str],
) -> NetlistIntent:
    """Move one terminal of a bridged two-terminal capacitor to ground.

    A capacitor with both terminals on one net is electrically a piece of wire:
    populated, paid for, assembled, doing nothing. It is the defect
    ``two_terminal_not_shorted`` reports, and a real run shipped it — ``C10``, the
    fourth VDD decoupling capacitor, with both pads on ``VDD33`` because no
    fourth supply pin was left to attach it to.

    Why this is a correction and not a choice
    ----------------------------------------
    The terminals of a non-polarised capacitor are interchangeable, and a
    capacitor bridging one rail has exactly one repair that keeps the part: give
    one terminal the return path. There is no second candidate to weigh, which is
    the only condition under which the pipeline corrects rather than reports.

    What it deliberately does not consult
    -------------------------------------
    ``role`` is not read. "This is a decoupling capacitor" is precisely the claim
    that was wrong when ``C10`` shipped, so identity comes from the reference
    designator and from the connectivity itself.

    The symbol library is not read either, and that is a correctness point rather
    than a shortcut: ``symbol_pins`` returns ``None`` for every part when the
    library roots resolve to nothing, and it reports that the same way it reports
    a genuinely unknown symbol. A correction that stops applying because the
    machine's ``KICAD_SYMBOL_DIR`` points somewhere stale is worse than one that
    never existed. The condition used instead is self-contained: a reference with
    exactly two wired pins, both on one net, is bridged whatever its symbol turns
    out to have.

    A capacitor bridged across the *ground* net is left alone. Moving a terminal
    to ground would change nothing, and which rail it should decouple is a design
    decision.
    """
    ground = plan.ground_net
    known_refs = {
        part.ref
        for part in [*(selection.parts if selection else []), *plan.additional_parts]
    }
    # ref -> [(net name, pin identifier)] for every wired pin, in file order.
    wired: dict[str, list[tuple[str, str]]] = {}
    for net in plan.nets:
        for pin in net.pins:
            wired.setdefault(pin.ref, []).append((net.name, pin.pin))

    # ref -> the net it is bridging
    bridged: dict[str, str] = {}
    for ref, entries in wired.items():
        if len(entries) != 2 or ref not in known_refs:
            continue
        if not ref.upper().startswith("C"):
            continue
        (first_net, first_pin), (second_net, second_pin) = entries
        # Same pin listed twice is a duplicate declaration, which
        # ``no_double_assigned_pins`` owns; correcting it here would hide it.
        if first_net != second_net or first_pin == second_pin:
            continue
        if first_net == ground:
            continue
        bridged[ref] = first_net
    if not bridged:
        return plan

    # The pin identifier to relocate: the second of the two, kept verbatim so no
    # assumption is made about a capacitor's pins being numbered "1" and "2".
    relocate = {ref: wired[ref][1][1] for ref in bridged}
    moved: set[str] = set()
    rebuilt: list[NetIntent] = []
    for net in plan.nets:
        kept: list[LogicalPin] = []
        for pin in net.pins:
            if (
                bridged.get(pin.ref) == net.name
                and pin.ref not in moved
                and relocate[pin.ref] == pin.pin
            ):
                moved.add(pin.ref)
                continue
            kept.append(pin)
        rebuilt.append(net.model_copy(update={"pins": kept}, deep=True))

    if not moved:
        return plan
    ground_net = next((n for n in rebuilt if n.name == ground), None)
    if ground_net is None:
        ground_net = NetIntent(name=ground, kind="ground", pins=[])
        rebuilt.append(ground_net)
    ground_pins = [*ground_net.pins]
    for ref in sorted(moved):
        ground_pins.append(LogicalPin(ref=ref, pin=relocate[ref]))
        fixes.append(
            f"{ref} had both terminals on {bridged[ref]}; moved pin "
            f"{relocate[ref]} to {ground} so the capacitor is across a rail "
            "instead of shorting it"
        )
    rebuilt = [
        n.model_copy(update={"pins": ground_pins}, deep=True)
        if n is ground_net
        else n
        for n in rebuilt
    ]
    return plan.model_copy(update={"nets": rebuilt}, deep=True)


def _normalize_duplicate_supply_pins(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
    fixes: list[str],
) -> NetlistIntent:
    """Put every pin of a repeated supply name on the one net its siblings use.

    A package that breaks one supply out across several pads gives them the same
    name: an LQFP-48 STM32 carries ``VDD`` on four pins, all bonded to one die
    rail. Wiring some and leaving the rest floating is not a design variant, it is
    an omission — the die cannot hold ``VDD`` at two potentials, so the unwired
    pads are the same node whether the netlist says so or not.

    Why this is a correction and not a choice
    ----------------------------------------
    The destination is not selected, it is read off the siblings. The pass fires
    only when the already-wired members of a name group agree on exactly one net,
    so there is nothing to weigh. Two nets means the design is asserting
    something this pass cannot adjudicate, and none wired means there is no
    sibling to copy; both are left for the checks to report.

    Grouping is by name **and** electrical type, never by name alone. ``VDD`` and
    ``VDDA`` differ for a reason — an analogue supply is routinely fed through its
    own filter or ferrite, and joining the two would erase a deliberate
    separation. Only ``power_in`` is considered: a repeated ``power_out`` is a
    converter's output stage, where paralleling is a layout decision rather than
    a bonding fact.

    Pins listed in ``no_connect_pins`` are skipped. A supply pin marked unused is
    a defect, but it is a *stated* one, and overwriting a statement would remove
    the evidence instead of the fault.

    Unlike its sibling passes this one reads the symbol library, because which
    pads share a supply name and which are ``power_in`` exists nowhere else. When
    the library is unreachable ``symbol_pins`` returns ``None`` and the pass does
    nothing, matching the fail-open every other library-dependent check uses.
    """
    parts = {
        part.ref: part
        for part in [*(selection.parts if selection else []), *plan.additional_parts]
    }
    if not parts:
        return plan
    net_of: dict[tuple[str, str], str] = {}
    for net in plan.nets:
        for pin in net.pins:
            net_of[(pin.ref, pin.pin)] = net.name
    declared_open = {(pin.ref, pin.pin) for pin in plan.no_connect_pins}

    additions: dict[str, list[LogicalPin]] = {}
    for ref, part in sorted(parts.items()):
        pins = symbols.symbol_pins(part.symbol)
        if not pins:
            continue
        groups: dict[str, list[str]] = {}
        for entry in pins:
            name = str(entry.get("name", ""))
            number = str(entry.get("number", ""))
            if not name or not number or str(entry.get("type", "")) != "power_in":
                continue
            groups.setdefault(name, []).append(number)
        for name, numbers in sorted(groups.items()):
            if len(numbers) < 2:
                continue
            wired = {
                net_of[(ref, number)]
                for number in numbers
                if (ref, number) in net_of
            }
            if len(wired) != 1:
                continue
            target = next(iter(wired))
            missing = [
                number
                for number in sorted(numbers)
                if (ref, number) not in net_of
                and (ref, number) not in declared_open
            ]
            if not missing:
                continue
            additions.setdefault(target, []).extend(
                LogicalPin(ref=ref, pin=number) for number in missing
            )
            fixes.append(
                f"{ref}: {name} is on pins {', '.join(sorted(numbers))} and only "
                f"some were wired; attached {', '.join(missing)} to {target} "
                "alongside its already-wired siblings"
            )
    if not additions:
        return plan
    rebuilt = [
        net.model_copy(
            update={"pins": [*net.pins, *additions.get(net.name, [])]},
            deep=True,
        )
        for net in plan.nets
    ]
    return plan.model_copy(update={"nets": rebuilt}, deep=True)


def _normalize_standard_connector_no_connects(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Enforce reserved/no-connect pins of recognized standard connectors."""
    if selection is None:
        return plan
    swd_refs = {
        part.ref
        for part in [*selection.parts, *plan.additional_parts]
        if "swd" in part.role.lower()
        and len(symbols.symbol_pins(part.symbol) or []) >= 10
    }
    if not swd_refs:
        return plan

    forced_nc = {(ref, pin) for ref in swd_refs for pin in ("7", "8")}
    valid_swo_refs = {
        pin.ref
        for net in plan.nets
        if any(
            token in re.sub(r"[^A-Z0-9]", "", net.name.upper())
            for token in ("SWO", "JTDO")
        )
        for pin in net.pins
        if pin.ref in swd_refs and pin.pin == "6"
    }
    nets: list[NetIntent] = []
    for net in plan.nets:
        normalized_name = re.sub(r"[^A-Z0-9]", "", net.name.upper())
        pins: list[LogicalPin] = []
        for pin in net.pins:
            key = (pin.ref, pin.pin)
            if key in forced_nc:
                continue
            if (
                pin.ref in swd_refs
                and pin.pin == "6"
                and not any(token in normalized_name for token in ("SWO", "JTDO"))
            ):
                if pin.ref not in valid_swo_refs:
                    forced_nc.add(key)
                continue
            pins.append(pin)
        nets.append(net.model_copy(update={"pins": pins}, deep=True))

    no_connects = {
        (pin.ref, pin.pin): pin.model_copy(deep=True)
        for pin in plan.no_connect_pins
    }
    for ref, pin in forced_nc:
        no_connects[(ref, pin)] = LogicalPin(ref=ref, pin=pin)
    return plan.model_copy(
        update={
            "nets": nets,
            "no_connect_pins": list(no_connects.values()),
        },
        deep=True,
    )


def _complete_evident_connector_power_pins(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Complete one connector power pin only when the net graph proves its rail."""
    if selection is None:
        return plan
    parts = {
        part.ref: part
        for part in [*selection.parts, *plan.additional_parts]
    }
    supply_by_lower = {name.lower(): name for name in plan.supply_nets}
    nets = [net.model_copy(deep=True) for net in plan.nets]
    pin_nets: dict[str, list[NetIntent]] = {}
    for net in nets:
        for pin in net.pins:
            pin_nets.setdefault(pin.ref, []).append(net)

    no_connect_keys = {
        (pin.ref, pin.pin)
        for pin in plan.no_connect_pins
    }
    for connector in parts.values():
        role = connector.role.lower()
        if not any(token in role for token in ("connector", "header", "interface")):
            continue
        physical_pins = symbols.symbol_pins(connector.symbol) or []
        if not 2 <= len(physical_pins) <= 16:
            continue
        connected_numbers = {
            number
            for net in nets
            for pin in net.pins
            if pin.ref == connector.ref
            and (
                number := _resolve_logical_pin(physical_pins, pin.pin)
            ) is not None
        }
        no_connect_numbers = {
            number
            for ref, logical in no_connect_keys
            if ref == connector.ref
            and (
                number := _resolve_logical_pin(physical_pins, logical)
            ) is not None
        }
        missing = [
            str(pin["number"])
            for pin in physical_pins
            if str(pin["number"]) not in connected_numbers
            and str(pin["number"]) not in no_connect_numbers
            and str(pin.get("type", "")).lower() != "no_connect"
        ]
        connector_nets = pin_nets.get(connector.ref, [])
        if len(missing) != 1 or any(
            net.name.lower() in supply_by_lower
            for net in connector_nets
        ):
            continue

        rail_votes: dict[str, int] = {}
        for signal_net in connector_nets:
            if signal_net.kind in {"power", "ground"}:
                continue
            for peer_pin in signal_net.pins:
                peer = parts.get(peer_pin.ref)
                if peer is None or peer.ref == connector.ref:
                    continue
                if len(symbols.symbol_pins(peer.symbol) or []) > 4:
                    continue
                for peer_net in pin_nets.get(peer.ref, []):
                    rail = supply_by_lower.get(peer_net.name.lower())
                    if rail is not None:
                        rail_votes[rail] = rail_votes.get(rail, 0) + 1
        ranked = sorted(
            rail_votes.items(),
            key=lambda item: (-item[1], item[0]),
        )
        if not ranked or ranked[0][1] < 2:
            continue
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            continue
        target = next(
            net for net in nets if net.name.lower() == ranked[0][0].lower()
        )
        target.pins.append(
            LogicalPin(ref=connector.ref, pin=missing[0])
        )
        pin_nets.setdefault(connector.ref, []).append(target)

    return plan.model_copy(update={"nets": nets}, deep=True)


_REPAIR_ROLE_DOMAINS: dict[str, tuple[str, ...]] = {
    "buck": ("buck", "switching_regulator"),
    "power_mux": ("power_mux", "power_path", "pmux"),
    "flash": ("flash",),
    "accelerometer": ("accelerometer", "motion_sensor"),
    "usb": ("usb",),
    "can": ("can",),
    "led": ("led",),
    "swd": ("swd",),
    "crystal": ("crystal", "oscillator"),
    "analog": ("analog",),
    "microsd": ("microsd", "sdio"),
    "i2c": ("i2c",),
}
_SEMANTIC_ROLE_STOPWORDS = {
    "board",
    "capacitor",
    "circuit",
    "component",
    "control",
    "controller",
    "device",
    "input",
    "interface",
    "output",
    "power",
    "protection",
    "resistor",
    "signal",
    "support",
}


def _semantic_role_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z][a-z0-9]{2,}", text.lower())
        if token not in _SEMANTIC_ROLE_STOPWORDS
    }


def _net_interface_prefix(name: str) -> str | None:
    """Return a conservative prefix for a visibly grouped interface net."""
    tokens = [
        token
        for token in re.split(r"[^A-Z0-9]+", name.upper())
        if token
    ]
    if len(tokens) < 2 or len(tokens[0]) < 2:
        return None
    return tokens[0]


def _connection_repair_scope(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
    checks: list[CheckResult],
) -> tuple[set[str], set[str], str]:
    """Build a compact, failure-related repair context."""
    failure_text = "\n".join(
        f"- {check.name}: {check.message}"
        for check in checks
        if not check.ok and check.severity == Severity.ERROR
    )
    related_refs = set(re.findall(r"\b[A-Z]{1,4}\d+\b", failure_text))
    single_nets = {
        net.name for net in plan.nets
        if len(net.pins) < 2
    }
    single_prefixes = {
        prefix
        for name in single_nets
        if (prefix := _net_interface_prefix(name)) is not None
    }
    interface_nets = {
        net.name
        for net in plan.nets
        if (
            (prefix := _net_interface_prefix(net.name)) is not None
            and prefix in single_prefixes
        )
    }
    for net in plan.nets:
        if net.name in single_nets or net.name in interface_nets:
            related_refs.update(pin.ref for pin in net.pins)

    scope_text = " ".join(
        [
            failure_text.lower(),
            *(name.lower() for name in single_nets),
        ]
    )
    parts = selection.parts if selection is not None else []
    by_ref = {part.ref: part for part in [*parts, *plan.additional_parts]}
    for ref in tuple(related_refs):
        part = by_ref.get(ref)
        if part is not None:
            scope_text += f" {part.role.lower()}"

    dynamic_tokens = _semantic_role_tokens(scope_text)
    domains = {
        domain
        for domain, aliases in _REPAIR_ROLE_DOMAINS.items()
        if any(alias in scope_text for alias in aliases)
    }
    for part in by_ref.values():
        role = part.role.lower()
        if any(
            any(alias in role for alias in _REPAIR_ROLE_DOMAINS[domain])
            for domain in domains
        ) or (_semantic_role_tokens(role) & dynamic_tokens):
            related_refs.add(part.ref)
    mentioned_nets = {
        net.name
        for net in plan.nets
        if net.name.lower() in failure_text.lower()
    }
    small_related_refs = {
        ref
        for ref in related_refs
        if (
            (part := by_ref.get(ref)) is not None
            and len(symbols.symbol_pins(part.symbol) or []) <= 4
        )
    }
    relevant_nets = {
        net.name
        for net in plan.nets
        if net.name in single_nets
        or net.name in interface_nets
        or net.name in mentioned_nets
        or any(pin.ref in small_related_refs for pin in net.pins)
    }
    # A failed connector/interface mapping must show the repair model all nets
    # on that bounded connector, not only the one whose label was rejected.
    # Avoid expanding around large ICs/MCUs, which would make the patch unsafe.
    explicitly_failed_refs = set(
        re.findall(r"\b[A-Z]{1,4}\d+\b", failure_text)
    )
    bounded_failed_refs = {
        ref
        for ref in explicitly_failed_refs
        if (
            (part := by_ref.get(ref)) is not None
            and len(symbols.symbol_pins(part.symbol) or []) <= 16
        )
    }
    relevant_nets.update(
        net.name
        for net in plan.nets
        if any(pin.ref in bounded_failed_refs for pin in net.pins)
    )
    if "power_input_net_has_source" in failure_text:
        relevant_nets.update(plan.supply_nets)
        # A failed power-input island may belong on an active device's internal
        # regulator output rather than an external supply rail. Expose only the
        # power-output nets of the failed devices so the repair model can choose
        # the grounded source without receiving every MCU signal net.
        for net in plan.nets:
            for logical in net.pins:
                if logical.ref not in explicitly_failed_refs:
                    continue
                part = by_ref.get(logical.ref)
                part_pins = (
                    symbols.symbol_pins(part.symbol) or []
                    if part is not None
                    else []
                )
                number = _resolve_logical_pin(part_pins, logical.pin)
                physical = next(
                    (
                        pin
                        for pin in part_pins
                        if str(pin.get("number", "")) == number
                    ),
                    None,
                )
                if physical is not None and str(
                    physical.get("type", "")
                ).lower() in {"power_out", "power_output"}:
                    relevant_nets.add(net.name)
                    break
    if "component_pins_accounted" in failure_text:
        failed_connectors = {
            ref
            for ref in bounded_failed_refs
            if (
                (part := by_ref.get(ref)) is not None
                and any(
                    token in part.role.lower()
                    for token in ("connector", "header", "interface")
                )
            )
        }
        if failed_connectors:
            relevant_nets.update(plan.supply_nets)
            relevant_nets.add(plan.ground_net)
    if re.search(
        r"\b(?:[A-Z0-9_]*VDD[A-Z0-9_]*|VCC|VBAT|VSUPPLY)\b",
        failure_text,
        re.IGNORECASE,
    ) or "power-output" in failure_text.lower():
        relevant_nets.update(plan.supply_nets)
    if re.search(
        r"\b(?:[A-Z0-9_]*VSS[A-Z0-9_]*|GND|GROUND|AGND)\b",
        failure_text,
        re.IGNORECASE,
    ):
        relevant_nets.add(plan.ground_net)
    # Interface repairs may need one previously unused MCU GPIO. Add the MCU
    # only after calculating the failed-net set; otherwise every valid MCU net
    # would enter the compact repair scope.
    if domains & {"led", "analog", "swd"}:
        related_refs.update(
            part.ref
            for part in by_ref.values()
            if part.role.lower() in {"mcu", "controller"}
            or part.symbol.lower().startswith("mcu_")
        )
    compact = {
        "failed_checks": failure_text,
        "related_refs": sorted(related_refs),
        "relevant_nets": [
            net.model_dump()
            for net in plan.nets
            if net.name in relevant_nets
        ],
        "relevant_no_connect_pins": [
            pin.model_dump()
            for pin in plan.no_connect_pins
            if pin.ref in related_refs
        ],
        "ground_net": plan.ground_net,
        "supply_nets": plan.supply_nets,
    }
    return related_refs, relevant_nets, json.dumps(
        compact,
        ensure_ascii=False,
    )


def _limit_netlist_patch_to_scope(
    patch: NetlistPatch,
    plan: NetlistIntent,
    related_refs: set[str],
    relevant_nets: set[str],
) -> NetlistPatch:
    """Prevent a repair delta from rewriting unrelated, already-valid nets."""
    additional_parts = patch.additional_parts[:8]
    allowed_refs = {
        *related_refs,
        *(part.ref for part in additional_parts),
    }
    existing_names = {net.name for net in plan.nets}
    protected_names = {
        plan.ground_net,
        *plan.supply_nets,
    }
    allowed_existing_names = {
        *relevant_nets,
        *protected_names,
    }
    upserts: list[NetIntent] = []
    for update in patch.upsert_nets:
        if (
            update.name in existing_names
            and update.name not in allowed_existing_names
        ):
            continue
        pins = [
            pin for pin in update.pins
            if pin.ref in allowed_refs
        ]
        if pins:
            upserts.append(update.model_copy(update={"pins": pins}, deep=True))
    return NetlistPatch(
        additional_parts=additional_parts,
        remove_nets=[
            name for name in patch.remove_nets
            if name in relevant_nets and name not in protected_names
        ],
        remove_pins=[
            pin for pin in patch.remove_pins
            if pin.ref in allowed_refs
        ],
        upsert_nets=upserts,
        add_no_connect_pins=[
            pin for pin in patch.add_no_connect_pins
            if pin.ref in allowed_refs
        ],
        remove_no_connect_pins=[
            pin for pin in patch.remove_no_connect_pins
            if pin.ref in allowed_refs
        ],
    )


class SchConnectionsStep(PipelineStepBase):
    """Schematic connection design: the electrical netlist *intent*.

    Produces named nets of logical pins (no real pin numbers yet — that is the
    pin-mapping step). Bottom-line check: no single-pin/empty nets, and both a
    supply rail and a ground net must exist.
    """

    step = PipelineStep.SCH_CONNECTIONS
    knowledge_role = "schematic"

    def knowledge_query(self, state: PipelineState) -> str | None:
        return f"net and connectivity design, power ground signals for: {state.requirement_text}"

    def _selected_parts(self, state: PipelineState) -> list[SelectedPart]:
        sel = state.artifact(PipelineStep.SELECTION)
        return list(sel.parts) if isinstance(sel, SelectionPlan) else []

    def fact_sheets_for_step(
        self, state: PipelineState
    ) -> list[tuple[str, FactSheetBase]] | None:
        """The REAL selection, because by now there is one.

        Unlike topology and selection, this step resolves devices from the parts
        actually chosen, so the brief is keyed to board reference designators and
        covers support devices the requirement never named. This is where the
        vendor's decoupling table, reset requirement, strapping pins and mandatory
        peripherals arrive — the facts that decide what has to be connected.
        """
        return factbrief.resolve_sheets(self._selected_parts(state)) or None

    def uncovered_parts_for_step(self, state: PipelineState) -> list[str]:
        return factbrief.uncovered_names(self._selected_parts(state))

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        def fallback() -> NetlistIntent:
            """No proposal means no netlist — never a different board's netlist.

            Same reason as :meth:`SelectionStep.propose`: the ATmega reference
            netlist used to stand in here. An empty intent is constructible by
            contract so the connection checks report it (fail closed).
            """
            return NetlistIntent(
                nets=[],
                supply_nets=[],
                ground_net="GND",
                rationale="no proposal: no usable LLM output",
            )

        sel = state.artifact(PipelineStep.SELECTION)
        selected_count = len(sel.parts) if isinstance(sel, SelectionPlan) else 0
        additional_budget = min(
            8, max(0, _MAX_SELECTION_PARTS - selected_count)
        )
        additional_policy = (
            "additional_parts MUST be an empty array; the supplied selection is "
            "authoritative and already contains the required physical parts. Reuse "
            "those parts according to their roles."
            if additional_budget == 0
            else (
                f"At most {additional_budget} genuinely missing physical parts may "
                "be defined in additional_parts. Reuse a supplied part with the "
                "needed role before adding anything."
            )
        )
        system = (
            "You design the electrical connectivity as JSON: additional_parts[], "
            "nets[] with name, kind (power/ground/signal/clock), pins[] "
            "({ref, pin}), purpose; no_connect_pins[] ({ref, pin}); plus "
            "supply_nets[], ground_net, rationale. "
            f"{additional_policy} "
            "Every terminal of an added two-pin component must be connected. "
            "Every real pin of every selected component must occur in exactly one "
            "net or in no_connect_pins. Every selected component must have at least "
            "one genuinely connected pin. Explicitly list unused MCU GPIO, connector "
            "reserved/SBU pins, and IC NC pins in no_connect_pins. Never abandon a "
            "selected passive, protection, switch, crystal, diode, transistor, or "
            "other <=4-pin part with a no-connect marker; connect it or remove it. "
            "Every net needs >= 2 pins. "
            "A component pin may appear on exactly one electrical net; never reuse "
            "one TVS/protection pin or crystal pin on multiple nets. "
            "Use the single declared ground_net for every ground return. Do not "
            "create per-channel GND aliases or connect one MCU VSS pin to multiple "
            "named nets. Likewise, reuse the declared supply net instead of making "
            "a one-pin rail alias. "
            "Never emit an unused pin or a test point as a standalone one-pin net: "
            "either attach a test point to the existing functional net, connect a "
            "required signal to its selected peer components, or omit that net. "
            "NRST, BOOT, SWD, buses, LEDs, and clocks are not valid one-pin nets. "
            "Use component references from the supplied list or from your explicit "
            "additional_parts definitions. Never emit an undefined R/C/U/J/D "
            "reference. Do not add a component when a datasheet-approved direct "
            "strap or an existing selected part is sufficient. "
            "IMPORTANT: for each component pin, use ONLY a pin name (or number) "
            "from the exact list given for that component below. Do not invent "
            "pin names such as VIN/AGND/ANODE if they are not in the list. "
            "For a standard 10-pin Cortex SWD connector use pin 1 VTref, 2 SWDIO, "
            "3 GND, 4 SWCLK, 5 GND, 6 SWO, 7 NC/key, 8 NC, 9 GNDDetect, 10 NRST. "
            "VTref must join the MCU I/O supply rail (normally 3V3), never a "
            "standalone one-pin alias. Mark unused connector pins as no-connect. "
            "Connect every selected bootstrap capacitor directly between its "
            "converter BOOT and SW nets. Strap power-mux mode/priority inputs to a "
            "valid existing rail or GND according to the device function; do not "
            "create one-pin configuration nets. For selectable CAN "
            "termination create a series chain CANH--120R--TERM_LINK--jumper--CANL, "
            "not two parts independently across CANH/CANL. Route both CAN choke "
            "input pins from the transceiver and both output pins to the connector. "
            "Never create a one-pin net for a converter or power-path control pin: "
            "connect its selected support network/strap, or mark it no-connect only "
            "when the exact device allows that state. Tie unused active-low SPI "
            "flash WP and HOLD/RESET control pins to the I/O supply using a "
            "datasheet-valid direct strap or selected pull-up; never mark RESET as "
            "no-connect. Wire each LED and its own resistor as a real series path: "
            "supply-or-MCU -- resistor -- unique intermediate net -- LED -- "
            "ground-or-supply. The LED and resistor must share exactly one net. "
            "Never connect two power-output pins to one rail. Every real power-input "
            "pin must be on a declared supply rail or on a rail driven by exactly "
            "one real power-output pin; do not create isolated signal nets for "
            "power-input pins. An IC's internal "
            "regulator output must use its own datasheet rail for core-supply pins "
            "and decoupling, not the external regulator output rail. "
            f"{_FACT_AUTHORITY}"
        )
        refs_block = _selected_parts_pin_block(
            sel if isinstance(sel, SelectionPlan) else None
        )
        user = (
            f"Requirement:\n{_original_requirement(state.requirement_text)}\n\n"
            "Selected components with their roles and REAL pin names/numbers:\n"
            f"{refs_block}\n\n"
            f"{_facts_block(ctx)}"
            f"Knowledge:\n{knowledge}"
        )
        plan, used = propose_structured(
            ctx, model=NetlistIntent, system=system, user=user, fallback=fallback
        )
        selected = sel if isinstance(sel, SelectionPlan) else None
        # Sequential rather than nested: each pass is a separate normalisation,
        # and the ones that correct rather than tidy have to report what they did.
        fixes: list[str] = []
        _ground_selected_parts(plan.additional_parts, state.requirement_text, fixes)
        plan = _normalize_additional_parts(selected, plan)
        plan = _complete_evident_connector_power_pins(selected, plan)
        plan = _normalize_standard_connector_no_connects(selected, plan)
        plan = _remove_invalid_no_connect_pins(selected, plan)
        plan = _normalize_bridged_capacitor_return(selected, plan, fixes)
        plan = _normalize_duplicate_supply_pins(selected, plan, fixes)
        for fix in fixes:
            state.record_auto_fix(self.step, fix)
        return plan, used

    def repair(
        self,
        state: PipelineState,
        ctx: PipelineContext,
        knowledge: str,
        artifact: BaseModel,
        checks: list[CheckResult],
    ) -> tuple[BaseModel, bool]:
        assert isinstance(artifact, NetlistIntent)
        selection = state.artifact(PipelineStep.SELECTION)
        related_refs, relevant_nets, compact_context = _connection_repair_scope(
            selection if isinstance(selection, SelectionPlan) else None,
            artifact,
            checks,
        )
        scoped_parts = [
            part
            for part in [
                *(
                    selection.parts
                    if isinstance(selection, SelectionPlan)
                    else []
                ),
                *artifact.additional_parts,
            ]
            if part.ref in related_refs
        ]
        refs_block = _selected_parts_pin_block(
            SelectionPlan(parts=scoped_parts)
            if scoped_parts
            else None
        )
        system = (
            "Repair an existing electrical netlist by returning one JSON patch with "
            "fields: additional_parts (the COMPLETE replacement list, maximum 8), "
            "remove_nets[], remove_pins[] ({ref,pin}), upsert_nets[] "
            "({name,kind,pins[],purpose}), add_no_connect_pins[] and "
            "remove_no_connect_pins[]. Do not return the full netlist. Pins in an "
            "upsert net are moved from their old net. Preserve correct existing nets. "
            "Never solve a duplicate assignment by merging unrelated nets. Every "
            "selected <=4-pin physical part must remain fully connected. Remove every "
            "pin whose component ref is absent from the selected/additional part list. "
            "Resolve every reported unaccounted pin: connect a required functional pin "
            "or add an explicit no-connect marker for a genuinely unused pin. A series "
            "LED path is supply-or-MCU -- resistor -- unique intermediate net -- LED "
            "-- ground-or-supply. The LED and its channel resistor share exactly one "
            "net; do not place both parts in parallel on the same two nets. Remove "
            "the obsolete isolated endpoint nets after moving their pins. Move all "
            "ground-return pins into the existing ground_net named in the compact "
            "context; never create per-channel GND aliases or reuse one MCU VSS pin "
            "as their peer. Move supply endpoints into an existing declared supply "
            "net in the same way. Use an "
            "unused MCU GPIO from the supplied no-connect list for a controllable "
            "status LED. Tie unused active-low SPI flash WP and HOLD/RESET pins to "
            "the I/O supply using a datasheet-valid direct strap or selected pull-up; "
            "never leave RESET as no-connect. A "
            "selectable CAN termination path needs the resistor and jumper in series "
            "between CANH and CANL. Never leave two power-output pins on one rail; "
            "every power-input pin must be on a declared supply rail or a rail with "
            "exactly one real power-output pin. Move an internal regulator output "
            "and its core-supply/decoupling pins "
            "to their own datasheet rail. When one member of a named interface bus "
            "is single-ended, inspect every supplied net with the same interface "
            "prefix and rebuild a one-to-one mapping between the endpoint devices; "
            "real pin labels may use direction or IO aliases rather than the net "
            "name. For a standard 10-pin Cortex SWD connector preserve pin 1 VTref, "
            "2 SWDIO, 3 GND, 4 SWCLK, 5 GND, 6 SWO, 7 NC/key, 8 NC, "
            "9 GNDDetect, and 10 NRST. On a rejected SWD mapping, explicitly "
            "remove pins 6/7/8 from any wrong supply or ground net and mark the "
            "unused pins as no-connect; reserved/key pins never join a rail."
            " For an unaccounted pin on a bounded external connector/header, use "
            "the connector role and its already-wired peers to decide whether it "
            "is the interface power pin. Join the appropriate existing supply "
            "rail when the interface exposes power; do not invent a one-pin net "
            "or mark a required connector power pin as unused."
        )
        user = (
            "Patch only the failed checks in the rejected netlist. Use exact selected "
            "references and exact real pin names/numbers below. If a genuinely missing "
            "support part is necessary, declare it in additional_parts with a real "
            "symbol and footprint; otherwise keep the current grounded additions.\n\n"
            f"Selected components:\n{refs_block}"
        )
        ctx.repair_feedback = (
            "Repair only this compact failure scope. Do not restate or modify "
            "unlisted nets/components:\n"
            f"{compact_context}"
        )
        patch, used = propose_structured(
            ctx,
            model=NetlistPatch,
            system=system,
            user=user,
            fallback=NetlistPatch,
        )
        patch = _limit_netlist_patch_to_scope(
            patch,
            artifact,
            related_refs,
            relevant_nets,
        )
        repaired = _remove_unknown_netlist_refs(
            _apply_netlist_patch(artifact, patch),
            selection if isinstance(selection, SelectionPlan) else None,
        )
        selected = selection if isinstance(selection, SelectionPlan) else None
        fixes: list[str] = []
        _ground_selected_parts(
            repaired.additional_parts, state.requirement_text, fixes
        )
        repaired = _normalize_additional_parts(selected, repaired)
        repaired = _complete_evident_connector_power_pins(selected, repaired)
        repaired = _normalize_standard_connector_no_connects(selected, repaired)
        repaired = _remove_invalid_no_connect_pins(selected, repaired)
        repaired = _normalize_bridged_capacitor_return(selected, repaired, fixes)
        repaired = _normalize_duplicate_supply_pins(selected, repaired, fixes)
        for fix in fixes:
            state.record_auto_fix(self.step, fix)
        return repaired, used

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, NetlistIntent)
        checks: list[CheckResult] = [
            CheckResult(
                name="has_nets", ok=bool(artifact.nets),
                message="connectivity must define at least one net",
            )
        ]
        # No single-pin or empty nets (the classic wiring mistake).
        singles = [n.name for n in artifact.nets if len(n.pins) < 2]
        checks.append(CheckResult(
            name="no_single_pin_nets", ok=not singles,
            message=f"single-pin/empty nets: {singles}",
        ))
        # A supply rail and a ground net must both exist.
        net_names = {n.name for n in artifact.nets}
        has_power = bool(artifact.supply_nets) or any(n.kind == "power" for n in artifact.nets)
        checks.append(CheckResult(
            name="has_supply_net", ok=has_power,
            message="no supply/power net defined",
        ))
        checks.append(CheckResult(
            name="has_ground_net", ok=artifact.ground_net in net_names
            or any(n.kind == "ground" for n in artifact.nets),
            message=f"ground net {artifact.ground_net!r} not present in the netlist",
        ))
        ground_names = {
            artifact.ground_net.lower(),
            *(n.name.lower() for n in artifact.nets if n.kind == "ground"),
        }
        ground_in_supply = sorted(
            name for name in artifact.supply_nets
            if name.lower() in ground_names
        )
        checks.append(CheckResult(
            name="ground_not_declared_as_supply",
            ok=not ground_in_supply,
            message=f"ground nets cannot be listed as supply rails: {ground_in_supply}",
        ))
        # A connection step may discover required support parts that selection
        # could not anticipate. They are accepted only as an explicit, grounded
        # delta whose combined selection still passes every selection gate.
        sel = state.artifact(PipelineStep.SELECTION)
        combined_selection = sel if isinstance(sel, SelectionPlan) else None
        existing_refs = (
            {part.ref for part in sel.parts}
            if isinstance(sel, SelectionPlan)
            else set()
        )
        selected_count = len(sel.parts) if isinstance(sel, SelectionPlan) else 0
        additional_budget = min(
            8, max(0, _MAX_SELECTION_PARTS - selected_count)
        )
        checks.append(CheckResult(
            name="additional_part_budget",
            ok=len(artifact.additional_parts) <= additional_budget,
            message=(
                f"connection step added {len(artifact.additional_parts)} parts; "
                f"budget is {additional_budget} for a {selected_count}-part selection"
            ),
        ))
        additional_refs = {part.ref for part in artifact.additional_parts}
        conflicting_refs = sorted(existing_refs & additional_refs)
        if artifact.additional_parts:
            checks.append(CheckResult(
                name="additional_part_refs_new",
                ok=isinstance(sel, SelectionPlan) and not conflicting_refs,
                message=(
                    "additional parts require an existing selection and must use "
                    f"new references; conflicts: {conflicting_refs}"
                ),
            ))
            if isinstance(sel, SelectionPlan) and not conflicting_refs:
                combined_selection = SelectionPlan(
                    parts=[*sel.parts, *artifact.additional_parts],
                    rationale=sel.rationale,
                )
                for selection_check in SelectionStep().check(
                    state,
                    combined_selection,
                ):
                    checks.append(CheckResult(
                        name=f"additional_parts:{selection_check.name}",
                        ok=selection_check.ok,
                        severity=selection_check.severity,
                        message=selection_check.message,
                    ))

            used_by_ref: dict[str, list[str]] = {
                part.ref: [] for part in artifact.additional_parts
            }
            for net in artifact.nets:
                for pin in net.pins:
                    if pin.ref in used_by_ref:
                        used_by_ref[pin.ref].append(pin.pin)
            unused = sorted(
                ref for ref, pins in used_by_ref.items() if not pins
            )
            checks.append(CheckResult(
                name="additional_parts_used",
                ok=not unused,
                message=f"declared additional parts not used in any net: {unused}",
            ))

            incomplete: list[str] = []
            for part in artifact.additional_parts:
                part_pins = symbols.symbol_pins(part.symbol) or []
                logical_pins = used_by_ref[part.ref]
                if len(part_pins) == 2:
                    expected_numbers = {
                        str(pin["number"])
                        for pin in part_pins
                        if pin["number"]
                    }
                    used_numbers = {
                        number
                        for logical in logical_pins
                        if (
                            number := _resolve_logical_pin(
                                part_pins,
                                logical,
                            )
                        ) is not None
                    }
                    missing = sorted(expected_numbers - used_numbers)
                    if missing:
                        incomplete.append(
                            f"{part.ref} missing terminal(s) {missing}"
                        )
                elif not part_pins and len(set(logical_pins)) < 2:
                    incomplete.append(
                        f"{part.ref} has fewer than two connected terminals"
                    )
            checks.append(CheckResult(
                name="additional_two_pin_parts_fully_connected",
                ok=not incomplete,
                message=f"incomplete additional two-pin parts: {incomplete}",
            ))

        # Consistency: every net ref must be selected or declared above.
        if isinstance(sel, SelectionPlan) and sel.parts:
            known = (
                {part.ref for part in combined_selection.parts}
                if isinstance(combined_selection, SelectionPlan)
                else existing_refs
            )
            unknown = sorted(
                {p.ref for n in artifact.nets for p in n.pins if p.ref not in known}
            )
            checks.append(CheckResult(
                name="pins_reference_selected_parts", ok=not unknown,
                message=f"nets reference unknown component refs: {unknown}",
            ))
        # No logical pin on two nets (a short). Catching it here — at an LLM
        # step — lets the repair loop feed it back and fix it, instead of
        # failing later at deterministic pin-mapping where no self-repair runs.
        seen_pins: dict[str, str] = {}
        shorted: list[str] = []
        for n in artifact.nets:
            for p in n.pins:
                key = f"{p.ref}:{p.pin}"
                if key in seen_pins and seen_pins[key] != n.name:
                    shorted.append(f"{key} in {seen_pins[key]} & {n.name}")
                seen_pins[key] = n.name
        checks.append(CheckResult(
            name="no_pin_on_multiple_nets", ok=not shorted,
            message=f"logical pin(s) on multiple nets (short): {shorted}",
        ))
        # Every logical pin must resolve to a real pin on its part's symbol, so
        # deterministic pin-mapping downstream cannot fail. Verified here — an
        # LLM step — so the repair loop can fix a bad/invented pin name.
        if (
            isinstance(combined_selection, SelectionPlan)
            and combined_selection.parts
            and config.symbol_dir() is not None
        ):
            ref_syms = {
                part.ref: part.symbol
                for part in combined_selection.parts
            }
            bad_pins: list[str] = []
            for n in artifact.nets:
                for p in n.pins:
                    part_pins = symbols.symbol_pins(ref_syms.get(p.ref, "")) or []
                    if not part_pins:
                        continue  # zero-pin symbol (e.g. mounting hole)
                    nums = {str(x["number"]) for x in part_pins}
                    ok = _resolve_logical_pin(part_pins, p.pin) is not None or (
                        p.pin.isdigit() and p.pin in nums
                    )
                    if not ok:
                        bad_pins.append(f"{p.ref}:{p.pin}")
            checks.append(CheckResult(
                name="logical_pins_resolve", ok=not bad_pins,
                message=f"logical pins not found on the part symbol: {bad_pins}",
            ))
            ref_parts = {
                part.ref: part
                for part in combined_selection.parts
            }
            connected_numbers: set[str] = set()
            physical_owners: dict[str, str] = {}
            physical_shorts: list[str] = []
            physical_endpoints_by_net: dict[str, set[str]] = {}
            for net in artifact.nets:
                physical_endpoints_by_net[net.name] = set()
                for logical in net.pins:
                    part = ref_parts.get(logical.ref)
                    part_pins = (
                        symbols.symbol_pins(part.symbol) or []
                        if part is not None
                        else []
                    )
                    number = _resolve_logical_pin(part_pins, logical.pin)
                    if number is not None:
                        key = f"{logical.ref}:{number}"
                        physical_endpoints_by_net[net.name].add(key)
                        previous_net = physical_owners.get(key)
                        if previous_net is not None and previous_net != net.name:
                            physical_shorts.append(
                                f"{key} in {previous_net} & {net.name}"
                            )
                        physical_owners[key] = net.name
                        connected_numbers.add(key)
            checks.append(CheckResult(
                name="no_physical_pin_on_multiple_nets",
                ok=not physical_shorts,
                message=(
                    "resolved physical pin(s) on multiple nets (short): "
                    f"{physical_shorts}"
                ),
            ))
            single_physical_nets = sorted(
                net.name
                for net in artifact.nets
                if (
                    len(net.pins) >= 2
                    and len(physical_endpoints_by_net[net.name]) < 2
                )
            )
            checks.append(CheckResult(
                name="no_single_physical_pin_nets",
                ok=not single_physical_nets,
                message=(
                    "nets collapse to fewer than two distinct physical pins "
                    f"after pin-name resolution: {single_physical_nets}"
                ),
            ))
            rail_polarity_conflicts: list[str] = []
            supply_names = {name.lower() for name in artifact.supply_nets}
            ground_names = {
                artifact.ground_net.lower(),
                *(
                    net.name.lower()
                    for net in artifact.nets
                    if net.kind == "ground"
                ),
            }
            for net in artifact.nets:
                for logical in net.pins:
                    part = ref_parts.get(logical.ref)
                    part_pins = (
                        symbols.symbol_pins(part.symbol) or []
                        if part is not None
                        else []
                    )
                    number = _resolve_logical_pin(part_pins, logical.pin)
                    physical = next(
                        (
                            pin
                            for pin in part_pins
                            if str(pin.get("number", "")) == number
                        ),
                        None,
                    )
                    if physical is None:
                        continue
                    pin_name = str(physical.get("name", "")).strip().upper()
                    pin_type = str(physical.get("type", "")).lower()
                    is_ground_pin = bool(
                        re.search(
                            r"(?:^|[_/])(GND[A-Z0-9]*|[A-Z]*VSS[A-Z0-9]*)(?:$|[_/])",
                            pin_name,
                        )
                    )
                    if (
                        net.name.lower() in ground_names
                        and pin_type in {"power_in", "power_input"}
                        and not is_ground_pin
                    ):
                        rail_polarity_conflicts.append(
                            f"{logical.ref}:{number}({pin_name}) positive power "
                            f"input is on ground net {net.name}"
                        )
                    elif (
                        net.name.lower() in supply_names
                        and is_ground_pin
                    ):
                        rail_polarity_conflicts.append(
                            f"{logical.ref}:{number}({pin_name}) ground pin is "
                            f"on supply net {net.name}"
                        )
            checks.append(CheckResult(
                name="power_pin_rail_polarity",
                ok=not rail_polarity_conflicts,
                message=(
                    "power and ground pin polarity conflicts: "
                    f"{rail_polarity_conflicts}"
                ),
            ))
            power_output_conflicts: list[str] = []
            for net in artifact.nets:
                outputs: set[str] = set()
                for logical in net.pins:
                    part = ref_parts.get(logical.ref)
                    part_pins = (
                        symbols.symbol_pins(part.symbol) or []
                        if part is not None
                        else []
                    )
                    number = _resolve_logical_pin(part_pins, logical.pin)
                    physical = next(
                        (
                            pin
                            for pin in part_pins
                            if str(pin.get("number", "")) == number
                        ),
                        None,
                    )
                    if physical is None or str(
                        physical.get("type", "")
                    ).lower() not in {"power_out", "power_output"}:
                        continue
                    outputs.add(
                        f"{logical.ref}:{number}({physical.get('name', '')})"
                    )
                if len(outputs) > 1:
                    power_output_conflicts.append(
                        f"{net.name} has power outputs {sorted(outputs)}"
                    )
            checks.append(CheckResult(
                name="single_power_output_per_net",
                ok=not power_output_conflicts,
                message=(
                    "multiple power-output pins must not drive one rail: "
                    f"{power_output_conflicts}"
                ),
            ))
            power_input_source_gaps: list[str] = []
            for net in artifact.nets:
                if (
                    net.name.lower() in supply_names
                    or net.name.lower() in ground_names
                ):
                    continue
                inputs: set[str] = set()
                outputs: set[str] = set()
                for logical in net.pins:
                    part = ref_parts.get(logical.ref)
                    part_pins = (
                        symbols.symbol_pins(part.symbol) or []
                        if part is not None
                        else []
                    )
                    number = _resolve_logical_pin(part_pins, logical.pin)
                    physical = next(
                        (
                            pin
                            for pin in part_pins
                            if str(pin.get("number", "")) == number
                        ),
                        None,
                    )
                    if physical is None:
                        continue
                    pin_type = str(physical.get("type", "")).lower()
                    endpoint = (
                        f"{logical.ref}:{number}({physical.get('name', '')})"
                    )
                    if pin_type in {"power_in", "power_input"}:
                        inputs.add(endpoint)
                    elif pin_type in {"power_out", "power_output"}:
                        outputs.add(endpoint)
                if inputs and not outputs:
                    power_input_source_gaps.append(
                        f"{net.name} has power inputs {sorted(inputs)} but no "
                        "power output and is not a declared supply rail"
                    )
            checks.append(CheckResult(
                name="power_input_net_has_source",
                ok=not power_input_source_gaps,
                message=(
                    "power-input-only islands must join a declared supply rail or "
                    "a net with one real power output: "
                    f"{power_input_source_gaps}"
                ),
            ))

            no_connect_numbers: set[str] = set()
            invalid_no_connects: list[str] = []
            for logical in artifact.no_connect_pins:
                part = ref_parts.get(logical.ref)
                part_pins = (
                    symbols.symbol_pins(part.symbol) or []
                    if part is not None
                    else []
                )
                number = _resolve_logical_pin(part_pins, logical.pin)
                if number is None:
                    invalid_no_connects.append(logical.key())
                    continue
                no_connect_numbers.add(f"{logical.ref}:{number}")
            checks.append(CheckResult(
                name="no_connect_pins_resolve",
                ok=not invalid_no_connects,
                message=(
                    "no-connect pins not found on the selected symbol: "
                    f"{invalid_no_connects}"
                ),
            ))

            conflicting_no_connects = sorted(
                connected_numbers & no_connect_numbers
            )
            checks.append(CheckResult(
                name="no_connect_pins_not_connected",
                ok=not conflicting_no_connects,
                message=(
                    "pins cannot be both connected and marked no-connect: "
                    f"{conflicting_no_connects}"
                ),
            ))

            unused_components: list[str] = []
            missing_pin_disposition: list[str] = []
            abandoned_small_parts: list[str] = []
            for part in combined_selection.parts:
                part_pins = symbols.symbol_pins(part.symbol) or []
                pin_by_number = {
                    str(pin["number"]): pin
                    for pin in part_pins
                    if pin["number"]
                }
                if not pin_by_number:
                    continue
                connected_for_part = {
                    key.partition(":")[2]
                    for key in connected_numbers
                    if key.startswith(f"{part.ref}:")
                }
                no_connect_for_part = {
                    key.partition(":")[2]
                    for key in no_connect_numbers
                    if key.startswith(f"{part.ref}:")
                }
                if not connected_for_part:
                    unused_components.append(part.ref)
                for number, pin in pin_by_number.items():
                    name = str(pin.get("name", "")).strip().upper()
                    pin_type = str(pin.get("type", "")).lower()
                    library_no_connect = (
                        pin_type == "no_connect"
                        or name in {"NC", "N/C", "DNC", "DO_NOT_CONNECT"}
                    )
                    if (
                        number not in connected_for_part
                        and number not in no_connect_for_part
                        and not library_no_connect
                    ):
                        missing_pin_disposition.append(
                            f"{part.ref}:{number}({name or '~'})"
                        )
                if len(pin_by_number) <= 4:
                    for number in no_connect_for_part:
                        pin = pin_by_number.get(number, {})
                        name = str(pin.get("name", "")).strip().upper()
                        pin_type = str(pin.get("type", "")).lower()
                        if (
                            pin_type != "no_connect"
                            and name not in {"NC", "N/C", "DNC", "DO_NOT_CONNECT"}
                        ):
                            abandoned_small_parts.append(
                                f"{part.ref}:{number}({name or '~'})"
                            )
            checks.append(CheckResult(
                name="selected_components_used",
                ok=not unused_components,
                message=(
                    "selected components absent from every electrical net: "
                    f"{sorted(unused_components)}"
                ),
            ))
            checks.append(CheckResult(
                name="component_pins_accounted",
                ok=not missing_pin_disposition,
                message=(
                    "real pins must be connected or explicitly marked no-connect: "
                    f"{sorted(missing_pin_disposition)}"
                ),
            ))
            checks.append(CheckResult(
                name="small_parts_fully_connected",
                ok=not abandoned_small_parts,
                message=(
                    "selected <=4-pin parts cannot be abandoned with no-connect "
                    f"markers: {sorted(abandoned_small_parts)}"
                ),
            ))
            checks.extend(
                _functional_connection_checks(combined_selection, artifact)
            )
        return checks

    def run(self, state: PipelineState, ctx: PipelineContext) -> StepResult:
        result = super().run(state, ctx)
        if result.blocked:
            return result
        artifact = state.artifact(PipelineStep.SCH_CONNECTIONS)
        selection = state.artifact(PipelineStep.SELECTION)
        if (
            isinstance(artifact, NetlistIntent)
            and artifact.additional_parts
            and isinstance(selection, SelectionPlan)
        ):
            merged_selection = SelectionPlan(
                parts=[*selection.parts, *artifact.additional_parts],
                rationale=selection.rationale,
            )
            state.artifacts[PipelineStep.SELECTION] = merged_selection
            for previous_result in state.results:
                if previous_result.step == PipelineStep.SELECTION:
                    previous_result.summary = SelectionStep().summarize(
                        merged_selection
                    )
                    break
        return result

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, NetlistIntent)
        return (
            f"{len(artifact.nets)} nets, supply={artifact.supply_nets}, "
            f"gnd={artifact.ground_net}, "
            f"additional_parts={len(artifact.additional_parts)}"
        )


def _resolve_logical_pin(pins: list[dict[str, object]], logical: str) -> str | None:
    """Map a logical pin (name or number) to a real device pin number.

    Resolution order: exact number, exact name, name token match (e.g. 'XTAL1'
    inside 'XTAL1/PB6'), then substring. If still unresolved and the name is a
    known power/ground synonym (e.g. 'AGND' on a part with no dedicated analog
    ground), retry with the base name ('GND'). Returns the pin number or None.
    """
    ll = logical.strip().lower()
    if not ll:
        return None

    def _try(term: str) -> str | None:
        for p in pins:  # exact number
            if str(p["number"]).lower() == term:
                return str(p["number"])
        for p in pins:  # exact name
            if str(p["name"]).lower() == term:
                return str(p["number"])
        for p in pins:  # token inside a slash/brace-delimited name
            tokens = re.split(r"[/~{}() ]+", str(p["name"]).lower())
            if term in [t for t in tokens if t]:
                return str(p["number"])
        for p in pins:  # substring fallback
            if term in str(p["name"]).lower():
                return str(p["number"])
        return None

    hit = _try(ll)
    if hit is None and ll in _PIN_SYNONYMS:
        hit = _try(_PIN_SYNONYMS[ll])
    if hit is None and len(pins) == 2 and (ll in _ANODE_TERMS or ll in _CATHODE_TERMS):
        # 2-terminal polarized part (diode/LED/TVS): KiCad names pins K/A (or
        # A1/A2 for a bidirectional TVS), never 'anode'/'cathode'. Map by the
        # A/K name when present, else by pin-number order, keeping the two
        # terminals distinct.
        by_num = sorted(pins, key=lambda p: str(p["number"]))
        if ll in _ANODE_TERMS:
            named = next((p for p in pins if str(p["name"]).upper() == "A"), None)
            return str((named or by_num[-1])["number"])
        named = next((p for p in pins if str(p["name"]).upper() == "K"), None)
        return str((named or by_num[0])["number"])
    if hit is None and len(pins) == 2 and (ll in _FIRST_TERMS or ll in _SECOND_TERMS):
        # Generic 2-terminal part (fuse/ferrite/jumper) with unnamed numbered
        # pins: map input-side terms to pin 1, output-side terms to pin 2.
        by_num = sorted(pins, key=lambda p: str(p["number"]))
        return str((by_num[0] if ll in _FIRST_TERMS else by_num[-1])["number"])
    return hit


# Power/ground synonyms: many parts have no dedicated analog rail pin, so the
# analog name collapses onto its base (AGND->GND, VDDA->AVCC, ...). Used only as
# a last-resort fallback after direct name/number/token matching fails.
_PIN_SYNONYMS = {
    "agnd": "gnd",
    "dgnd": "gnd",
    "pgnd": "gnd",
    "vss": "gnd",
    "gnda": "gnd",
    "avdd": "avcc",
    "vdda": "avcc",
    "dvcc": "vcc",
    "vddio": "vcc",
    "vdd": "vcc",
    # Regulator / supply in-out.
    "vout": "out",
    "vo": "out",
    "vin": "in",
    "vi": "in",
    # MOSFET terminals.
    "drain": "d",
    "gate": "g",
    "source": "s",
    # USB data pair (KiCad names them "D+"/"D-").
    "dplus": "d+",
    "dminus": "d-",
    "dp": "d+",
    "dm": "d-",
    "d_plus": "d+",
    "d_minus": "d-",
    "usbdp": "d+",
    "usbdm": "d-",
}

# Polarity terms for 2-terminal parts, used only as a positional fallback.
_ANODE_TERMS = {"anode", "an", "a", "pos", "positive", "+"}
_CATHODE_TERMS = {"cathode", "cat", "cath", "k", "c", "neg", "negative", "-"}
# Generic 2-terminal in/out terms (fuse, ferrite, jumper, crystal) -> pin1/pin2.
_FIRST_TERMS = {"1", "in", "input", "vin", "vi", "p1", "pri", "primary", "l1",
                "x1", "xtal1", "xin", "osc1"}
_SECOND_TERMS = {"2", "out", "output", "vout", "vo", "p2", "sec", "secondary", "l2",
                 "x2", "xtal2", "xout", "osc2"}


def _library_no_connect(pin: dict[str, object]) -> bool:
    name = str(pin.get("name", "")).strip().upper()
    pin_type = str(pin.get("type", "")).lower()
    return (
        pin_type == "no_connect"
        or name in {"NC", "N/C", "DNC", "DO_NOT_CONNECT"}
    )


# Whole-token names that denote ground. Whole tokens rather than substrings
# because a signal called ``GNDSENSE`` is a measurement of ground, not the ground
# net itself, and classifying it as ground would let a check accept it as the
# return path.
#
# The domain-prefixed and -suffixed variants are here because whole-token
# matching alone missed them: the official ``BeagleBone-Black-Cape`` template
# names its digital ground ``GNDD``, which left ``ground_nets`` empty and made
# ``power_pin_rail_class`` report every ground pin on that board as unconnected
# to ground.
_GROUND_NAME_TOKENS = (
    "GND",
    "GNDA",
    "GNDD",
    "GNDPWR",
    "AGND",
    "DGND",
    "PGND",
    "EGND",
    "GROUND",
    "VSS",
    "VSSA",
    "VEE",
    "EARTH",
)


def _looks_like_ground(net: str) -> bool:
    """Whether a net NAME denotes ground.

    Used only when reading an existing schematic, where nothing declares intent.
    Matching is on whole tokens so a signal called ``GNDSENSE`` is not mistaken
    for the ground net itself, and unrecognised names are simply not classified —
    the checks that consume this treat an unclassified net as "not known to be
    ground" rather than as "known not to be ground".
    """
    upper = (net or "").upper().strip("+-")
    if not upper:
        return False
    tokens = {t for t in re.split(r"[^A-Z0-9]+", upper) if t}
    if tokens & set(_GROUND_NAME_TOKENS):
        return True
    # A trailing index names one instance of a rail, not a different signal:
    # ``GND1`` and ``VSS2`` are the same net class as ``GND`` and ``VSS``.
    # Stripping only digits keeps ``GNDSENSE`` out.
    return any(
        re.sub(r"\d+$", "", t) in set(_GROUND_NAME_TOKENS) for t in tokens
    )


def _looks_like_supply(net: str) -> bool:
    """Whether a net NAME denotes a positive supply rail.

    Recognises the conventions this codebase already parses elsewhere: ``3V3``,
    ``+3.3V``, ``5V``, ``VCC``, ``VDD``, ``VBUS``, ``VBAT``, ``VIN``.
    """
    upper = (net or "").upper().strip("+-")
    if not upper or _looks_like_ground(upper):
        return False
    tokens = {t for t in re.split(r"[^A-Z0-9.]+", upper) if t}
    if tokens & {"VCC", "VDD", "VBUS", "VBAT", "VIN", "VDDA", "VCCIO", "AVDD"}:
        return True
    # 3V3 / 5V / 1V8 / 3.3V shapes.
    return any(re.fullmatch(r"\d{1,2}(?:V\d{0,2}|\.\d+V)", t) for t in tokens)


@dataclass
class _ConnectivityView:
    """Resolved physical-pin view used by role-based topology checks."""

    parts: dict[str, SelectedPart]
    pins: dict[str, list[dict[str, object]]]
    pin_nets: dict[tuple[str, str], str]
    no_connect: set[tuple[str, str]]
    ground_nets: set[str]
    supply_nets: set[str]

    @classmethod
    def build(
        cls,
        selection: SelectionPlan,
        intent: NetlistIntent,
    ) -> _ConnectivityView:
        parts = {part.ref: part for part in selection.parts}
        pins = {
            ref: symbols.symbol_pins(part.symbol) or []
            for ref, part in parts.items()
        }
        pin_nets: dict[tuple[str, str], str] = {}
        for net in intent.nets:
            for logical in net.pins:
                number = _resolve_logical_pin(
                    pins.get(logical.ref, []),
                    logical.pin,
                )
                if number is not None:
                    pin_nets[(logical.ref, number)] = net.name
        no_connect: set[tuple[str, str]] = set()
        for logical in intent.no_connect_pins:
            number = _resolve_logical_pin(
                pins.get(logical.ref, []),
                logical.pin,
            )
            if number is not None:
                no_connect.add((logical.ref, number))
        ground_nets = {
            intent.ground_net,
            *(net.name for net in intent.nets if net.kind == "ground"),
        }
        supply_nets = {
            *intent.supply_nets,
            *(net.name for net in intent.nets if net.kind == "power"),
        } - ground_nets
        return cls(
            parts=parts,
            pins=pins,
            pin_nets=pin_nets,
            no_connect=no_connect,
            ground_nets=ground_nets,
            supply_nets=supply_nets,
        )

    @classmethod
    def from_schematic(
        cls,
        path: str | Path,
        *,
        cli_path: str | None = None,
    ) -> _ConnectivityView:
        """Build the same view from a real ``.kicad_sch`` instead of a proposal.

        This is what lets one check implementation serve two very different
        inputs: the pipeline's own netlist intent, and any KiCad schematic on
        disk. A corpus of correct boards is the only affordable way to show a new
        check does not fire on good designs, and generating correct boards is not
        affordable, so the checks have to be able to read files.

        Connectivity comes from ``kicad-cli sch export netlist``; see
        :mod:`ratsnestpro.eda.netlist` for why nothing here recomputes it, and
        note that a hierarchical project is resolved in that one call. Raises
        :class:`~ratsnestpro.eda.netlist.NetlistError` when the export is
        unavailable rather than returning a partial view: a view missing
        connections makes every connection check pass, which reads as "this
        design is fine".

        Power and ground flags need no filtering here. KiCad does not emit
        ``#PWR`` / ``#FLG`` symbols as components -- it resolves them into the
        net names they stand for, which is the form these checks want anyway.
        Filtering them out of a raw sheet parse is harder than it looks: the
        stock library is ``power:GND``, but a project shipping its own copy
        names it anything it likes -- the demo corpus uses ``antmicropower:GND``
        -- so only the reference prefix identifies them reliably.

        One field cannot come from a file and is deliberately left empty:

        ``role``
            Free text an LLM assigned to express what a part is *for*. A file
            records what a part *is*. Checks that filter on ``role`` therefore
            find no candidates here and stay silent — which is the correct
            fail-open outcome, and a reminder that a check keyed on ``role``
            can never be validated against real designs. Prefer keying on fact
            sheets or symbol identity.

        ``no_connect`` *is* populated, which reading the file alone could not
        justify: KiCad stores a no-connect as a coordinate marker, and a wrong
        pin attribution would silently suppress a real dangling-pin finding. The
        netlister performs that attribution itself and reports it per node, so
        the claim is KiCad's rather than a guess.

        Supply and ground nets are inferred from net names, because a file
        declares no intent. The inference is deliberately narrow: an unrecognised
        name is simply not classified.
        """
        from ratsnestpro.eda.netlist import netlist_for_schematic

        netlist = netlist_for_schematic(path, cli_path=cli_path)
        parts = {
            ref: SelectedPart(
                ref=ref,
                symbol=comp.lib_id,
                value=comp.value,
                footprint=comp.footprint,
                role="",
            )
            for ref, comp in netlist.components.items()
        }
        net_names = netlist.net_names
        ground_nets = {n for n in net_names if _looks_like_ground(n)}
        supply_nets = {n for n in net_names if _looks_like_supply(n)} - ground_nets
        return cls(
            parts=parts,
            pins={ref: list(netlist.pins.get(ref, [])) for ref in parts},
            pin_nets=dict(netlist.pin_nets),
            no_connect=set(netlist.no_connect),
            ground_nets=ground_nets,
            supply_nets=supply_nets,
        )

    def part_nets(self, part: SelectedPart) -> set[str]:
        return {
            net
            for (ref, _number), net in self.pin_nets.items()
            if ref == part.ref
        }

    def named_pin_net(
        self,
        part: SelectedPart,
        *names: str,
    ) -> str | None:
        for name in names:
            number = _resolve_logical_pin(self.pins.get(part.ref, []), name)
            if number is not None:
                net = self.pin_nets.get((part.ref, number))
                if net is not None:
                    return net
        return None

    def net_has_mcu_pin(self, net: str, *tokens: str) -> bool:
        wanted = tuple(token.upper() for token in tokens)
        for ref, part in self.parts.items():
            if "mcu" not in part.role.lower():
                continue
            for pin in self.pins.get(ref, []):
                number = str(pin.get("number", ""))
                if self.pin_nets.get((ref, number)) != net:
                    continue
                name = str(pin.get("name", "")).upper()
                if any(token in name for token in wanted):
                    return True
        return False

    def net_has_any_mcu_pin(self, net: str) -> bool:
        return any(
            self.pin_nets.get((ref, str(pin.get("number", "")))) == net
            for ref, part in self.parts.items()
            if "mcu" in part.role.lower()
            for pin in self.pins.get(ref, [])
        )


def _role_parts(
    view: _ConnectivityView,
    *tokens: str,
) -> list[SelectedPart]:
    lowered = tuple(token.lower() for token in tokens)
    return [
        part
        for part in view.parts.values()
        if all(token in part.role.lower() for token in lowered)
    ]


def _two_terminal_grounded(
    view: _ConnectivityView,
    part: SelectedPart | None,
    signal_net: str,
) -> bool:
    if part is None:
        return False
    nets = view.part_nets(part)
    return (
        len(nets) == 2
        and signal_net in nets
        and bool(nets & view.ground_nets)
    )


def _critical_function_pin_checks(
    view: _ConnectivityView,
) -> list[CheckResult]:
    role_tokens = (
        "buck",
        "regulator",
        "ldo",
        "power_mux",
        "power_path",
        "ideal_diode",
        "reverse_blocking",
        "flash",
        "can_transceiver",
    )
    abandoned_hard: list[str] = []
    abandoned_advisory: list[str] = []
    for part in view.parts.values():
        role = part.role.lower()
        if not any(token in role for token in role_tokens):
            continue
        for pin in view.pins.get(part.ref, []):
            number = str(pin.get("number", ""))
            if (
                number
                and not _library_no_connect(pin)
                and (part.ref, number) in view.no_connect
            ):
                item = f"{part.ref}:{number}({pin.get('name') or '~'})"
                pin_type = str(pin.get("type", "")).lower()
                pin_name = str(pin.get("name", "")).upper()
                if (
                    pin_type in {"power_in", "power_out"}
                    or any(
                        token in pin_name
                        for token in ("BOOT", "RESET", "NRST", "VCAP")
                    )
                ):
                    abandoned_hard.append(item)
                else:
                    abandoned_advisory.append(item)
    return [
        CheckResult(
            name="critical_power_reset_pins_connected",
            ok=not abandoned_hard,
            message=(
                "power, reset, boot, and internal-regulator capacitor pins "
                f"cannot be abandoned as no-connect: {sorted(abandoned_hard)}"
            ),
        ),
        CheckResult(
            name="functional_control_pins_reviewed",
            ok=not abandoned_advisory,
            severity=Severity.WARNING,
            message=(
                "functional IC control pins were explicitly left open; confirm "
                "each choice against the selected device datasheet during review: "
                f"{sorted(abandoned_advisory)}"
            ),
        ),
    ]


def _power_pin_rail_checks(view: _ConnectivityView) -> list[CheckResult]:
    """Keep real ground and positive-supply pins on the correct rail class."""
    misplaced: list[str] = []
    for ref, pins in view.pins.items():
        for pin in pins:
            number = str(pin.get("number", ""))
            if not number or _library_no_connect(pin):
                continue
            name = re.sub(
                r"[^A-Z0-9_+-]",
                "",
                str(pin.get("name", "")).upper(),
            )
            net = view.pin_nets.get((ref, number))
            is_ground = name.startswith(("GND", "AGND", "PGND", "VSS"))
            is_positive_supply = name.startswith(
                ("VDD", "VDDA", "VCC", "AVCC", "VBAT")
            )
            if is_ground and net not in view.ground_nets:
                misplaced.append(f"{ref}:{number}({name})->{net or 'unconnected'}")
            elif is_positive_supply and net not in view.supply_nets:
                misplaced.append(f"{ref}:{number}({name})->{net or 'unconnected'}")
    return [
        CheckResult(
            name="power_pin_rail_class",
            ok=not misplaced,
            message=(
                "real ground pins must join ground_net and positive supply pins "
                f"must join a declared supply rail: {sorted(misplaced)}"
            ),
        )
    ]


def _crystal_topology_checks(view: _ConnectivityView) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for part in view.parts.values():
        role = part.role.lower()
        if (
            "crystal" not in role
            or not (
                part.ref.upper().startswith("Y")
                or "crystal" in part.symbol.lower()
            )
        ):
            continue
        real_pins = [
            pin for pin in view.pins.get(part.ref, [])
            if pin.get("number") and not _library_no_connect(pin)
        ]
        if len(real_pins) != 2:
            continue
        nets = [
            view.pin_nets.get((part.ref, str(pin["number"])))
            for pin in real_pins
        ]
        ok = (
            None not in nets
            and len(set(nets)) == 2
            and not (set(nets) & view.ground_nets)
            and not (set(nets) & view.supply_nets)
        )
        checks.append(CheckResult(
            name=f"crystal_two_distinct_signal_nets:{part.ref}",
            ok=ok,
            message=(
                f"{part.ref} crystal terminals must connect the two distinct MCU "
                f"oscillator nets, never GND/supply or one shared net; got {nets}"
            ),
        ))
    return checks


def _led_series_checks(view: _ConnectivityView) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for led in view.parts.values():
        role = led.role.lower()
        if (
            "led" not in role
            or "current" in role
            or "limit" in role
            or led.ref.upper().startswith("R")
        ):
            continue
        channel = next(
            (token for token in ("power", "system", "status", "user")
             if token in role),
            None,
        )
        if channel is None:
            continue
        # ``role`` is free text chosen by the model: a correct series resistor
        # has arrived as led_series_resistor, led_current_limit_red and
        # led_power_current_limit. Requiring the words "current"/"limit" here
        # emptied the candidate list for the first spelling and failed the check
        # on a circuit that was wired correctly, so identity is decided by the
        # series topology below rather than by the wording of the role.
        resistors = [
            part
            for part in view.parts.values()
            if part.ref.upper().startswith("R")
            and "led" in part.role.lower()
        ]
        led_nets = view.part_nets(led)
        valid = [
            resistor.ref
            for resistor in resistors
            if (
                len(led_nets) == 2
                and len(view.part_nets(resistor)) == 2
                and len(led_nets & view.part_nets(resistor)) == 1
                and not (
                    (led_nets & view.part_nets(resistor))
                    & (view.ground_nets | view.supply_nets)
                )
                and len(led_nets | view.part_nets(resistor)) == 3
            )
        ]
        checks.append(CheckResult(
            name=f"led_current_limit_in_series:{led.ref}",
            ok=bool(valid),
            message=(
                f"{led.ref} must be in series with its channel current-limit "
                f"resistor, not wired in parallel; LED nets={sorted(led_nets)}, "
                f"candidate resistors={[p.ref for p in resistors]}"
            ),
        ))
    return checks


def _two_terminal_short_checks(view: _ConnectivityView) -> list[CheckResult]:
    """A two-pin device must not have both terminals on one net.

    Such a part is electrically a plain wire: populated, paid for, assembled,
    and doing nothing. Real runs produced a decoupling capacitor with both pads
    on VDD (no fourth supply pin was left to attach it to) and a polyfuse with
    both terminals on the 5 V input, silently removing the overcurrent
    protection. KiCad ERC does not report this: no net is shorted to another
    net, only the component is defeated.
    """
    checks: list[CheckResult] = []
    for part in view.parts.values():
        numbers = [
            str(pin["number"])
            for pin in view.pins.get(part.ref) or []
            if pin.get("number")
        ]
        if len(numbers) != 2:
            continue
        wired = [n for n in numbers if (part.ref, n) in view.pin_nets]
        # Decide only once both terminals are wired; a half-connected part is
        # reported by the dangling-pin checks, not here.
        if len(wired) < 2:
            continue
        nets = {view.pin_nets[(part.ref, n)] for n in wired}
        checks.append(CheckResult(
            name=f"two_terminal_not_shorted:{part.ref}",
            ok=len(nets) == 2,
            message=(
                f"{part.ref} ({part.value}) has both terminals on "
                f"{sorted(nets)}; a two-pin device bridged by a single net is a "
                "wire, not a component"
            ),
            targets=[part.ref, *sorted(nets)],
        ))
    return checks


_MECHANICAL_ROLE_TOKENS = (
    "mount",
    "mechanical",
    "fiducial",
    "standoff",
    "testpoint",
)


def _mechanical_part_checks(view: _ConnectivityView) -> list[CheckResult]:
    """A mechanical part must not be a multi-pin electrical device.

    Mounting holes have arrived as a relabelled 6-pin oscillator symbol
    (``Oscillator:Si512A_2.5x3.2mm`` carrying the value ``MountingHole_M2``)
    whose pins were then wired to the supply rails, so the BOM ordered four
    oscillators. Both the role and the value said "mounting hole"; only the
    grounded symbol showed what was really selected. Identity is therefore
    decided here from the symbol's real pin set rather than from model-authored
    text, which is also why this does not live in the topology-coverage check:
    that one legitimately accepts ``role`` as semantic evidence.
    """
    checks: list[CheckResult] = []
    for part in view.parts.values():
        role = part.role.lower()
        if not any(token in role for token in _MECHANICAL_ROLE_TOKENS):
            continue
        numbers = [
            str(pin["number"])
            for pin in view.pins.get(part.ref) or []
            if pin.get("number")
        ]
        nets = {
            view.pin_nets[(part.ref, n)]
            for n in numbers
            if (part.ref, n) in view.pin_nets
        }
        # A real mounting hole is pinless, or exposes one optional pad that may
        # be stitched to ground. Anything else is an electrical device wearing a
        # mechanical label.
        non_ground = sorted(nets - view.ground_nets)
        checks.append(CheckResult(
            name=f"mechanical_part_not_electrical:{part.ref}",
            ok=len(numbers) <= 1 and not non_ground,
            message=(
                f"{part.ref} has mechanical role {part.role!r} but symbol "
                f"{part.symbol!r} exposes {len(numbers)} pins"
                + (f" wired to {non_ground}" if non_ground else "")
                + "; select a real mounting-hole symbol instead of relabelling "
                "an electrical device"
            ),
        ))
    return checks


def _swd_topology_checks(view: _ConnectivityView) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for connector in _role_parts(view, "swd"):
        pins = view.pins.get(connector.ref, [])
        if len({str(pin.get("number", "")) for pin in pins}) < 10:
            continue
        pin_net = {
            number: view.pin_nets.get((connector.ref, number))
            for number in map(str, range(1, 11))
        }
        failures: list[str] = []
        if pin_net["1"] not in view.supply_nets:
            failures.append(f"pin1 VTref -> supply, got {pin_net['1']}")
        for number in ("3", "5", "9"):
            if pin_net[number] not in view.ground_nets:
                failures.append(
                    f"pin{number} GND/GNDDetect -> ground, got {pin_net[number]}"
                )
        expected_mcu = {
            "2": ("SWDIO", "JTMS"),
            "4": ("SWCLK", "JTCK"),
            "10": ("NRST",),
        }
        for number, tokens in expected_mcu.items():
            net = pin_net[number]
            normalized_net = re.sub(r"[^A-Z0-9]", "", (net or "").upper())
            semantically_named = any(
                re.sub(r"[^A-Z0-9]", "", token.upper()) in normalized_net
                for token in tokens
            )
            grounded_to_mcu = (
                net is not None
                and (
                    view.net_has_mcu_pin(net, *tokens)
                    or (
                        semantically_named
                        and view.net_has_any_mcu_pin(net)
                    )
                )
            )
            if not grounded_to_mcu:
                failures.append(
                    f"pin{number} -> MCU {'/'.join(tokens)}, got {net}"
                )
        pin6_net = pin_net["6"]
        pin6_nc = (connector.ref, "6") in view.no_connect
        if pin6_net is not None and not (
            view.net_has_mcu_pin(pin6_net, "SWO", "JTDO")
            or (
                any(token in re.sub(r"[^A-Z0-9]", "", pin6_net.upper())
                    for token in ("SWO", "JTDO"))
                and view.net_has_any_mcu_pin(pin6_net)
            )
        ):
            failures.append(f"pin6 SWO or NC, got {pin6_net}")
        elif pin6_net is None and not pin6_nc:
            failures.append("pin6 SWO or explicit NC is not accounted")
        for number in ("7", "8"):
            if pin_net[number] is not None:
                failures.append(
                    f"pin{number} reserved/key must be NC, got {pin_net[number]}"
                )
            elif (connector.ref, number) not in view.no_connect:
                failures.append(f"pin{number} reserved/key lacks explicit NC")
        checks.append(CheckResult(
            name=f"cortex_swd_10pin_mapping:{connector.ref}",
            ok=not failures,
            message=(
                f"{connector.ref} must follow the standard 10-pin Cortex SWD "
                f"mapping; errors: {failures}"
            ),
        ))
    return checks


def _can_topology_checks(view: _ConnectivityView) -> list[CheckResult]:
    checks: list[CheckResult] = []
    termination = _role_parts(view, "can", "termination")
    chain_ok = False
    chain_detail: list[str] = []
    for index, first in enumerate(termination):
        first_nets = view.part_nets(first)
        for second in termination[index + 1:]:
            second_nets = view.part_nets(second)
            shared = first_nets & second_nets
            endpoints = (first_nets | second_nets) - shared
            normalized = {
                net: re.sub(r"[^a-z0-9]", "", net.lower())
                for net in endpoints
            }
            has_h = any("canh" in name for name in normalized.values())
            has_l = any("canl" in name for name in normalized.values())
            valid = (
                len(first_nets) == 2
                and len(second_nets) == 2
                and len(shared) == 1
                and len(endpoints) == 2
                and not (endpoints & view.ground_nets)
                and has_h
                and has_l
            )
            chain_detail.append(
                f"{first.ref}{sorted(first_nets)} + "
                f"{second.ref}{sorted(second_nets)}"
            )
            chain_ok = chain_ok or valid
    if termination:
        checks.append(CheckResult(
            name="can_selectable_termination_across_pair",
            ok=chain_ok,
            message=(
                "the termination resistor and jumper must form a selectable "
                "series path between CANH and CANL, never to GND; "
                f"candidates={chain_detail}"
            ),
        ))

    tvs_parts = [
        part for part in view.parts.values()
        if "can" in part.role.lower()
        and any(token in part.role.lower() for token in ("tvs", "esd"))
    ]
    if tvs_parts:
        covered: set[str] = set()
        details: list[str] = []
        for part in tvs_parts:
            nets = view.part_nets(part)
            details.append(f"{part.ref}{sorted(nets)}")
            if not (nets & view.ground_nets):
                continue
            for net in nets - view.ground_nets:
                normalized = re.sub(r"[^a-z0-9]", "", net.lower())
                if "canh" in normalized:
                    covered.add("CANH")
                if "canl" in normalized:
                    covered.add("CANL")
        checks.append(CheckResult(
            name="can_tvs_connected_to_both_lines",
            ok=covered == {"CANH", "CANL"},
            message=(
                "real grounded TVS channels must protect both CANH and CANL; "
                f"covered={sorted(covered)}, connections={details}"
            ),
        ))
    return checks


def _analog_input_topology_checks(
    view: _ConnectivityView,
) -> list[CheckResult]:
    checks: list[CheckResult] = []

    def role_channel(role: str) -> int | None:
        lowered = role.lower()
        match = re.search(r"analog_input_(\d+)(?:_|$)", lowered)
        if match is None:
            match = re.search(r"analog_input_.*_(\d+)$", lowered)
        return int(match.group(1)) if match is not None else None

    channels = sorted({
        channel
        for part in view.parts.values()
        for channel in [role_channel(part.role)]
        if channel is not None
    })
    for channel in channels:
        def belongs(
            part: SelectedPart,
            channel_number: int = channel,
        ) -> bool:
            return role_channel(part.role) == channel_number

        def one(*suffixes: str) -> SelectedPart | None:
            return next(
                (
                    part for part in view.parts.values()
                    if belongs(part)
                    and any(
                        suffix in part.role.lower()
                        for suffix in suffixes
                    )
                ),
                None,
            )

        top = one("divider_top", "divider_upper")
        bottom = one("divider_bottom", "divider_lower")
        current = one("current_limit")
        filter_cap = one("filter_cap", "filtering_cap")
        protection = next(
            (
                part for part in view.parts.values()
                if belongs(part)
                and any(
                    token in part.role.lower()
                    for token in ("tvs", "overvoltage", "clamp", "protection")
                )
            ),
            None,
        )
        present = [top, bottom, current, filter_cap, protection]
        if not any(present):
            continue
        missing = [
            name
            for name, part in zip(
                ("divider_top", "divider_bottom", "current_limit",
                 "filter_cap", "overvoltage_protection"),
                present,
                strict=True,
            )
            if part is None
        ]
        ok = not missing
        details: list[str] = []
        if ok:
            assert top is not None
            assert bottom is not None
            assert current is not None
            assert filter_cap is not None
            assert protection is not None
            top_nets = view.part_nets(top)
            bottom_nets = view.part_nets(bottom)
            current_nets = view.part_nets(current)
            filter_nets = view.part_nets(filter_cap)
            protection_nets = view.part_nets(protection)
            divider_candidates = bottom_nets - view.ground_nets
            divider = next(iter(divider_candidates), None)
            series_shared = top_nets & current_nets
            series_endpoints = (top_nets | current_nets) - series_shared
            sense_candidates = [
                net for net in series_endpoints
                if view.net_has_any_mcu_pin(net)
            ]
            sense = (
                sense_candidates[0]
                if len(sense_candidates) == 1
                else None
            )
            ok = (
                len(bottom_nets) == 2
                and bool(bottom_nets & view.ground_nets)
                and len(divider_candidates) == 1
                and len(filter_nets) == 2
                and sense in filter_nets
                and bool(filter_nets & view.ground_nets)
                and len(top_nets) == 2
                and len(current_nets) == 2
                and not ((top_nets | current_nets) & view.ground_nets)
                and series_shared == {divider}
                and len(series_endpoints) == 2
                and sense in series_endpoints
                and len(protection_nets) == 2
                and bool(protection_nets & view.ground_nets)
                and len(protection_nets - view.ground_nets) == 1
                and bool(
                    (protection_nets - view.ground_nets)
                    <= {divider, sense}
                )
                and sense is not None
            )
            details = [
                f"{top.ref}={sorted(top_nets)}",
                f"{current.ref}={sorted(current_nets)}",
                f"{bottom.ref}={sorted(bottom_nets)}",
                f"{filter_cap.ref}={sorted(filter_nets)}",
                f"{protection.ref}={sorted(protection_nets)}",
                f"divider={divider}",
                f"sense={sense}",
            ]
        checks.append(CheckResult(
            name=f"analog_input_safe_chain:{channel}",
            ok=ok,
            message=(
                f"analog channel {channel} requires a series current-limit/divider "
                "path ending at one MCU ADC sense node, with divider-bottom, RC "
                "capacitor, and overvoltage protection returned to GND; "
                f"missing={missing}, topology={details}"
            ),
        ))
    return checks


def _passive_edges(
    view: _ConnectivityView,
) -> list[tuple[SelectedPart, set[str]]]:
    return [
        (part, view.part_nets(part))
        for part in view.parts.values()
        if part.ref.upper().startswith(("R", "C"))
        and len(view.part_nets(part)) == 2
    ]


def _passive_path_to_ground(
    view: _ConnectivityView,
    start: str | None,
    max_edges: int,
) -> bool:
    if start is None:
        return False
    frontier = {start}
    visited = {start}
    for _ in range(max_edges):
        following: set[str] = set()
        for _part, nets in _passive_edges(view):
            if not (nets & frontier):
                continue
            following.update(nets - visited)
        if following & view.ground_nets:
            return True
        visited.update(following)
        frontier = following
    return False


def _buck_topology_checks(view: _ConnectivityView) -> list[CheckResult]:
    checks: list[CheckResult] = []
    converters = [
        part for part in view.parts.values()
        if "buck" in part.role.lower()
        and any(token in part.role.lower() for token in ("converter", "regulator"))
    ]
    inductors = _role_parts(view, "buck", "inductor")
    for converter in converters:
        vin = view.named_pin_net(converter, "VIN")
        switch = view.named_pin_net(converter, "SW")
        boot = view.named_pin_net(converter, "BOOT")
        feedback = view.named_pin_net(converter, "FB")
        timing = view.named_pin_net(converter, "RT/CLK", "RT")
        compensation = view.named_pin_net(converter, "COMP")
        output: str | None = None
        inductor_detail: list[str] = []
        for inductor in inductors:
            nets = view.part_nets(inductor)
            inductor_detail.append(f"{inductor.ref}={sorted(nets)}")
            if len(nets) == 2 and switch in nets:
                output = next(iter(nets - {switch}))
                break

        output_caps = _role_parts(view, "buck", "output", "capacitor")
        input_caps = [
            part for part in view.parts.values()
            if "buck" in part.role.lower()
            and "input" in part.role.lower()
            and "capacitor" in part.role.lower()
        ]
        boot_cap = any(
            nets == {boot, switch}
            for part, nets in _passive_edges(view)
            if part.ref.upper().startswith("C")
        )
        feedback_high = any(
            nets == {output, feedback}
            for part, nets in _passive_edges(view)
            if part.ref.upper().startswith("R")
        )
        feedback_low = any(
            feedback in nets and bool(nets & view.ground_nets)
            for part, nets in _passive_edges(view)
            if part.ref.upper().startswith("R")
        )
        timing_grounded = any(
            timing in nets and bool(nets & view.ground_nets)
            for part, nets in _passive_edges(view)
            if part.ref.upper().startswith("R")
        )
        failures: list[str] = []
        if (
            output is None
            or output in view.ground_nets
            or output == switch
        ):
            failures.append(
                f"SW must feed a two-terminal buck inductor and a distinct "
                f"non-ground output; SW={switch}, inductors={inductor_detail}"
            )
        if not any(
            _two_terminal_grounded(view, cap, output or "")
            for cap in output_caps
        ):
            failures.append(
                f"buck output capacitor must connect {output} to ground"
            )
        if not any(
            _two_terminal_grounded(view, cap, vin or "")
            for cap in input_caps
        ):
            failures.append(f"buck input capacitor must connect {vin} to ground")
        if not boot_cap:
            failures.append(
                f"bootstrap capacitor must connect BOOT={boot} to SW={switch}"
            )
        if not (feedback_high and feedback_low):
            failures.append(
                f"feedback divider must connect output={output} -> FB={feedback} "
                "-> ground"
            )
        if not timing_grounded:
            failures.append(
                f"timing resistor must connect RT/CLK={timing} to ground"
            )
        if not _passive_path_to_ground(view, compensation, max_edges=2):
            failures.append(
                f"COMP={compensation} needs a grounded compensation network"
            )
        checks.append(CheckResult(
            name=f"buck_reference_topology:{converter.ref}",
            ok=not failures,
            severity=Severity.WARNING,
            message=(
                f"{converter.ref} appears to omit or miswire datasheet support "
                "networks. This heuristic is advisory because valid compensation "
                f"topologies vary by exact device; review findings: {failures}"
            ),
        ))
    return checks


def _power_mux_topology_checks(view: _ConnectivityView) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for mux in (
        part for part in _role_parts(view, "power_mux")
        if part.ref.upper().startswith("U")
        and not any(
            token in part.role.lower()
            for token in ("decoupling", "capacitor", "resistor")
        )
    ):
        vin1 = view.named_pin_net(mux, "VIN1")
        vin2 = view.named_pin_net(mux, "VIN2")
        ground = view.named_pin_net(mux, "GND")
        vout_numbers = [
            str(pin.get("number", ""))
            for pin in view.pins.get(mux.ref, [])
            if str(pin.get("name", "")).upper() == "VOUT"
        ]
        vout_nets = {
            view.pin_nets.get((mux.ref, number))
            for number in vout_numbers
        }
        failures: list[str] = []
        if (
            vin1 is None
            or vin2 is None
            or vin1 == vin2
            or vin1 in view.ground_nets
            or vin2 in view.ground_nets
        ):
            failures.append(f"VIN1/VIN2 must be distinct sources, got {vin1}/{vin2}")
        if ground not in view.ground_nets:
            failures.append(f"GND pin must be grounded, got {ground}")
        if None in vout_nets or len(vout_nets) != 1:
            failures.append(f"all VOUT pins must share one rail, got {vout_nets}")
        output = next(iter(vout_nets), None)
        if output in {vin1, vin2}:
            failures.append(
                f"VOUT rail must be distinct from both input rails, got {output}"
            )
        checks.append(CheckResult(
            name=f"power_mux_distinct_inputs_output:{mux.ref}",
            ok=not failures,
            message=f"{mux.ref} source-priority topology errors: {failures}",
        ))
    return checks


def _datasheet_connection_checks(
    selection: SelectionPlan,
    intent: NetlistIntent,
) -> list[CheckResult]:
    """Datasheet limits that only become checkable once connectivity exists.

    The selection step already gates what it can see from parts and rails alone
    (supply range, speed grade, a regulator's input voltage). Three slots cannot
    be judged there because they need to know WHICH node a component sits on:

    * ``required_cin`` / ``required_cout`` - a capacitor is only an input or an
      output capacitor relative to a regulator's terminals, and the datasheet
      minimum applies to the node, so parallel parts must be summed. Before the
      netlist exists there is nothing to attribute, and guessing would be worse
      than silence.
    * ``cc_pulldown_ohm`` - Rd is identified by sharing a CC net with the
      receptacle, not by its value.

    The same call also re-runs the parametric gates with better information:
    ``upstream_v`` becomes the voltage of a regulator's actual input net rather
    than the worst-case highest rail on the board, and a TVS is judged against
    the rail it is really wired to.

    Findings carry the severity their slot's consequence implies and cite the
    page-level source of the fact they violate.
    """
    findings = factgate.gate_findings(selection.parts, netlist=intent)
    checks: list[CheckResult] = []
    for finding in findings:
        checks.append(CheckResult(
            name=f"datasheet_connection:{finding.ref}:{finding.slot}",
            ok=False,
            severity=finding.severity,
            message=finding.as_text(),
            targets=list(finding.all_targets()),
        ))
    if not checks:
        checks.append(CheckResult(
            name="datasheet_connection",
            ok=True,
            message=(
                "connected values satisfy the datasheet minimums for every part "
                "with a fact sheet"
            ),
        ))
    return checks


def _crystal_channel_checks(view: _ConnectivityView) -> list[CheckResult]:
    """A crystal must sit on the oscillator channel rated for its frequency.

    Delegates to :func:`ratsnestpro.eda.factgate.crystal_channel_conflicts`,
    which holds the reasoning. Classed as a pin conflict rather than a wiring
    error because the nets are right and the pins are not — in the run this was
    found in, the nets were even named ``HSE_OSC_IN`` / ``HSE_OSC_OUT``.

    Nothing is emitted when the check does not apply: the symbol must declare
    alternate functions, the MCU must have a fact sheet, and the crystal's value
    must carry a frequency.
    """
    findings = factgate.crystal_channel_conflicts(
        list(view.parts.values()),
        pin_nets=view.pin_nets,
    )
    return [
        CheckResult(
            name=f"crystal_on_rated_oscillator_channel:{finding.ref}",
            ok=False,
            severity=finding.severity,
            message=finding.as_text(),
            targets=list(finding.all_targets()),
        )
        for finding in findings
    ]


def _mcu_supply_source_checks(view: _ConnectivityView) -> list[CheckResult]:
    """A supply pin must not sit on the net that feeds its own regulator.

    Delegates to :func:`ratsnestpro.eda.factgate.supply_pin_conflicts`, which
    holds the reasoning and the datasheet citation. Two things are worth knowing
    at this level.

    The identities come from fact sheets, not from ``role``: this fires on a real
    STM32 wired to a real AMS1117 whatever the model called them, and stays
    silent on a part it has no datasheet for.

    Nothing is emitted when the check does not apply. There is no summarising
    "supply pins are fine" result, because the preconditions are narrow - both
    devices need a fact sheet, the regulator needs an unambiguous output pin, and
    that output has to actually feed this device - and a pass would report "not
    determinable" as "determined to be correct".
    """
    findings = factgate.supply_pin_conflicts(
        list(view.parts.values()),
        pin_nets=view.pin_nets,
        pins=view.pins,
    )
    return [
        CheckResult(
            name=f"supply_pin_not_on_regulator_input:{finding.ref}",
            ok=False,
            severity=finding.severity,
            message=finding.as_text(),
            targets=list(finding.all_targets()),
        )
        for finding in findings
    ]


def _functional_connection_checks(
    selection: SelectionPlan,
    intent: NetlistIntent,
) -> list[CheckResult]:
    """Validate safety-critical topology after all logical pins resolve."""
    view = _ConnectivityView.build(selection, intent)
    checks: list[CheckResult] = []
    checks.extend(_power_pin_rail_checks(view))
    checks.extend(_critical_function_pin_checks(view))
    checks.extend(_crystal_topology_checks(view))
    checks.extend(_led_series_checks(view))
    checks.extend(_two_terminal_short_checks(view))
    checks.extend(_mechanical_part_checks(view))
    checks.extend(_swd_topology_checks(view))
    checks.extend(_can_topology_checks(view))
    checks.extend(_analog_input_topology_checks(view))
    checks.extend(_buck_topology_checks(view))
    checks.extend(_power_mux_topology_checks(view))
    checks.extend(_mcu_supply_source_checks(view))
    checks.extend(_crystal_channel_checks(view))
    checks.extend(_datasheet_connection_checks(selection, intent))
    return checks


_POWER_PIN_NAMES = ("VCC", "VDD", "AVCC", "VBAT", "VIN", "GND", "VSS", "AGND")


# Pin identifiers a requirement can name. Port-bit form covers ST/GD/NXP
# ("PC13", "PA0"), bare GPIO form covers Espressif/Raspberry ("GPIO2", "GP15").
# Deliberately narrow: an identifier this does not recognise is simply not
# demanded, which is safer than demanding one that does not exist.
_REQUESTED_PIN_RE = re.compile(
    r"\b(?:P[A-L]\d{1,2}|GPIO\d{1,2}|GP\d{1,2})\b",
    re.IGNORECASE,
)


def _requested_pin_names(requirement: str, constraints: Sequence[str]) -> set[str]:
    """Pin identifiers the requirement names, from its prose and its constraints.

    Both are read because the two carry different things: a constraint is where a
    model was asked to restate a hard requirement, and the prose is where the user
    actually wrote it. Reading only the constraints would make the check depend on
    the model having restated the pin, which is exactly the dependency that made
    ``led_current_limit_in_series`` unfalsifiable.
    """
    text = " ".join([_original_requirement(requirement), *constraints])
    return {match.group(0).upper() for match in _REQUESTED_PIN_RE.finditer(text)}


def _requested_pin_checks(
    state: PipelineState,
    artifact: PinMapPlan,
) -> list[CheckResult]:
    """A pin the requirement names must appear in the final pin map.

    The defect this exists for
    --------------------------
    A requirement said the status LED is on ``PC13``. The finished board never
    mentioned ``PC13`` at all: the LED sat between 3V3 and ground through a
    resistor, permanently lit and not under software control. Every check passed,
    because each one was about internal consistency and none was about whether
    the design did what was asked.

    Why the symbol library is consulted
    ----------------------------------
    The regex reads prose, so it will pick up tokens that look like pins and are
    not. Intersecting with the pins the selected MCU actually has is what makes a
    false extraction harmless: a demand can only be raised for an identifier the
    device really offers. ``PA0`` in "connect PA0 to the button" survives;
    ``GPIO`` in a sentence about GPIO in general does not match at all; a stray
    ``PB99`` matches the regex and is then dropped for not existing.

    The alternate names are searched too, because a requirement may name a
    peripheral function rather than a port bit.
    """
    requested = _requested_pin_names(
        state.requirement_text,
        getattr(state.artifact(PipelineStep.REQUIREMENTS), "constraints", []) or [],
    )
    if not requested:
        return []
    selection = state.artifact(PipelineStep.SELECTION)
    if not isinstance(selection, SelectionPlan):
        return []

    # identifier -> {ref: pin number}, for identifiers that really exist
    offered: dict[str, dict[str, str]] = {}
    for part in selection.parts:
        for pin in symbols.symbol_pins(part.symbol) or []:
            number = str(pin.get("number", "")).strip()
            if not number:
                continue
            names = {str(pin.get("name", "")).upper()}
            names.update(str(a).upper() for a in (pin.get("alternates") or ()))
            for name in names & requested:
                offered.setdefault(name, {})[part.ref] = number
    if not offered:
        return []

    mapped = {f"{mp.ref}:{mp.number}" for net in artifact.nets for mp in net.pins}
    checks: list[CheckResult] = []
    for name in sorted(offered):
        places = offered[name]
        used = [
            f"{ref}:{number}"
            for ref, number in sorted(places.items())
            if f"{ref}:{number}" in mapped
        ]
        checks.append(CheckResult(
            name=f"requested_pin_used:{name}",
            ok=bool(used),
            message=(
                f"the requirement names {name}, which exists on "
                f"{', '.join(f'{r}:{n}' for r, n in sorted(places.items()))}, "
                f"but no net in the finished pin map connects it. A requested pin "
                f"that never appears is a requirement the board does not meet, "
                f"however consistent the rest of it is"
            ),
            targets=[f"{ref}:{number}" for ref, number in sorted(places.items())],
        ))
    return checks


class SchPinMapStep(PipelineStepBase):
    """Map each net's logical pins to real device pin numbers (grounded).

    The mapping is always verified against the real symbol library: a mapped
    number must be a genuine pin of that component's symbol. Bottom-line: no
    unresolved pins, no pin assigned to two nets; floating power/ground pins
    are surfaced as warnings.
    """

    step = PipelineStep.SCH_PINMAP
    knowledge_role = "schematic"

    def _ref_symbols(self, state: PipelineState) -> dict[str, str]:
        sel = state.artifact(PipelineStep.SELECTION)
        if isinstance(sel, SelectionPlan):
            return {p.ref: p.symbol for p in sel.parts}
        return {}

    def _deterministic_map(self, state: PipelineState) -> PinMapPlan:
        intent = state.artifact(PipelineStep.SCH_CONNECTIONS)
        ref_syms = self._ref_symbols(state)
        nets: list[MappedNet] = []
        unresolved: list[str] = []
        if not isinstance(intent, NetlistIntent):
            return PinMapPlan(nets=nets, unresolved=unresolved)
        for net in intent.nets:
            mapped: list[MappedPin] = []
            used_in_net: set[str] = set()
            for lp in net.pins:
                sym = ref_syms.get(lp.ref)
                pins = symbols.symbol_pins(sym) if sym else None
                number = _resolve_logical_pin(pins, lp.pin) if pins else None
                if number is None:
                    # Without a symbol library there is no authoritative pin
                    # number to resolve. Preserve the logical pin as a virtual
                    # number so the offline netlist keeps its full cardinality;
                    # the unavailable-library warning remains explicit.
                    if config.symbol_dir() is None:
                        number = lp.pin
                    elif lp.pin.isdigit():
                        number = lp.pin
                    else:
                        unresolved.append(f"{lp.ref}:{lp.pin}")
                        continue
                key = f"{lp.ref}:{number}"
                if key in used_in_net:
                    # Redundant WITHIN this net (e.g. an analog-ground alias
                    # collapsing onto a GND pin already on this net). Skip.
                    # Cross-net duplicates are deliberately kept so the
                    # no_double_assigned_pins check can flag a real short.
                    continue
                used_in_net.add(key)
                mapped.append(MappedPin(ref=lp.ref, logical=lp.pin, number=number))
            nets.append(MappedNet(name=net.name, kind=net.kind, pins=mapped))
        return PinMapPlan(
            nets=nets, unresolved=unresolved, rationale="deterministic pin mapping",
        )

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        # Deterministic, library-grounded mapping is authoritative even in LLM
        # modes: the LLM cannot invent pin numbers. (A future refinement may let
        # the LLM disambiguate multi-match names, still verified below.)
        return self._deterministic_map(state), False

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, PinMapPlan)
        checks: list[CheckResult] = []
        if config.symbol_dir() is None:
            checks.append(CheckResult(
                name="tool_unavailable.symbol_library", ok=False, severity=Severity.WARNING,
                message=(
                    "no symbol library found in KICAD_SYMBOL_DIR or any KiCad install; "
                    "pin numbers not verified"
                ),
            ))
            return checks
        ref_syms = self._ref_symbols(state)
        # Every mapped number must be a real pin of that symbol.
        bad: list[str] = []
        for net in artifact.nets:
            for mp in net.pins:
                pins = symbols.symbol_pins(ref_syms.get(mp.ref, ""))
                numbers = {str(p["number"]) for p in pins} if pins else set()
                if mp.number not in numbers:
                    bad.append(f"{mp.ref}:{mp.number}({net.name})")
        checks.append(CheckResult(
            name="mapped_pins_exist", ok=not bad,
            message=f"mapped pins not found in symbol: {bad}",
        ))
        checks.append(CheckResult(
            name="all_pins_resolved", ok=not artifact.unresolved,
            message=f"unresolved logical pins: {artifact.unresolved}",
        ))
        # No real pin assigned to two different nets.
        seen: dict[str, str] = {}
        dup: list[str] = []
        for net in artifact.nets:
            for mp in net.pins:
                key = f"{mp.ref}:{mp.number}"
                if key in seen and seen[key] != net.name:
                    dup.append(f"{key} in {seen[key]} & {net.name}")
                seen[key] = net.name
        checks.append(CheckResult(
            name="no_double_assigned_pins", ok=not dup,
            message=f"pins on multiple nets: {dup}",
        ))
        # Floating power/ground pins (warning — surfaced, not blocking).
        floating = self._floating_power_pins(ref_syms, artifact)
        checks.append(CheckResult(
            name="power_pins_connected", ok=not floating, severity=Severity.WARNING,
            message=f"unconnected power/ground pins: {floating}",
        ))
        checks.extend(_requested_pin_checks(state, artifact))
        return checks

    def _floating_power_pins(
        self, ref_syms: dict[str, str], artifact: PinMapPlan
    ) -> list[str]:
        connected: set[str] = {
            f"{mp.ref}:{mp.number}" for net in artifact.nets for mp in net.pins
        }
        floating: list[str] = []
        for ref, sym in ref_syms.items():
            pins = symbols.symbol_pins(sym)
            if not pins:
                continue
            for p in pins:
                name = str(p["name"]).upper()
                ptype = str(p["type"])
                is_power = ptype in ("power_in", "power_out") or any(
                    name == pn or name.startswith(pn) for pn in _POWER_PIN_NAMES
                )
                if is_power and f"{ref}:{p['number']}" not in connected:
                    floating.append(f"{ref}:{p['number']}({name})")
        return floating

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, PinMapPlan)
        total = sum(len(n.pins) for n in artifact.nets)
        return f"{total} pins mapped across {len(artifact.nets)} nets, " \
               f"{len(artifact.unresolved)} unresolved"


_SHEET_COLS = 6
_SYMBOL_HALF_MM = 6.0
_SHEET_CLEARANCE_MM = 5.08
_SHEET_PACK_GAP_MM = 2 * _SHEET_CLEARANCE_MM


def _symbol_half_extents(symbol: str | None, rotation: float = 0.0) -> tuple[float, float]:
    """Return a conservative symbol half-width/height from real pin geometry."""
    pins = symbols.symbol_pins(symbol or "") or []
    half_width = max(
        [_SYMBOL_HALF_MM, *(abs(float(pin["x"])) for pin in pins)]
    )
    half_height = max(
        [_SYMBOL_HALF_MM, *(abs(float(pin["y"])) for pin in pins)]
    )
    radians = math.radians(rotation % 360.0)
    cos_value = abs(math.cos(radians))
    sin_value = abs(math.sin(radians))
    return (
        cos_value * half_width + sin_value * half_height,
        sin_value * half_width + cos_value * half_height,
    )


def _sheet_overlaps(
    placements: list[SheetPlacement],
    symbol_by_ref: dict[str, str] | None = None,
) -> list[str]:
    """Return pairs whose real symbol envelopes overlap on the sheet."""
    out: list[str] = []
    items = list(placements)
    symbol_by_ref = symbol_by_ref or {}
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            aw, ah = _symbol_half_extents(symbol_by_ref.get(a.ref), a.rotation)
            bw, bh = _symbol_half_extents(symbol_by_ref.get(b.ref), b.rotation)
            if (
                abs(a.x - b.x) < aw + bw + _SHEET_CLEARANCE_MM
                and abs(a.y - b.y) < ah + bh + _SHEET_CLEARANCE_MM
            ):
                out.append(f"{a.ref}&{b.ref}")
    return out


def _reflow_sheet_placements(
    placements: list[SheetPlacement],
    symbol_by_ref: dict[str, str],
) -> list[SheetPlacement]:
    """Pack symbols in rows using their real pin extents and a safe clearance."""
    rows = [
        placements[index:index + _SHEET_COLS]
        for index in range(0, len(placements), _SHEET_COLS)
    ]
    result: list[SheetPlacement] = []
    y_cursor = _SHEET_PACK_GAP_MM
    for row in rows:
        extents = [
            _symbol_half_extents(symbol_by_ref.get(item.ref), item.rotation)
            for item in row
        ]
        row_half_height = max((height for _, height in extents), default=_SYMBOL_HALF_MM)
        center_y = y_cursor + row_half_height
        x_cursor = _SHEET_PACK_GAP_MM
        for item, (half_width, _) in zip(row, extents, strict=True):
            center_x = x_cursor + half_width
            result.append(SheetPlacement(
                ref=item.ref,
                x=center_x,
                y=center_y,
                rotation=item.rotation,
            ))
            x_cursor = center_x + half_width + _SHEET_PACK_GAP_MM
        y_cursor = center_y + row_half_height + _SHEET_PACK_GAP_MM
    return result


class SchLayoutStep(PipelineStepBase):
    """Schematic sheet layout: place symbols and choose wire vs net-label.

    Bottom-line: every part is placed, symbols do not overlap on the sheet, and
    every net drawn as a label names a real net (so the label netlist matches).
    """

    step = PipelineStep.SCH_LAYOUT
    knowledge_role = "schematic"

    def knowledge_query(self, state: PipelineState) -> str | None:
        return "schematic readability, sheet layout, wires vs net labels"

    def _refs(self, state: PipelineState) -> list[str]:
        sel = state.artifact(PipelineStep.SELECTION)
        return [p.ref for p in sel.parts] if isinstance(sel, SelectionPlan) else []

    def _symbol_by_ref(self, state: PipelineState) -> dict[str, str]:
        sel = state.artifact(PipelineStep.SELECTION)
        if not isinstance(sel, SelectionPlan):
            return {}
        return {part.ref: part.symbol for part in sel.parts}

    def _net_names(self, state: PipelineState) -> list[str]:
        intent = state.artifact(PipelineStep.SCH_CONNECTIONS)
        return [n.name for n in intent.nets] if isinstance(intent, NetlistIntent) else []

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        def fallback() -> SchLayoutPlan:
            placements = _reflow_sheet_placements(
                [SheetPlacement(ref=ref, x=0.0, y=0.0) for ref in self._refs(state)],
                self._symbol_by_ref(state),
            )
            intent = state.artifact(PipelineStep.SCH_CONNECTIONS)
            labels: list[str] = []
            if isinstance(intent, NetlistIntent):
                labels = [intent.ground_net, *intent.supply_nets]
                labels = [n for n in labels if any(x.name == n for x in intent.nets)]
            return SchLayoutPlan(
                placements=placements, label_nets=labels,
                rationale="deterministic grid sheet layout; power/ground as labels",
            )

        system = (
            "You lay out a schematic sheet as JSON: placements[] ({ref, x, y, "
            "rotation}) in mm, label_nets[] (nets drawn as labels vs local wires), "
            "rationale. Keep symbols from overlapping; power/ground as labels."
        )
        user = f"Components: {self._refs(state)}\nNets: {self._net_names(state)}\n\n{knowledge}"
        plan, used = propose_structured(
            ctx, model=SchLayoutPlan, system=system, user=user, fallback=fallback
        )
        # Keep the LLM's grouping/rotation choices, but reflow unsafe geometry.
        # Real KiCad symbols can extend tens of millimetres beyond their origin;
        # checking only origin distance can put pins from different nets at the
        # same coordinate and create a real electrical short.
        symbol_by_ref = self._symbol_by_ref(state)
        if isinstance(plan, SchLayoutPlan) and _sheet_overlaps(
            plan.placements, symbol_by_ref
        ):
            plan.placements = _reflow_sheet_placements(
                plan.placements, symbol_by_ref
            )
        return plan, used

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, SchLayoutPlan)
        refs = set(self._refs(state))
        placed = {p.ref for p in artifact.placements}
        checks: list[CheckResult] = []
        if refs:
            missing = sorted(refs - placed)
            checks.append(CheckResult(
                name="all_parts_placed", ok=not missing,
                message=f"unplaced components: {missing}",
            ))
        # No symbol overlaps on the sheet.
        overlaps = _sheet_overlaps(artifact.placements, self._symbol_by_ref(state))
        checks.append(CheckResult(
            name="no_symbol_overlap", ok=not overlaps,
            message=f"overlapping symbols: {overlaps}",
        ))
        # Every label net must be a real net (label netlist round-trips).
        net_names = set(self._net_names(state))
        if net_names:
            bad = sorted(set(artifact.label_nets) - net_names)
            checks.append(CheckResult(
                name="labels_match_netlist", ok=not bad,
                message=f"label nets not in the netlist: {bad}",
            ))
        return checks

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, SchLayoutPlan)
        return f"{len(artifact.placements)} symbols placed, {len(artifact.label_nets)} label nets"


class SchMaterializeStep(PipelineStepBase):
    """Write the real ``.kicad_sch``, embedding real symbol pin geometry.

    Deterministic (no LLM): assembles the selected parts at their sheet
    placements and labels every mapped pin at its true coordinate. Bottom-line:
    the reloaded label netlist round-trips to the pin map (same net names and
    per-net pin counts) and all components are present.
    """

    step = PipelineStep.SCH_MATERIALIZE

    def _components(self, state: PipelineState) -> list[dict[str, Any]]:
        sel = state.artifact(PipelineStep.SELECTION)
        layout = state.artifact(PipelineStep.SCH_LAYOUT)
        places: dict[str, SheetPlacement] = {}
        if isinstance(layout, SchLayoutPlan):
            places = {p.ref: p for p in layout.placements}
        out: list[dict[str, Any]] = []
        if isinstance(sel, SelectionPlan):
            for i, p in enumerate(sel.parts):
                pl = places.get(p.ref)
                out.append({
                    "ref": p.ref, "symbol": p.symbol, "value": p.value,
                    "footprint": p.footprint,
                    "x": pl.x if pl else 25.4 * (i % 6),
                    "y": pl.y if pl else 25.4 * (i // 6),
                    "rotation": pl.rotation if pl else 0.0,
                })
        return out

    def _nets(self, state: PipelineState) -> list[dict[str, Any]]:
        pm = state.artifact(PipelineStep.SCH_PINMAP)
        if not isinstance(pm, PinMapPlan):
            return []
        return [
            {"name": n.name, "pins": [{"ref": mp.ref, "number": mp.number} for mp in n.pins]}
            for n in pm.nets
        ]

    def _no_connect_pins(self, state: PipelineState) -> list[dict[str, str]]:
        intent = state.artifact(PipelineStep.SCH_CONNECTIONS)
        sel = state.artifact(PipelineStep.SELECTION)
        if not isinstance(intent, NetlistIntent) or not isinstance(sel, SelectionPlan):
            return []
        ref_symbols = {part.ref: part.symbol for part in sel.parts}
        out: list[dict[str, str]] = []
        for logical in intent.no_connect_pins:
            pins = symbols.symbol_pins(ref_symbols.get(logical.ref, "")) or []
            number = _resolve_logical_pin(pins, logical.pin)
            if number is not None:
                out.append({"ref": logical.ref, "number": number})
        return out

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        components = self._components(state)
        nets = self._nets(state)
        no_connect_pins = self._no_connect_pins(state)
        intent = state.artifact(PipelineStep.SCH_CONNECTIONS)
        supply = intent.supply_nets if isinstance(intent, NetlistIntent) else []
        ground = intent.ground_net if isinstance(intent, NetlistIntent) else "GND"
        doc = materialize_pinmapped(
            components,
            nets,
            no_connect_pins=no_connect_pins,
            supply_nets=supply,
            ground_net=ground,
        )

        out_dir = Path(ctx.out_dir) if ctx.out_dir else Path(tempfile.mkdtemp(prefix="rnp_sch_"))
        out_dir.mkdir(parents=True, exist_ok=True)
        sch_path = out_dir / f"{state.project_name}.kicad_sch"
        doc.save(sch_path)
        pm = state.artifact(PipelineStep.SCH_PINMAP)
        label_count = sum(len(n.pins) for n in pm.nets) if isinstance(pm, PinMapPlan) else 0
        result = MaterializeResult(
            sch_path=str(sch_path), component_count=len(components),
            net_count=len(nets), label_count=label_count,
        )
        return result, False

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, MaterializeResult)
        from ratsnestpro.eda import SchematicDoc

        checks: list[CheckResult] = []
        doc = SchematicDoc.load(artifact.sch_path)
        # All components present.
        sel = state.artifact(PipelineStep.SELECTION)
        expected_refs = {p.ref for p in sel.parts} if isinstance(sel, SelectionPlan) else set()
        actual_refs = set(doc.references())
        checks.append(CheckResult(
            name="all_components_written", ok=expected_refs <= actual_refs,
            message=f"missing components in sch: {sorted(expected_refs - actual_refs)}",
        ))
        # Label netlist round-trips to the pin map (names + per-net counts).
        pm = state.artifact(PipelineStep.SCH_PINMAP)
        netlist = doc.label_netlist()
        if isinstance(pm, PinMapPlan):
            expected = {n.name: len(n.pins) for n in pm.nets if n.pins}
            got = {name: len(coords) for name, coords in netlist.items()}
            missing_nets = sorted(set(expected) - set(got))
            checks.append(CheckResult(
                name="netlist_names_round_trip", ok=not missing_nets,
                message=f"nets missing after materialize: {missing_nets}",
            ))
            count_mismatch = sorted(
                f"{k}({got.get(k, 0)}!={v})" for k, v in expected.items() if got.get(k, 0) != v
            )
            checks.append(CheckResult(
                name="netlist_pin_counts_round_trip", ok=not count_mismatch,
                message=f"label count != pin count: {count_mismatch}",
            ))
        # Symbol graphics embedded in lib_symbols → the sheet renders and is
        # self-contained. Without the symbol library configured we can't embed,
        # so this surfaces as a non-blocking WARNING rather than a hard failure.
        embedded = set(doc.lib_symbol_ids())
        want_symbols = (
            {p.symbol for p in sel.parts} if isinstance(sel, SelectionPlan) else set()
        )
        missing_syms = sorted(want_symbols - embedded)
        if config.symbol_dir() is None:
            checks.append(CheckResult(
                name="lib_symbols_embedded", ok=False, severity=Severity.WARNING,
                message=(
                    "no symbol library found in KICAD_SYMBOL_DIR or any KiCad install; "
                    "symbol graphics not embedded"
                ),
            ))
        else:
            checks.append(CheckResult(
                name="lib_symbols_embedded", ok=not missing_syms,
                message=f"symbol graphics missing from lib_symbols: {missing_syms}",
            ))
        return checks

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, MaterializeResult)
        return (
            f"wrote {artifact.sch_path} "
            f"({artifact.component_count} parts, {artifact.net_count} nets, "
            f"{artifact.label_count} pin labels)"
        )


class ErcStep(PipelineStepBase):
    """Schematic ERC bottom-line.

    Deterministic, authoritative checks (block on failure): no shorted nets,
    no single-pin nets, and zero real kicad-cli ERC errors when the CLI is
    available. kicad-cli being unavailable is reported as a warning, never as
    a pass.
    """

    step = PipelineStep.ERC

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        from ratsnestpro.eda import SchematicDoc

        mat = state.artifact(PipelineStep.SCH_MATERIALIZE)
        if not isinstance(mat, MaterializeResult):
            return ErcSummary(sch_path=""), False
        doc = SchematicDoc.load(mat.sch_path)
        shorted = doc.shorted_nets()
        single = [name for name, coords in doc.label_netlist().items() if len(coords) < 2]
        erc = run_erc(mat.sch_path)
        return (
            ErcSummary(
                sch_path=mat.sch_path,
                shorted_nets=shorted,
                single_pin_nets=single,
                cli_available=erc.available,
                cli_ran=erc.ran,
                cli_error_count=erc.error_count,
                cli_warning_count=erc.warning_count,
            ),
            False,
        )

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, ErcSummary)
        if not artifact.sch_path:
            return [CheckResult(
                name="schematic_available", ok=False,
                message="no materialized schematic to check",
            )]
        checks = [
            CheckResult(
                name="no_shorted_nets", ok=not artifact.shorted_nets,
                message=f"shorted nets: {artifact.shorted_nets}",
            ),
            CheckResult(
                name="no_single_pin_nets", ok=not artifact.single_pin_nets,
                message=f"single-pin nets: {artifact.single_pin_nets}",
            ),
        ]
        # kicad-cli ERC: unavailable is a warning (never a pass); real ERC
        # errors are authoritative and must stop the production pipeline.
        if not artifact.cli_available:
            checks.append(CheckResult(
                name="kicad_cli_erc", ok=False, severity=Severity.WARNING,
                message="kicad-cli unavailable; real ERC skipped (not a pass)",
            ))
        else:
            checks.append(CheckResult(
                name="kicad_cli_erc", ok=artifact.cli_error_count == 0,
                message=f"kicad-cli ERC reported {artifact.cli_error_count} error(s)",
            ))
        return checks

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, ErcSummary)
        cli = "unavailable" if not artifact.cli_available else f"{artifact.cli_error_count} err"
        return (
            f"shorts={len(artifact.shorted_nets)}, single-pin={len(artifact.single_pin_nets)}, "
            f"cli ERC={cli}"
        )


class LayoutPartitionStep(PipelineStepBase):
    """Board outline + functional zones. Bottom-line: zones lie within the board."""

    step = PipelineStep.LAYOUT_PARTITION
    knowledge_role = "layout"

    def knowledge_query(self, state: PipelineState) -> str | None:
        return f"board partitioning and functional zones for: {state.requirement_text}"

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        def fallback() -> BoardPartition:
            # A neutral default outline, not a board template: the partition
            # needs *some* rectangle, and taking it from the ATmega reference
            # plan made this step depend on a specific board.
            w, h = _DEFAULT_OUTLINE_MM
            zones = [
                BoardZone(name="power", kind="power", x1=0.5, y1=0.5, x2=w * 0.33, y2=h - 0.5),
                BoardZone(name="mcu_digital", kind="digital",
                          x1=w * 0.33, y1=0.5, x2=w * 0.75, y2=h - 0.5),
                BoardZone(name="connectors", kind="connector",
                          x1=w * 0.75, y1=0.5, x2=w - 0.5, y2=h - 0.5),
            ]
            return BoardPartition(
                board_width=w, board_height=h, zones=zones,
                rationale="deterministic power|digital|connector partition",
            )

        system = (
            "You partition a PCB into functional zones. Return JSON: board_width, "
            "board_height (mm), zones[] ({name, kind, x1, y1, x2, y2}), rationale. "
            "Zones must fit inside the board."
        )
        user = f"Requirement:\n{state.requirement_text}\n\nKnowledge:\n{knowledge}"
        return propose_structured(
            ctx, model=BoardPartition, system=system, user=user, fallback=fallback
        )

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, BoardPartition)
        w, h = artifact.board_width, artifact.board_height
        out_of_bounds = [
            z.name for z in artifact.zones
            if z.x1 < 0 or z.y1 < 0 or z.x2 > w or z.y2 > h
        ]
        return [
            CheckResult(
                name="has_board_outline", ok=w > 0 and h > 0,
                message="board must have positive dimensions",
            ),
            CheckResult(
                name="zones_within_board", ok=not out_of_bounds,
                message=f"zones outside the board outline: {out_of_bounds}",
            ),
        ]

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, BoardPartition)
        return (
            f"board {artifact.board_width}x{artifact.board_height} mm, "
            f"{len(artifact.zones)} zones"
        )


_LAYOUT_GRID_MM = 0.5
_LEGAL_ROTATIONS = (0.0, 90.0, 180.0, 270.0)
_EDGE_MARGIN_MM = 12.0
_CRYSTAL_NEAR_MM = 15.0
_DECOUPLE_NEAR_MM = 15.0
_PLACE_SPACING_MM = 10.0
_PLACE_MARGIN_MM = 5.0
_PLACEMENT_TARGET_WEIGHT = 2.0


def _roles(state: PipelineState) -> dict[str, str]:
    sel = state.artifact(PipelineStep.SELECTION)
    return {p.ref: p.role for p in sel.parts} if isinstance(sel, SelectionPlan) else {}


def _footprints_of(state: PipelineState) -> dict[str, str]:
    sel = state.artifact(PipelineStep.SELECTION)
    return {p.ref: p.footprint for p in sel.parts} if isinstance(sel, SelectionPlan) else {}


def _board_dims(state: PipelineState) -> tuple[float, float]:
    part = state.artifact(PipelineStep.LAYOUT_PARTITION)
    if isinstance(part, BoardPartition):
        return part.board_width, part.board_height
    return 70.0, 50.0


def _snap(v: float, grid: float = _LAYOUT_GRID_MM) -> float:
    return round(round(v / grid) * grid, 3)


def _dist(a: PcbPlacement, b: PcbPlacement) -> float:
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5


def _grid_cells(
    w: float,
    h: float,
    spacing: float = _PLACE_SPACING_MM,
) -> list[tuple[float, float]]:
    """Row-major grid of placement cells inside the board margins."""
    cells: list[tuple[float, float]] = []
    y = _PLACE_MARGIN_MM
    while y <= h - _PLACE_MARGIN_MM + 1e-6:
        x = _PLACE_MARGIN_MM
        while x <= w - _PLACE_MARGIN_MM + 1e-6:
            cells.append((_snap(x), _snap(y)))
            x += spacing
        y += spacing
    return cells


def _is_mcu_role(role: str) -> bool:
    text = role.lower()
    return text in {"mcu", "controller", "mcu_controller"} or text.endswith("_mcu")


def _is_crystal_role(role: str) -> bool:
    text = role.lower()
    return any(token in text for token in ("crystal", "xtal", "oscillator"))


def _is_decoupling_role(role: str) -> bool:
    text = role.lower()
    return any(token in text for token in ("decoupling", "bypass", "vcap"))


def _is_close_memory_role(role: str) -> bool:
    text = role.lower()
    return any(
        token in text
        for token in ("flash", "qspi", "memory", "sram", "sdram", "storage")
    )


def _is_connector_role(role: str) -> bool:
    text = role.lower()
    return any(
        token in text
        for token in ("connector", "header", "socket", "power_input", "breakout")
    )


_PLACEMENT_ROLE_NOISE = {
    "bypass",
    "bulk",
    "cap",
    "capacitor",
    "crystal",
    "decoupling",
    "external",
    "input",
    "load",
    "memory",
    "oscillator",
    "output",
    "storage",
    "vcap",
    "vdd",
    "vdda",
    "xtal",
}


def _functional_anchor_target(
    ref: str,
    role: str,
    roles: dict[str, str],
    targets: dict[str, tuple[float, float]],
) -> tuple[float, float] | None:
    """Find the active functional block a proximity-sensitive part serves."""
    wanted = _semantic_role_tokens(role) - _PLACEMENT_ROLE_NOISE
    candidates: list[tuple[int, str]] = []
    for candidate_ref, candidate_role in roles.items():
        if (
            candidate_ref == ref
            or candidate_ref not in targets
            or _is_decoupling_role(candidate_role)
            or _is_connector_role(candidate_role)
            or _is_crystal_role(candidate_role)
        ):
            continue
        score = len(wanted & _semantic_role_tokens(candidate_role))
        if score:
            candidates.append((score, candidate_ref))
    if candidates:
        _, anchor_ref = max(
            candidates,
            key=lambda item: (item[0], -list(roles).index(item[1])),
        )
        return targets[anchor_ref]
    mcu_ref = next(
        (
            candidate_ref
            for candidate_ref, candidate_role in roles.items()
            if candidate_ref in targets and _is_mcu_role(candidate_role)
        ),
        None,
    )
    return targets.get(mcu_ref) if mcu_ref is not None else None


class LayoutCriticalStep(PipelineStepBase):
    """Place strongly-constrained parts: MCU central, its crystal/decoupling
    clustered next to it, connectors on the edge. Parts occupy distinct grid
    cells so the baseline is overlap-free; the bottom-line verifies the
    proximity/edge constraints hold."""

    step = PipelineStep.LAYOUT_CRITICAL
    knowledge_role = "layout"

    def knowledge_query(self, state: PipelineState) -> str | None:
        return "critical placement constraints: decoupling, crystal, connectors"

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        roles = _roles(state)
        w, h = _board_dims(state)
        proximity_count = sum(
            1
            for role in roles.values()
            if (
                _is_mcu_role(role)
                or _is_crystal_role(role)
                or _is_close_memory_role(role)
                or _is_decoupling_role(role)
            )
        )
        # This stage records proximity targets rather than manufacturing-ready
        # courtyard placement.  A fixed 10 mm grid falsely rejects MCUs with
        # many supply pins because their legitimate decouplers spill outside
        # the proximity radius.  Densify the target grid as the cluster grows;
        # LayoutGeneralStep later performs the real courtyard-aware packing.
        target_spacing = min(
            _PLACE_SPACING_MM,
            max(
                0.5,
                _DECOUPLE_NEAR_MM / (math.sqrt(max(proximity_count, 1)) + 1.0),
            ),
        )
        cells = _grid_cells(w, h, spacing=target_spacing)
        used: set[tuple[float, float]] = set()
        placements: list[PcbPlacement] = []

        def take_near(cx: float, cy: float) -> tuple[float, float]:
            free = [c for c in cells if c not in used]
            best = min(free, key=lambda c: (c[0] - cx) ** 2 + (c[1] - cy) ** 2)
            used.add(best)
            return best

        def take_edge(left: bool) -> tuple[float, float]:
            free = [c for c in cells if c not in used]
            key = (lambda c: (c[0], c[1])) if left else (lambda c: (-c[0], c[1]))
            best = min(free, key=key)
            used.add(best)
            return best

        cx, cy = w / 2, h / 2
        # MCU near center, then cluster crystal + decoupling around it.
        clustered = list(dict.fromkeys(
            [r for r, role in roles.items() if _is_mcu_role(role)]
            + [r for r, role in roles.items() if _is_crystal_role(role)]
            + [r for r, role in roles.items() if _is_close_memory_role(role)]
            + [r for r, role in roles.items() if _is_decoupling_role(role)]
        ))
        for ref in clustered:
            x, y = take_near(cx, cy)
            placements.append(PcbPlacement(ref=ref, x=x, y=y))
        for ref, role in roles.items():
            if not _is_connector_role(role):
                continue
            x, y = take_edge(
                left=any(token in role.lower() for token in ("usb", "power", "input"))
            )
            placements.append(PcbPlacement(ref=ref, x=x, y=y))
        plan = PcbPlacementPlan(
            board_width=w, board_height=h, placements=placements,
            rationale="critical parts clustered by the MCU; connectors on the edge",
        )
        return plan, False

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, PcbPlacementPlan)
        roles = _roles(state)
        by_ref = artifact.by_ref()
        w, h = artifact.board_width, artifact.board_height
        mcus = [
            by_ref[ref]
            for ref, role in roles.items()
            if _is_mcu_role(role) and ref in by_ref
        ]

        placement_targets = {
            ref: (placement.x, placement.y)
            for ref, placement in by_ref.items()
        }

        def near_functional_anchor(ref: str) -> float:
            placement = by_ref[ref]
            anchor = _functional_anchor_target(
                ref,
                roles[ref],
                roles,
                placement_targets,
            )
            if anchor is None:
                return min((_dist(placement, m) for m in mcus), default=0.0)
            return math.dist((placement.x, placement.y), anchor)

        bad_edge = [
            ref for ref, role in roles.items()
            if _is_connector_role(role) and ref in by_ref
            and min(
                by_ref[ref].x,
                w - by_ref[ref].x,
                by_ref[ref].y,
                h - by_ref[ref].y,
            ) > _EDGE_MARGIN_MM
        ]
        far_xtal = [
            ref for ref, role in roles.items()
            if _is_crystal_role(role) and ref in by_ref
            and near_functional_anchor(ref) > _CRYSTAL_NEAR_MM
        ]
        far_dec = [
            ref for ref, role in roles.items()
            if _is_decoupling_role(role) and ref in by_ref
            and near_functional_anchor(ref) > _DECOUPLE_NEAR_MM
        ]
        far_memory = [
            ref for ref, role in roles.items()
            if _is_close_memory_role(role) and ref in by_ref
            and not _is_decoupling_role(role)
            and near_functional_anchor(ref) > 20.0
        ]
        return [
            CheckResult(name="connectors_on_edge", ok=not bad_edge,
                        message=f"connectors not near a board edge: {bad_edge}"),
            CheckResult(name="crystal_near_mcu", ok=not far_xtal,
                        message=f"crystal too far from MCU: {far_xtal}"),
            CheckResult(name="decoupling_near_mcu", ok=not far_dec,
                        message=f"decoupling too far from MCU: {far_dec}"),
            CheckResult(name="memory_near_mcu", ok=not far_memory,
                        message=f"close-coupled memory too far from MCU: {far_memory}"),
        ]

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, PcbPlacementPlan)
        return f"{len(artifact.placements)} critical parts placed"


_UNKNOWN_FOOTPRINT_HALF_MM = 1.5


def _placement_bbox(fp: str) -> tuple[float, float, float, float]:
    bbox = footprints.footprint_courtyard_bbox(fp) if fp else None
    if bbox is None:
        half = _UNKNOWN_FOOTPRINT_HALF_MM
        return -half, -half, half, half
    return bbox


def _rotated_bbox(
    bbox: tuple[float, float, float, float],
    rotation: float,
) -> tuple[float, float, float, float]:
    radians = math.radians(rotation % 360.0)
    cos_value, sin_value = math.cos(radians), math.sin(radians)
    points = [
        (
            x * cos_value + y * sin_value,
            -x * sin_value + y * cos_value,
        )
        for x in (bbox[0], bbox[2])
        for y in (bbox[1], bbox[3])
    ]
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _role_group(role: str) -> str:
    text = role.lower()
    if "mcu" in text:
        return "digital"
    if "usb" in text:
        return "usb"
    if "can" in text:
        return "can"
    if any(token in text for token in ("microsd", "sdio", "flash", "storage")):
        return "storage"
    if any(token in text for token in ("analog", "adc")):
        return "analog"
    if any(token in text for token in ("sensor", "accelerometer", "i2c")):
        return "sensor"
    if any(token in text for token in ("led", "button", "user")):
        return "interface"
    if any(token in text for token in ("connector", "header", "socket")):
        return "connector"
    if any(
        token in text
        for token in (
            "crystal",
            "xtal",
            "oscillator",
            "decoupling",
            "bypass",
            "vcap",
        )
    ):
        return "digital"
    if any(token in text for token in ("power", "regulator", "buck", "ldo", "fuse")):
        return "power"
    return ""


def _zone_targets(state: PipelineState) -> dict[str, tuple[float, float]]:
    partition = state.artifact(PipelineStep.LAYOUT_PARTITION)
    if not isinstance(partition, BoardPartition):
        return {}
    roles = _roles(state)
    zones = partition.zones
    targets: dict[str, tuple[float, float]] = {}
    aliases = {
        "digital": ("digital", "mcu"),
        "usb": ("usb",),
        "can": ("can",),
        "storage": ("storage", "flash", "sd"),
        "analog": ("analog",),
        "sensor": ("sensor", "mixed", "i2c"),
        "interface": ("interface", "user"),
        "connector": ("connector",),
        "power": ("power",),
    }
    for ref, role in roles.items():
        group = _role_group(role)
        role_tokens = _semantic_role_tokens(role)
        scored = []
        for index, zone in enumerate(zones):
            zone_text = f"{zone.kind} {zone.name}".lower()
            fixed_match = bool(
                group
                and any(token in zone_text for token in aliases[group])
            )
            overlap = len(role_tokens & _semantic_role_tokens(zone_text))
            score = (100 if fixed_match else 0) + overlap
            if score:
                scored.append((score, -index, zone))
        if not scored:
            continue
        zone = max(scored)[2]
        targets[ref] = ((zone.x1 + zone.x2) / 2, (zone.y1 + zone.y2) / 2)
    return targets


def _prune_free_rectangles(
    rectangles: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    out: list[tuple[float, float, float, float]] = []
    for index, rect in enumerate(rectangles):
        x, y, width, height = rect
        if width <= 1e-6 or height <= 1e-6:
            continue
        contained = any(
            index != other_index
            and x >= other[0] - 1e-6
            and y >= other[1] - 1e-6
            and x + width <= other[0] + other[2] + 1e-6
            and y + height <= other[1] + other[3] + 1e-6
            for other_index, other in enumerate(rectangles)
        )
        if not contained:
            out.append(rect)
    return out


def _split_free_rectangles(
    rectangles: list[tuple[float, float, float, float]],
    used: tuple[float, float, float, float],
) -> list[tuple[float, float, float, float]]:
    ux, uy, used_width, used_height = used
    result: list[tuple[float, float, float, float]] = []
    for x, y, width, height in rectangles:
        if (
            ux >= x + width
            or ux + used_width <= x
            or uy >= y + height
            or uy + used_height <= y
        ):
            result.append((x, y, width, height))
            continue
        if ux > x:
            result.append((x, y, ux - x, height))
        if ux + used_width < x + width:
            result.append((
                ux + used_width,
                y,
                x + width - ux - used_width,
                height,
            ))
        if uy > y:
            result.append((x, y, width, uy - y))
        if uy + used_height < y + height:
            result.append((
                x,
                uy + used_height,
                width,
                y + height - uy - used_height,
            ))
    return _prune_free_rectangles(result)


def _maxrect_pack(
    order: list[str],
    fps: dict[str, str],
    board_width: float,
    board_height: float,
    clearance: float,
    targets: dict[str, tuple[float, float]] | None = None,
    priority_refs: set[str] | None = None,
) -> tuple[list[PcbPlacement], list[str]]:
    """Pack real footprint courtyards inside the fixed board outline."""
    targets = targets or {}
    priority_refs = priority_refs or set()
    edge = max(config.process_capability().min_board_edge_clearance, 0.5)
    available_pitch = math.sqrt(
        board_width * board_height / max(len(order), 1)
    )
    routing_channel = min(0.75, max(0.25, available_pitch * 0.08))
    pad = max(clearance, 0.2) + routing_channel
    free = [(
        edge,
        edge,
        board_width - 2 * edge,
        board_height - 2 * edge,
    )]
    boxes = {ref: _placement_bbox(fps.get(ref, "")) for ref in order}
    order_index = {ref: index for index, ref in enumerate(order)}
    packed_order = sorted(
        order,
        key=lambda ref: (
            0 if ref in priority_refs else 1,
            (
                order_index[ref]
                if ref in priority_refs
                else -max(
                    boxes[ref][2] - boxes[ref][0],
                    boxes[ref][3] - boxes[ref][1],
                )
            ),
            (
                0.0
                if ref in priority_refs
                else -(
                    (boxes[ref][2] - boxes[ref][0])
                    * (boxes[ref][3] - boxes[ref][1])
                )
            ),
            order_index[ref],
        ),
    )
    placements: list[PcbPlacement] = []
    unplaced: list[str] = []
    diagonal = max(math.hypot(board_width, board_height), 1.0)
    for ref in packed_order:
        target = targets.get(ref)
        candidates: list[
            tuple[
                tuple[float, float, float, float],
                tuple[float, float, float, float],
                float,
                float,
                float,
            ]
        ] = []
        for free_rect in free:
            fx, fy, free_width, free_height = free_rect
            for rotation in (0.0, 90.0):
                bbox = _rotated_bbox(boxes[ref], rotation)
                width = bbox[2] - bbox[0] + 2 * pad
                height = bbox[3] - bbox[1] + 2 * pad
                if width > free_width + 1e-6 or height > free_height + 1e-6:
                    continue
                desired = [(fx, fy)]
                if target is not None:
                    desired.append((
                        min(max(target[0] - width / 2, fx), fx + free_width - width),
                        min(max(target[1] - height / 2, fy), fy + free_height - height),
                    ))
                for desired_x, desired_y in desired:
                    # Keep the exact packing coordinates for geometry tests.
                    # Rounding here can move the used rectangle a fraction of a
                    # micron outside its free rectangle; after the first split,
                    # every later candidate can then be rejected as out of bounds.
                    origin_x = desired_x + pad - bbox[0]
                    origin_y = desired_y + pad - bbox[1]
                    used_x = origin_x + bbox[0] - pad
                    used_y = origin_y + bbox[1] - pad
                    if (
                        used_x < fx - 1e-6
                        or used_y < fy - 1e-6
                        or used_x + width > fx + free_width + 1e-6
                        or used_y + height > fy + free_height + 1e-6
                    ):
                        continue
                    short_left = min(free_width - width, free_height - height)
                    long_left = max(free_width - width, free_height - height)
                    center_x = origin_x + (bbox[0] + bbox[2]) / 2
                    center_y = origin_y + (bbox[1] + bbox[3]) / 2
                    distance = (
                        math.dist((center_x, center_y), target) / diagonal
                        if target is not None
                        else 0.0
                    )
                    fit_score = (short_left + 0.1 * long_left) / diagonal
                    # MaxRects still owns geometric legality, but a functional
                    # partition must materially affect the chosen rectangle.
                    # The old near-zero weight routinely put the MCU outside
                    # its digital zone and increased routing congestion.
                    score = fit_score + _PLACEMENT_TARGET_WEIGHT * distance
                    candidates.append((
                        (score, short_left, long_left, used_y),
                        (used_x, used_y, width, height),
                        origin_x,
                        origin_y,
                        rotation,
                    ))
        if not candidates:
            unplaced.append(ref)
            continue
        _, used, origin_x, origin_y, rotation = min(
            candidates, key=lambda candidate: candidate[0]
        )
        placements.append(PcbPlacement(
            ref=ref,
            x=origin_x,
            y=origin_y,
            rotation=rotation,
        ))
        free = _split_free_rectangles(free, used)
    return placements, unplaced


class LayoutGeneralStep(PipelineStepBase):
    """Place the remaining parts in free grid cells and tidy: snap to grid,
    normalize rotation. Bottom-line: all parts placed, on-grid, legal orient."""

    step = PipelineStep.LAYOUT_GENERAL
    knowledge_role = "layout"

    def knowledge_query(self, state: PipelineState) -> str | None:
        return "placement alignment, grid, orientation, tidy layout"

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        roles = _roles(state)
        w, h = _board_dims(state)
        fps = _footprints_of(state)
        crit = state.artifact(PipelineStep.LAYOUT_CRITICAL)
        crit_refs = [p.ref for p in crit.placements] if isinstance(crit, PcbPlacementPlan) else []
        # Only geometrically dominant critical parts are packed ahead of size
        # sorting. Small decouplers and crystals still keep their critical
        # targets, but packing all of them first fragments the board before the
        # MCU and connectors can be placed.
        priority_refs = {
            ref
            for ref, role in roles.items()
            if (
                _is_mcu_role(role)
                or (
                    _is_close_memory_role(role)
                    and not _is_decoupling_role(role)
                )
                or _is_connector_role(role)
            )
        }
        priority_order = [
            ref
            for ref, role in roles.items()
            if _is_mcu_role(role)
        ] + [
            ref
            for ref, role in roles.items()
            if (
                _is_close_memory_role(role)
                and not _is_decoupling_role(role)
                and not _is_mcu_role(role)
            )
        ] + [
            ref
            for ref, role in roles.items()
            if _is_connector_role(role)
        ]
        order = list(dict.fromkeys([
            *priority_order,
            *crit_refs,
            *roles,
        ]))
        cap = config.process_capability()
        targets = _zone_targets(state)
        if isinstance(crit, PcbPlacementPlan):
            targets.update({
                placement.ref: (placement.x, placement.y)
                for placement in crit.placements
            })
        for ref, role in roles.items():
            if not (
                _is_decoupling_role(role)
                or _is_crystal_role(role)
                or _is_close_memory_role(role)
            ):
                continue
            anchor = _functional_anchor_target(ref, role, roles, targets)
            if anchor is not None:
                targets[ref] = anchor
        placements, _unplaced = _maxrect_pack(
            order,
            fps,
            w,
            h,
            cap.min_clearance,
            targets,
            priority_refs,
        )
        # Critical-plan coordinates are targets, not the final packed
        # coordinates.  Refine dependent targets from the first pass so a
        # regulator/flash decoupler follows the regulator/flash's *actual*
        # location rather than its stale critical-plan location.
        actual_targets = dict(targets)
        actual_targets.update({
            placement.ref: (placement.x, placement.y)
            for placement in placements
        })
        refined_targets = dict(targets)
        for ref, role in roles.items():
            if not (
                _is_decoupling_role(role)
                or _is_crystal_role(role)
                or _is_close_memory_role(role)
            ):
                continue
            anchor = _functional_anchor_target(ref, role, roles, actual_targets)
            if anchor is not None:
                refined_targets[ref] = anchor
        placements, _unplaced = _maxrect_pack(
            order,
            fps,
            w,
            h,
            cap.min_clearance,
            refined_targets,
            priority_refs,
        )
        plan = PcbPlacementPlan(
            board_width=w,
            board_height=h,
            placements=placements,
            rationale=(
                "real-courtyard MaxRects placement inside the fixed board outline; "
                "functional zones used as placement targets"
            ),
        )
        return plan, False

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, PcbPlacementPlan)
        roles = _roles(state)
        placed = {p.ref for p in artifact.placements}
        missing = sorted(set(roles) - placed)
        off_grid = [
            p.ref for p in artifact.placements
            if abs(p.x / _LAYOUT_GRID_MM - round(p.x / _LAYOUT_GRID_MM)) > 1e-6
            or abs(p.y / _LAYOUT_GRID_MM - round(p.y / _LAYOUT_GRID_MM)) > 1e-6
        ]
        bad_rot = [p.ref for p in artifact.placements if p.rotation not in _LEGAL_ROTATIONS]
        expected_width, expected_height = _board_dims(state)
        resized = (
            abs(artifact.board_width - expected_width) > 1e-6
            or abs(artifact.board_height - expected_height) > 1e-6
        )
        checks = [
            CheckResult(name="all_parts_placed", ok=not missing,
                        message=f"unplaced parts: {missing}"),
            CheckResult(
                name="board_outline_preserved",
                ok=not resized,
                message=(
                    f"placement changed board from {expected_width}x{expected_height} "
                    f"to {artifact.board_width}x{artifact.board_height} mm"
                ),
            ),
            CheckResult(
                name="grid_aligned",
                ok=not off_grid,
                severity=Severity.WARNING,
                message=f"off-grid placements: {off_grid}",
            ),
            CheckResult(name="legal_rotation", ok=not bad_rot,
                        message=f"illegal rotations: {bad_rot}"),
        ]
        # The critical plan supplies targets, not immutable coordinates. Verify
        # the final packed placement still satisfies those functional gates.
        checks.extend(LayoutCriticalStep().check(state, artifact))
        return checks

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, PcbPlacementPlan)
        return f"{len(artifact.placements)} parts placed and aligned"


def _abs_bbox(p: PcbPlacement, fp: str) -> tuple[float, float, float, float] | None:
    x1, y1, x2, y2 = _rotated_bbox(_placement_bbox(fp), p.rotation)
    return (p.x + x1, p.y + y1, p.x + x2, p.y + y2)


def _boxes_overlap(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float], margin: float
) -> bool:
    return (
        a[0] - margin < b[2] and b[0] - margin < a[2]
        and a[1] - margin < b[3] and b[1] - margin < a[3]
    )


class LayoutWriteStep(PipelineStepBase):
    """Courtyard overlap / out-of-bounds / spacing bottom-line, then write the
    .kicad_pcb with real footprint geometry embedded."""

    step = PipelineStep.LAYOUT_WRITE

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        from ratsnestpro.eda.vendor.footprint import load_footprint_node
        from ratsnestpro.eda.vendor.pcb import PcbBoard

        plan = state.artifact(PipelineStep.LAYOUT_GENERAL)
        fps = _footprints_of(state)
        w, h = _board_dims(state)
        if isinstance(plan, PcbPlacementPlan):
            w, h = plan.board_width, plan.board_height
        cap = config.process_capability()
        margin = cap.min_clearance

        overlaps: list[str] = []
        oob: list[str] = []
        if isinstance(plan, PcbPlacementPlan):
            boxes: dict[str, tuple[float, float, float, float]] = {}
            for p in plan.placements:
                bb = _abs_bbox(p, fps.get(p.ref, ""))
                if bb is not None:
                    boxes[p.ref] = bb
            refs = list(boxes)
            for i in range(len(refs)):
                for j in range(i + 1, len(refs)):
                    if _boxes_overlap(boxes[refs[i]], boxes[refs[j]], margin):
                        overlaps.append(f"{refs[i]}&{refs[j]}")
            edge = cap.min_board_edge_clearance
            for ref, bb in boxes.items():
                if bb[0] < edge or bb[1] < edge or bb[2] > w - edge or bb[3] > h - edge:
                    oob.append(ref)

        out_dir = Path(ctx.out_dir) if ctx.out_dir else Path(tempfile.mkdtemp(prefix="rnp_pcb_"))
        out_dir.mkdir(parents=True, exist_ok=True)
        pcb_path = out_dir / f"{state.project_name}.kicad_pcb"
        board = PcbBoard.blank()
        board.set_board_outline(0.0, 0.0, w, h)
        count = 0
        if isinstance(plan, PcbPlacementPlan):
            sel = state.artifact(PipelineStep.SELECTION)
            values = {p.ref: p.value for p in sel.parts} if isinstance(sel, SelectionPlan) else {}
            for p in plan.placements:
                fp = fps.get(p.ref, "")
                embed = None
                fp_path = footprints.footprint_path(fp) if fp else None
                if fp_path is not None:
                    try:
                        embed = load_footprint_node(fp_path)
                    except Exception:
                        embed = None
                try:
                    board.add_footprint(
                        lib_id=fp or "unknown:unknown", reference=p.ref,
                        value=values.get(p.ref, ""), x=p.x, y=p.y, rotation=p.rotation,
                        embed_node=embed,
                    )
                    count += 1
                except Exception:
                    continue
        board.save(pcb_path)
        # Routing mutates the PCB in place. Keep the deterministic layout output
        # so retries and layer escalation always restart from identical geometry
        # instead of accumulating tracks from a failed attempt.
        baseline_path = pcb_path.with_name(f"{pcb_path.stem}.unrouted.kicad_pcb")
        shutil.copy2(pcb_path, baseline_path)
        # Re-read to confirm the Edge.Cuts outline is really on disk (a board
        # with no outline is unmanufacturable — fab houses reject it).
        from ratsnestpro.eda.vendor.sexpr import find_all, find_first

        has_outline = False
        try:
            reloaded = PcbBoard.load(pcb_path)
            for node in find_all(reloaded.root, "gr_line"):
                layer = find_first(node, "layer")
                if layer is not None and any(
                    str(tok) == "Edge.Cuts" for tok in layer[1:]
                ):
                    has_outline = True
                    break
        except Exception:
            has_outline = False
        return (
            PcbWriteResult(
                pcb_path=str(pcb_path), component_count=count,
                overlaps=overlaps, out_of_bounds=oob,
                has_board_outline=has_outline,
            ),
            False,
        )

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, PcbWriteResult)
        checks: list[CheckResult] = []
        if config.footprint_dir() is None:
            checks.append(CheckResult(
                name="tool_unavailable.footprint_library", ok=False, severity=Severity.WARNING,
                message=(
                    "no footprint library found in KICAD_FOOTPRINT_DIR or any KiCad install; "
                    "courtyard checks skipped"
                ),
            ))
        checks.append(CheckResult(
            name="no_courtyard_overlap", ok=not artifact.overlaps,
            message=f"overlapping footprints: {artifact.overlaps}",
        ))
        checks.append(CheckResult(
            name="within_board", ok=not artifact.out_of_bounds,
            message=f"footprints past the board edge: {artifact.out_of_bounds}",
        ))
        checks.append(CheckResult(
            name="board_written", ok=bool(artifact.pcb_path),
            message="no .kicad_pcb written",
        ))
        checks.append(CheckResult(
            name="board_outline_present", ok=artifact.has_board_outline,
            message="no Edge.Cuts board outline written (board is unmanufacturable)",
        ))
        return checks

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, PcbWriteResult)
        return (
            f"wrote {artifact.pcb_path} ({artifact.component_count} footprints, "
            f"{len(artifact.overlaps)} overlaps, {len(artifact.out_of_bounds)} out-of-bounds)"
        )


def _explicit_requested_layer_count(requirement: str) -> int | None:
    text = requirement.lower()
    candidates: list[tuple[int, int]] = []
    for match in re.finditer(r"\b(1[0-6]|[2-9])[\s-]*layers?\b", text):
        if not _model_mention_is_negated(text, match.start()):
            candidates.append((match.start(), int(match.group(1))))
    layer_words = {
        "two": 2,
        "four": 4,
        "six": 6,
        "eight": 8,
        "二层": 2,
        "两层": 2,
        "四层": 4,
        "六层": 6,
        "八层": 8,
        "十层": 10,
        "十二层": 12,
        "十六层": 16,
    }
    for word, count in layer_words.items():
        for match in re.finditer(
            rf"(?<![a-z]){re.escape(word)}(?:[\s-]*layers?)?(?![a-z])",
            text,
        ):
            if not _model_mention_is_negated(text, match.start()):
                candidates.append((match.start(), count))
    if candidates:
        return max(candidates)[1]
    return None


def _requested_layer_count(requirement: str) -> int:
    return _explicit_requested_layer_count(requirement) or 2


def _copper_layer_tokens(layers: int) -> set[str]:
    """Return the KiCad copper layer names available in an ``layers``-layer stackup.

    Used to reject plane assignments that name a layer the board does not have
    (a 2-layer board has only ``F.Cu``/``B.Cu``, so ``L3:POWER`` is not a
    placement, it is a leak from some other board's reference stackup).
    """
    count = max(1, layers)
    tokens = {"F.Cu"}
    if count >= 2:
        tokens.add("B.Cu")
    tokens.update(f"In{index}.Cu" for index in range(1, count - 1))
    return tokens


def _has_explicit_routing_geometry(requirement: str) -> bool:
    """Return whether the user fixed trace/clearance/via dimensions."""
    text = requirement.lower()
    geometry = (
        r"(?:track|trace|line[\s-]*width|clearance|spacing|via|"
        r"线宽|间距|过孔)"
    )
    dimension = r"\d+(?:\.\d+)?\s*mm\b"
    return bool(
        re.search(rf"{geometry}[^\n.;]{{0,48}}{dimension}", text)
        or re.search(rf"{dimension}[^\n.;]{{0,32}}{geometry}", text)
    )


class RoutePlanStep(PipelineStepBase):
    """Stackup + net-class routing rules. Bottom-line: every rule value is at
    or above the fab process minimum (widths, clearance, via)."""

    step = PipelineStep.ROUTE_PLAN
    knowledge_role = "routing"

    def knowledge_query(self, state: PipelineState) -> str | None:
        return "layer stackup, net classes, trace width and clearance rules"

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        cap = config.process_capability()
        requested_layers = _requested_layer_count(state.requirement_text)

        def fallback() -> RoutePlan:
            w = max(cap.min_track_width, 0.2)
            cl = max(cap.min_clearance, 0.2)
            via = max(cap.min_via_diameter, 0.6)
            drill = max(cap.min_via_drill, 0.3)
            classes = [
                NetClass(name="power", width=max(w, 0.5), clearance=cl,
                         via_diameter=via, via_drill=drill, layer="F.Cu"),
                NetClass(name="signal", width=max(w, 0.25), clearance=cl,
                         via_diameter=via, via_drill=drill, layer="F.Cu"),
                NetClass(name="default", width=max(w, 0.25), clearance=cl,
                         via_diameter=via, via_drill=drill, layer="F.Cu"),
            ]
            return RoutePlan(
                layers=requested_layers,
                net_classes=classes,
                rationale=(
                    f"{requested_layers}-layer stackup; power wider than signal"
                ),
            )

        system = (
            "You define a PCB stackup and net classes. Return JSON: layers (2-16), "
            "net_classes[] ({name, width, clearance, via_diameter, via_drill, layer}) "
            "in mm, rationale. Values must meet the fab minimums."
        )
        user = (
            f"Requirement:\n{state.requirement_text}\n\n"
            f"Fab minimums: {cap.model_dump()}\n\nKnowledge:\n{knowledge}"
        )
        return propose_structured(ctx, model=RoutePlan, system=system, user=user, fallback=fallback)

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, RoutePlan)
        cap = config.process_capability()
        thin = [c.name for c in artifact.net_classes if c.width < cap.min_track_width]
        tight = [c.name for c in artifact.net_classes if c.clearance < cap.min_clearance]
        small_via = [c.name for c in artifact.net_classes if c.via_diameter < cap.min_via_diameter]
        small_drill = [
            c.name for c in artifact.net_classes
            if c.via_drill < cap.min_via_drill
        ]
        small_annular = [
            c.name for c in artifact.net_classes
            if (c.via_diameter - c.via_drill) / 2 < cap.min_annular_ring
        ]
        explicit_layers = _explicit_requested_layer_count(
            state.requirement_text
        )
        return [
            CheckResult(
                name="requested_layer_count",
                ok=(
                    explicit_layers is None
                    or artifact.layers == explicit_layers
                ),
                message=(
                    f"explicit requirement is {explicit_layers} layers, "
                    f"but route plan selected {artifact.layers}"
                ),
            ),
            CheckResult(name="has_net_classes", ok=bool(artifact.net_classes),
                        message="no net classes defined"),
            CheckResult(name="track_width_ok", ok=not thin,
                        message=f"net classes below min track width: {thin}"),
            CheckResult(name="clearance_ok", ok=not tight,
                        message=f"net classes below min clearance: {tight}"),
            CheckResult(name="via_ok", ok=not small_via,
                        message=f"net classes below min via: {small_via}"),
            CheckResult(name="via_drill_ok", ok=not small_drill,
                        message=f"net classes below min drill: {small_drill}"),
            CheckResult(name="annular_ring_ok", ok=not small_annular,
                        message=f"net classes below min annular ring: {small_annular}"),
        ]

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, RoutePlan)
        return f"{artifact.layers}-layer, {len(artifact.net_classes)} net classes"


class RoutePlanesStep(PipelineStepBase):
    """Power/ground planes + critical-net priority. Bottom-line: a ground plane
    exists and the critical nets are known."""

    step = PipelineStep.ROUTE_PLANES
    knowledge_role = "routing"

    def knowledge_query(self, state: PipelineState) -> str | None:
        return "power and ground planes, return paths, critical net routing first"

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        intent = state.artifact(PipelineStep.SCH_CONNECTIONS)
        ground = intent.ground_net if isinstance(intent, NetlistIntent) else "GND"
        plan = state.artifact(PipelineStep.ROUTE_PLAN)
        layers = plan.layers if isinstance(plan, RoutePlan) else 2
        net_names = (
            [net.name for net in intent.nets] if isinstance(intent, NetlistIntent) else []
        )

        def fallback() -> PlanePlan:
            critical: list[str] = []
            if isinstance(intent, NetlistIntent):
                critical = [n.name for n in intent.nets if n.kind in ("clock", "power")]
            return PlanePlan(
                ground_net=ground, planes=[f"B.Cu:{ground}"], critical_nets=critical,
                rationale="ground pour on B.Cu; clock/power nets routed first",
            )

        system = (
            "You plan copper planes and critical-net priority. Return JSON: "
            "ground_net, planes[] ('Layer:NET'), critical_nets[], rationale."
        )
        # Without the real net names and stackup this step has nothing to ground
        # on but the retrieved knowledge, and will restate whichever reference
        # board that knowledge describes.
        user = (
            f"Ground net: {ground}\n"
            f"Copper layers: {layers}; the only valid layer names are "
            f"{sorted(_copper_layer_tokens(layers))}\n"
            f"Nets in this design ({len(net_names)}), use these names verbatim "
            f"and invent none: {net_names}\n\n"
            f"Knowledge:\n{knowledge}"
        )
        return propose_structured(ctx, model=PlanePlan, system=system, user=user, fallback=fallback)

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, PlanePlan)
        intent = state.artifact(PipelineStep.SCH_CONNECTIONS)
        plan = state.artifact(PipelineStep.ROUTE_PLAN)
        layers = plan.layers if isinstance(plan, RoutePlan) else 2
        allowed = _copper_layer_tokens(layers)

        has_gnd_plane = any(artifact.ground_net in p for p in artifact.planes)
        bad_layers = sorted(
            plane for plane in artifact.planes
            if plane.split(":", 1)[0].strip() not in allowed
        )
        # The netlist is always present in canonical order; it is absent only in
        # unit tests that build a partial state, where there is nothing to
        # verify against rather than something verified as correct.
        if isinstance(intent, NetlistIntent):
            known = {net.name for net in intent.nets}
            unknown_nets = sorted(n for n in artifact.critical_nets if n not in known)
            nets_message = (
                f"critical nets absent from the netlist: {unknown_nets}"
            )
        else:
            unknown_nets = []
            nets_message = "no netlist artifact available to verify critical nets against"

        return [
            CheckResult(name="ground_plane_present", ok=has_gnd_plane,
                        message=f"no ground plane for {artifact.ground_net!r}"),
            CheckResult(name="critical_nets_exist", ok=not unknown_nets,
                        message=nets_message),
            CheckResult(
                name="plane_layers_in_stackup",
                ok=not bad_layers,
                message=(
                    f"planes name layers outside the {layers}-layer stackup "
                    f"{sorted(allowed)}: {bad_layers}"
                ),
            ),
        ]

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, PlanePlan)
        return f"{len(artifact.planes)} planes, {len(artifact.critical_nets)} critical nets"


class RouteSignalsStep(PipelineStepBase):
    """Route remaining signals with Freerouting.

    Builds fail closed by default. Planning/test contexts may explicitly set
    ``require_freerouting=False`` when a deferred route is intentionally only
    advisory.
    """

    step = PipelineStep.ROUTE_SIGNALS
    knowledge_role = "routing"

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        from ratsnestpro.eda import routing

        pm = state.artifact(PipelineStep.SCH_PINMAP)
        pcb_path = (
            Path(ctx.out_dir) / f"{state.project_name}.kicad_pcb" if ctx.out_dir else None
        )
        if not isinstance(pm, PinMapPlan) or pcb_path is None or not pcb_path.is_file():
            return RouteResult(
                method="deferred", required=ctx.require_freerouting,
                routed_nets=0, total_nets=0,
                note="no board or pin-map available; signal routing deferred",
            ), False

        # net -> [(ref, pad_number)]; only nets with >=2 pins are routable.
        netmap = {
            n.name: [[p.ref, p.number] for p in n.pins]
            for n in pm.nets if len(n.pins) >= 2
        }
        route_plan = state.artifact(PipelineStep.ROUTE_PLAN)
        planned_layers = (
            route_plan.layers if isinstance(route_plan, RoutePlan) else 2
        )
        net_classes = (
            route_plan.net_classes if isinstance(route_plan, RoutePlan) else []
        )
        cap = config.process_capability()
        route_rules = {
            "clearance_mm": min(
                (net_class.clearance for net_class in net_classes),
                default=cap.min_clearance,
            ),
            "track_width_mm": min(
                (net_class.width for net_class in net_classes),
                default=cap.min_track_width,
            ),
            "via_diameter_mm": min(
                (net_class.via_diameter for net_class in net_classes),
                default=cap.min_via_diameter,
            ),
            "via_drill_mm": min(
                (net_class.via_drill for net_class in net_classes),
                default=cap.min_via_drill,
            ),
        }
        layers = max(
            planned_layers,
            _requested_layer_count(state.requirement_text),
        )
        explicit_layers = _explicit_requested_layer_count(
            state.requirement_text
        )
        allow_layer_escalation = explicit_layers is None
        baseline_path = pcb_path.with_name(f"{pcb_path.stem}.unrouted.kicad_pcb")
        if baseline_path.is_file():
            shutil.copy2(baseline_path, pcb_path)
        previous_ses = pcb_path.with_suffix(".ses")
        if (
            allow_layer_escalation
            and
            layers < 4
            and previous_ses.is_file()
            and previous_ses.stat().st_size > 0
        ):
            layers = 4
        outcome = routing.autoroute(
            pcb_path,
            netmap,
            max_passes=routing.pass_budget(netmap, layers),
            layer_count=layers,
            **route_rules,
        )
        # Escalate an incomplete two-layer attempt once. This is a bounded
        # geometric fallback, not a relaxed release gate: SES import and zero
        # remaining connections are still mandatory.
        if (
            allow_layer_escalation
            and outcome.ok
            and outcome.unconnected > 0
            and layers < 4
        ):
            if baseline_path.is_file():
                shutil.copy2(baseline_path, pcb_path)
            outcome = routing.autoroute(
                pcb_path,
                netmap,
                max_passes=routing.pass_budget(netmap, 4),
                layer_count=4,
                **route_rules,
            )
        adaptive_rules_used = False
        adaptive_rules = {
            "clearance_mm": cap.min_clearance,
            "track_width_mm": cap.min_track_width,
            "via_diameter_mm": cap.min_via_diameter,
            "via_drill_mm": cap.min_via_drill,
        }
        can_tighten_within_fab = any(
            adaptive_rules[name] < route_rules[name] - 1e-9
            for name in adaptive_rules
        )
        adaptive_allowed = (
            can_tighten_within_fab
            and not _has_explicit_routing_geometry(state.requirement_text)
        )
        if (
            outcome.ok
            and outcome.unconnected > 0
            and not adaptive_allowed
            and (
                outcome.layers >= 4
                or not allow_layer_escalation
            )
        ):
            if baseline_path.is_file():
                shutil.copy2(baseline_path, pcb_path)
            outcome = routing.autoroute(
                pcb_path,
                netmap,
                max_passes=min(
                    100,
                    routing.pass_budget(netmap, outcome.layers) * 2,
                ),
                layer_count=outcome.layers,
                **route_rules,
            )
        if (
            outcome.ok
            and outcome.unconnected > 0
            and adaptive_allowed
        ):
            if baseline_path.is_file():
                shutil.copy2(baseline_path, pcb_path)
            outcome = routing.autoroute(
                pcb_path,
                netmap,
                max_passes=min(
                    100,
                    routing.pass_budget(netmap, outcome.layers) * 2,
                ),
                layer_count=outcome.layers,
                **adaptive_rules,
            )
            adaptive_rules_used = True
        # Freerouting reports remaining unconnected ratsnest items; when it is 0
        # every routable connection was made. Fall back conservatively otherwise.
        if outcome.ok and outcome.unconnected == 0:
            routed = outcome.nets
        else:
            routed = 0
        return RouteResult(
            method=outcome.method,
            required=ctx.require_freerouting,
            layers=outcome.layers,
            routed_nets=routed,
            total_nets=outcome.nets,
            assigned_pads=outcome.assigned_pads,
            routed_tracks=outcome.routed_tracks,
            unconnected=outcome.unconnected,
            dsn_path=outcome.dsn_path,
            ses_path=outcome.ses_path,
            note=(
                "adaptive fab-min routing rules used; "
                f"{outcome.note}"
                if adaptive_rules_used
                else outcome.note
            ),
        ), False

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, RouteResult)
        complete = (
            artifact.method == "freerouting"
            and artifact.total_nets > 0
            and artifact.routed_nets >= artifact.total_nets
            and artifact.unconnected == 0
            and bool(artifact.dsn_path)
            and bool(artifact.ses_path)
        )
        return [
            CheckResult(
                name="signals_routed",
                ok=complete,
                severity=Severity.ERROR if artifact.required else Severity.WARNING,
                message=f"{artifact.routed_nets}/{artifact.total_nets} nets routed "
                        f"({artifact.method}), tracks={artifact.routed_tracks}, "
                        f"unconnected={artifact.unconnected}; {artifact.note}",
            ),
        ]

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, RouteResult)
        return (
            f"{artifact.method} ({artifact.layers} layers): "
            f"{artifact.routed_nets}/{artifact.total_nets} nets"
        )


class RouteFabStep(PipelineStepBase):
    """Fabrication bottom-line audit: every planned width/clearance/via meets the
    process minimums. Blocks on any violation (anti-board-burn)."""

    step = PipelineStep.ROUTE_FAB

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        cap = config.process_capability()
        plan = state.artifact(PipelineStep.ROUTE_PLAN)
        violations: list[str] = []
        if isinstance(plan, RoutePlan):
            for c in plan.net_classes:
                if c.width < cap.min_track_width:
                    violations.append(f"{c.name}: width {c.width} < {cap.min_track_width}")
                if c.clearance < cap.min_clearance:
                    violations.append(f"{c.name}: clearance {c.clearance} < {cap.min_clearance}")
                if c.via_diameter < cap.min_via_diameter:
                    violations.append(f"{c.name}: via {c.via_diameter} < {cap.min_via_diameter}")
                if c.via_drill < cap.min_via_drill:
                    violations.append(f"{c.name}: drill {c.via_drill} < {cap.min_via_drill}")
        violations.extend(self._routed_geometry_violations(state, plan, cap))
        return FabAudit(violations=violations), False

    @staticmethod
    def _routed_geometry_violations(
        state: PipelineState,
        plan: BaseModel | None,
        cap: config.ProcessCapability,
    ) -> list[str]:
        """Audit the copper that actually shipped, not just the planned rules.

        Auditing only the plan let a router lay 0.15 mm tracks under a 0.3 mm
        net class and still report zero violations; the mismatch then surfaced
        two steps later as hundreds of ``track_width`` DRC errors. A step whose
        whole purpose is the fabrication bottom line must read the board file.
        """
        routed = state.artifact(PipelineStep.ROUTE_SIGNALS)
        if not isinstance(routed, RouteResult):
            return []
        if routed.routed_tracks <= 0:
            # Nothing was laid down, so "no width violations" would be a hollow
            # pass. Only an explicitly deferred router is allowed to be silent.
            if routed.required:
                return [
                    "routing produced no tracks, so the fabrication audit has "
                    f"nothing to verify (method={routed.method!r})"
                ]
            return []

        written = state.artifact(PipelineStep.LAYOUT_WRITE)
        pcb_path = written.pcb_path if isinstance(written, PcbWriteResult) else ""
        if not pcb_path:
            return []
        from ratsnestpro.eda import routing

        widths = routing.copper_track_widths(pcb_path)
        if not widths:
            return []

        violations: list[str] = []
        below_fab = [(w, layer) for w, layer in widths if w < cap.min_track_width]
        if below_fab:
            worst = min(w for w, _ in below_fab)
            layers = sorted({layer for _, layer in below_fab})
            violations.append(
                f"{len(below_fab)} routed track(s) at {worst} mm on {layers} are "
                f"below the fab minimum {cap.min_track_width} mm"
            )

        if isinstance(plan, RoutePlan) and plan.net_classes:
            narrowest_planned = min(c.width for c in plan.net_classes)
            thinner = [(w, layer) for w, layer in widths if w < narrowest_planned]
            if thinner:
                worst = min(w for w, _ in thinner)
                layers = sorted({layer for _, layer in thinner})
                violations.append(
                    f"{len(thinner)} routed track(s) at {worst} mm on {layers} are "
                    f"thinner than the narrowest planned net class "
                    f"({narrowest_planned} mm)"
                )
            # The router is driven by a single global width, so a plan that
            # differentiates power from signal silently collapses to its
            # narrowest class: nets specified at 0.5 mm ship at 0.15 mm, which
            # is a current-capacity defect and not just a DRC nit. If the widest
            # planned class never appears in the copper, that collapse happened.
            widest_planned = max(c.width for c in plan.net_classes)
            widest_routed = max(w for w, _ in widths)
            if widest_routed < widest_planned:
                wide_classes = sorted(
                    c.name for c in plan.net_classes if c.width > widest_routed
                )
                violations.append(
                    f"widest routed track is {widest_routed} mm but the route plan "
                    f"specifies up to {widest_planned} mm; net class(es) "
                    f"{wide_classes} were never applied to the copper"
                )
        return violations

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, FabAudit)
        return [
            CheckResult(name="fab_rules_met", ok=not artifact.violations,
                        message=f"process violations: {artifact.violations}"),
        ]

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, FabAudit)
        return f"{len(artifact.violations)} fab violations"


def _run_kicad_drc(
    cli: str,
    pcb_path: Path,
    report_path: Path,
) -> list[str]:
    """Return error-severity findings from the actual final PCB file."""
    import subprocess

    try:
        subprocess.run(
            [
                cli,
                "pcb",
                "drc",
                "--format",
                "json",
                "--severity-all",
                "--output",
                str(report_path),
                "--exit-code-violations",
                str(pcb_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, subprocess.SubprocessError, ValueError):
        return ["kicad_cli: final DRC did not produce a parseable report"]

    findings = [
        item
        for key in ("violations", "schematic_parity", "unconnected_items")
        for item in data.get(key, [])
        if isinstance(item, dict)
    ]
    return [
        "kicad_cli:"
        f"{finding.get('type', 'unknown')}:"
        f"{finding.get('description', 'DRC error')}"
        for finding in findings
        if str(finding.get("severity", "error")) == "error"
    ]


def _routing_release_violations(
    state: PipelineState,
    pcb_path: Path | None,
) -> list[str]:
    """Prove that the final board contains imported, fully routed copper.

    KiCad reports zero unconnected items on a board whose pads have no nets at
    all.  Therefore a clean DRC is insufficient: release also needs a complete
    Freerouting result, real DSN/SES files, assigned pads, named PCB nets and
    copper tracks in the final board.
    """
    route = state.artifact(PipelineStep.ROUTE_SIGNALS)
    if not isinstance(route, RouteResult):
        return ["routing: signal-routing result is missing"]

    violations: list[str] = []
    if route.method != "freerouting":
        violations.append(f"routing: expected freerouting, got {route.method}")
    if route.total_nets <= 0 or route.routed_nets < route.total_nets:
        violations.append(
            f"routing: only {route.routed_nets}/{route.total_nets} nets are complete"
        )
    if route.unconnected != 0:
        violations.append(f"routing: unconnected count is {route.unconnected}, expected 0")
    if route.assigned_pads <= 0:
        violations.append("routing: no PCB pads were assigned to electrical nets")
    if route.routed_tracks <= 0:
        violations.append("routing: no copper tracks were imported")
    for label, path_value in (("DSN", route.dsn_path), ("SES", route.ses_path)):
        artifact_path = Path(path_value) if path_value else None
        if (
            artifact_path is None
            or not artifact_path.is_file()
            or artifact_path.stat().st_size <= 0
        ):
            violations.append(f"routing: {label} artifact is missing")

    if pcb_path is None or not pcb_path.is_file():
        violations.append("routing: final PCB file is missing")
        return violations
    try:
        from ratsnestpro.eda.vendor.pcb import PcbBoard

        board = PcbBoard.load(pcb_path)
        board_nets = {
            str(item.get("name", "")).lstrip("/")
            for item in board.list_nets()
            if str(item.get("name", "")).strip()
        }
        if not board_nets:
            violations.append("routing: final PCB contains no named electrical nets")
        pad_nets = {name.lstrip("/") for name in board.pad_net_names()}
        if not pad_nets:
            violations.append("routing: final PCB pads have no electrical net assignments")
        pinmap = state.artifact(PipelineStep.SCH_PINMAP)
        if isinstance(pinmap, PinMapPlan):
            expected_nets = {net.name.lstrip("/") for net in pinmap.nets if net.pins}
            missing_nets = sorted(expected_nets - board_nets)
            if missing_nets:
                violations.append(
                    "routing: final PCB is missing schematic nets "
                    f"{missing_nets[:20]}"
                )
            missing_pad_nets = sorted(expected_nets - pad_nets)
            if missing_pad_nets:
                violations.append(
                    "routing: schematic nets are not assigned to final PCB pads "
                    f"{missing_pad_nets[:20]}"
                )
        tracks = board.list_tracks()
        if not tracks:
            violations.append("routing: final PCB contains no copper track segments")
    except (OSError, TypeError, ValueError) as exc:
        violations.append(f"routing: final PCB connectivity cannot be read ({type(exc).__name__})")
    return list(dict.fromkeys(violations))


class ManufactureStep(PipelineStepBase):
    """DRC bottom-line + manufacturing outputs (BOM, CPL, optional Gerber).

    DRC aggregates the deterministic layout/routing findings (overlaps,
    out-of-bounds, fab-rule violations) — authoritative and blocking. BOM and
    CPL are always written (pure data). Gerber export runs only when kicad-cli
    is available; its absence is a warning, never a pass.
    """

    step = PipelineStep.MANUFACTURE

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        import csv

        out_dir = Path(ctx.out_dir) if ctx.out_dir else Path(tempfile.mkdtemp(prefix="rnp_mfg_"))
        out_dir.mkdir(parents=True, exist_ok=True)

        # --- DRC bottom-line: aggregate deterministic findings -------------
        drc: list[str] = []
        write = state.artifact(PipelineStep.LAYOUT_WRITE)
        if isinstance(write, PcbWriteResult):
            drc += [f"overlap:{o}" for o in write.overlaps]
            drc += [f"out_of_bounds:{r}" for r in write.out_of_bounds]
        fab = state.artifact(PipelineStep.ROUTE_FAB)
        if isinstance(fab, FabAudit):
            drc += [f"fab:{v}" for v in fab.violations]
        write_art = state.artifact(PipelineStep.LAYOUT_WRITE)
        pcb_path = Path(write_art.pcb_path) if isinstance(
            write_art, PcbWriteResult
        ) else None
        cli = kicad_cli_available()
        drc += _routing_release_violations(state, pcb_path)
        if cli and pcb_path is not None and pcb_path.is_file():
            drc += _run_kicad_drc(
                cli,
                pcb_path,
                out_dir / f"{state.project_name}.drc.json",
            )

        # --- BOM (grouped-ish flat CSV from the selection) -----------------
        bom_path = out_dir / f"{state.project_name}_bom.csv"
        sel = state.artifact(PipelineStep.SELECTION)
        preparation = state.artifact(PipelineStep.COMPONENT_PREPARE)
        component_release_ready = (
            preparation.release_ready
            if isinstance(preparation, ComponentPrepareResult)
            else False
        )
        component_release_blockers = (
            list(preparation.release_blockers)
            if isinstance(preparation, ComponentPrepareResult)
            else ["component preparation report is unavailable"]
        )
        unresolved_manifest_path = out_dir / f"{state.project_name}_component_release.json"
        prepared_by_ref = (
            {component.ref: component for component in preparation.components}
            if isinstance(preparation, ComponentPrepareResult)
            else {}
        )
        if isinstance(sel, SelectionPlan):
            with bom_path.open("w", newline="", encoding="utf-8") as fh:
                wr = csv.writer(fh)
                wr.writerow([
                    "Reference", "Value", "Footprint", "MPN", "LCSC", "Provider",
                    "Provider Part ID", "Manufacturer", "Quantity", "Unit Price",
                    "Currency", "Package Match", "Selection Confidence",
                    "Catalog Snapshot", "Source URL", "Constraint Gaps",
                    "ReleaseReady", "DNP", "Resolution", "Unresolved", "Evidence",
                ])
                for part in sel.parts:
                    prepared_component = prepared_by_ref.get(part.ref)
                    wr.writerow([
                        part.ref, part.value, part.footprint, part.mpn, part.lcsc,
                        part.catalog_provider, part.provider_part_id, part.manufacturer,
                        part.quantity, part.unit_price, part.price_currency,
                        part.package_match, part.selection_confidence,
                        part.catalog_snapshot_id, part.catalog_source_url,
                        " | ".join(part.constraint_gaps),
                        "yes" if prepared_component and prepared_component.release_ready else "no",
                        "yes" if part.dnp else "no",
                        prepared_component.status if prepared_component else "unresolved",
                        "yes" if not prepared_component or prepared_component.unresolved else "no",
                        " | ".join(prepared_component.evidence)
                        if prepared_component else "",
                    ])
        if isinstance(preparation, ComponentPrepareResult):
            proofs = [component.model_dump() for component in preparation.components]
            unresolved_components = [
                {
                    "ref": component.ref,
                    "status": component.status,
                    "reason": "; ".join(component.blockers)
                    or "component closure is unresolved",
                    "evidence": component.evidence,
                }
                for component in preparation.components
                if not component.release_ready
            ]
            manifest = {
                "schema_version": COMPONENT_RELEASE_MANIFEST_SCHEMA,
                "release_policy": COMPONENT_RELEASE_POLICY,
                "release_ready": preparation.release_ready,
                "selection_component_count": len(preparation.components),
                "release_proven_component_count": sum(
                    component.release_ready for component in preparation.components
                ),
                "component_release_proofs": proofs,
                "unresolved_components": unresolved_components,
                "catalog_issues": preparation.catalog_issues,
                "notes": preparation.notes,
            }
            unresolved_manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        # --- CPL / pick-and-place (from the general placement) -------------
        cpl_path = out_dir / f"{state.project_name}_cpl.csv"
        plan = state.artifact(PipelineStep.LAYOUT_GENERAL)
        if isinstance(plan, PcbPlacementPlan):
            with cpl_path.open("w", newline="", encoding="utf-8") as fh:
                wr = csv.writer(fh)
                wr.writerow(["Designator", "Mid X", "Mid Y", "Rotation", "Layer"])
                for pp in plan.placements:
                    layer = "top" if pp.side == "front" else "bottom"
                    wr.writerow([pp.ref, pp.x, pp.y, pp.rotation, layer])

        # --- Gerber (best-effort via kicad-cli) ----------------------------
        gerber_dir = out_dir / "gerber"
        gerber_ok = False
        if cli and pcb_path is not None and pcb_path.is_file() and not drc:
            try:
                import subprocess

                gerber_dir.mkdir(parents=True, exist_ok=True)
                proc = subprocess.run(
                    [
                        cli,
                        "pcb",
                        "export",
                        "gerbers",
                        "--output",
                        str(gerber_dir),
                        str(pcb_path),
                    ],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
                gerber_ok = proc.returncode == 0 and any(gerber_dir.iterdir())
            except Exception:
                gerber_ok = False

        return (
            ManufactureResult(
                bom_path=str(bom_path) if isinstance(sel, SelectionPlan) else "",
                cpl_path=str(cpl_path) if isinstance(plan, PcbPlacementPlan) else "",
                unresolved_manifest_path=(
                    str(unresolved_manifest_path)
                    if isinstance(preparation, ComponentPrepareResult)
                    else ""
                ),
                component_release_ready=component_release_ready,
                component_release_blockers=component_release_blockers,
                gerber_dir=str(gerber_dir) if gerber_ok else "",
                gerber_exported=gerber_ok,
                drc_violations=drc,
            ),
            False,
        )

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, ManufactureResult)
        checks = [
            CheckResult(name="drc_clean", ok=not artifact.drc_violations,
                        message=f"DRC violations: {artifact.drc_violations}"),
            CheckResult(name="bom_written", ok=bool(artifact.bom_path),
                        message="BOM not written"),
            CheckResult(name="cpl_written", ok=bool(artifact.cpl_path),
                        message="CPL not written"),
            CheckResult(
                name="component_release_ready",
                ok=artifact.component_release_ready,
                severity=Severity.WARNING,
                message=(
                    "component release is not ready: "
                    f"{artifact.component_release_blockers or ['no release report']}"
                ),
            ),
        ]
        if not artifact.gerber_exported:
            checks.append(CheckResult(
                name="gerber_exported", ok=False, severity=Severity.WARNING,
                message="Gerber not exported (kicad-cli unavailable or export failed)",
            ))
        return checks

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, ManufactureResult)
        g = "gerber+" if artifact.gerber_exported else ""
        return f"{g}BOM+CPL written; {len(artifact.drc_violations)} DRC violations"


# Registered steps, in canonical order.
ALL_STEPS: list[PipelineStepBase] = [
    RequirementsStep(),
    TopologyStep(),
    SelectionStep(),
    ComponentPrepareStep(),
    SchConnectionsStep(),
    SchPinMapStep(),
    SchLayoutStep(),
    SchMaterializeStep(),
    ErcStep(),
    LayoutPartitionStep(),
    LayoutCriticalStep(),
    LayoutGeneralStep(),
    LayoutWriteStep(),
    RoutePlanStep(),
    RoutePlanesStep(),
    RouteSignalsStep(),
    RouteFabStep(),
    ManufactureStep(),
]

ARTIFACT_MODELS: dict[PipelineStep, type[BaseModel]] = {
    PipelineStep.REQUIREMENTS: RequirementSpec,
    PipelineStep.TOPOLOGY: TopologyPlan,
    PipelineStep.SELECTION: SelectionPlan,
    PipelineStep.COMPONENT_PREPARE: ComponentPrepareResult,
    PipelineStep.SCH_CONNECTIONS: NetlistIntent,
    PipelineStep.SCH_PINMAP: PinMapPlan,
    PipelineStep.SCH_LAYOUT: SchLayoutPlan,
    PipelineStep.SCH_MATERIALIZE: MaterializeResult,
    PipelineStep.ERC: ErcSummary,
    PipelineStep.LAYOUT_PARTITION: BoardPartition,
    PipelineStep.LAYOUT_CRITICAL: PcbPlacementPlan,
    PipelineStep.LAYOUT_GENERAL: PcbPlacementPlan,
    PipelineStep.LAYOUT_WRITE: PcbWriteResult,
    PipelineStep.ROUTE_PLAN: RoutePlan,
    PipelineStep.ROUTE_PLANES: PlanePlan,
    PipelineStep.ROUTE_SIGNALS: RouteResult,
    PipelineStep.ROUTE_FAB: FabAudit,
    PipelineStep.MANUFACTURE: ManufactureResult,
}


def restore_pipeline_state(
    *,
    requirement_text: str,
    project_name: str,
    intermediate_artifacts: dict[str, Any],
    steps: list[dict[str, Any]],
) -> PipelineState:
    """Restore the longest contiguous, non-blocked prefix from a checkpoint."""
    state = PipelineState(
        requirement_text=requirement_text,
        project_name=project_name,
    )
    for expected, saved in zip(CANONICAL_ORDER, steps, strict=False):
        if str(saved.get("name", "")) != expected.value or bool(saved.get("blocked")):
            break
        raw_artifact = intermediate_artifacts.get(expected.value)
        if raw_artifact is None:
            break
        artifact = ARTIFACT_MODELS[expected].model_validate(raw_artifact)
        state.artifacts[expected] = artifact
        state.results.append(StepResult(
            step=expected,
            used_llm=bool(saved.get("used_llm")),
            summary=str(saved.get("summary", "")),
        ))
    return state


# --------------------------------------------------------------------------- #
# The pipeline runner
# --------------------------------------------------------------------------- #


class PipelineOrderError(RuntimeError):
    """Raised when the registered steps are not a valid canonical prefix."""


class Pipeline:
    """Runs a contiguous prefix of the pinned step sequence, in order.

    Steps cannot be skipped or reordered: the registered list must equal the
    first N entries of :data:`CANONICAL_ORDER`. Execution stops at the first
    blocking step (fail closed) or after ``until`` (inclusive) if given.
    """

    def __init__(self, steps: list[PipelineStepBase] | None = None) -> None:
        self.steps = steps if steps is not None else ALL_STEPS
        self._validate_order()

    def _validate_order(self) -> None:
        for i, step in enumerate(self.steps):
            if CANONICAL_ORDER[i] != step.step:
                raise PipelineOrderError(
                    f"step {i} is {step.step!r}, expected {CANONICAL_ORDER[i]!r}; "
                    "steps must follow the fixed pipeline order without gaps"
                )

    def run(
        self,
        state: PipelineState,
        ctx: PipelineContext | None = None,
        until: PipelineStep | None = None,
    ) -> PipelineState:
        ctx = ctx or PipelineContext()
        limit = _ORDER_INDEX[until] if until is not None else len(self.steps) - 1
        completed = state.completed
        if completed != CANONICAL_ORDER[:len(completed)] or (
            state.blocked and not ctx.continue_on_blocked
        ):
            raise PipelineOrderError(
                "resumed state must contain a contiguous, non-blocked canonical prefix"
            )
        completed_set = set(completed)
        for step in self.steps:
            if _ORDER_INDEX[step.step] > limit:
                break
            if step.step in completed_set:
                continue
            if ctx.on_step_started is not None:
                ctx.on_step_started(state, step.step)
            try:
                result = step.run(state, ctx)
            except LlmError as exc:
                if not ctx.capture_step_errors:
                    raise
                result = StepResult(
                    step=step.step,
                    used_llm=True,
                    checks=[
                        CheckResult(
                            name="llm_proposal_failed",
                            ok=False,
                            message=str(exc),
                        ),
                    ],
                    blocked=True,
                    summary=f"LLM proposal failed: {exc}",
                )
                state.results.append(result)
            if ctx.on_step_completed is not None:
                ctx.on_step_completed(state, result)
            if result.blocked and not ctx.continue_on_blocked:
                break  # fail closed: do not advance past a blocked step
        return state
