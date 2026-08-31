"""Open decisions as options, not as open questions.

An open question ("which supply voltage do you need?") puts the burden of
knowing the answer on the user and leaves the system with no record of what it
did not know. This module turns every such gap into a closed set of options with
a stated basis, so that

* the user answers by CHOOSING rather than by composing a value;
* the choice arrives as a token this module can validate, which keeps the
  ``ACK-RISK`` safety argument intact — a model may propose a mapping from prose
  to a token, but only code decides, and only against the options the user was
  actually shown;
* the set of unknowns is data (``OpenDecision`` records in the graph state)
  rather than prose buried in a chat message, so the final report can list what
  is still undecided and what the user settled.

Options never invent a number. When a datasheet limit is violated, the "stay
inside the limit" option delegates the concrete value to the phase that holds
the fact sheets and cites them; it does not guess a value here, because a value
without provenance is exactly what the project forbids.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agents.language import localized

# The answer transport. Mirrors ``ACK-RISK:`` deliberately: one line, one
# decision, greppable in a transcript and parseable without a model.
PICK_PREFIX = "PICK:"
# What a settled decision looks like inside the requirement text. Model-facing,
# so it is written in English regardless of the reply language.
DECISION_PREFIX = "DECISION:"
# Fenced block that carries the options as data for a frontend that can render
# them as radio buttons. Text stays authoritative; this is an enhancement.
PAYLOAD_FENCE = "ratsnest-decisions"

_PICK_RE = re.compile(
    rf"{PICK_PREFIX}\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([A-Za-z0-9_.+-]+)",
    re.IGNORECASE,
)
# Shorthand for "item 1, option B". The index and the key are both validated
# against the decisions actually offered, which is what keeps a board dimension
# like "60x40" from being read as item 60 option X.
_SHORTHAND_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,2})\s*[.):\-]?\s*([A-Za-z])(?![A-Za-z0-9])")
_PAYLOAD_RE = re.compile(
    rf"```{PAYLOAD_FENCE}\s*(\{{.*?\}})\s*```",
    re.DOTALL,
)


class DecisionOption(BaseModel):
    """One selectable answer.

    ``value`` is the model-facing directive appended to the requirement when this
    option wins. ``ack_token`` is non-empty only for the option that accepts a
    documented risk, so choosing it feeds the existing acknowledgement path
    instead of inventing a second one. ``free_text`` marks the escape hatch: the
    user supplies a value the option set cannot enumerate.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    value: str = ""
    basis: str = ""
    ack_token: str = ""
    free_text: bool = False


class OpenDecision(BaseModel):
    """One thing the system does not know, with every answer it will accept."""

    model_config = ConfigDict(extra="forbid")

    slot: str
    question: str
    kind: str = "value"
    options: list[DecisionOption] = Field(default_factory=list, max_length=6)
    recommended_key: str = ""
    citation: str = ""

    def option(self, key: str) -> DecisionOption | None:
        wanted = key.strip().upper()
        for option in self.options:
            if option.key.upper() == wanted:
                return option
        return None


# --------------------------------------------------------------------------- #
# Localized surface text
# --------------------------------------------------------------------------- #

_HEADER = {
    "en": "{count} item(s) are undecided. Pick one option for each, then I continue.",
    "zh": "有 {count} 项数据尚未确定。每项选一个选项，我就继续。",
}
_RECOMMENDED = {"en": "recommended", "zh": "推荐"}
_FOOTER = {
    "en": (
        "Answer by copying a token, e.g. `PICK: {example_slot}={example_key}`, or "
        "with the shorthand `1{example_key}`. Several on one line is fine. "
        "Anything you leave unanswered stays on the open list and I ask again "
        "rather than guessing."
    ),
    "zh": (
        "回复时复制 token，例如 `PICK: {example_slot}={example_key}`，"
        "或用简写 `1{example_key}`。多项可以写在一行。"
        "没回答的项会留在未定清单里，我会再问一次，不会替你猜。"
    ),
}
_FREE_TEXT_HINT = {
    "en": "choose this and write the value in the same reply",
    "zh": "选它并在同一条回复里写出数值",
}

