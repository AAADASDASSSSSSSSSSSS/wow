"""Gate-driven LangGraph workflow for the RatsNestPro multi-agent system."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, MessagesState, StateGraph
from ratsnestpro.eda import factclaim
from ratsnestpro.eda.factclaim import ACK_PREFIX
from ratsnestpro.eda.factsheet import fact_sheets_named

from agents.language import language_directive, localized, reply_language
from agents.ratsnestpro import local_evidence
from agents.ratsnestpro.capability import (
    ConstraintSet,
    acquire_symbol,
    evaluate_coverage,
    required_capabilities,
    resolve_parts,
    resolve_primary_mcu,
)
from agents.ratsnestpro.decisions import (
    OpenDecision,
    accepted_tokens,
    apply_decisions,
    assumption_decisions,
    from_state,
    intent_decisions,
    ledger,
    parse_picks,
    payload_block,
    pick_token,
    render,
    resolve,
    risk_decisions,
    to_state,
)
from agents.ratsnestpro.diagnosis import FailureDiagnoser
from agents.ratsnestpro.evidence import (
    compare_signatures,
    digest_from_pipeline_result,
)
from agents.ratsnestpro.gaps import merge_assumptions, requirement_gaps
from agents.ratsnestpro.intent import (
    IntentDecision,
    ParsedRequest,
    WorkflowMode,
    classify_intent,
    parse_request,
)
from agents.ratsnestpro.repair import (
    RepairPatch,
    RepairPreconditions,
    evaluate_change,
    plan_repair,
    repair_regressed,
    resume_plan,
)
from agents.ratsnestpro.tools import (
    ratsnest_lookup_kicad_symbol,
    ratsnest_review_kicad_project,
    ratsnest_run_pcb_pipeline,
    ratsnest_search_parts,
)
from agents.ratsnestpro.web_tools import fetch_datasheet, web_search
from core import get_model, settings

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_.-]+")

# Marks the amendment appended in :func:`initialize`. Model-facing, so it names
# what the text is rather than which language it arrived in; the reply language is
# resolved separately.
_FOLLOW_UP_PREFIX = (
    "Follow-up from the user — answers to the open decisions above, "
    "and/or amendments to this requirement: "
)
_NAME_LABELS = {
    "run_name": ("run_name", "run name"),
    "project_name": ("project_name", "project name", "项目名称", "项目名"),
}


class RatsNestWorkflowState(MessagesState, total=False):
    requirement: str
    # The design request itself, held separately from ``requirement`` because a
    # risk acknowledgement arrives as its own turn ("I know, use 5 V anyway") and
    # would otherwise REPLACE the request it was answering.
    base_requirement: str
    claim_verdicts: list[dict[str, Any]]
    pending_acks: list[str]
    accepted_acks: list[str]
    # What the system does not know, shaped as options. Written by the clarify
    # nodes, consumed by the next turn's ``initialize``: holding it as data (not
    # as prose in a chat message) is what lets a reply be validated against the
    # options actually offered, and lets the report list what is still open.
    open_decisions: list[dict[str, Any]]
    resolved_decisions: list[dict[str, Any]]
    # Assumed values are offered for confirmation once per request. Without this
    # latch an unanswered menu would be re-offered on every pass and no board
    # would ever be built.
    missing_data_asked: bool
    workflow_mode: WorkflowMode
    reply_language: str
    intent: dict[str, Any]
    component_constraints: list[dict[str, Any]]
    capability: dict[str, Any]
    diagnosis: dict[str, Any]
    repair_patches: list[dict[str, Any]]
    verification_digest: str
    verification_signatures: list[str]
    change_evaluations: list[dict[str, Any]]
    run_name: str
    project_name: str
    architecture: dict[str, Any]
    parts: dict[str, Any]
    hardware: dict[str, Any]
    hardware_attempts: list[dict[str, Any]]
    review: dict[str, Any]
    review_round: int
    max_review_rounds: int
    review_target: str
    trace: list[dict[str, Any]]


_TRANSIENT_TOOL_STATUSES = {
    "error",
    "temporarily_unavailable",
    "timeout",
    "internal_error",
}


def _workflow_event(
    phase: str,
    status: str,
    *,
    detail: str = "",
    attempt: int | None = None,
) -> None:
    """Emit a stable phase event without coupling the graph to one frontend."""
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    event: dict[str, Any] = {
        "kind": "workflow_event",
        "phase": phase,
        "status": status,
    }
    if detail:
        event["detail"] = detail
    if attempt is not None:
        event["attempt"] = attempt
    writer(event)


async def _call_json_with_retry(
    operation: Callable[[], str],
    *,
    phase: str,
    tool: str,
    attempts: int = 2,
    require_nonempty: str | None = None,
) -> tuple[str, dict[str, Any], int]:
    """Retry only transient/empty tool outcomes; preserve the last evidence."""
    last_raw = ""
    last_result: dict[str, Any] = {"status": "error", "error": "not executed"}
    bounded_attempts = max(1, min(attempts, 3))
    for attempt in range(1, bounded_attempts + 1):
        try:
            last_raw = await asyncio.to_thread(operation)
        except Exception as exc:  # noqa: BLE001 - external tool boundary
            last_raw = json.dumps(
                {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        last_result = _json_object(last_raw)
        status = str(last_result.get("status", "error"))
        empty = require_nonempty is not None and not last_result.get(require_nonempty)
        transient = status in _TRANSIENT_TOOL_STATUSES or empty
        if not transient or attempt == bounded_attempts:
            return last_raw, last_result, attempt
        _workflow_event(
            phase,
            "retrying",
            detail=f"{tool} returned {status or 'empty'}",
            attempt=attempt + 1,
        )
        await asyncio.sleep(0.25 * (2 ** (attempt - 1)))
    return last_raw, last_result, bounded_attempts


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
    return str(content)


def _latest_requirement(state: RatsNestWorkflowState) -> str:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            return _message_text(message)
    return ""


def _safe_name(value: str, fallback: str) -> str:
    cleaned = _SAFE_NAME.sub("-", value.strip()).strip(".-")
    return cleaned[:80] or fallback


def _reply_language(state: RatsNestWorkflowState) -> str:
    """Read the language resolved once by `initialize`.

    Resolving it per phase would let the report headings disagree with the
    Architect narrative when a run mixes languages, so every phase reads the same
    value and only falls back to detection if the field is missing (a state dict
    assembled by a unit test, for example).
    """
    stored = state.get("reply_language")
    if isinstance(stored, str) and stored:
        return stored
    return reply_language(state.get("messages"))


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _positive_mcu_mentions(requirement: str) -> list[str]:
    """Grounded part tokens the requirement asks for, best candidate first."""
    return [part.token for part in resolve_parts(requirement)]


def _primary_mcu_mention(requirement: str) -> str:
    """The MCU order code the requirement pins, resolved open-world."""
    resolved = resolve_primary_mcu(requirement)
    return resolved.token if resolved else ""


def _configured_name_pattern(key: str) -> re.Pattern[str]:
    labels = _NAME_LABELS.get(key, (key,))
    label_pattern = "|".join(re.escape(label) for label in labels)
    return re.compile(
        rf"(?<![A-Za-z0-9_])(?:{label_pattern})(?![A-Za-z0-9_])"
        r"\s*(?:(?:使用|use)\s*)?(?:=|:|：|为\s*[:：]?)"
        r"\s*[\"']?\s*([a-zA-Z0-9_.-]+)",
        re.IGNORECASE,
    )


def _configured_name(
    requirement: str,
    config: RunnableConfig,
    key: str,
    fallback: str,
) -> str:
    configured = config.get("configurable", {}).get(key)
    if configured:
        return _safe_name(str(configured), fallback)
    match = _configured_name_pattern(key).search(requirement)
    return _safe_name(match.group(1), fallback) if match else fallback


def _append_trace(
    state: RatsNestWorkflowState,
    *,
    agent: str,
    tool: str,
    status: str,
    evidence: str = "",
) -> list[dict[str, Any]]:
    return [
        *state.get("trace", []),
        {
            "agent": agent,
            "tool": tool,
            "status": status,
            "evidence": evidence,
        },
    ]


def _tool_messages(name: str, args: dict[str, Any], result: str) -> list[Any]:
    call_id = str(uuid4())
    return [
        AIMessage(
            content=f"Executing required tool: {name}",
            tool_calls=[
                {
                    "name": name,
                    "args": args,
                    "id": call_id,
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content=result, tool_call_id=call_id),
    ]


def _json_object(raw: str, fallback_status: str = "error") -> dict[str, Any]:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"status": fallback_status}
    except (json.JSONDecodeError, TypeError):
        return {"status": fallback_status, "error": "tool returned invalid JSON"}


_VALID_WORKFLOW_MODES: tuple[WorkflowMode, ...] = (
    "build",
    "review",
    "research",
    "parts",
)

_INTENT_SYSTEM_PROMPT = (
    "You are the RatsNestPro intent router. Classify one hardware request into a "
    "single primary_intent: 'build' (design or generate a new board/artifacts), "
    "'review' (audit an EXISTING project whose path the user supplied), 'parts' "
    "(only look up purchasable components), or 'research' (advisory information "
    "only, generate no files). A request that generates a new board and also asks "
    "for review/ERC/DRC is 'build' with those as post_actions. Reply with ONLY a "
    "compact JSON object with keys primary_intent, post_actions (subset of "
    "['review','manufacture','export']), source_project_path (string or null), "
    "requested_outputs (list of strings), confidence (0..1), evidence (list of "
    "short strings), needs_clarification (bool). No prose, no code fences."
)


def _explicit_workflow_mode(configurable: dict[str, Any]) -> WorkflowMode | None:
    value = configurable.get("workflow_mode")
    if isinstance(value, str) and value in _VALID_WORKFLOW_MODES:
        return value  # type: ignore[return-value]
    return None


def _llm_intent_classifier(
    config: RunnableConfig,
) -> Callable[[ParsedRequest], IntentDecision]:
    """Best-effort structured LLM classifier used only for genuine ambiguity.

    classify_intent invokes this solely when deterministic rules are
    inconclusive, and wraps it so any failure falls back to a safe default.
    """
    selected_model = config.get("configurable", {}).get("model", settings.DEFAULT_MODEL)

    def _classify(parsed: ParsedRequest) -> IntentDecision:
        response = (
            get_model(selected_model)
            .with_config(tags=["skip_stream"])
            .invoke(
                [
                    SystemMessage(content=_INTENT_SYSTEM_PROMPT),
                    HumanMessage(content=parsed.text[:4000]),
                ]
            )
        )
        return IntentDecision.model_validate(_json_object(_message_text(response)))

    return _classify


def _is_follow_up(latest: str, carried: str, decision: IntentDecision) -> bool:
    """Whether this turn answers the design in flight instead of starting one.

    The second kind of answer-turn, alongside a risk acknowledgement. The
    Architect ends with a "Decisions needed" list; the reply to it names no board.
    ``"a 加入 添加 默认"`` was classified as a fresh request, routed to the
    clarifier, and came back as "give me the path of the existing KiCad project to
    review" — which answers nothing and discards the design it was answering.

    The test is whether the message carries a SUBJECT of its own. A standalone
    request names the part it is about, or the project to review; an answer or a
    one-line amendment ("make the board 60x40") names neither and only makes sense
    against what came before. Deliberately not ``needs_clarification``: that flag
    depends on which classifier is configured, so the same reply merged with a
    model available and started a new empty design without one.
    """
    if not carried:
        return False
    if _positive_mcu_mentions(latest):
        return False
    return not decision.source_project_path


async def initialize(
    state: RatsNestWorkflowState,
    config: RunnableConfig,
) -> dict[str, Any]:
    latest = _latest_requirement(state)
    configurable = config.get("configurable", {})
    # A risk acknowledgement arrives as its own turn, so this message may be an
    # ANSWER rather than a new request. Treating "I know, use 5 V anyway" as a
    # fresh requirement would discard the design it was answering.
    pending = [str(token) for token in state.get("pending_acks", []) if token]
    carried = str(state.get("base_requirement", ""))
    # An offered option set is consumed before anything else: a reply that picks
    # one is an ANSWER, not a new request, and picking the "accept the risk"
    # option must produce exactly the token the free-text path produced.
    offered = from_state(state.get("open_decisions"))
    resolved_now, still_open = (
        _selected_from_reply(latest, offered, config)
        if offered and carried
        else ([], [])
    )
    newly_accepted = accepted_tokens(resolved_now) & set(pending)
    if pending and carried and not resolved_now:
        newly_accepted = _accepted_from_reply(latest, pending, config)
    if newly_accepted:
        still_open = [
            decision
            for decision in still_open
            if not any(option.ack_token in newly_accepted for option in decision.options)
        ]
    answered = bool(resolved_now) or bool(newly_accepted)
    if answered:
        base_requirement = carried
        accepted = set(state.get("accepted_acks", [])) | newly_accepted
        settled = _merge_decisions(state.get("resolved_decisions"), resolved_now)
    else:
        # No recognisable acceptance: this is a new request. Fail closed — an
        # unparsed acknowledgement means the question gets asked again rather
        # than being assumed answered. Earlier answers belonged to the earlier
        # request and do not carry over.
        base_requirement = latest
        accepted = factclaim.parse_acks(latest)
        settled = []
    requirement = _with_acks(apply_decisions(base_requirement, settled), accepted)

    thread_id = str(
        configurable.get(
            "client_thread_id",
            configurable.get("thread_id", "run"),
        )
    )
    configured_path = configurable.get("project_path")

    def _classify(text: str) -> IntentDecision:
        return classify_intent(
            parse_request(
                text,
                explicit_mode=_explicit_workflow_mode(configurable),
                config_project_path=str(configured_path) if configured_path else None,
                has_positive_mcu=bool(_positive_mcu_mentions(text)),
            ),
            llm_classifier=_llm_intent_classifier(config),
        )

    decision = _classify(requirement)
    followed_up = _is_follow_up(latest, carried, decision) and not answered
    if followed_up:
        base_requirement = f"{carried}\n\n{_FOLLOW_UP_PREFIX}{latest}"
        requirement = _with_acks(apply_decisions(base_requirement, settled), accepted)
        decision = _classify(requirement)
        _workflow_event("intent", "amended", detail=latest[:200])

    # Keep every unanswered item from the exact menu the user saw. A partial
    # reply consumes only the selected slots; an unparsed follow-up keeps the
    # complete menu open. A genuinely new request still discards the stale menu.
    remaining_decisions = still_open if answered or followed_up else []

    arbitration = _arbitrate_requirement(requirement)
    still_pending = [v.ack_token for v in arbitration.blocking if v.ack_token]

    # Section 4.4: resolve the fixed components ONCE here. Every later phase
    # reads these structured constraints instead of re-parsing the long prompt
    # (which is how runtime-appended evidence used to redefine the MCU).
    constraints = ConstraintSet.from_requirement(requirement)
    primary = resolve_primary_mcu(requirement)
    default_project = f"{primary.token.lower()}-board" if primary else "ratsnestpro-board"
    capabilities = required_capabilities(requirement)
    return {
        "requirement": requirement,
        "base_requirement": base_requirement,
        "claim_verdicts": [_verdict_payload(v) for v in arbitration.verdicts],
        "pending_acks": still_pending,
        "accepted_acks": sorted(accepted),
        "open_decisions": to_state(remaining_decisions),
        "resolved_decisions": settled,
        # A partially answered assumption menu is not complete. Re-open the
        # latch so a defensive recomputation cannot let the remaining items fall
        # through to hardware even if their serialized menu is later unavailable.
        "missing_data_asked": (
            bool(answered or followed_up)
            and bool(state.get("missing_data_asked"))
            and not remaining_decisions
        ),
        "workflow_mode": decision.primary_intent,
        # Section 4.4 again: resolve the reply language ONCE so the Architect
        # narrative, the clarification prompt and the final report all speak the
        # language the user wrote in.
        # An answer must not change the reply language. A pick arrives as
        # ``PICK: slot=A`` lines — enough Latin letters to read as English to a
        # script census — and a Chinese run then produced an English report from
        # the turn the user answered onward. When this turn is an answer the
        # language resolved for the request it answers still holds.
        "reply_language": (
            str(state.get("reply_language") or "") or reply_language(state.get("messages"), config)
        )
        if answered
        else reply_language(state.get("messages"), config),
        "intent": decision.model_dump(),
        "component_constraints": constraints.to_state(),
        "capability": {
            "required_capabilities": capabilities,
            "primary_mcu": primary.token if primary else "",
            "primary_mcu_sources": list(primary.sources) if primary else [],
            "primary_mcu_package": primary.package if primary else "",
        },
        "diagnosis": {},
        "repair_patches": [],
        "run_name": _configured_name(
            requirement,
            config,
            "run_name",
            f"ratsnest-{thread_id[:8]}",
        ),
        "project_name": _configured_name(
            requirement,
            config,
            "project_name",
            default_project,
        ),
        "architecture": {},
        "parts": {},
        "hardware": {},
        "hardware_attempts": [],
        "review": {},
        "review_round": 0,
        "max_review_rounds": _bounded_int(
            configurable.get("max_review_rounds"),
            default=2,
            minimum=0,
            maximum=3,
        ),
        "review_target": decision.source_project_path or "",
        "trace": [],
    }


_CLARIFICATION_REQUEST = {
    "en": (
        "Clarification needed: give the path of the existing KiCad project to "
        "review, or state explicitly that this is a new design task."
    ),
    "zh": "需要澄清：请提供要审查的现有 KiCad 工程路径，或明确说明这是新建设计任务。",
}


# --------------------------------------------------------------------------- #
# Risk arbitration — asking before building something the datasheet forbids
# --------------------------------------------------------------------------- #

_ACK_SYSTEM_PROMPT = (
    "The user was warned that specific requested values break documented "
    "datasheet limits, and was given one acknowledgement token per risk. Decide "
    "which tokens the user's reply ACCEPTS. Reply with only a compact JSON "
    'object: {"accepted": ["<token>", ...]}. Copy tokens EXACTLY from the list '
    "given; never invent one. Include a token only when the reply clearly "
    "accepts that specific risk. If the reply is a question, a refusal, a "
    "request to change the value, or anything ambiguous, return an empty list."
)

_RISK_HEADER = {
    "en": (
        "STOP — the requested design breaks a documented datasheet limit. "
        "Building it as asked can damage the parts or the board."
    ),
    "zh": "请注意 —— 当前需求与数据手册中的硬性限值冲突。按原样制板可能损坏器件或板子。",
}

# No free-text footer: the answer is a choice from the options below, and an
# invitation to "reply in your own words" only invites a reply nothing can
# validate. The risk token still appears per item because it is the scoped waiver
# the pipeline honours and the report cites — data, not a call to action.
_RISK_ITEM = {
    "en": "{index}. {message}\n     Source: {citation}\n     Risk token: {token}",
    "zh": "{index}. {message}\n     依据：{citation}\n     风险 token：{token}",
}


def _verdict_payload(verdict: factclaim.ClaimVerdict) -> dict[str, Any]:
    """A verdict flattened for the graph state.

    LangGraph state is checkpointed as plain data, so the frozen dataclass cannot
    be stored directly. Only the fields the conversation and the report need are
    kept — including the citation, because an accepted risk without its source is
    an unfalsifiable claim.
    """
    return {
        "slot": verdict.slot,
        "tier": verdict.tier,
        "ok": verdict.ok,
        "value": verdict.claim.value,
        "unit": verdict.claim.unit,
        "quote": verdict.claim.quote,
        "device": verdict.device,
        "citation": verdict.citation,
        "message": verdict.message,
        "ack_token": verdict.ack_token,
        "acknowledged": verdict.acknowledged,
    }


def _arbitrate_requirement(requirement: str) -> factclaim.Arbitration:
    """Tier 1 only — deterministic, no model call.

    The experience tier (Tier 2) is deliberately left to
    ``RequirementsStep.propose`` inside the pipeline: it needs the soft corpus, it
    can never block, and running it here as well would spend a second model call
    to produce a warning that the final report shows either way.
    """
    claims = factclaim.extract_claims(requirement)
    if not claims:
        return factclaim.Arbitration()
    return factclaim.arbitrate(
        claims,
        list(fact_sheets_named(requirement)),
        acks=factclaim.parse_acks(requirement),
    )


def _accepted_from_reply(reply: str, pending: list[str], config: RunnableConfig) -> set[str]:
    """Tokens the user's reply accepts, validated against what was actually asked.

    A model proposes; deterministic code decides. Every returned token must be one
    of the ``pending`` tokens the user was shown, so a hallucinated or
    over-generous answer cannot waive a limit that was never discussed. Any
    failure yields the empty set, which leaves the risk unacknowledged — the safe
    direction, because the cost of a missed acceptance is one more question and
    the cost of a false one is a dead board.
    """
    if not pending:
        return set()
    allowed = set(pending)
    explicit = factclaim.parse_acks(reply) & allowed
    if explicit:
        return explicit
    selected_model = config.get("configurable", {}).get("model", settings.DEFAULT_MODEL)
    try:
        response = (
            get_model(selected_model)
            .with_config(tags=["skip_stream"])
            .invoke(
                [
                    SystemMessage(content=_ACK_SYSTEM_PROMPT),
                    HumanMessage(
                        content=(
                            f"Tokens the user may accept:\n{json.dumps(sorted(allowed))}\n\n"
                            f"The user replied:\n{reply[:2000]}"
                        )
                    ),
                ]
            )
        )
    except Exception:  # noqa: BLE001 - model boundary; silence means "not accepted"
        return set()
    payload = _json_object(_message_text(response))
    proposed = payload.get("accepted")
    if not isinstance(proposed, list):
        return set()
    return {str(token).strip() for token in proposed} & allowed


_PICK_SYSTEM_PROMPT = (
    "The user was shown a numbered list of undecided items, each with lettered "
    "options. Decide which option the user picked for each item. Reply with only "
    'a compact JSON object: {"picks": {"<slot>": "<option key>"}}. Use only the '
    "slots and option keys given; never invent one. Omit any item the reply does "
    "not clearly answer. If the reply is a question, a refusal, or a new request, "
    "return an empty object."
)


def _model_picks(
    reply: str, decisions: list[OpenDecision], config: RunnableConfig
) -> dict[str, str]:
    """Ask a model to map prose onto the offered option keys, then verify it.

    Same division of labour as :func:`_accepted_from_reply`: the model proposes,
    code decides. The proposal is re-validated by rebuilding canonical tokens and
    parsing them with :func:`parse_picks`, so an invented slot or a key that was
    never offered cannot survive.
    """
    catalogue = {
        decision.slot: [option.key for option in decision.options] for decision in decisions
    }
    selected_model = config.get("configurable", {}).get("model", settings.DEFAULT_MODEL)
    try:
        response = (
            get_model(selected_model)
            .with_config(tags=["skip_stream"])
            .invoke(
                [
                    SystemMessage(content=_PICK_SYSTEM_PROMPT),
                    HumanMessage(
                        content=(
                            f"Items and their option keys:\n"
                            f"{json.dumps(catalogue, ensure_ascii=False)}\n\n"
                            f"The user replied:\n{reply[:2000]}"
                        )
                    ),
                ]
            )
        )
    except Exception:  # noqa: BLE001 - model boundary; silence means "not answered"
        return {}
    proposed = _json_object(_message_text(response)).get("picks")
    if not isinstance(proposed, dict):
        return {}
    tokens = "\n".join(
        pick_token(str(slot), str(key))
        for slot, key in proposed.items()
        if isinstance(slot, str) and isinstance(key, str)
    )
    return parse_picks(tokens, decisions)


def _selected_from_reply(
    reply: str, decisions: list[OpenDecision], config: RunnableConfig
) -> tuple[list[dict[str, Any]], list[OpenDecision]]:
    """Return the choices this reply settles and the exact items still open.

    An unparsed answer must leave the item open rather than pick for the user:
    the cost of a missed pick is one more question, the cost of a wrong one is a
    decision that was never made showing up in a fabricated board.
    """
    if not decisions:
        return [], []
    if not reply:
        return [], list(decisions)
    picks = parse_picks(reply, decisions)
    if not picks:
        picks = _model_picks(reply, decisions, config)
    resolved, unresolved_slots = resolve(decisions, picks, reply=reply)
    unresolved = set(unresolved_slots)
    return resolved, [decision for decision in decisions if decision.slot in unresolved]


def _merge_decisions(carried: Any, fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Accumulate answers across turns; a later answer for a slot replaces it."""
    merged: dict[str, dict[str, Any]] = {}
    for record in list(carried or []) + list(fresh):
        if isinstance(record, dict) and record.get("slot"):
            merged[str(record["slot"])] = record
    return list(merged.values())


