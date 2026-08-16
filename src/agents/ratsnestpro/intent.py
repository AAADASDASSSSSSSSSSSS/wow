"""Structured intent routing for the RatsNestPro multi-agent workflow.

Replaces brittle keyword classification (`review` term + a file extension ->
`review`) with an explicit, evidence-carrying :class:`IntentDecision`. The core
fix is separating the *source artifact* a user asks the system to read from the
*requested output* a user asks the system to generate: a file extension that
appears only as an acceptance criterion is output evidence, never a review
trigger. See ``docs/Intent_Routing_and_AHE_EHE.md`` section 3.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Kept in lockstep with schema.WorkflowMode and the values consumed by the
# LangGraph route functions in ratsnestpro_agent.py.
WorkflowMode = Literal["build", "review", "research", "parts"]
PostAction = Literal["review", "manufacture", "export"]

_CLAUSE_SEPARATORS = ".!?。！？;\n"

# Negation primitives are the single source of truth for the whole agent: the
# MCU extractor in ratsnestpro_agent.py imports is_negated_mention from here.
_NEGATION_RE = re.compile(
    r"\b(?:not|never|without|instead\s+of|rather\s+than|"
    r"do\s+not|don't|must\s+not|forbid(?:den)?)"
    r"(?:\s+(?:use|using|choose|select|replace|design|generate|build|create))?\b|"
    r"(?:不要|不是|而非|禁止|不得|不能|不用|不允许)"
    r"(?:使用|采用|选用|替换(?:为)?|设计|生成|创建|制作)?",
    re.IGNORECASE,
)
_POSITIVE_SELECTION_RE = re.compile(
    r"\b(?:use|using|choose|select|must\s+be|required|replace\s+with)\b|"
    r"(?:主控(?:必须)?是|使用|采用|选用|改为)",
    re.IGNORECASE,
)

_CREATE_ACTION_RE = re.compile(
    r"\b(?:design\w*|generat\w*|build\w*|creat\w*|develop\w*|implement\w*|"
    r"fabricat\w*|produc\w*|re-?spin)\b|"
    r"(?:设计|生成|新建|新做|制作|制做|制板|布板|打样|出图|画板|做板|"
    r"做一[块版个张]|做一版|重新做|重做|重新设计)",
    re.IGNORECASE,
)
_BOARD_NOUN_RE = re.compile(
    r"\b(?:pcb|schematic|board|netlist|gerber|layout)\b|"
    r"(?:原理图|电路板|印制板|板子|布线|布局)",
    re.IGNORECASE,
)
_REVIEW_ACTION_RE = re.compile(
    r"\b(?:review\w*|audit\w*|inspect\w*|erc|drc)\b|"
    r"(?:审查|审核|复审|评审|检视|检查(?:工程|设计|原理图|电路|已有))",
    re.IGNORECASE,
)
_PARTS_RE = re.compile(
    r"\b(?:parts?|part\s*number|mpn|lcsc|jlcpcb|datasheet\s*lookup|"
    r"purchas\w*|procure\w*|in\s*stock)\b|"
    r"(?:器件|料号|选型|采购|可采购|购买|备货|库存)",
    re.IGNORECASE,
)
_RESEARCH_RE = re.compile(
    r"\b(?:research\w*|investigat\w*|study|studies|survey|explain|"
    r"understand|clarify\s+the\s+requirement)\b|"
    r"(?:研究|调研|了解|查阅|梳理|评估要求)",
    re.IGNORECASE,
)
# Explicit "do not build a PCB" style negation, a strong research signal.
_NEGATED_BUILD_RE = re.compile(
    r"(?:\b(?:do\s+not|don't|must\s+not)\b[^.\n]{0,30}"
    r"\b(?:design|generate|build|create)\b[^.\n]{0,40}\bpcb\b|"
    r"(?:不要|不得|禁止|无需|不需要)[^。；\n]{0,20}(?:设计|生成|创建|制作)"
    r"[^。；\n]{0,20}PCB)",
    re.IGNORECASE,
)

_PROJECT_EXT = r"(?:kicad_sch|kicad_pcb|kicad_pro|pro|net)"
_SOURCE_PATH_RES = (
    # Quoted path ending in a KiCad extension.
    re.compile(rf"[\"']([^\"']+\.{_PROJECT_EXT})[\"']", re.IGNORECASE),
    # Windows drive path ending in a KiCad extension.
    re.compile(rf"([a-zA-Z]:\\[^\r\n,;\"']+\.{_PROJECT_EXT})\b", re.IGNORECASE),
    # POSIX absolute path ending in a KiCad extension.
    re.compile(rf"(/[^\s\"',;]+\.{_PROJECT_EXT})\b", re.IGNORECASE),
    # Workspace-relative run/review reference (directory or file).
    re.compile(
        r"\b((?:runs|reviews)/[A-Za-z0-9_][A-Za-z0-9_./-]*)",
    ),
)

# Requested-output detection maps surface tokens to canonical artifact labels.
# These are *output* evidence only and must never trigger review by themselves.
_OUTPUT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "KiCad schematic",
        re.compile(r"\bkicad_sch\b|\bschematic\b|原理图", re.IGNORECASE),
    ),
    (
        "KiCad PCB",
        re.compile(r"\bkicad_pcb\b|\bpcb\b|电路板|印制板", re.IGNORECASE),
    ),
    ("DSN", re.compile(r"\bdsn\b", re.IGNORECASE)),
    ("SES", re.compile(r"\bses\b", re.IGNORECASE)),
    ("BOM", re.compile(r"\bbom\b|物料清单", re.IGNORECASE)),
    ("CPL", re.compile(r"\bcpl\b|贴装|坐标文件", re.IGNORECASE)),
    ("Gerber", re.compile(r"\bgerber\b", re.IGNORECASE)),
)
_MANUFACTURE_RE = re.compile(
    r"\b(?:gerber|bom|cpl|fabricat\w*|manufactur\w*|assembl\w*)\b|"
    r"(?:制造|生产|贴装|打样|投产)",
    re.IGNORECASE,
)
_EXPORT_RE = re.compile(
    r"\b(?:export\w*|gerber|deliver\w*|output\s+files)\b|(?:导出|交付|出图)",
    re.IGNORECASE,
)


def is_negated_mention(text: str, start: int) -> bool:
    """Return True when the token at ``start`` sits under an unfulfilled negation.

    A trailing positive-selection cue ("use", "主控必须是") cancels an earlier
    negation, so "禁止替换为其他 STM32，主控必须是 STM32F405" is not negated.
    """
    clause_start = max(
        (text.rfind(separator, 0, start) for separator in _CLAUSE_SEPARATORS),
        default=-1,
    )
    prefix = text[clause_start + 1 : start]
    negations = list(_NEGATION_RE.finditer(prefix))
    if not negations:
        return False
    positives = list(_POSITIVE_SELECTION_RE.finditer(prefix))
    last_negation = negations[-1]
    return not any(positive.start() >= last_negation.end() for positive in positives)


def _has_positive_action(text: str, pattern: re.Pattern[str]) -> bool:
    """True if ``pattern`` matches at least once outside a negation clause."""
    return any(not is_negated_mention(text, match.start()) for match in pattern.finditer(text))


def extract_source_project_path(text: str) -> str | None:
    """Extract a path to an *existing* project the user asks the system to read.

    Only concrete paths count: quoted, Windows-drive, POSIX-absolute, or a
    workspace-relative ``runs/``/``reviews/`` reference. A bare file extension
    inside an acceptance criterion (e.g. "produce a .kicad_pcb file") is a
    requested output and is intentionally not matched here.
    """
    for pattern in _SOURCE_PATH_RES:
        match = pattern.search(text)
        if match:
            return match.group(1).strip().rstrip(".,;")
    return None


def detect_requested_outputs(text: str) -> list[str]:
    """Canonical list of output artifacts the request asks to be generated."""
    return [label for label, pattern in _OUTPUT_PATTERNS if pattern.search(text)]


class IntentDecision(BaseModel):
    """Structured routing decision carrying its own evidence and gates."""

    model_config = ConfigDict(extra="forbid")

    primary_intent: WorkflowMode
    post_actions: list[PostAction] = Field(default_factory=list)
    source_project_path: str | None = None
    requested_outputs: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification: str | None = None


@dataclass
class ParsedRequest:
    """Deterministic features extracted from a natural-language requirement."""

    text: str
    explicit_mode: WorkflowMode | None = None
    has_create_action: bool = False
    requests_generated_pcb: bool = False
    has_review_action: bool = False
    has_research_action: bool = False
    is_negated_build: bool = False
    source_project_path: str | None = None
    requested_outputs: list[str] = field(default_factory=list)
    post_actions: list[PostAction] = field(default_factory=list)
    parts_hit: bool = False
    has_positive_mcu: bool = False

    @property
    def is_parts_only(self) -> bool:
        return (
            self.parts_hit
            and not self.has_create_action
            and not self.requests_generated_pcb
            and not self.has_review_action
        )


def _derive_post_actions(text: str, *, has_review_action: bool) -> list[PostAction]:
    actions: list[PostAction] = []
    if has_review_action:
        actions.append("review")
    if _MANUFACTURE_RE.search(text):
        actions.append("manufacture")
    if _EXPORT_RE.search(text):
        actions.append("export")
    return list(dict.fromkeys(actions))


def parse_request(
    requirement: str,
    *,
    explicit_mode: WorkflowMode | None = None,
    config_project_path: str | None = None,
    has_positive_mcu: bool = False,
) -> ParsedRequest:
    """Extract deterministic routing features from a requirement string."""
    text = requirement or ""
    has_create_action = _has_positive_action(text, _CREATE_ACTION_RE)
    has_review_action = _has_positive_action(text, _REVIEW_ACTION_RE)
    source_project_path = config_project_path or extract_source_project_path(text)
    # Board-noun detection ignores the source path so a "pcb" inside a path such
    # as runs/demo-pcb is not mistaken for a request to generate a new board.
    board_text = text.replace(source_project_path, " ") if source_project_path else text
    requests_generated_pcb = has_create_action and bool(_BOARD_NOUN_RE.search(board_text))
    return ParsedRequest(
        text=text,
        explicit_mode=explicit_mode,
        has_create_action=has_create_action,
        requests_generated_pcb=requests_generated_pcb,
        has_review_action=has_review_action,
        has_research_action=_has_positive_action(text, _RESEARCH_RE),
        is_negated_build=bool(_NEGATED_BUILD_RE.search(text)),
        source_project_path=source_project_path,
        requested_outputs=detect_requested_outputs(text),
        post_actions=_derive_post_actions(text, has_review_action=has_review_action),
        parts_hit=bool(_PARTS_RE.search(text)),
        has_positive_mcu=has_positive_mcu,
    )


_CLARIFY_REVIEW = (
    "This looks like a review request, but no existing KiCad project path or "
    "attachment was provided. Please supply the project path to review, or "
    "confirm this is a new build."
)


def _build_decision(parsed: ParsedRequest, *, evidence: list[str]) -> IntentDecision:
    return IntentDecision(
        primary_intent="build",
        post_actions=parsed.post_actions,
        source_project_path=parsed.source_project_path,
        requested_outputs=parsed.requested_outputs,
        confidence=0.95,
        evidence=evidence,
    )


def _review_decision(parsed: ParsedRequest, *, evidence: list[str]) -> IntentDecision:
    return IntentDecision(
        primary_intent="review",
        source_project_path=parsed.source_project_path,
        requested_outputs=parsed.requested_outputs,
        confidence=0.92,
        evidence=evidence,
    )


def _parts_decision(parsed: ParsedRequest) -> IntentDecision:
    return IntentDecision(
        primary_intent="parts",
        confidence=0.9,
        evidence=["Only a grounded part lookup was requested."],
    )


def _research_decision(
    parsed: ParsedRequest, *, evidence: list[str], confidence: float
) -> IntentDecision:
    return IntentDecision(
        primary_intent="research",
        source_project_path=parsed.source_project_path,
        confidence=confidence,
        evidence=evidence,
    )


def _clarify_decision(
    parsed: ParsedRequest,
    *,
    best_guess: WorkflowMode,
    message: str,
    evidence: list[str],
) -> IntentDecision:
    return IntentDecision(
        primary_intent=best_guess,
        source_project_path=parsed.source_project_path,
        requested_outputs=parsed.requested_outputs,
        confidence=0.3,
        evidence=evidence,
        needs_clarification=True,
        clarification=message,
    )


def _apply_review_gate(parsed: ParsedRequest, *, evidence: list[str]) -> IntentDecision:
    """Section 3.5 gate: a review intent needs a resolvable source project path.

    Missing path -> prefer a new build when a create action exists, otherwise
    ask the user to clarify rather than calling the Reviewer with no project.
    """
    if parsed.source_project_path:
        return _review_decision(parsed, evidence=evidence)
    if parsed.has_create_action or parsed.requests_generated_pcb:
        return _build_decision(
            parsed,
            evidence=[
                *evidence,
                "Review requested without a source project path, but a new-build "
                "action is present; routing to build.",
            ],
        )
    return _clarify_decision(
        parsed,
        best_guess="review",
        message=_CLARIFY_REVIEW,
        evidence=[*evidence, "Review action present but no source project path."],
    )


def classify_intent(
    parsed: ParsedRequest,
    *,
    llm_classifier: Callable[[ParsedRequest], IntentDecision] | None = None,
) -> IntentDecision:
    """Classify a parsed request using the doc's priority order.

    explicit API mode > high-confidence deterministic rules > LLM structured
    classification (genuine ambiguity only) > safe research default. The review
    parameter gate is applied to explicit and LLM decisions too, so the Reviewer
    is never invoked without a source project.
    """
    # 1. Explicit API workflow_mode (highest priority), still gated for review.
    if parsed.explicit_mode is not None:
        evidence = [f"Explicit workflow_mode API field: {parsed.explicit_mode}."]
        if parsed.explicit_mode == "review":
            return _apply_review_gate(parsed, evidence=evidence)
        if parsed.explicit_mode == "build":
            return IntentDecision(
                primary_intent="build",
                post_actions=parsed.post_actions,
                source_project_path=parsed.source_project_path,
                requested_outputs=parsed.requested_outputs,
                confidence=1.0,
                evidence=evidence,
            )
        if parsed.explicit_mode == "parts":
            return IntentDecision(primary_intent="parts", confidence=1.0, evidence=evidence)
        return _research_decision(parsed, evidence=evidence, confidence=1.0)

    # 2. A create action targeting a board = wants a NEW board -> build. This
    #    dominates review/ERC/DRC noise so "build then review" routes to build.
    if parsed.requests_generated_pcb:
        return _build_decision(
            parsed,
            evidence=[
                "A new-board design/generate action is present; review, ERC and "
                "DRC mentions are post-build acceptance actions, not the primary "
                "intent."
            ],
        )
    # 3. Review of an existing project: a review action plus a resolvable source
    #    path. A "generate report" create verb here is part of reviewing.
    if parsed.has_review_action and parsed.source_project_path:
        return _review_decision(
            parsed,
            evidence=[
                "A review action targets an existing project path; any generate "
                "action refers to the review output, not a new board."
            ],
        )
    # 4. A create action with no board noun and no competing review+path -> build.
    if parsed.has_create_action:
        return _build_decision(
            parsed,
            evidence=["A create/design action is present with no source project."],
        )
    # 5. Review action without a resolvable source path -> parameter gate.
    if parsed.has_review_action:
        return _apply_review_gate(
            parsed,
            evidence=["A review action is present with no new-build action."],
        )
    if parsed.is_parts_only:
        return _parts_decision(parsed)
    if parsed.is_negated_build or parsed.has_research_action:
        return _research_decision(
            parsed,
            evidence=["Research/advisory request with no build or review action."],
            confidence=0.85,
        )

    # 3. Genuine ambiguity -> optional LLM structured classification, gated.
    if llm_classifier is not None:
        try:
            decision = llm_classifier(parsed)
        except Exception:  # noqa: BLE001 - model boundary; fall back deterministically
            decision = None
        if decision is not None:
            if decision.primary_intent == "review":
                return _apply_review_gate(
                    parsed,
                    evidence=[*decision.evidence, "LLM structured classification."],
                )
            return decision

    # 4. Safe default: advisory research rather than an unverifiable build/review.
    return _research_decision(
        parsed,
        evidence=["No high-confidence intent signal; defaulting to research."],
        confidence=0.4,
    )


def classify_requirement(
    requirement: str,
    *,
    explicit_mode: WorkflowMode | None = None,
    config_project_path: str | None = None,
    has_positive_mcu: bool = False,
    llm_classifier: Callable[[ParsedRequest], IntentDecision] | None = None,
) -> IntentDecision:
    """Convenience: parse then classify a raw requirement in one call."""
    parsed = parse_request(
        requirement,
        explicit_mode=explicit_mode,
        config_project_path=config_project_path,
        has_positive_mcu=has_positive_mcu,
    )
    return classify_intent(parsed, llm_classifier=llm_classifier)