_RISK_QUESTION = {
    "en": "{slot} — how should this be handled?",
    "zh": "{slot} —— 怎么处理？",
}
_RISK_OPT_LIMIT = {
    "en": "Stay inside the documented limit (source as cited above)",
    "zh": "改到手册限值内（依据同上）",
}
_RISK_OPT_ACCEPT = {
    "en": "Keep {value} and accept the risk (recorded in the final report)",
    "zh": "保留 {value} 并接受风险（会记入最终报告）",
}
_RISK_OPT_CUSTOM = {
    "en": "Use a different value for {slot}",
    "zh": "{slot} 换成别的数值",
}
# Model-facing: this text becomes part of the requirement the pipeline reads.
_RISK_DIRECTIVE_LIMIT = (
    "For {slot}, the requested {rejected} is rejected. Use a value that stays "
    "inside the documented limit and cite {citation} for it. Do not silently "
    "keep the rejected value."
)
_CUSTOM_DIRECTIVE = "For {slot}, the user supplied: {text}"

_ASSUMPTION_QUESTION = {
    "en": "{slot} — not stated in your requirement. I assumed {assumed}.",
    "zh": "{slot} —— 需求里没写，我先按 {assumed} 处理。",
}
_ASSUMPTION_BASIS = {"en": "assumption", "zh": "假设值"}
_ASSUMPTION_OPT_KEEP = {
    "en": "Keep {assumed} — {basis}",
    "zh": "就用 {assumed} —— {basis}",
}
_ASSUMPTION_OPT_ALT = {"en": "Use {alternative} instead", "zh": "改用 {alternative}"}
_ASSUMPTION_OPT_CUSTOM = {
    "en": "Something else for {slot}",
    "zh": "{slot} 换成别的",
}
# Model-facing: becomes part of the requirement every later phase reads.
_ASSUMPTION_DIRECTIVE = (
    "For {slot}, the user confirmed: {value}. Use exactly this and do not substitute it."
)

_TASK_KIND_QUESTION = {
    "en": "What kind of task is this?",
    "zh": "这次是哪种任务？",
}
_TASK_NEW = {"en": "New design, build it from scratch", "zh": "新建设计，从零做一块板"}
_TASK_REVIEW = {
    "en": "Review an existing KiCad project (give the path in the same reply)",
    "zh": "审查已有 KiCad 工程（在同一条回复里给出工程路径）",
}
_TASK_PARTS = {
    "en": "Part selection only, do not generate a project",
    "zh": "只做选型，不要生成工程",
}
_TASK_NEW_DIRECTIVE = "This is a NEW PCB design task. There is no existing KiCad project to review."
_TASK_PARTS_DIRECTIVE = (
    "Only do grounded part selection and datasheet lookup. Do not generate a KiCad project."
)

# Report section
_LEDGER_TITLE = {"en": "Data ledger — decided and undecided", "zh": "数据清单 —— 已定与未定"}
_LEDGER_SETTLED = {"en": "Settled by you", "zh": "你已选定"}
_LEDGER_OPEN = {"en": "Still undecided", "zh": "仍未确定"}
_LEDGER_EMPTY = {
    "en": "Nothing is waiting on a decision from you.",
    "zh": "没有等待你决策的数据。",
}


# --------------------------------------------------------------------------- #
# Building decisions out of what the system found unknown
# --------------------------------------------------------------------------- #