def _with_acks(requirement: str, tokens: set[str]) -> str:
    """Append the accepted tokens so the pipeline sees the same decision.

    The requirement text is the transport: ``_hardware_requirement`` passes it
    verbatim to ``ratsnest_run_pcb_pipeline``, where ``RequirementsStep`` parses
    the same tokens and downgrades the same checks. One decision, recorded once,
    honoured in both layers.
    """
    if not tokens:
        return requirement
    lines = [f"{factclaim.ACK_PREFIX} {token}" for token in sorted(tokens)]
    return f"{requirement}\n" + "\n".join(lines)


async def clarify_risk(state: RatsNestWorkflowState) -> dict[str, Any]:
    """Report every unacknowledged hard conflict as a closed set of options.

    The same shape as :func:`clarify`: state it, stop, and let the user decide.
    No design work happens in between, so nothing has to be unwound if they
    change the value instead of accepting the risk.

    The per-conflict lines keep the ``ACK-RISK`` token, because that token is the
    scoped waiver the pipeline honours. The turn then ends on the options and
    nothing else: the three ways out — stay inside the limit, accept the risk,
    supply a different value — are the options, so restating them as an
    open-ended invitation would only solicit an answer nothing can validate.
    """
    language = _reply_language(state)
    verdicts = [v for v in state.get("claim_verdicts", []) if isinstance(v, dict)]
    blocking = [v for v in verdicts if not v.get("ok") and not v.get("acknowledged")]
    lines = [localized(_RISK_HEADER, language), ""]
    for index, verdict in enumerate(blocking, start=1):
        lines.append(
            localized(_RISK_ITEM, language).format(
                index=index,
                message=str(verdict.get("message", "")),
                citation=str(verdict.get("citation") or "n/a"),
                token=f"{factclaim.ACK_PREFIX} {verdict.get('ack_token', '')}",
            )
        )
        lines.append("")
    open_decisions = risk_decisions(blocking, language)
    if open_decisions:
        lines.extend([render(open_decisions, language), payload_block(open_decisions)])
    message = "\n".join(lines)
    _workflow_event("risk-arbitration", "needs_acknowledgement", detail=message[:200])
    return {
        "messages": [AIMessage(content=message)],
        "open_decisions": to_state(open_decisions),
        "trace": _append_trace(
            state,
            agent="Risk Arbiter",
            tool="factclaim.arbitrate",
            status="needs_acknowledgement",
            evidence=f"{len(blocking)} unacknowledged datasheet conflict(s)",
        ),
    }


