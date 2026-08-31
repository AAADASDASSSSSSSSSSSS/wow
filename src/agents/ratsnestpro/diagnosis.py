"""Failure diagnosis for the AHE in-task repair loop.

Turns a blocked pipeline step, a tool error, or a review verdict into a typed
:class:`Diagnosis` carrying a failure class from the documented taxonomy and the
strategy that class allows. This is what replaces "blocked -> stop": a
recoverable class yields a repair strategy, a capability gap is recorded for the
EHE outer loop, and a genuine user-constraint violation stays honestly blocked.

See ``docs/Intent_Routing_and_AHE_EHE.md`` section 4.3.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

FailureClass = Literal[
    "constraint_violation",
    "missing_component",
    "missing_support_network",
    "symbol_unavailable",
    "symbol_mismatch",
    "footprint_mismatch",
    "pin_conflict",
    "erc_violation",
    "tool_unavailable",
    "transient_external_failure",
    "routing_congestion",
    "manufacturing_violation",
    "harness_defect",
    "unknown",
]

RepairStrategy = Literal[
    "retry_with_alternate_source",
    "extend_selection",
    "acquire_symbol",
    "reassign_pins",
    "adjust_layout_or_routing",
    "fix_flagged_object",
    "record_capability_gap",
    "block_honestly",
]

# Strategy allowed per failure class (doc section 4.3 table).
_STRATEGY: dict[FailureClass, RepairStrategy] = {
    "constraint_violation": "block_honestly",
    "missing_component": "extend_selection",
    "missing_support_network": "extend_selection",
    "symbol_unavailable": "acquire_symbol",
    "symbol_mismatch": "acquire_symbol",
    "footprint_mismatch": "acquire_symbol",
    "pin_conflict": "reassign_pins",
    "erc_violation": "fix_flagged_object",
    "tool_unavailable": "record_capability_gap",
    "transient_external_failure": "retry_with_alternate_source",
    "routing_congestion": "adjust_layout_or_routing",
    "manufacturing_violation": "fix_flagged_object",
    "harness_defect": "record_capability_gap",
    "unknown": "record_capability_gap",
}

# Which pipeline step owns the repair for a class, so a patch can re-enter the
# flow at the right place instead of restarting from step 1.
_REPAIR_SCOPE: dict[FailureClass, str] = {
    "missing_component": "selection",
    "missing_support_network": "selection",
    "symbol_unavailable": "selection",
    "symbol_mismatch": "selection",
    "footprint_mismatch": "selection",
    "pin_conflict": "schematic_pinmap",
    # ERC failures are connectivity facts, so the connections step owns the fix.
    "erc_violation": "schematic_connections",
    "routing_congestion": "layout_general",
    "manufacturing_violation": "route_fab",
    "transient_external_failure": "requirements",
    "constraint_violation": "",
    "tool_unavailable": "",
    "harness_defect": "",
    "unknown": "",
}

_CLASS_PATTERNS: tuple[tuple[FailureClass, re.Pattern[str]], ...] = (
    (
        "pin_conflict",
        re.compile(
            r"belongs to both|pin conflict|duplicate pin|assigned twice|"
            r"two different nets|unresolved logical pin|unresolved pin",
            re.IGNORECASE,
        ),
    ),
    (
        "footprint_mismatch",
        re.compile(
            r"pin_pad_compatibility|symbol pins do not match|pads do not match|"
            r"footprint .*not found|unknown footprint|pad count",
            re.IGNORECASE,
        ),
    ),
    (
        "symbol_mismatch",
        re.compile(
            r"does not satisfy|order code .*mismatch|wrong pin count|"
            r"package .*requires \d+|identity check failed|requested mcu .*not",
            re.IGNORECASE,
        ),
    ),
    (
        "symbol_unavailable",
        re.compile(
            r"symbol .*(?:not found|unavailable|missing)|no such symbol|"
            r"unknown symbol|no grounded kicad symbol|no_results",
            re.IGNORECASE,
        ),
    ),
    (
        "missing_support_network",
        re.compile(
            r"missing\b[^.\n]{0,48}?\b(?:support|pull-?ups?|decoupling|capacitors?|"
            r"inductors?|termination|bootstrap|feedback|compensation|esd|tvs|"
            r"clamp|divider|filter)|"
            r"support network|obligation[^.\n]{0,40}not satisfied",
            re.IGNORECASE,
        ),
    ),
    (
        "missing_component",
        re.compile(
            r"missing\b[^.\n]{0,48}?\b(?:component|part|block|transceiver|phy|"
            r"regulator|sensor|connector|mcu)|"
            r"capability[^.\n]{0,40}not implemented|block[^.\n]{0,40}not implemented|"
            r"required_capability",
            re.IGNORECASE,
        ),
    ),
    (
        "erc_violation",
        re.compile(
            r"\berc\b[^.\n]{0,40}\b(?:reported|found)\b[^.\n]{0,20}\d+\s*error|"
            r"\berc\b[^.\n]{0,20}=\s*\d+\s*err|"
            r"\d+\s*erc\s*errors?",
            re.IGNORECASE,
        ),
    ),
    (
        "routing_congestion",
        re.compile(
            r"congest|unrouted|could not route|routing failed|"
            r"unconnected (?:items?|count) is not zero|freerouting did not complete",
            re.IGNORECASE,
        ),
    ),
    (
        "manufacturing_violation",
        re.compile(
            r"below (?:minimum|process)|clearance|track width|via (?:drill|diameter)|"
            r"annular ring|board edge|silkscreen|drc reported|fab audit",
            re.IGNORECASE,
        ),
    ),
    (
        "tool_unavailable",
        re.compile(
            r"unavailable|not installed|not found on path|kicad-cli .*unavailable|"
            r"freerouting .*(?:missing|unavailable)|no local jlcpcb cache",
            re.IGNORECASE,
        ),
    ),
    (
        "transient_external_failure",
        re.compile(
            r"timeout|timed out|temporarily|rate limit|connection (?:reset|refused|"
            r"error)|502|503|504|empty (?:response|result)|returned empty",
            re.IGNORECASE,
        ),
    ),
    (
        "constraint_violation",
        re.compile(
            r"substitution=forbidden|forbidden substitution|user constraint|"
            r"hard constraint|contract .*rejected|violates the .*contract",
            re.IGNORECASE,
        ),
    ),
    (
        "harness_defect",
        re.compile(
            r"traceback|internal error|unexpected keyword|attributeerror|"
            r"typeerror|keyerror|notimplemented",
            re.IGNORECASE,
        ),
    ),
)


@dataclass
class Diagnosis:
    """One classified failure plus the action it authorises."""

    failure_class: FailureClass
    strategy: RepairStrategy
    repair_scope: str = ""
    summary: str = ""
    evidence: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()

    @property
    def recoverable(self) -> bool:
        """Whether AHE may attempt an in-task repair for this failure."""
        return self.strategy not in {"block_honestly", "record_capability_gap"}

    @property
    def capability_gap(self) -> bool:
        """Whether this failure should be handed to the EHE outer loop."""
        return self.strategy == "record_capability_gap"

    def to_dict(self) -> dict[str, object]:
        return {
            "failure_class": self.failure_class,
            "strategy": self.strategy,
            "repair_scope": self.repair_scope,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "targets": list(self.targets),
            "recoverable": self.recoverable,
            "capability_gap": self.capability_gap,
        }


_TARGET_REF_RE = re.compile(r"\b([UJDRCLQYFK]\d{1,3})\b")

# Aggregate blockers restate the consequence of an earlier root cause ("the
# pipeline did not finish"). Diagnosing them again adds noise and would record a
# bogus capability gap, so they are skipped in favour of the real failure.
_DERIVED_BLOCKER_RE = re.compile(
    r"(?:17|18)-step pipeline did not complete|pipeline stopped before|"
    r"no actual (?:\w+ )*\.(?:kicad_sch|kicad_pcb|dsn|ses) artifact|"
    r"routing unconnected count is not zero",
    re.IGNORECASE,
)


def is_derived_blocker(message: str) -> bool:
    """True for aggregate blockers that merely restate an earlier failure."""
    return bool(_DERIVED_BLOCKER_RE.search(message or ""))


def _targets_in(text: str) -> tuple[str, ...]:
    """Component references named by a failure message."""
    return tuple(dict.fromkeys(_TARGET_REF_RE.findall(text)))


def _as_class(value: str) -> FailureClass | None:
    """A declared class name, validated against the taxonomy.

    An unrecognised string is discarded rather than trusted: a payload from a
    newer engine could name a class this diagnoser has no strategy for, and
    guessing a strategy is worse than falling back to inference.
    """
    return value if value in _STRATEGY else None  # type: ignore[return-value]


def _declared_class(check_name: str) -> FailureClass | None:
    """The class the pipeline declared for this check, if it declared one.

    The pipeline owns the mapping (``ratsnestpro.orchestration.check_classes``)
    because that is where the checks are written. Import failures are swallowed:
    the diagnosis layer must keep working against a payload produced by an older
    engine, and inference remains as the fallback.
    """
    if not check_name:
        return None
    try:
        from ratsnestpro.orchestration.check_classes import failure_class_for
    except Exception:  # noqa: BLE001 - older engine, or engine not importable
        return None
    declared = failure_class_for(check_name)
    return declared if declared in _STRATEGY else None  # type: ignore[return-value]


def classify_failure(
    message: str,
    *,
    step: str = "",
    check_name: str = "",
    declared_class: str = "",
    targets: tuple[str, ...] | list[str] | None = None,
) -> Diagnosis:
    """Classify one failure message into the taxonomy.

    A class declared by the pipeline wins outright: the engine knew what kind of
    failure it was raising, so re-deriving it from the prose can only lose
    information. ``declared_class`` is that declaration when it arrives on the
    payload; otherwise the table is consulted for ``check_name``. Inference from
    the message is the last resort, for blockers that are not per-check and for
    payloads written by an engine that predates the declaration.

    ``targets`` is the same story one level down. The engine knows which objects
    a check was about; :func:`_targets_in` can only find single-letter-prefixed
    references in prose, so it misses ``FB1``, ``MH1`` and every net name. A
    declared list is used verbatim and an empty one falls back to the regex.

    Ordering within the fallback matters: specific structural failures are tested
    before the generic "unavailable"/"timeout" wording that also appears inside
    richer messages.
    """
    text = message or ""
    found = tuple(targets) if targets else _targets_in(text)
    declared = _as_class(declared_class) or _declared_class(check_name)
    if declared is not None:
        return Diagnosis(
            failure_class=declared,
            strategy=_STRATEGY[declared],
            repair_scope=_REPAIR_SCOPE.get(declared, "") or step,
            summary=" ".join(text.split())[:400],
            evidence=(text[:600],) if text else (),
            targets=found,
        )
    for failure_class, pattern in _CLASS_PATTERNS:
        if pattern.search(text):
            return Diagnosis(
                failure_class=failure_class,
                strategy=_STRATEGY[failure_class],
                repair_scope=_REPAIR_SCOPE.get(failure_class, "") or step,
                summary=" ".join(text.split())[:400],
                evidence=(text[:600],) if text else (),
                targets=found,
            )
    return Diagnosis(
        failure_class="unknown",
        strategy=_STRATEGY["unknown"],
        repair_scope=step,
        summary=" ".join(text.split())[:400],
        evidence=(text[:600],) if text else (),
        targets=found,
    )


@dataclass
class DiagnosisReport:
    """All diagnoses for one blocked attempt, plus the resulting plan."""

    diagnoses: list[Diagnosis] = field(default_factory=list)

    @property
    def recoverable(self) -> list[Diagnosis]:
        return [d for d in self.diagnoses if d.recoverable]

    @property
    def capability_gaps(self) -> list[Diagnosis]:
        return [d for d in self.diagnoses if d.capability_gap]

    @property
    def blocking(self) -> list[Diagnosis]:
        return [d for d in self.diagnoses if d.strategy == "block_honestly"]

    @property
    def should_attempt_repair(self) -> bool:
        """Repair only when something is recoverable and nothing is a hard block."""
        return bool(self.recoverable) and not self.blocking

    def primary_scope(self) -> str:
        """Earliest pipeline scope that must be re-entered."""
        order = [
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
        ]
        scopes = [d.repair_scope for d in self.recoverable if d.repair_scope in order]
        if not scopes:
            return ""
        return min(scopes, key=order.index)

    def to_dict(self) -> dict[str, object]:
        return {
            "diagnoses": [d.to_dict() for d in self.diagnoses],
            "should_attempt_repair": self.should_attempt_repair,
            "primary_scope": self.primary_scope(),
            "capability_gaps": [d.failure_class for d in self.capability_gaps],
        }


class FailureDiagnoser:
    """Builds a :class:`DiagnosisReport` from real pipeline/tool evidence."""

    def diagnose_pipeline_result(self, result: dict[str, object]) -> DiagnosisReport:
        """Diagnose one ``ratsnest_run_pcb_pipeline`` result payload."""
        report = DiagnosisReport()
        steps = result.get("steps")
        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, dict) or not step.get("blocked"):
                    continue
                step_name = str(step.get("name", ""))
                checks = step.get("failed_checks")
                if isinstance(checks, list) and checks:
                    for check in checks:
                        if not isinstance(check, dict):
                            continue
                        name = str(check.get("name", ""))
                        text = f"{name}: {check.get('message', '')}"
                        raw_targets = check.get("targets")
                        report.diagnoses.append(
                            classify_failure(
                                text,
                                step=step_name,
                                check_name=name,
                                declared_class=str(check.get("failure_class", "")),
                                targets=(
                                    [str(t) for t in raw_targets if t]
                                    if isinstance(raw_targets, list)
                                    else None
                                ),
                            )
                        )
                else:
                    report.diagnoses.append(
                        classify_failure(str(step.get("summary", "")), step=step_name)
                    )
        for blocker in self._string_list(result.get("release_blockers")):
            if is_derived_blocker(blocker):
                continue
            report.diagnoses.append(classify_failure(blocker))
        for blocker in self._string_list(result.get("verification_blockers")):
            if is_derived_blocker(blocker):
                continue
            report.diagnoses.append(classify_failure(blocker))
        error = result.get("error")
        if isinstance(error, str) and error:
            report.diagnoses.append(classify_failure(error))
        return report

    def diagnose_messages(self, messages: list[str], *, step: str = "") -> DiagnosisReport:
        return DiagnosisReport(
            [classify_failure(message, step=step) for message in messages if message]
        )

    @staticmethod
    def _string_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if item]