def risk_decisions(verdicts: list[dict[str, Any]], language: str) -> list[OpenDecision]:
    """One decision per unacknowledged datasheet conflict.

    The option that accepts the risk carries the verdict's own ``ack_token``, so
    the downstream behaviour is byte-for-byte what the free-text acknowledgement
    produced: same token, same scope, same downgrade in the pipeline.
    """
    decisions: list[OpenDecision] = []
    for verdict in verdicts:
        if not isinstance(verdict, dict):
            continue
        token = str(verdict.get("ack_token") or "")
        if verdict.get("ok") or verdict.get("acknowledged") or not token:
            continue
        slot = str(verdict.get("slot") or "")
        unit = str(verdict.get("unit") or "")
        shown = f"{verdict.get('value')}{' ' + unit if unit else ''}"
        citation = str(verdict.get("citation") or "") or "n/a"
        decisions.append(
            OpenDecision(
                slot=slot,
                kind="risk",
                citation=citation,
                question=localized(_RISK_QUESTION, language).format(slot=slot),
                options=[
                    DecisionOption(
                        key="A",
                        label=localized(_RISK_OPT_LIMIT, language),
                        value=_RISK_DIRECTIVE_LIMIT.format(
                            slot=slot, rejected=shown, citation=citation
                        ),
                        basis=citation,
                    ),
                    DecisionOption(
                        key="B",
                        label=localized(_RISK_OPT_ACCEPT, language).format(value=shown),
                        ack_token=token,
                    ),
                    DecisionOption(
                        key="C",
                        label=localized(_RISK_OPT_CUSTOM, language).format(slot=slot),
                        free_text=True,
                    ),
                ],
                recommended_key="A",
            )
        )
    return decisions


def assumption_decisions(
    assumptions: list[dict[str, Any]],
    language: str,
    *,
    settled: frozenset[str] = frozenset(),
    limit: int = 6,
) -> list[OpenDecision]:
    """One decision per value the Architect had to assume for lack of a source.

    The Architect never stops for missing material — it adopts a conventional
    value and records it. That keeps a run alive but leaves the user with an
    assumption they never saw until the final report. This turns each recorded
    assumption into a choice whose FIRST option is that same assumption, so
    submitting the form unchanged is byte-for-byte the old behaviour and the
    design still never waits on an answer it could default.

    Nothing here invents a value: ``assumed`` and ``alternatives`` come from the
    phase that held the evidence, and an entry without an assumed value is
    dropped rather than turned into an open question.
    """
    decisions: list[OpenDecision] = []
    for item in assumptions:
        if len(decisions) >= limit:
            break
        if not isinstance(item, dict):
            continue
        slot = str(item.get("slot") or "").strip()
        assumed = str(item.get("assumed") or "").strip()
        if not slot or not assumed or slot in settled:
            continue
        basis = str(item.get("basis") or "").strip() or localized(_ASSUMPTION_BASIS, language)
        options = [
            DecisionOption(
                key="A",
                label=localized(_ASSUMPTION_OPT_KEEP, language).format(
                    assumed=assumed, basis=basis
                ),
                value=_ASSUMPTION_DIRECTIVE.format(slot=slot, value=assumed),
                basis=basis,
            )
        ]
        alternatives = [
            str(alt).strip()
            for alt in (item.get("alternatives") or [])
            if str(alt).strip() and str(alt).strip() != assumed
        ]
        for offset, alternative in enumerate(alternatives[:4]):
            options.append(
                DecisionOption(
                    key=chr(ord("B") + offset),
                    label=localized(_ASSUMPTION_OPT_ALT, language).format(
                        alternative=alternative
                    ),
                    value=_ASSUMPTION_DIRECTIVE.format(slot=slot, value=alternative),
                )
            )
        options.append(
            DecisionOption(
                key=chr(ord("B") + len(alternatives[:4])),
                label=localized(_ASSUMPTION_OPT_CUSTOM, language).format(slot=slot),
                free_text=True,
            )
        )
        question = str(item.get("question") or "").strip() or localized(
            _ASSUMPTION_QUESTION, language
        ).format(slot=slot, assumed=assumed)
        decisions.append(
            OpenDecision(
                slot=slot,
                kind="assumption",
                citation=basis,
                question=question,
                options=options,
                recommended_key="A",
            )
        )
    return decisions