_MISSING_DATA_HEADER = {
    "en": (
        "Before I build the board, confirm the values I had to assume. The first "
        "option on each item is what I would have used anyway, so submitting the "
        "form unchanged keeps the design moving."
    ),
    "zh": (
        "开始制板前，先确认几个只能按假设处理的数据。"
        "每项的第一个选项就是我本来会用的值，原样提交就照旧继续。"
    ),
}


def _pending_assumption_decisions(state: RatsNestWorkflowState) -> list[OpenDecision]:
    """Assumed values not yet settled by the user, as options.

    Shared by the router and the node so the question asked is exactly the
    question the router decided to ask.

    Two sources, merged. The Architect records what it assumed next to the
    evidence it read, which is the better record but exists only when the model
    answered in the required format — a gateway timeout produced an empty list
    and therefore no question at all. ``requirement_gaps`` reads the requirement
    text instead, so the slots a requirement never mentions are raised whether or
    not any model was reachable.
    """
    settled = frozenset(
        str(record.get("slot", ""))
        for record in state.get("resolved_decisions", [])
        if isinstance(record, dict)
    )
    language = _reply_language(state)
    recorded = [
        item
        for item in state.get("architecture", {}).get("assumptions", [])
        if isinstance(item, dict)
    ]
    derived = requirement_gaps(str(state.get("requirement", "")), language, settled=settled)
    return assumption_decisions(
        merge_assumptions(recorded, derived),
        language,
        settled=settled,
    )


async def clarify_missing_data(state: RatsNestWorkflowState) -> dict[str, Any]:
    """Offer the assumed values as choices, once, before any board file exists.

    Placed after the parts phase rather than before the Architect because the
    assumptions do not exist until the Architect has read its evidence, and
    placed before the hardware phase because nothing has been written yet — a
    changed answer here costs no rework, which is the same argument
    :func:`clarify_risk` makes for datasheet conflicts.
    """
    language = _reply_language(state)
    open_decisions = _pending_assumption_decisions(state)
    message = "\n".join(
        [
            localized(_MISSING_DATA_HEADER, language),
            "",
            render(open_decisions, language),
            payload_block(open_decisions),
        ]
    )
    _workflow_event("missing-data", "needs_decision", detail=message[:200])
    return {
        "messages": [AIMessage(content=message)],
        "open_decisions": to_state(open_decisions),
        "missing_data_asked": True,
        "trace": _append_trace(
            state,
            agent="Architect",
            tool="assumption_decisions",
            status="needs_decision",
            evidence=f"{len(open_decisions)} assumed value(s) awaiting confirmation",
        ),
    }


_REMAINING_DECISIONS_HEADER = {
    "en": "I recorded the choices you supplied. These items still need your decision:",
    "zh": "我已经记录你刚才确认的选项，以下事项仍需要你决定：",
}


async def clarify_open_decisions(state: RatsNestWorkflowState) -> dict[str, Any]:
    """Re-ask only the unanswered portion of the previous turn's menu."""
    language = _reply_language(state)
    open_decisions = from_state(state.get("open_decisions"))
    message = "\n".join(
        [
            localized(_REMAINING_DECISIONS_HEADER, language),
            "",
            render(open_decisions, language),
            payload_block(open_decisions),
        ]
    )
    _workflow_event("open-decisions", "needs_decision", detail=message[:200])
    return {
        "messages": [AIMessage(content=message)],
        "open_decisions": to_state(open_decisions),
        "missing_data_asked": bool(state.get("missing_data_asked"))
        or any(decision.kind == "assumption" for decision in open_decisions),
        "trace": _append_trace(
            state,
            agent="Supervisor",
            tool="open_decisions",
            status="needs_decision",
            evidence=f"{len(open_decisions)} decision(s) still unanswered",
        ),
    }


async def clarify(state: RatsNestWorkflowState) -> dict[str, Any]:
    intent = state.get("intent", {})
    language = _reply_language(state)
    message = intent.get("clarification") or localized(_CLARIFICATION_REQUEST, language)
    open_decisions = intent_decisions(intent, language)
    if open_decisions:
        message = "\n".join(
            [message, "", render(open_decisions, language), payload_block(open_decisions)]
        )
    _workflow_event("intent", "needs_clarification", detail=message[:200])
    return {
        "messages": [AIMessage(content=message)],
        "open_decisions": to_state(open_decisions),
        "trace": _append_trace(
            state,
            agent="Intent Router",
            tool="classify_intent",
            status="needs_clarification",
            evidence=message,
        ),
    }


_ARCHITECT_INSTRUCTIONS = (
    "You are the RatsNestPro Architect. Produce a concise design basis from the "
    "supplied evidence.\n"
    "\n"
    "EVIDENCE ORDER. fact_sheets are curated datasheet extracts with citations and "
    "are authoritative. The installed KiCad symbol pin map is authoritative for "
    "package pin numbers: read its per-pin 'alternates' list, because a pin whose "
    "primary name is a plain GPIO (PD0, PD1, PC14, PC15) is the oscillator pin when "
    "its alternates say so. Do not transcribe or infer a different pin table from "
    "PDF image text. design_practice is advisory only — never cite it as a limit. "
    "conventions and process_capability supply the board-level values no datasheet "
    "states.\n"
    "\n"
    "NEVER ASK FOR EVIDENCE YOU WERE GIVEN. Before writing that something is "
    "missing, search the supplied fact_sheets, candidate_by_step, symbol pin map "
    "and conventions for it. Pin numbers, decoupling values and placement, "
    "oscillator load capacitance, regulator input/output capacitors, and connector "
    "CC pulldowns are all normally present. A value stated there is settled: state "
    "it with its citation and move on. Do not ask the user for datasheet pages, "
    "symbol libraries, pinouts or part numbers.\n"
    "\n"
    "UNSTATED VALUES BECOME ASSUMPTIONS, NOT QUESTIONS. When no supplied source "
    "states a value, adopt the applicable convention, or a standard engineering "
    "value if no convention covers it, and keep going. Record each one under "
    "'Assumptions' as: the value, one line of reasoning, and that it is an "
    "assumption rather than a datasheet fact. A slot marked NOT STATED is unknown "
    "rather than unlimited, so an assumption there must be conservative and must "
    "say what would confirm it. Never stop the design to collect material.\n"
    "\n"
    "ASK ONLY WHAT YOU CANNOT DECIDE, ONCE. A question is warranted only when the "
    "options are mutually exclusive, the wrong choice is consequential, and no "
    "convention or datasheet fact settles it — a genuine product decision, not an "
    "evidence gap. Put every such item in ONE numbered list headed 'Decisions "
    "needed'. Each item states the options and the default you will proceed with if "
    "it goes unanswered. Then continue on those defaults in the same reply. Never "
    "split questions across turns, never re-ask something already answered or "
    "defaulted, and never end a turn with nothing but questions.\n"
    "\n"
    "SCOPE. Blocking findings in this phase cover the primary MCU and board-level "
    "architecture. Supporting ICs not yet selected belong to the Parts Specialist "
    "and Hardware Engineer phases; list their evidence as pending, not as an "
    "Architect blocker. Do not claim that KiCad files, routing, review, or "
    "manufacturing outputs exist.\n"
    "\n"
    "MACHINE-READABLE ASSUMPTIONS. After the prose, append exactly one fenced "
    "block labelled ratsnest-assumptions holding "
    '{"assumptions": [{"slot": "...", "question": "...", "assumed": "...", '
    '"basis": "...", "alternatives": ["..."]}]} — one entry for each item you '
    "recorded under 'Assumptions' or 'Decisions needed', and nothing a supplied "
    "source already settles. slot is a short stable identifier (tvs_part, "
    "crystal_load_pf). assumed is the value you are proceeding with, written "
    "exactly as it should appear in the design. basis is where it came from: a "
    "convention name, a candidate list, or 'engineering default'. alternatives "
    "lists other genuinely buildable choices and is empty when only one value is "
    "sensible. Emit the block as data: no comments inside it, no text after it. "
    "Omit the block entirely when you assumed nothing."
)

# The Architect ships its assumptions twice: as prose for the reader and as this
# block for the code that turns them into a choice the user can override.
_ASSUMPTION_FENCE = "ratsnest-assumptions"
_ASSUMPTION_RE = re.compile(
    rf"```{_ASSUMPTION_FENCE}\s*(\{{.*?\}})\s*```",
    re.DOTALL,
)


def _split_assumptions(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Prose and recorded assumptions, separately.

    A missing or malformed block yields no assumptions and leaves the text
    untouched: the prose already states them, so the cost is a lost prompt, never
    a lost fact, and a model that ignores the format cannot stall a build.
    """
    match = _ASSUMPTION_RE.search(text or "")
    if not match:
        return text, []
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return text, []
    items = payload.get("assumptions")
    if not isinstance(items, list):
        return text, []
    stripped = (text[: match.start()] + text[match.end() :]).strip()
    return stripped, [item for item in items if isinstance(item, dict)]


_ARCHITECT_STATUS_LABEL = {
    "en": "Architect grounded-evidence status",
    "zh": "架构师证据落地状态",
}

_ARCHITECT_UNAVAILABLE = {
    "en": (
        "Architect narrative unavailable. Grounded tool evidence remains "
        "authoritative. Error: {error}"
    ),
    "zh": "架构师叙述不可用，工具取得的实证仍然是权威依据。错误：{error}",
}

# Phase summaries surface in the chat transcript, so they follow the reply
# language too. Status tokens and counts stay verbatim inside the placeholders.
_PARTS_SUMMARY = {
    "en": (
        "Parts Specialist used only the local catalog. Status: {status}. "
        "No external availability claims were added."
    ),
    "zh": "选型专家仅使用本地目录。状态：{status}。未添加任何外部库存/供货声明。",
}

_HARDWARE_SUMMARY = {
    "en": (
        "Hardware Engineer real pipeline status: {status}. "
        "Completed steps: {completed}/17. Actual files: {files}. "
        "Release blockers: {blockers}."
    ),
    "zh": (
        "硬件工程师实际流水线状态：{status}。已完成步骤：{completed}/17。"
        "实际产出文件：{files}。发布阻塞项：{blockers}。"
    ),
}

_HARDWARE_ROLLED_BACK = {
    "en": (
        " AHE rolled back this repair: {evaluation}. The earlier result and its "
        "artifacts remain authoritative."
    ),
    "zh": " AHE 已回滚此次修复：{evaluation}。此前的结果及其产物仍然是权威依据。",
}

_HARDWARE_EVALUATED = {
    "en": " AHE change evaluation: {evaluation}.",
    "zh": " AHE 变更评估：{evaluation}。",
}

_HARDWARE_PATCHED = {
    "en": (" AHE diagnosis: {failure_classes}; scoped repair at {scope} with {actions} action(s)."),
    "zh": " AHE 诊断：{failure_classes}；在 {scope} 处做定域修复，共 {actions} 个动作。",
}

_REVIEWER_SUMMARY = {
    "en": "Reviewer audited project path {project_path}. Status: {status}; report_exists={report_exists}.",
    "zh": "评审员审查了工程路径 {project_path}。状态：{status}；report_exists={report_exists}。",
}


async def architect_phase(
    state: RatsNestWorkflowState,
    config: RunnableConfig,
) -> dict[str, Any]:
    _workflow_event("architect", "started")
    requirement = state["requirement"]
    # Read the constraint resolved once at initialize; only fall back to parsing
    # when this phase is invoked directly (tests, single-phase reruns).
    constraints = ConstraintSet.from_state(state.get("component_constraints"))
    mcu_constraint = constraints.mcu
    if mcu_constraint is None:
        constraints = ConstraintSet.from_requirement(requirement)
        mcu_constraint = constraints.mcu
    symbol_query = (
        mcu_constraint.manufacturer_part_number
        if mcu_constraint
        else (_primary_mcu_mention(requirement) or requirement[:120])
    )
    required_package = mcu_constraint.package if mcu_constraint else ""
    symbol_args = {
        "query": symbol_query,
        "limit": 3,
        "required_package": required_package,
    }
    symbol_raw, symbol_result, _ = await _call_json_with_retry(
        lambda: ratsnest_lookup_kicad_symbol(**symbol_args),
        phase="architect",
        tool="ratsnest_lookup_kicad_symbol",
        attempts=2,
        require_nonempty="candidates",
    )

    # Every other constrained part, not just the MCU. The symbol index is local,
    # so this costs no network, and without it the Architect reports a connector
    # or regulator pinout as missing evidence while the library holds it.
    supporting_symbols: list[dict[str, Any]] = []
    supporting_calls: list[tuple[dict[str, Any], str]] = []
    for constraint in constraints.constraints:
        if constraint.role == "mcu":
            continue
        query = constraint.manufacturer_part_number or constraint.role
        if not query:
            continue
        args = {"query": query, "limit": 2, "required_package": constraint.package or ""}
        raw = ratsnest_lookup_kicad_symbol(**args)
        supporting_calls.append((args, raw))
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        supporting_symbols.append({"role": constraint.role, "query": query, "result": parsed})
        if len(supporting_symbols) >= 5:
            break

    local_args = {"requirement": requirement[:200]}
    local_facts = local_evidence.collect(requirement)
    local_coverage = local_evidence.coverage(local_facts)
    local_raw = json.dumps(
        {"status": "ok", "coverage": local_coverage}, ensure_ascii=False, indent=2
    )

    search_query = f"{symbol_query} official manufacturer datasheet hardware design reference"
    search_args = {"query": search_query}
    search_raw, search_result, _ = await _call_json_with_retry(
        lambda: web_search.invoke(search_args),
        phase="architect",
        tool="web_search",
        attempts=3,
        require_nonempty="results",
    )

    datasheet_args: dict[str, Any] | None = None
    datasheet_raw = ""
    datasheet_result: dict[str, Any] = {"status": "not_found"}
    candidate_urls = (
        [str(symbol_result.get("candidates", [{}])[0].get("properties", {}).get("Datasheet", ""))]
        if symbol_result.get("candidates")
        else []
    )
    candidate_urls.extend(
        str(result.get("href", "")) for result in search_result.get("results", [])
    )
    datasheet_attempts: list[tuple[dict[str, Any], str]] = []
    for url in dict.fromkeys(candidate_urls):
        if url.lower().endswith(".pdf") and url.lower().startswith("https://"):
            datasheet_args = {
                "url": url,
                "query": (
                    "VCAP CEXT external capacitor 2.2 uF decoupling power supply "
                    "HSE LSE ADC absolute maximum current consumption LQFP64"
                ),
                "max_pages": 8,
            }
            datasheet_raw, datasheet_result, _ = await _call_json_with_retry(
                lambda args=datasheet_args: fetch_datasheet.invoke(args),
                phase="architect",
                tool="fetch_datasheet",
                attempts=2,
                require_nonempty="matched_pages",
            )
            datasheet_attempts.append((dict(datasheet_args), datasheet_raw))
            if datasheet_result.get("status") in {"ok", "partial"} and (
                datasheet_result.get("matched_pages") or datasheet_result.get("text")
            ):
                break

    evidence = {
        "requirement": requirement,
        "kicad_symbol": symbol_result,
        "supporting_symbols": supporting_symbols,
        "fact_sheets": local_facts["fact_sheets"],
        "design_practice": local_facts["design_practice"],
        "process_capability": local_facts["process_capability"],
        "conventions": local_facts["conventions"],
        "official_sources": search_result.get("results", [])[:6],
        "datasheet": {
            **{key: value for key, value in datasheet_result.items() if key != "matched_pages"},
            "matched_pages": [
                {
                    "page": page.get("page"),
                    "text": str(page.get("text", ""))[:2_000],
                }
                for page in datasheet_result.get("matched_pages", [])
            ],
        },
    }
    system = SystemMessage(
        content=f"{_ARCHITECT_INSTRUCTIONS}\n\n{language_directive(_reply_language(state))}"
    )
    try:
        selected_model = config.get("configurable", {}).get("model", settings.DEFAULT_MODEL)
        response = (
            await get_model(selected_model)
            .with_config(tags=["skip_stream"])
            .ainvoke(
                [
                    system,
                    HumanMessage(content=json.dumps(evidence, ensure_ascii=False)),
                ],
                config,
            )
        )
        summary = _message_text(response)
        summary, assumptions = _split_assumptions(summary)
    except Exception as exc:  # noqa: BLE001 - model provider boundary
        summary = localized(_ARCHITECT_UNAVAILABLE, _reply_language(state)).format(
            error=type(exc).__name__
        )
        assumptions = []

    symbol_ok = symbol_result.get("status") == "ok" and bool(symbol_result.get("candidates"))
    source_ok = search_result.get("status") in {"ok", "partial"}
    datasheet_ok = datasheet_result.get("status") in {"ok", "partial"}
    # Section 4.5: a candidate list is not acceptance. When the requirement pins
    # an exact part, the acquisition ladder decides whether any installed symbol
    # truly satisfies its identity and package, so a near neighbour with a
    # different pin count blocks instead of silently standing in.
    acquisition = acquire_symbol(mcu_constraint) if mcu_constraint else None
    symbol_ok = symbol_result.get("status") == "ok" and bool(symbol_result.get("candidates"))
    if acquisition is not None:
        symbol_ok = acquisition.resolved
    if not symbol_ok:
        status = "blocked"
    elif source_ok and datasheet_ok:
        status = "ok"
    else:
        # Missing external evidence remains visible and prevents a clean final
        # release, but it is recoverable: downstream agents can still build
        # intermediate artifacts from grounded KiCad/library evidence.
        status = "partial"
    inner_messages = [
        *_tool_messages("ratsnest_lookup_kicad_symbol", symbol_args, symbol_raw),
        *_tool_messages("ratsnest_local_evidence", local_args, local_raw),
        *_tool_messages("web_search", search_args, search_raw),
    ]
    for supporting_args, supporting_raw in supporting_calls:
        inner_messages.extend(
            _tool_messages("ratsnest_lookup_kicad_symbol", supporting_args, supporting_raw)
        )
    for attempted_args, attempted_raw in datasheet_attempts:
        inner_messages.extend(_tool_messages("fetch_datasheet", attempted_args, attempted_raw))
    inner_messages.append(
        AIMessage(
            content=(
                f"{localized(_ARCHITECT_STATUS_LABEL, _reply_language(state))}: "
                f"{status}\n\n{summary}"
            )
        )
    )
    acquisition_payload: dict[str, Any] = {}
    if acquisition is not None:
        acquisition_payload = {
            "requested_mpn": acquisition.requested_mpn,
            "tier": acquisition.tier,
            "lib_id": acquisition.lib_id,
            "pin_count": acquisition.pin_count,
            "resolved": acquisition.resolved,
            "failure_class": acquisition.failure_class,
            "rejected": [
                {"lib_id": item.lib_id, "reason": item.reason} for item in acquisition.rejected
            ],
            "next_actions": list(acquisition.next_actions),
        }
    architecture = {
        "status": status,
        "symbol": symbol_result,
        "search": search_result,
        "datasheet": datasheet_result,
        "summary": summary,
        "assumptions": assumptions,
        "symbol_acquisition": acquisition_payload,
        "local_evidence": local_coverage,
        "mcu_constraint": mcu_constraint.model_dump() if mcu_constraint else {},
    }
    if acquisition is not None and not acquisition.resolved:
        evidence_detail = (
            f"{acquisition.requested_mpn}: {acquisition.failure_class}; "
            f"{len(acquisition.rejected)} candidate(s) rejected"
        )
    elif acquisition is not None:
        evidence_detail = f"{acquisition.lib_id} ({acquisition.tier})"
    elif symbol_result.get("candidates"):
        evidence_detail = str(symbol_result.get("candidates", [{}])[0].get("lib_id", ""))
    else:
        evidence_detail = "no grounded KiCad symbol"
    _workflow_event(
        "architect",
        "completed" if status == "ok" else status,
        detail=evidence_detail,
    )
    return {
        "architecture": architecture,
        "component_constraints": constraints.to_state(),
        "trace": _append_trace(
            state,
            agent="Architect",
            tool="ratsnest_lookup_kicad_symbol + web_search + fetch_datasheet",
            status=status,
            evidence=evidence_detail,
        ),
        "messages": inner_messages,
    }


def _component_queries(requirement: str) -> list[str]:
    ignored = {
        "LQFP64",
        "USB-C",
        "SDIO",
        "SPI1",
        "SPI2",
        "I2C1",
        "CAN1",
    }
    queries: list[str] = []
    primary_mcu = _primary_mcu_mention(requirement)
    if primary_mcu:
        queries.append(primary_mcu)
    queries.extend(token for token in _positive_mcu_mentions(requirement) if token not in ignored)
    return list(dict.fromkeys(queries))[:12] or [requirement[:120]]


async def parts_phase(state: RatsNestWorkflowState) -> dict[str, Any]:
    _workflow_event("parts-specialist", "started")
    results: list[dict[str, Any]] = []
    inner_messages: list[Any] = []
    for query in _component_queries(state["requirement"]):
        args = {"query": query, "limit": 10}
        raw, parsed, _ = await _call_json_with_retry(
            lambda args=args: ratsnest_search_parts(**args),
            phase="parts-specialist",
            tool="ratsnest_search_parts",
            attempts=2,
        )
        results.append({"query": query, "result": parsed})
        inner_messages.extend(_tool_messages("ratsnest_search_parts", args, raw))
        if parsed.get("status") == "unavailable":
            break

    statuses = {item["result"].get("status") for item in results}
    if "unavailable" in statuses:
        status = "unavailable"
    elif any(item["result"].get("results") for item in results):
        status = "ok"
    else:
        # An empty optional/local catalog is an evidence gap, not proof that the
        # requested design is impossible. Continue without inventing MPN/stock.
        status = "partial"
    summary = localized(_PARTS_SUMMARY, _reply_language(state)).format(status=status)
    inner_messages.append(AIMessage(content=summary))
    parts = {"status": status, "queries": results}
    if status == "unavailable":
        trace_evidence = f"{len(results)} local catalog query attempt(s); cache unavailable"
    else:
        grounded_hits = sum(len(item["result"].get("results", [])) for item in results)
        trace_evidence = f"{grounded_hits} grounded catalog result(s)"
    _workflow_event(
        "parts-specialist",
        "completed" if status == "ok" else status,
        detail=trace_evidence,
    )
    return {
        "parts": parts,
        "trace": _append_trace(
            state,
            agent="Parts Specialist",
            tool="ratsnest_search_parts",
            status=status,
            evidence=trace_evidence,
        ),
        "messages": inner_messages,
    }


def _workspace_root() -> Path:
    import os

    return Path(os.getenv("RATSNESTPRO_WORKSPACE_ROOT", "data/ratsnestpro")).resolve()


def _validate_hardware_result(result: dict[str, Any]) -> dict[str, Any]:
    validated = dict(result)
    run_directory_value = str(result.get("run_directory", ""))
    run_directory = Path(run_directory_value) if run_directory_value else None
    artifacts = [str(item) for item in result.get("artifacts", [])]
    root = _workspace_root()
    actual_files: list[str] = []
    for artifact in artifacts:
        candidate = (root / artifact).resolve()
        if candidate.is_file():
            actual_files.append(str(candidate))
    if run_directory is not None and run_directory.is_dir():
        for candidate in run_directory.rglob("*"):
            if candidate.is_file() and str(candidate.resolve()) not in actual_files:
                actual_files.append(str(candidate.resolve()))

    has_schematic = any(path.endswith(".kicad_sch") for path in actual_files)
    has_pcb = any(path.endswith(".kicad_pcb") for path in actual_files)
    has_dsn = any(path.endswith(".dsn") for path in actual_files)
    has_ses = any(path.endswith(".ses") for path in actual_files)
    routing = result.get("routing") if isinstance(result.get("routing"), dict) else {}
    verification = (
        result.get("verification") if isinstance(result.get("verification"), dict) else {}
    )
    erc = verification.get("erc") if isinstance(verification.get("erc"), dict) else {}
    drc = verification.get("drc") if isinstance(verification.get("drc"), dict) else {}
    erc_clean = (
        erc.get("applicable") is True
        and erc.get("available") is True
        and erc.get("ran") is True
        and erc.get("errors") == 0
    )
    drc_clean = (
        drc.get("applicable") is True
        and drc.get("available") is True
        and drc.get("ran") is True
        and drc.get("errors") == 0
        and drc.get("unconnected") == 0
    )
    release_ready = (
        result.get("status") == "ok"
        and result.get("completed_steps") == result.get("total_steps") == 17
        and routing.get("method") == "freerouting"
        and routing.get("unconnected") == 0
        and has_schematic
        and has_pcb
        and has_dsn
        and has_ses
        and erc_clean
        and drc_clean
    )
    blockers: list[str] = []
    if not has_schematic:
        blockers.append("no actual .kicad_sch artifact")
    if not has_pcb:
        blockers.append("no actual .kicad_pcb artifact")
    if not has_dsn:
        blockers.append("no actual Freerouting .dsn artifact")
    if not has_ses:
        blockers.append("no actual Freerouting .ses artifact")
    if routing.get("method") != "freerouting":
        blockers.append("Freerouting did not complete")
    if routing.get("unconnected") != 0:
        blockers.append("routing unconnected count is not zero")
    if result.get("completed_steps") != 17:
        blockers.append("17-step pipeline did not complete")
    if not erc.get("available"):
        blockers.append("kicad-cli ERC unavailable")
    elif not erc.get("ran"):
        blockers.append("kicad-cli ERC did not run")
    elif erc.get("errors") != 0:
        blockers.append(f"kicad-cli ERC reported {erc.get('errors')} error(s)")
    if not drc.get("available"):
        blockers.append("kicad-cli DRC unavailable")
    elif not drc.get("ran"):
        blockers.append("kicad-cli DRC did not run")
    elif drc.get("errors") != 0:
        blockers.append(f"kicad-cli DRC reported {drc.get('errors')} error(s)")
    if drc.get("ran") and drc.get("unconnected") != 0:
        blockers.append(f"kicad-cli DRC reported {drc.get('unconnected')} unconnected item(s)")
    validated["actual_files"] = sorted(actual_files)
    validated["project_available"] = has_schematic or has_pcb
    validated["release_ready"] = release_ready
    validated["release_blockers"] = blockers
    return validated


def _last_patch(state: RatsNestWorkflowState) -> RepairPatch | None:
    """The patch whose predictions the current attempt is meant to fulfil."""
    patches = state.get("repair_patches", [])
    if not patches or not isinstance(patches[-1], dict):
        return None
    try:
        return RepairPatch.model_validate(patches[-1].get("patch", {}))
    except Exception:  # noqa: BLE001 - stored state may predate a schema change
        return None


def _hardware_requirement(state: RatsNestWorkflowState, review_feedback: str = "") -> str:
    requirement = state["requirement"]
    for key in ("run_name", "project_name"):
        requirement = _configured_name_pattern(key).sub("", requirement)
    requirement = requirement.strip()
    requirement += (
        "\n\nThe Architect verified that the requested MCU has an exact installed "
        "KiCad symbol. Resolve its symbol, footprint, and pins from the installed "
        "library; do not infer package pin numbers from narrative text."
    )
    architecture = state.get("architecture", {})
    candidates = architecture.get("symbol", {}).get("candidates", [])
    primary_candidate = candidates[0] if candidates else {}
    primary_symbol = {
        "lib_id": primary_candidate.get("lib_id", ""),
        "pin_count": primary_candidate.get("pin_count"),
        "properties": primary_candidate.get("properties", {}),
    }
    official_sources = [
        {
            "title": source.get("title", ""),
            "href": source.get("href", ""),
            "body": str(source.get("body", ""))[:500],
        }
        for source in architecture.get("search", {}).get("results", [])[:6]
    ]
    datasheet = architecture.get("datasheet", {})
    datasheet_evidence = {
        "status": datasheet.get("status"),
        "source_url": datasheet.get("source_url"),
        "retrieval_method": datasheet.get("retrieval_method"),
        "document_pages": datasheet.get("document_pages"),
        "matched_pages": [
            {
                "page": page.get("page"),
                "text": str(page.get("text", ""))[:2_000],
            }
            for page in datasheet.get("matched_pages", [])[:8]
        ],
    }
    grounded_evidence = {
        "symbol": primary_symbol,
        "official_sources": official_sources,
        "datasheet": datasheet_evidence,
    }
    requirement += (
        "\n\nGROUNDED ARCHITECT EVIDENCE — use this evidence in component selection, "
        "power design, pin mapping, and checks; do not replace it with recalled facts:\n"
        f"{json.dumps(grounded_evidence, ensure_ascii=False)}"
    )
    # The object-level digest is what makes a repair actionable. Feeding back a
    # bare error count made the model delete nets and the error count grew, so
    # name the exact refs, pins and rules that must be resolved.
    digest_text = str(state.get("verification_digest", ""))
    if digest_text:
        requirement += (
            "\n\nDETERMINISTIC VERIFICATION FAILURES TO FIX — these are facts from "
            "kicad-cli, not opinions. Resolve these exact objects. Do NOT remove "
            "nets, pins or required functions to make the count drop; connect or "
            "correct them:\n"
            f"{digest_text[:12_000]}"
        )
    if review_feedback:
        requirement += f"\n\nINDEPENDENT REVIEW FEEDBACK TO REPAIR:\n{review_feedback[:8_000]}"
    return requirement


async def _run_hardware(
    state: RatsNestWorkflowState,
    *,
    repair: bool,
) -> dict[str, Any]:
    phase_name = "hardware-repair" if repair else "hardware-engineer"
    _workflow_event(phase_name, "started")
    review_round = state.get("review_round", 0) + (1 if repair else 0)
    # Section 4.8: a repair re-enters the SAME run so the checkpointed artifacts
    # before the failing step survive. Using a fresh run name would regenerate
    # the whole design and lose the very context the patch depends on.
    run_name = state["run_name"]
    resume_from = ""
    drop_refs: list[str] = []
    if repair:
        last = _last_patch(state)
        if last is not None:
            resume_from = last.repair_scope
            # G3: the typed actions change the design state. replace_symbol
            # removals are applied to the checkpointed SelectionPlan so the
            # selection step re-picks a grounded part instead of the bad one.
            drop_refs = [
                action.target
                for action in last.actions
                if action.type == "replace_symbol" and action.target
            ]
        patches_state = state.get("repair_patches", [])
        if not resume_from and patches_state and isinstance(patches_state[-1], dict):
            resume_from = str(patches_state[-1].get("resume", {}).get("resume_from", ""))
    review_feedback = str(state.get("review", {}).get("review", "")) if repair else ""
    args = {
        "requirement": _hardware_requirement(state, review_feedback),
        "run_name": run_name,
        "project_name": state["project_name"],
        "llm_mode": "required",
        "resume_from": resume_from,
        "drop_refs": drop_refs,
    }
    try:
        raw = await asyncio.to_thread(ratsnest_run_pcb_pipeline, **args)
    except Exception as exc:  # noqa: BLE001 - pipeline tool boundary
        raw = json.dumps(
            {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )
    result = _validate_hardware_result(_json_object(raw))
    attempts = [*state.get("hardware_attempts", []), result]
    # G1/G2: score the attempt against the object-level violations it claimed to
    # fix. A bare error count cannot distinguish "fixed nothing" from "made it
    # worse", and it cannot tell the model which objects still need work.
    digest = digest_from_pipeline_result(result)
    previous_signatures = [str(item) for item in state.get("verification_signatures", [])]
    scoring = bool(repair and previous_signatures)
    flips = (
        compare_signatures(previous_signatures, digest.error_signatures)
        if scoring
        else {"fixed": [], "introduced": [], "persisted": []}
    )
    evaluation = evaluate_change(_last_patch(state), flips) if scoring else None
    regressed = False
    authoritative = result
    if evaluation is not None and not evaluation.keeps_result and state.get("hardware"):
        regressed = True
        authoritative = state["hardware"]
    elif repair and not scoring and state.get("hardware"):
        # No report to compare object by object (e.g. ERC never ran), so fall
        # back to the coarse attempt score rather than accepting blindly.
        if repair_regressed(state["hardware"], result):
            regressed = True
            authoritative = state["hardware"]
    status = "ok" if authoritative["release_ready"] else "blocked"
    # Section 4.3/4.8: a blocked attempt is diagnosed, then answered with a
    # scoped patch that re-enters the pipeline at the failing step instead of
    # restarting the 17 steps or giving up.
    report = FailureDiagnoser().diagnose_pipeline_result(authoritative)
    constraints = ConstraintSet.from_state(state.get("component_constraints"))
    # Coverage must be judged against what the pipeline actually selected, not
    # against the constraint list (which only names the parts the user pinned).
    selected_roles = [str(role) for role in authoritative.get("selected_roles", []) if role]
    part_roles = selected_roles or [constraint.role for constraint in constraints.constraints]
    coverage = evaluate_coverage(
        state.get("capability", {}).get("required_capabilities", []),
        part_roles,
        present_roles=part_roles,
    )
    blocked_steps = [
        str(step.get("name", ""))
        for step in authoritative.get("steps", [])
        if isinstance(step, dict) and step.get("blocked")
    ]
    patch = None
    # A verdict that says the strategy is not working stops the loop; planning
    # another identical patch would just burn rounds.
    verdict_allows_retry = evaluation is None or evaluation.should_continue
    if status != "ok" and not regressed and verdict_allows_retry:
        patch = plan_repair(
            report,
            preconditions=RepairPreconditions(
                state_version=review_round,
                selection_version=int(authoritative.get("completed_steps", 0) or 0),
                completed_steps=int(authoritative.get("completed_steps", 0) or 0),
            ),
            missing_roles=[item.role for item in coverage.missing_obligations],
            missing_blocks=list(coverage.missing_capabilities),
            max_scope=blocked_steps[0] if blocked_steps else "",
            # The repair claims it will clear exactly the violations observed now,
            # so the next run can falsify that claim object by object.
            predicted_fixes=sorted(digest.error_signatures),
            risk_objects=digest.target_refs(),
        )
    diagnosis_payload = report.to_dict()
    diagnosis_payload["coverage"] = {
        "missing_capabilities": list(coverage.missing_capabilities),
        "missing_obligations": [item.key for item in coverage.missing_obligations],
    }
    patches = list(state.get("repair_patches", []))
    if patch is not None:
        completed_step_names = [
            str(step.get("name", "")) for step in result.get("steps", []) if isinstance(step, dict)
        ]
        patches.append(
            {
                "patch": patch.model_dump(),
                "resume": resume_plan(patch, completed_step_names),
            }
        )
    language = _reply_language(state)
    summary = localized(_HARDWARE_SUMMARY, language).format(
        status=status,
        completed=authoritative.get("completed_steps", 0),
        files=len(authoritative["actual_files"]),
        blockers=authoritative["release_blockers"],
    )
    if regressed:
        summary += localized(_HARDWARE_ROLLED_BACK, language).format(
            evaluation=evaluation.summary() if evaluation else "it scored worse"
        )
    elif evaluation is not None:
        summary += localized(_HARDWARE_EVALUATED, language).format(evaluation=evaluation.summary())
    if patch is not None:
        summary += localized(_HARDWARE_PATCHED, language).format(
            failure_classes=patch.failure_classes,
            scope=patch.repair_scope,
            actions=len(patch.actions),
        )
    inner_messages = [
        *_tool_messages("ratsnest_run_pcb_pipeline", args, raw),
        AIMessage(content=summary),
    ]
    _workflow_event(
        phase_name,
        "completed" if status == "ok" else ("rolled_back" if regressed else "blocked"),
        detail=f"{authoritative.get('completed_steps', 0)}/17 steps"
        + (f"; {len(digest.errors)} verification error(s)" if digest.findings else ""),
    )
    return {
        "hardware": authoritative,
        "hardware_attempts": attempts,
        "review": {} if repair else state.get("review", {}),
        "review_round": review_round,
        "review_target": str(authoritative.get("run_directory", "")),
        "diagnosis": diagnosis_payload,
        "repair_patches": patches,
        "verification_digest": (
            digest.to_prompt() if not regressed else str(state.get("verification_digest", ""))
        ),
        "verification_signatures": (
            sorted(digest.error_signatures) if not regressed else previous_signatures
        ),
        "change_evaluations": [
            *state.get("change_evaluations", []),
            *([evaluation.model_dump()] if evaluation is not None else []),
        ],
        "trace": _append_trace(
            state,
            agent="Hardware Engineer",
            tool="ratsnest_run_pcb_pipeline",
            status="rolled_back" if regressed else status,
            evidence=(
                f"{authoritative.get('completed_steps', 0)}/17 steps; "
                f"release_ready={authoritative['release_ready']}"
                + (f"; {evaluation.summary()}" if evaluation is not None else "")
            ),
        ),
        "messages": inner_messages,
    }


async def hardware_phase(state: RatsNestWorkflowState) -> dict[str, Any]:
    return await _run_hardware(state, repair=False)


async def hardware_repair_phase(state: RatsNestWorkflowState) -> dict[str, Any]:
    return await _run_hardware(state, repair=True)


async def reviewer_phase(state: RatsNestWorkflowState) -> dict[str, Any]:
    _workflow_event("reviewer", "started")
    project_path = state.get("review_target", "")
    args = {
        "project_path": project_path,
        "report_name": (
            f"{state.get('run_name', 'ratsnest')}-review-{state.get('review_round', 0)}.md"
        ),
        "llm_mode": "auto",
    }
    raw, result, _ = await _call_json_with_retry(
        lambda: ratsnest_review_kicad_project(**args),
        phase="reviewer",
        tool="ratsnest_review_kicad_project",
        attempts=2,
    )
    report_path = Path(str(result.get("report_path", "")))
    report_exists = report_path.is_file()
    status = str(result.get("status", "error"))
    if not report_exists:
        status = "blocked"
        result["status"] = "blocked"
        result["error"] = "review did not produce a real report file"
    result["report_exists"] = report_exists
    summary = localized(_REVIEWER_SUMMARY, _reply_language(state)).format(
        project_path=repr(project_path),
        status=status,
        report_exists=report_exists,
    )
    inner_messages = [
        *_tool_messages("ratsnest_review_kicad_project", args, raw),
        AIMessage(content=summary),
    ]
    _workflow_event(
        "reviewer",
        "completed" if status == "ok" else "blocked",
        detail=str(result.get("report_path", "")),
    )
    return {
        "review": result,
        "trace": _append_trace(
            state,
            agent="Reviewer",
            tool="ratsnest_review_kicad_project",
            status=status,
            evidence=str(result.get("report_path", "")),
        ),
        "messages": inner_messages,
    }


def _actual_artifacts(state: RatsNestWorkflowState) -> list[str]:
    attempts = state.get("hardware_attempts", [])
    if not attempts and state.get("hardware"):
        attempts = [state["hardware"]]
    candidates = [str(path) for attempt in attempts for path in attempt.get("actual_files", [])]
    report_path = str(state.get("review", {}).get("report_path", ""))
    if report_path:
        candidates.append(report_path)
    return list(dict.fromkeys(path for path in candidates if Path(path).is_file()))


def _output_location(state: RatsNestWorkflowState) -> list[tuple[str, str]]:
    """The run directory and the few files worth opening, as (label key, path).

    ``actual_artifacts`` already lists every file the run produced, but a build
    emits thirty-odd gerbers and the one path a user actually wants — the KiCad
    project — is buried in the middle of them. This picks the directory and the
    entry points out of the same verified list, so nothing here is a filename the
    run merely intended to write.
    """
    attempts = state.get("hardware_attempts", [])
    if not attempts and state.get("hardware"):
        attempts = [state["hardware"]]
    directory = ""
    for attempt in reversed(attempts):
        candidate = str(attempt.get("run_directory", "") or "")
        if candidate and Path(candidate).is_dir():
            directory = candidate
            break
    files = _actual_artifacts(state)
    if not directory and not files:
        return []
    found: list[tuple[str, str]] = []
    if directory:
        found.append(("output_directory", directory))

    def _first(suffix: str, *, exclude: str = "") -> str:
        for path in files:
            lowered = path.lower()
            if lowered.endswith(suffix) and (not exclude or exclude not in lowered):
                return path
        return ""

    for key, suffix, exclude in (
        ("output_project", ".kicad_pro", ""),
        ("output_schematic", ".kicad_sch", ""),
        # The unrouted copy is kept for comparison; the routed one is the result.
        ("output_pcb", ".kicad_pcb", ".unrouted."),
        ("output_bom", "_bom.csv", ""),
    ):
        path = _first(suffix, exclude=exclude)
        if path:
            found.append((key, path))
    gerbers = [path for path in files if Path(path).parent.name.lower() == "gerber"]
    if gerbers:
        found.append(("output_gerber", str(Path(gerbers[0]).parent)))
    return found


# Human-readable labels of the execution report. Only prose is translated: every
# status token, path, tool name, net name and count stays verbatim so the report
# remains diff-able and greppable regardless of the reply language.
_REPORT_LABELS: dict[str, dict[str, str]] = {
    "title": {
        "en": "RatsNestPro execution report",
        "zh": "RatsNestPro 执行报告",
    },
    "overall_status": {"en": "Overall status", "zh": "总体状态"},
    "workflow_mode": {"en": "Workflow mode", "zh": "工作流模式"},
    "intent_confidence": {"en": "Intent confidence", "zh": "意图置信度"},
    "intent_decision": {"en": "Intent decision", "zh": "意图判定"},
    "verified_trace": {"en": "Verified execution trace", "zh": "已验证的执行轨迹"},
    "trace_header": {
        "en": "| Agent | Required tool | Status | Evidence |",
        "zh": "| Agent | 必需工具 | 状态 | 证据 |",
    },
    "release_gates": {"en": "Release gates", "zh": "发布门禁"},
    "pipeline_steps": {"en": "Pipeline: {done}/17 steps", "zh": "流水线：{done}/17 步"},
    "freerouting_method": {"en": "Freerouting method", "zh": "Freerouting 方式"},
    "unconnected": {"en": "Unconnected", "zh": "未连接数"},
    "erc_errors": {"en": "kicad-cli ERC errors", "zh": "kicad-cli ERC 错误"},
    "drc_errors": {"en": "kicad-cli DRC errors", "zh": "kicad-cli DRC 错误"},
    "drc_unconnected": {"en": "kicad-cli DRC unconnected", "zh": "kicad-cli DRC 未连接"},
    "independent_review": {"en": "Independent review", "zh": "独立评审"},
    "parts_verification": {"en": "Parts verification", "zh": "选型核验"},
    "blocking_conditions": {"en": "Blocking conditions", "zh": "阻塞条件"},
    "fixed_constraints": {"en": "Fixed component constraints", "zh": "固定器件约束"},
    "accepted_risks": {
        "en": "Accepted risks (user overrode a documented limit)",
        "zh": "已接受的风险（用户选择覆盖手册限值）",
    },
    "accepted_risk_note": {
        "en": (
            "Each item below was reported to the user with its source before the "
            "board was built, and the user chose to proceed with their own value. "
            "These are NOT resolved problems."
        ),
        "zh": (
            "以下每一项在建板前都已连同来源告知用户，用户选择按自己的数值继续。"
            "这些并非已解决的问题。"
        ),
    },
    "advisory_risks": {
        "en": "Advisory findings (no datasheet figure; engineering experience only)",
        "zh": "经验性提示（无手册数据，仅依据工程经验）",
    },
    "symbol_acquisition": {"en": "Symbol acquisition", "zh": "符号获取"},
    "requested": {"en": "Requested", "zh": "请求"},
    "rejected": {"en": "rejected", "zh": "已拒绝"},
    "next": {"en": "next", "zh": "下一步"},
    "ahe_diagnosis": {"en": "AHE failure diagnosis", "zh": "AHE 失败诊断"},
    "missing_capabilities": {"en": "Missing capabilities", "zh": "缺失能力"},
    "missing_obligations": {"en": "Missing obligations", "zh": "缺失义务项"},
    "capability_gaps": {
        "en": "Capability gaps recorded for controlled evolution",
        "zh": "已记录的能力缺口（用于受控演进）",
    },
    "ahe_patch": {"en": "AHE repair patch", "zh": "AHE 修复补丁"},
    "scope": {"en": "Scope", "zh": "范围"},
    "resume_from": {"en": "Resume from", "zh": "恢复自"},
    "resume_steps": {
        "en": "kept {kept} step(s), re-running {rerun}",
        "zh": "保留 {kept} 步，重跑 {rerun} 步",
    },
    "verification_digest": {
        "en": "Verification digest (object level)",
        "zh": "验证摘要（对象级）",
    },
    "repair_round": {
        "en": "Repair round {index} evaluation",
        "zh": "第 {index} 轮修复评估",
    },
    "verdict": {"en": "Verdict", "zh": "结论"},
    "hit_rate": {"en": "hit {rate}", "zh": "命中 {rate}"},
    "fixed_predicted": {"en": "Fixed as predicted", "zh": "按预期修复"},
    "fixed_unpredicted": {"en": "Fixed unpredicted", "zh": "意外修复"},
    "still_failing": {"en": "Predicted but still failing", "zh": "预测可修复但仍失败"},
    "introduced": {"en": "Newly introduced violations", "zh": "新引入的违规"},
    "risks_realised": {"en": "Predicted risks realised", "zh": "已发生的预测风险"},
    "unattributed": {"en": "Unattributed regressions", "zh": "无法归因的回归"},
    "pipeline_stop": {"en": "Pipeline stop detail", "zh": "流水线停止详情"},
    "stopped_at": {"en": "Pipeline stopped at", "zh": "流水线停止于"},
    "actual_artifacts": {"en": "Actual artifacts", "zh": "实际产物"},
    "output_location": {"en": "Where the result is", "zh": "产出位置"},
    "output_directory": {"en": "Directory", "zh": "目录"},
    "output_project": {"en": "KiCad project", "zh": "KiCad 工程"},
    "output_schematic": {"en": "Schematic", "zh": "原理图"},
    "output_pcb": {"en": "PCB (routed)", "zh": "PCB（已布线）"},
    "output_bom": {"en": "BOM", "zh": "BOM"},
    "output_gerber": {"en": "Gerber directory", "zh": "Gerber 目录"},
    "no_artifacts": {
        "en": "None. Expected filenames are not reported as completed.",
        "zh": "无。预期文件名不会被当作已完成来上报。",
    },
    "footer": {
        "en": (
            "This report is generated from tool results and filesystem checks. "
            "A narrative statement cannot override these gates."
        ),
        "zh": "本报告由工具结果与文件系统检查生成，叙述性说明无法覆盖这些门禁。",
    },
}


def final_report(state: RatsNestWorkflowState) -> dict[str, Any]:
    mode = state["workflow_mode"]
    trace = state.get("trace", [])

    def label(key: str, **values: Any) -> str:
        text = localized(_REPORT_LABELS[key], _reply_language(state))
        return text.format(**values) if values else text

    if mode == "research":
        overall = state.get("architecture", {}).get("status", "blocked")
    elif mode == "parts":
        overall = state.get("parts", {}).get("status", "blocked")
    elif mode == "review":
        overall = state.get("review", {}).get("status", "blocked")
    else:
        hardware = state.get("hardware", {})
        review = state.get("review", {})
        parts = state.get("parts", {})
        architecture = state.get("architecture", {})
        overall = (
            "success"
            if hardware.get("release_ready")
            and review.get("status") == "ok"
            and parts.get("status") in {"ok", "partial", "unavailable"}
            and architecture.get("status") == "ok"
            else "blocked"
        )

    intent = state.get("intent", {})
    lines = [
        f"# {label('title')}",
        "",
        f"{label('overall_status')}: **{overall.upper()}**",
        f"{label('workflow_mode')}: `{mode}`",
        f"{label('intent_confidence')}: {intent.get('confidence', 'n/a')}",
        "",
    ]
    # Placed before the trace on purpose: the first thing a user wants after a
    # build is the path to open, and the full artifact list at the bottom is
    # thirty gerbers deep.
    location = _output_location(state)
    if location:
        lines.append(f"## {label('output_location')}")
        lines.append("")
        lines.extend(f"- {label(key)}: `{path}`" for key, path in location)
        lines.append("")
    intent_evidence = intent.get("evidence", [])
    if intent_evidence:
        lines.append(f"## {label('intent_decision')}")
        lines.append("")
        lines.extend(f"- {str(item)}" for item in intent_evidence)
        lines.append("")
    lines.extend(
        [
            f"## {label('verified_trace')}",
            "",
            label("trace_header"),
            "|---|---|---|---|",
        ]
    )
    for item in trace:
        evidence = str(item.get("evidence", "")).replace("|", "\\|")
        lines.append(
            f"| {item.get('agent')} | `{item.get('tool')}` | {item.get('status')} | {evidence} |"
        )

    if mode == "build":
        hardware = state.get("hardware", {})
        lines.extend(
            [
                "",
                f"## {label('release_gates')}",
                "",
                f"- {label('pipeline_steps', done=hardware.get('completed_steps', 0))}",
                (
                    f"- {label('freerouting_method')}: "
                    f"{hardware.get('routing', {}).get('method', 'not_reached')}"
                ),
                (
                    f"- {label('unconnected')}: "
                    f"{hardware.get('routing', {}).get('unconnected', 'unknown')}"
                ),
                (
                    f"- {label('erc_errors')}: "
                    f"{hardware.get('verification', {}).get('erc', {}).get('errors', 'not_run')}"
                ),
                (
                    f"- {label('drc_errors')}: "
                    f"{hardware.get('verification', {}).get('drc', {}).get('errors', 'not_run')}"
                ),
                (
                    f"- {label('drc_unconnected')}: "
                    f"{hardware.get('verification', {}).get('drc', {}).get('unconnected', 'not_run')}"
                ),
                (
                    f"- {label('independent_review')}: "
                    f"{state.get('review', {}).get('status', 'not_run')}"
                ),
                (
                    f"- {label('parts_verification')}: "
                    f"{state.get('parts', {}).get('status', 'not_run')}"
                ),
            ]
        )
        blockers = hardware.get("release_blockers", [])
        if blockers:
            lines.extend(["", f"## {label('blocking_conditions')}", ""])
            lines.extend(f"- {blocker}" for blocker in blockers)

        # A risk the user accepted is not a risk that went away. Reporting it in
        # its own section — with the page that was overridden — is what makes the
        # override auditable instead of merely permitted.
        verdicts = [v for v in state.get("claim_verdicts", []) if isinstance(v, dict)]
        accepted = [
            v
            for v in verdicts
            if not v.get("ok") and v.get("acknowledged") and v.get("tier") == "hard"
        ]
        if accepted:
            lines.extend(
                [
                    "",
                    f"## {label('accepted_risks')}",
                    "",
                    label("accepted_risk_note"),
                    "",
                ]
            )
            for item in accepted:
                lines.append(
                    f"- `{item.get('slot', '')}`: user value "
                    f"**{item.get('value')} {item.get('unit', '')}** on "
                    f"{item.get('device') or 'the design'} "
                    f'(requested: "{item.get("quote", "")}")'
                )
                lines.append(f"  - {item.get('message', '')}")
                if item.get("citation"):
                    lines.append(f"  - Source: {item['citation']}")
                lines.append(f"  - Acknowledged as: `{ACK_PREFIX} {item.get('ack_token', '')}`")

        advisory = [v for v in verdicts if not v.get("ok") and v.get("tier") == "advisory"]
        if advisory:
            lines.extend(["", f"## {label('advisory_risks')}", ""])
            for item in advisory:
                lines.append(f"- `{item.get('slot', '')}`: {item.get('message', '')}")

        constraints = state.get("component_constraints", [])
        if constraints:
            lines.extend(["", f"## {label('fixed_constraints')}", ""])
            for constraint in constraints:
                if not isinstance(constraint, dict):
                    continue
                lines.append(
                    f"- `{constraint.get('role', '')}`: "
                    f"{constraint.get('manufacturer_part_number', '')} "
                    f"(substitution={constraint.get('substitution', '')}"
                    + (
                        f", package={constraint.get('package')}"
                        if constraint.get("package")
                        else ""
                    )
                    + ")"
                )

        acquisition = state.get("architecture", {}).get("symbol_acquisition", {})
        if acquisition:
            lines.extend(["", f"## {label('symbol_acquisition')}", ""])
            lines.append(
                f"- {label('requested')} `{acquisition.get('requested_mpn', '')}`: tier="
                f"{acquisition.get('tier', '')}, resolved={acquisition.get('resolved')}"
            )
            for rejected in acquisition.get("rejected", [])[:6]:
                if isinstance(rejected, dict):
                    lines.append(
                        f"  - {label('rejected')} `{rejected.get('lib_id', '')}`: "
                        f"{rejected.get('reason', '')}"
                    )
            for action in acquisition.get("next_actions", []):
                lines.append(f"  - {label('next')}: {action}")

        diagnosis = state.get("diagnosis", {})
        if diagnosis.get("diagnoses"):
            lines.extend(["", f"## {label('ahe_diagnosis')}", ""])
            for item in diagnosis["diagnoses"][:12]:
                if not isinstance(item, dict):
                    continue
                lines.append(
                    f"- `{item.get('failure_class', '')}` -> "
                    f"{item.get('strategy', '')}"
                    + (f" (scope `{item.get('repair_scope')}`)" if item.get("repair_scope") else "")
                    + f": {item.get('summary', '')[:180]}"
                )
            coverage = diagnosis.get("coverage", {})
            if coverage.get("missing_capabilities"):
                lines.append(
                    f"- {label('missing_capabilities')}: {coverage['missing_capabilities']}"
                )
            if coverage.get("missing_obligations"):
                lines.append(
                    f"- {label('missing_obligations')}: {coverage['missing_obligations'][:12]}"
                )
            if diagnosis.get("capability_gaps"):
                lines.append(f"- {label('capability_gaps')}: {diagnosis['capability_gaps']}")

        for entry in state.get("repair_patches", [])[:6]:
            if not isinstance(entry, dict):
                continue
            patch = entry.get("patch", {})
            resume = entry.get("resume", {})
            lines.extend(["", f"## {label('ahe_patch')}", ""])
            lines.append(
                f"- {label('scope')} `{patch.get('repair_scope', '')}`; actions="
                f"{[action.get('type') for action in patch.get('actions', [])]}"
            )
            lines.append(
                f"- {label('resume_from')} `{resume.get('resume_from', '')}`; "
                + label(
                    "resume_steps",
                    kept=len(resume.get("keep_steps", [])),
                    rerun=len(resume.get("rerun_steps", [])),
                )
            )

        digest_text = str(state.get("verification_digest", ""))
        if digest_text:
            lines.extend(
                [
                    "",
                    f"## {label('verification_digest')}",
                    "",
                    "```",
                    digest_text,
                    "```",
                ]
            )

        for index, item in enumerate(state.get("change_evaluations", []), start=1):
            if not isinstance(item, dict):
                continue
            lines.extend(["", f"## {label('repair_round', index=index)}", ""])
            lines.append(
                f"- {label('verdict')}: **{item.get('verdict', '')}** "
                f"({label('hit_rate', rate=item.get('hit_rate', ''))})"
            )
            if item.get("actually_fixed"):
                lines.append(f"- {label('fixed_predicted')}: {len(item['actually_fixed'])}")
            if item.get("unpredicted_fixes"):
                lines.append(f"- {label('fixed_unpredicted')}: {len(item['unpredicted_fixes'])}")
            if item.get("still_failed"):
                lines.append(f"- {label('still_failing')}: {len(item['still_failed'])}")
            if item.get("introduced"):
                lines.append(f"- {label('introduced')}: {len(item['introduced'])}")
            if item.get("risk_realized"):
                lines.append(f"- {label('risks_realised')}: {item['risk_realized'][:6]}")
            if item.get("unattributed_regressions"):
                lines.append(f"- {label('unattributed')}: {item['unattributed_regressions'][:6]}")

        blocked_steps = [step for step in hardware.get("steps", []) if step.get("blocked")]
        if blocked_steps:
            lines.extend(["", f"## {label('pipeline_stop')}", ""])
            for step in blocked_steps:
                lines.append(
                    f"- {label('stopped_at')} `{step.get('name', 'unknown')}`: "
                    f"{step.get('summary', '')}"
                )
                for check in step.get("failed_checks", []):
                    message = " ".join(str(check.get("message", "")).split())
                    lines.append(f"  - `{check.get('name', 'unknown')}`: {message}")

        lines.extend(["", f"## {label('actual_artifacts')}", ""])
        artifacts = _actual_artifacts(state)
        lines.extend(f"- `{path}`" for path in artifacts)
        if not artifacts:
            lines.append(f"- {label('no_artifacts')}")

    # Which data the system did not know, and what became of it. Kept outside the
    # build branch: a review or a parts lookup can be waiting on a decision too.
    data_ledger = ledger(
        [d for d in state.get("open_decisions", []) if isinstance(d, dict)],
        [r for r in state.get("resolved_decisions", []) if isinstance(r, dict)],
        _reply_language(state),
    )
    if data_ledger:
        lines.extend(["", data_ledger])

    lines.extend(["", label("footer")])
    return {"messages": [AIMessage(content="\n".join(lines))]}


def _after_initialize(state: RatsNestWorkflowState) -> str:
    # A partial answer must finish the exact menu already shown before intent,
    # risk, or hardware work can advance. Fresh requests clear this state in
    # ``initialize``, so this cannot capture an unrelated later message.
    if state.get("open_decisions"):
        return "clarify_open_decisions"
    if state.get("intent", {}).get("needs_clarification"):
        return "clarify"
    # A datasheet conflict is asked about BEFORE any design work starts, so a
    # change of mind costs nothing to unwind. Only a build actually produces a
    # board, so only a build is gated: a review or a parts lookup cannot damage
    # anything by proceeding.
    if state.get("pending_acks") and state["workflow_mode"] == "build":
        return "clarify_risk"
    # A gap a code reading of the requirement can find needs no evidence, so it
    # is raised before the Architect spends a model call rather than two phases
    # later. ``missing_data_asked`` then suppresses the post-parts ask, so a run
    # still asks at most once and never splits questions across turns.
    if (
        state["workflow_mode"] == "build"
        and not state.get("missing_data_asked")
        and _pending_assumption_decisions(state)
    ):
        return "clarify_missing_data"
    return {
        "build": "architect_phase",
        "research": "architect_phase",
        "parts": "parts_phase",
        "review": "reviewer_phase",
    }[state["workflow_mode"]]


def _after_architect(state: RatsNestWorkflowState) -> str:
    return (
        "parts_phase"
        if state["workflow_mode"] == "build"
        and state.get("architecture", {}).get("status") in {"ok", "partial"}
        else "final_report"
    )


def _after_parts(state: RatsNestWorkflowState) -> str:
    if not (
        state["workflow_mode"] == "build"
        and state.get("parts", {}).get("status") in {"ok", "partial", "unavailable"}
    ):
        return "final_report"
    # Same argument as the risk gate: ask while nothing has been written yet.
    if not state.get("missing_data_asked") and _pending_assumption_decisions(state):
        return "clarify_missing_data"
    return "hardware_phase"


def _ahe_enabled() -> bool:
    # Lets a run opt out of the in-task repair loop without editing the graph.
    import os

    raw = os.getenv("RATSNESTPRO_DISABLE_AHE", "")
    return raw.strip().lower() not in {"1", "true", "yes", "on"}


def _after_hardware(state: RatsNestWorkflowState) -> str:
    # Section 4.3: a blocked attempt with a recoverable diagnosis is repaired
    # in-task; an unrecoverable one falls through to review/reporting.
    if not state.get("hardware", {}).get("release_ready"):
        diagnosis = state.get("diagnosis", {})
        rounds_left = state.get("review_round", 0) < state.get("max_review_rounds", 2)
        if (
            _ahe_enabled()
            and diagnosis.get("should_attempt_repair")
            and state.get("repair_patches")
            and rounds_left
        ):
            return "hardware_repair_phase"
    return (
        "reviewer_phase" if state.get("hardware", {}).get("project_available") else "final_report"
    )


def _after_review(state: RatsNestWorkflowState) -> str:
    should_repair = (
        _ahe_enabled()
        and state["workflow_mode"] == "build"
        and state.get("review", {}).get("status") == "blocked"
        and state.get("review_round", 0) < state.get("max_review_rounds", 2)
    )
    return "hardware_repair_phase" if should_repair else "final_report"


builder = StateGraph(RatsNestWorkflowState)
builder.add_node("initialize", initialize)
builder.add_node("clarify", clarify)
builder.add_node("clarify_risk", clarify_risk)
builder.add_node("clarify_missing_data", clarify_missing_data)
builder.add_node("clarify_open_decisions", clarify_open_decisions)
builder.add_node("architect_phase", architect_phase)
builder.add_node("parts_phase", parts_phase)
builder.add_node("hardware_phase", hardware_phase)
builder.add_node("hardware_repair_phase", hardware_repair_phase)
builder.add_node("reviewer_phase", reviewer_phase)
builder.add_node("final_report", final_report)

builder.add_edge(START, "initialize")
builder.add_conditional_edges("initialize", _after_initialize)
builder.add_edge("clarify", END)
builder.add_edge("clarify_risk", END)
builder.add_edge("clarify_missing_data", END)
builder.add_edge("clarify_open_decisions", END)
builder.add_conditional_edges("architect_phase", _after_architect)
builder.add_conditional_edges("parts_phase", _after_parts)
builder.add_conditional_edges("hardware_phase", _after_hardware)
builder.add_conditional_edges("hardware_repair_phase", _after_hardware)
builder.add_conditional_edges("reviewer_phase", _after_review)
builder.add_edge("final_report", END)

ratsnestpro_multi_agent = builder.compile()