def intent_decisions(intent: dict[str, Any], language: str) -> list[OpenDecision]:
    """The routing question, asked as three exclusive options.

    Free text here used to cost a whole turn to interpret: "check my board" is a
    review with no path, a new build, or a parts lookup, and guessing wrong
    either fabricates a design or refuses to start one.
    """
    if not intent.get("needs_clarification"):
        return []
    question = str(intent.get("clarification") or "").strip() or localized(
        _TASK_KIND_QUESTION, language
    )
    return [
        OpenDecision(
            slot="task_kind",
            kind="intent",
            question=question,
            options=[
                DecisionOption(
                    key="A",
                    label=localized(_TASK_NEW, language),
                    value=_TASK_NEW_DIRECTIVE,
                ),
                DecisionOption(
                    key="B",
                    label=localized(_TASK_REVIEW, language),
                    free_text=True,
                ),
                DecisionOption(
                    key="C",
                    label=localized(_TASK_PARTS, language),
                    value=_TASK_PARTS_DIRECTIVE,
                ),
            ],
            recommended_key="A",
        )
    ]


def part_selection_decisions(
    candidates: list[dict[str, Any]],
    language: str,
    *,
    settled: frozenset[str] = frozenset(),
    limit: int = 6,
) -> list[OpenDecision]:
    """Offer a choice only when a named part has genuinely tied candidates."""
    decisions: list[OpenDecision] = []
    for item in candidates:
        if len(decisions) >= limit or not isinstance(item, dict):
            break
        query = str(item.get("query") or "").strip()
        options_data = [candidate for candidate in item.get("results", []) if isinstance(candidate, dict)]
        if not query or len(options_data) < 2:
            continue
        slot = f"catalog_{re.sub(r'[^A-Za-z0-9]+', '_', query).strip('_').lower()}"
        if slot in settled:
            continue
        top = options_data[:4]
        if not all(str(candidate.get("mpn") or "").strip() for candidate in top):
            continue
        first = top[0]
        second = top[1]
        first_score = (
            str(first.get("package_match") or "unknown"),
            bool(first.get("basic")),
            str(first.get("provider") or "catalog"),
            int(first.get("stock") or 0) > 0,
        )
        second_score = (
            str(second.get("package_match") or "unknown"),
            bool(second.get("basic")),
            str(second.get("provider") or "catalog"),
            int(second.get("stock") or 0) > 0,
        )
        first_price = float(first.get("price") or 0)
        second_price = float(second.get("price") or 0)
        price_near = (
            not first_price
            or not second_price
            or abs(first_price - second_price) / max(first_price, second_price) <= 0.1
        )
        first_lead = first.get("lead_days")
        second_lead = second.get("lead_days")
        lead_near = (
            first_lead is None
            or second_lead is None
            or abs(int(first_lead) - int(second_lead)) <= 7
        )
        same_mpn = str(first.get("mpn") or "").casefold() == str(
            second.get("mpn") or ""
        ).casefold()
        if first_score != second_score or not price_near or not lead_near or same_mpn:
            continue
        options = []
        for index, candidate in enumerate(top, start=0):
            provider = str(candidate.get("provider") or "catalog")
            mpn = str(candidate.get("mpn") or "")
            lcsc = str(candidate.get("lcsc") or "")
            package = str(candidate.get("package") or "")
            key = chr(ord("A") + index)
            options.append(
                DecisionOption(
                    key=key,
                    label=(
                        f"{mpn} / {lcsc or 'no LCSC'} / {package or 'package unknown'} "
                        f"({provider})"
                    ),
                    value=(
                        f"Use catalog candidate for {query}: MPN={mpn}; "
                        f"LCSC={lcsc}; provider={provider}; do not silently substitute."
                    ),
                    basis=str(candidate.get("snapshot_id") or "catalog query snapshot"),
                )
            )
        decisions.append(
            OpenDecision(
                slot=slot,
                kind="part_selection",
                question=(
                    f"{query} has multiple equally ranked catalog candidates; "
                    "which one should the BOM use?"
                    if language != "zh"
                    else f"{query} 有多个排名相同的目录候选，BOM 应使用哪一个？"
                ),
                options=options,
                recommended_key="A",
            )
        )
    return decisions


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render(decisions: list[OpenDecision], language: str) -> str:
    """The human-readable form. Every option line carries its own token."""
    if not decisions:
        return ""
    lines = [localized(_HEADER, language).format(count=len(decisions)), ""]
    for index, decision in enumerate(decisions, start=1):
        lines.append(f"{index}. {decision.question}")
        for option in decision.options:
            marks = []
            if option.key.upper() == decision.recommended_key.upper():
                marks.append(localized(_RECOMMENDED, language))
            if option.free_text:
                marks.append(localized(_FREE_TEXT_HINT, language))
            suffix = f"（{'；'.join(marks)}）" if marks and language == "zh" else ""
            if marks and language != "zh":
                suffix = f" ({'; '.join(marks)})"
            lines.append(f"   - `{pick_token(decision.slot, option.key)}` — {option.label}{suffix}")
        lines.append("")
    first = decisions[0]
    lines.append(
        localized(_FOOTER, language).format(
            example_slot=first.slot,
            example_key=(first.recommended_key or (first.options[0].key if first.options else "A")),
        )
    )
    return "\n".join(lines)


def payload_block(decisions: list[OpenDecision]) -> str:
    """The same options as data, for a frontend that renders real radio buttons."""
    if not decisions:
        return ""
    payload = {"decisions": [decision.model_dump() for decision in decisions]}
    return f"```{PAYLOAD_FENCE}\n{json.dumps(payload, ensure_ascii=False)}\n```"


def split_payload(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Split a rendered message into prose and option data.

    A frontend that understands the block renders controls and hides the JSON; a
    frontend that does not shows it verbatim, which is noisy but never wrong.
    """
    match = _PAYLOAD_RE.search(text or "")
    if not match:
        return text, []
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return text, []
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        return text, []
    stripped = (text[: match.start()] + text[match.end() :]).strip()
    return stripped, [d for d in decisions if isinstance(d, dict)]


def pick_token(slot: str, key: str) -> str:
    return f"{PICK_PREFIX} {slot}={key.upper()}"


# --------------------------------------------------------------------------- #
# Reading an answer
# --------------------------------------------------------------------------- #


def parse_picks(reply: str, decisions: list[OpenDecision]) -> dict[str, str]:
    """Choices found in ``reply``, keyed by slot, without calling a model.

    Validation is structural, not charitable: a slot must be one that was asked
    about and the key must be one of that slot's options. Everything else is
    dropped, which leaves the item unanswered — the safe direction, because the
    cost of a missed pick is one more question and the cost of a wrong one is a
    decision the user never made.
    """
    if not reply or not decisions:
        return {}
    by_slot = {decision.slot.lower(): decision for decision in decisions}
    picks: dict[str, str] = {}
    for slot_text, key_text in _PICK_RE.findall(reply):
        decision = by_slot.get(slot_text.lower())
        if decision is None:
            continue
        option = decision.option(key_text)
        if option is not None:
            picks[decision.slot] = option.key
    for index_text, key_text in _SHORTHAND_RE.findall(reply):
        index = int(index_text)
        if not 1 <= index <= len(decisions):
            continue
        decision = decisions[index - 1]
        if decision.slot in picks:
            continue
        option = decision.option(key_text)
        if option is not None:
            picks[decision.slot] = option.key
    return picks


def resolve(
    decisions: list[OpenDecision],
    picks: dict[str, str],
    *,
    reply: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    """Turn validated picks into records, and report what is still open."""
    resolved: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for decision in decisions:
        key = picks.get(decision.slot, "")
        option = decision.option(key) if key else None
        if option is None:
            unresolved.append(decision.slot)
            continue
        value = option.value
        if option.free_text:
            supplied = _free_text_remainder(reply, decisions)
            value = _CUSTOM_DIRECTIVE.format(slot=decision.slot, text=supplied) if supplied else ""
            if not supplied:
                # A free-text option with nothing supplied answers nothing.
                unresolved.append(decision.slot)
                continue
        resolved.append(
            {
                "slot": decision.slot,
                "kind": decision.kind,
                "key": option.key,
                "label": option.label,
                "value": value,
                "ack_token": option.ack_token,
                "citation": option.basis or decision.citation,
            }
        )
    return resolved, unresolved


def _free_text_remainder(reply: str, decisions: list[OpenDecision]) -> str:
    """The reply with the answer tokens removed — the value the user typed.

    Only shorthand that actually names an offered item and option is removed. A
    blanket strip ate the value it was supposed to preserve: in "1C use 3.3 V",
    the "3 V" tail looks exactly like "item 3, option V".
    """

    def _drop_valid_shorthand(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if 1 <= index <= len(decisions) and decisions[index - 1].option(match.group(2)):
            return " "
        return match.group(0)

    without_tokens = _PICK_RE.sub(" ", reply or "")
    without_shorthand = _SHORTHAND_RE.sub(_drop_valid_shorthand, without_tokens)
    return " ".join(without_shorthand.split()).strip(" .,;:—-")


def accepted_tokens(resolved: list[dict[str, Any]]) -> set[str]:
    """Acknowledgement tokens implied by the chosen options."""
    return {str(record.get("ack_token")) for record in resolved if record.get("ack_token")}


def apply_decisions(requirement: str, resolved: list[dict[str, Any]]) -> str:
    """Append settled decisions so every later phase reads the same text.

    The requirement text is the transport, the same trick ``_with_acks`` uses:
    one decision, recorded once, honoured by the agent and by the pipeline.
    """
    lines = [
        f"{DECISION_PREFIX} {record['slot']}={record['key']} — {record['value']}"
        for record in resolved
        if str(record.get("value") or "").strip()
    ]
    if not lines:
        return requirement
    return f"{requirement}\n" + "\n".join(lines)


def ledger(
    open_decisions: list[dict[str, Any]],
    resolved: list[dict[str, Any]],
    language: str,
) -> str:
    """The report section that names what is known and what is not."""
    settled = [record for record in resolved if isinstance(record, dict)]
    pending = [decision for decision in open_decisions if isinstance(decision, dict)]
    if not settled and not pending:
        return ""
    lines = [f"### {localized(_LEDGER_TITLE, language)}", ""]
    if settled:
        lines.append(f"**{localized(_LEDGER_SETTLED, language)}**")
        for record in settled:
            citation = str(record.get("citation") or "")
            tail = f" — {citation}" if citation else ""
            lines.append(
                f"- `{record.get('slot')}` = {record.get('key')} · {record.get('label')}{tail}"
            )
        lines.append("")
    if pending:
        lines.append(f"**{localized(_LEDGER_OPEN, language)}**")
        for decision in pending:
            keys = "/".join(
                str(option.get("key"))
                for option in decision.get("options", [])
                if isinstance(option, dict)
            )
            lines.append(f"- `{decision.get('slot')}` — {decision.get('question')} [{keys}]")
        lines.append("")
    if not settled and not pending:
        lines.append(localized(_LEDGER_EMPTY, language))
    return "\n".join(lines).rstrip()


def to_state(decisions: list[OpenDecision]) -> list[dict[str, Any]]:
    return [decision.model_dump() for decision in decisions]


def from_state(payload: Any) -> list[OpenDecision]:
    """Rebuild decisions from checkpointed plain data, dropping anything invalid."""
    if not isinstance(payload, list):
        return []
    decisions: list[OpenDecision] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            decisions.append(OpenDecision.model_validate(item))
        except Exception:  # noqa: BLE001 - a corrupt record must not break a turn
            continue
    return decisions
