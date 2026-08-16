"""INF1 — turning device fact sheets into grounded text for one pipeline step.

Where this sits
---------------
Three modules divide the fact base between them:

* :mod:`ratsnestpro.eda.factsheet` answers *what does the datasheet say?* — 37
  slots across 17 devices, every asserted value carrying page-level provenance.
* :mod:`ratsnestpro.eda.factgate` answers *what does this design do, and is it
  legal?* — it reads values off the artifacts and produces verdicts.
* this module answers *what should this step be looking at?*

Why the third one is needed
---------------------------
Of the 37 slots, 25 declare ``Comparison.NONE``: a vendor's decoupling table, an
oscillator's layout keepout, a supply domain produced by an on-chip LDO. None of
them is a threshold, so ``factgate`` cannot gate on them and — before this module
— nothing else read them either. They were collected, cited, tested, and then
had no effect on anything the pipeline produced.

The remaining twelve *are* gates, but gating is judgement after the fact. The
model proposed a design without ever seeing the datasheet, the gate rejected it,
and the repair loop guessed again. Facts arriving *before* the proposal are worth
more than the same facts arriving after it.

Two rules this module will not break
------------------------------------
1. **Hard facts stay separated from soft knowledge.**
   :mod:`ratsnestpro.knowledge.store` states plainly that retrieved corpus text
   is "never treated as fact". A brief is therefore rendered as its own block
   with its own citations, never merged into the retrieval string.

2. **Silence is never rendered as permission.** A slot nobody has extracted yet
   is not a slot without a limit. ``factsheet`` spent a four-state ``Status`` on
   that distinction; a renderer that dropped ``not_asserted`` would hand it
   straight back, because a model reading a fact list with no entry for
   "absolute maximum input voltage" concludes there isn't one.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ratsnestpro.eda.factgate import part_name, resolve_sheets
from ratsnestpro.eda.factsheet import (
    SLOT_SPECS,
    CapRequirement,
    ClockLayoutRule,
    Comparison,
    ConditionalFact,
    ConflictingRangeFact,
    Consequence,
    DecouplingRule,
    FactSheetBase,
    FixedFact,
    InductorRequirement,
    MandatoryPeripheral,
    PerPinCap,
    PinFact,
    QualitativeFact,
    RangeFact,
    ResetRule,
    Slot,
    SlotSpec,
    Source,
    Status,
    StrapPin,
    SupplyRail,
    ThermalPadRule,
    VbusRating,
    fact_sheets_named,
)

__all__ = [
    "BRIEFED_STEPS",
    "STEP_SLOTS",
    "brief",
    "part_name",
    "render_gap",
    "render_slot",
    "resolve_sheets",
    "sheets_mentioned",
    "slots_for_step",
]

# ``(ref, sheet)`` pairs. The ref is a board reference designator when the device
# came from a selection, and empty when it was merely named in prose — topology
# runs before selection, so at that point no reference exists yet.
SheetRefs = Sequence[tuple[str, FactSheetBase]]


def sheets_mentioned(text: str) -> list[tuple[str, FactSheetBase]]:
    """Sheets for every device named in free text, in order of appearance.

    The counterpart to :func:`resolve_sheets` for steps that run before any part
    has been selected. ``TopologyStep`` is the motivating case: it decides the
    power tree, which is precisely where an MCU's supply domains matter, yet it
    executes before ``SelectionStep`` and so has nothing but the requirement text
    to resolve devices from.

    Returns the same ``(ref, sheet)`` shape as :func:`resolve_sheets` with an
    empty ref, so :func:`brief` accepts either source without caring which.
    """
    return [("", sheet) for sheet in fact_sheets_named(text)]


def _sheet_key(entry: tuple[str, FactSheetBase]) -> tuple[str, str]:
    ref, sheet = entry
    return (ref, sheet.device)


def dedupe(entries: SheetRefs) -> list[tuple[str, FactSheetBase]]:
    """Drop repeated ``(ref, device)`` pairs while preserving order.

    A design legitimately holds several parts sharing one sheet (four decoupling
    capacitors are not four devices, but two USBLC6-2 parts are two references to
    one sheet). Rendering the same device's facts once per reference would spend
    the whole budget restating one datasheet.
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, FactSheetBase]] = []
    for entry in entries:
        key = _sheet_key(entry)
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def unique_devices(entries: SheetRefs) -> list[FactSheetBase]:
    """One sheet per device, order preserved."""
    seen: set[str] = set()
    out: list[FactSheetBase] = []
    for _, sheet in entries:
        if sheet.device in seen:
            continue
        seen.add(sheet.device)
        out.append(sheet)
    return out


def refs_for(entries: SheetRefs, device: str) -> list[str]:
    """Board references that resolved to ``device`` (empty refs excluded)."""
    return [ref for ref, sheet in entries if sheet.device == device and ref]


def uncovered_names(parts: list[Any]) -> list[str]:
    """Names of parts no sheet answers for, deduplicated in order.

    Used to tell a model explicitly that a device has no datasheet data, rather
    than leaving it to infer that from an absent entry — the same failure mode
    rule 2 in the module docstring guards against, one level up.
    """
    covered = {ref for ref, _ in resolve_sheets(parts)}
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        ref = getattr(part, "ref", "")
        if ref in covered:
            continue
        name = part_name(part)
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


# --------------------------------------------------------------------------- #
# Rendering — fact shapes
# --------------------------------------------------------------------------- #


def _num(value: float) -> str:
    return f"{value:g}"


# Notes and page references in the fact sheets are written for a human reviewer
# and run to several hundred characters — the STM32's supply_rails note is a
# paragraph on VDDA tolerance. Verbatim they crowd out other facts: a single slot
# could consume the whole block. Notes keep the larger allowance because they
# carry the engineering caveat that changes a design ("the minimum rises to
# 2.4 V when the ADC is used"); a citation only has to be specific enough to find
# the page, and "§5.3.1 Table 9, p.38" fits comfortably.
_MAX_NOTE_CHARS = 140
_MAX_CITATION_CHARS = 70


def _shorten(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def _note(text: str) -> str:
    return _shorten(text, _MAX_NOTE_CHARS)


def _unit_suffix(unit: str) -> str:
    return f" {unit}" if unit else ""


def _tolerance_suffix(fact: FixedFact) -> str:
    parts: list[str] = []
    if fact.tolerance_pct is not None:
        parts.append(f"+/-{_num(fact.tolerance_pct)}%")
    if fact.tolerance_ppm is not None:
        parts.append(f"+/-{_num(fact.tolerance_ppm)} ppm")
    return f" ({', '.join(parts)})" if parts else ""


def _describe_scalar(fact: FixedFact | RangeFact, spec: SlotSpec) -> str:
    """A fact's value, expressed the way the slot compares it.

    ``Bound.describe`` already renders a maximum as "<= v", a minimum as ">= v"
    and an interval as "lo-hi", so the direction of a limit comes from the slot's
    declared comparison rather than from prose written here — the same source of
    truth :func:`~ratsnestpro.eda.factsheet.evaluate` uses when it judges.

    Two cases need more than the bound alone:

    * ``Comparison.NONE`` has no direction to express (the slot is a datum, not a
      threshold), so the raw value is rendered instead of an "unbounded" bound.
    * ``Comparison.EXACT`` on a value with a tolerance produces the ACCEPTANCE
      WINDOW, which is what the gate needs but not what a designer specifies:
      USB-C Rd would read "4080-6120 ohm" and lose the 5.1 kOhm that actually has
      to be ordered. The nominal leads, the window follows in parentheses.
    """
    unit = fact.unit or spec.unit
    if isinstance(fact, FixedFact) and spec.comparison is Comparison.EXACT:
        described = f"{_num(fact.value)}{_unit_suffix(unit)}{_tolerance_suffix(fact)}"
        # Only a DECLARED tolerance is worth showing as a window. Without one the
        # bound is the float-comparison epsilon, and "36 pins (accepts
        # 35.99-36.01 pins)" states nothing while costing a line's worth of room.
        if fact.tolerance_pct is not None or fact.tolerance_ppm is not None:
            described += f" (accepts {fact.bound(spec.comparison).describe(spec.unit)})"
        return described
    if spec.comparison is not Comparison.NONE:
        described = fact.bound(spec.comparison).describe(spec.unit)
    elif isinstance(fact, FixedFact):
        described = f"{_num(fact.value)}{_unit_suffix(unit)}"
    else:
        described = f"{_num(fact.min)}-{_num(fact.max)}{_unit_suffix(unit)}"
    if isinstance(fact, FixedFact):
        described += _tolerance_suffix(fact)
    return described


def _describe_condition(condition: object) -> str:
    if isinstance(condition, list | tuple) and len(condition) == 2:
        return f"{condition[0]}..{condition[1]}"
    return str(condition)


def _render_conditional(fact: ConditionalFact, spec: SlotSpec) -> list[str]:
    """One line per premise. A conditional is NOT a conflict.

    Every arm is hard on its own — the AVR really does allow 16 MHz, but only at
    4.5 V and above. Flattening the arms into a single range would invent a limit
    that no premise supports, which is why each is listed with the condition that
    activates it.
    """
    lines = [f"varies with {fact.selector}:"]
    for branch in fact.branches:
        premise = ", ".join(
            f"{key}={_describe_condition(value)}" for key, value in branch.when.items()
        )
        lines.append(f"    when {premise} -> {_describe_scalar(branch.value, spec)}")
    if fact.on_unknown == "strictest":
        lines.append(
            f"    if {fact.selector} is undetermined the STRICTEST arm applies"
        )
    return lines


def _render_conflict(fact: ConflictingRangeFact, spec: SlotSpec) -> list[str]:
    """Both readings, named, plus which edge is the safe one.

    Reporting only one number would hide that the sources disagree; reporting the
    span without saying which edge is conservative would leave the reader to pick
    the permissive one. ``cite_all`` attributes each value to its document so the
    disagreement is auditable rather than asserted.
    """
    permissive, strict = fact.bounds()
    return [
        f"sources DISAGREE: {fact.low:g}-{fact.high:g}{_unit_suffix(spec.unit)}; "
        f"conservative choice {strict.describe(spec.unit)}, "
        f"widest claim {permissive.describe(spec.unit)}",
        f"    readings: {fact.cite_all()}",
    ]


def _render_fact(fact: object, spec: SlotSpec) -> list[str]:
    """A fact shape as display lines. Never fabricates a number."""
    match fact:
        case FixedFact() | RangeFact():
            line = _describe_scalar(fact, spec)
            if fact.note:
                line += f" — {_note(fact.note)}"
            return [line]
        case ConditionalFact():
            lines = _render_conditional(fact, spec)
            if fact.note:
                lines.append(f"    note: {_note(fact.note)}")
            return lines
        case ConflictingRangeFact():
            lines = _render_conflict(fact, spec)
            if fact.note:
                lines.append(f"    note: {_note(fact.note)}")
            return lines
        case QualitativeFact():
            # Verbatim, and deliberately without a number. The datasheet says
            # "as close as possible"; turning that into "<= 5 mm" here would be
            # the fabrication this whole layer exists to prevent.
            return [f'stated without a number: "{fact.text}"']
        case _:
            return []


# --------------------------------------------------------------------------- #
# Rendering — structured payloads
# --------------------------------------------------------------------------- #


def _render_supply_rails(rails: Sequence[SupplyRail]) -> list[str]:
    lines: list[str] = []
    for rail in rails:
        span = ""
        if rail.min_v is not None and rail.max_v is not None:
            span = f" {_num(rail.min_v)}-{_num(rail.max_v)} V"
        elif rail.typ_v is not None:
            span = f" {_num(rail.typ_v)} V typ"
        if rail.origin == "internal_ldo":
            # The distinction that damages boards when missed: this pin is an
            # OUTPUT of an on-chip regulator. Feeding it from the board fights
            # the internal LDO.
            origin = "produced by an on-chip LDO — MUST NOT be driven by the board"
        else:
            origin = "supplied by the board"
        current = (
            f", needs >= {_num(rail.min_current_ma)} mA"
            if rail.min_current_ma is not None
            else ""
        )
        note = f" — {_note(rail.note)}" if rail.note else ""
        lines.append(f"    {rail.name}:{span} ({origin}){current}{note}")
    return lines


def _render_per_pin_cap(entry: PerPinCap, spec: SlotSpec) -> list[str]:
    rendered = _render_fact(entry.cap, spec)
    head = f"    {entry.pin_group}: "
    count = f" x{entry.count_per_pin} per pin" if entry.count_per_pin != 1 else ""
    if not rendered:
        return [f"{head}(no value stated){count}"]
    lines = [f"{head}{rendered[0]}{count}"]
    lines.extend(f"    {extra}" for extra in rendered[1:])
    if entry.note:
        lines.append(f"        note: {_note(entry.note)}")
    return lines


def _render_decoupling(rule: DecouplingRule, spec: SlotSpec) -> list[str]:
    lines: list[str] = []
    for entry in rule.per_pin:
        lines.extend(_render_per_pin_cap(entry, spec))
    if rule.per_supply_pair_required is not None:
        # A COUNT requirement without a capacitance. Recording it as a per_pin
        # entry would mean inventing the value; dropping it would lose an
        # enforceable rule, so it is stated as what it is.
        state = "required" if rule.per_supply_pair_required else "not required"
        detail = (
            f" — {_note(rule.per_supply_pair_note)}" if rule.per_supply_pair_note else ""
        )
        lines.append(
            f"    one capacitor per supply/ground PAIR is {state} "
            f"(count stated, capacitance not){detail}"
        )
    if rule.bulk is not None:
        rendered = _render_fact(rule.bulk, spec)
        if rendered:
            lines.append(f"    bulk: {rendered[0]}")
            lines.extend(f"    {extra}" for extra in rendered[1:])
    if rule.max_distance_mm is not None:
        lines.append(f"    place within {_num(rule.max_distance_mm)} mm of the pin")
    if rule.placement_note:
        lines.append(f"    placement: {_note(rule.placement_note)}")
    return lines


def _render_clock_layout(rule: ClockLayoutRule) -> list[str]:
    lines: list[str] = []
    if rule.max_trace_mm is not None:
        lines.append(f"    clock trace <= {_num(rule.max_trace_mm)} mm")
    if rule.min_gap_to_clock_pin_mm is not None:
        lines.append(
            f"    keep other copper >= {_num(rule.min_gap_to_clock_pin_mm)} mm "
            f"from the clock pin"
        )
    if rule.vias_in_clock_trace_allowed is not None:
        allowed = "allowed" if rule.vias_in_clock_trace_allowed else "NOT allowed"
        lines.append(f"    vias in the clock trace are {allowed}")
    if rule.keepout_under is not None and rule.keepout_under:
        lines.append("    no routing under the crystal body")
    if rule.ground_guard is not None and rule.ground_guard:
        lines.append("    surround the oscillator with a ground guard")
    if rule.max_crystal_esr_ohm is not None:
        # A demand on the CRYSTAL, not on the layout — it lives here because
        # that is the section vendors state it in.
        lines.append(
            f"    the crystal's ESR must not exceed "
            f"{_num(rule.max_crystal_esr_ohm)} ohm"
        )
    if rule.series_resistor_ohm is not None:
        lines.append(
            f"    series resistor on the drive pin: "
            f"{_num(rule.series_resistor_ohm)} ohm"
        )
    if rule.note:
        lines.append(f"    note: {_note(rule.note)}")
    return lines


def _render_reset(rule: ResetRule) -> list[str]:
    state = "REQUIRED" if rule.external_required else "not required"
    line = f"    an external reset circuit is {state}"
    return [line, f"    {_note(rule.details)}"] if rule.details else [line]


def _render_straps(pins: Sequence[StrapPin]) -> list[str]:
    return [
        f"    {pin.pin} must be held {pin.required_state}"
        + (f" — {_note(pin.note)}" if pin.note else "")
        for pin in pins
    ]


def _render_mandatory(items: Sequence[MandatoryPeripheral]) -> list[str]:
    return [
        f"    {item.kind} is mandatory" + (f" — {_note(item.note)}" if item.note else "")
        for item in items
    ]


def _render_pins(pins: Sequence[PinFact]) -> list[str]:
    lines: list[str] = []
    for pin in pins:
        float_note = ""
        if pin.float_allowed is False:
            float_note = ", must not float"
        elif pin.float_allowed is True:
            float_note = ", may float"
        note = f" — {_note(pin.note)}" if pin.note else ""
        lines.append(f"    {pin.name}: {pin.kind}{float_note}{note}")
    return lines


def _render_thermal_pad(rule: ThermalPadRule) -> list[str]:
    state = "REQUIRED" if rule.required else "not required"
    line = f"    exposed pad grounding is {state}"
    if rule.applies_to_packages:
        # Without this a leaded variant of the same die would be flagged for a
        # pad it does not have.
        line += f" for {', '.join(rule.applies_to_packages)}"
    lines = [line]
    if rule.min_vias is not None:
        lines.append(f"    at least {rule.min_vias} thermal vias")
    if rule.note:
        lines.append(f"    note: {_note(rule.note)}")
    return lines


def _render_inductor(req: InductorRequirement, spec: SlotSpec) -> list[str]:
    lines: list[str] = []
    rendered = _render_fact(req.inductance, spec)
    if rendered:
        lines.append(f"    inductance: {rendered[0]}")
        lines.extend(f"    {extra}" for extra in rendered[1:])
    if req.isat is not None:
        rendered = _render_fact(req.isat, spec)
        if rendered:
            lines.append(f"    saturation current: {rendered[0]}")
    if req.note:
        lines.append(f"    note: {_note(req.note)}")
    return lines


def _render_caps(reqs: Sequence[CapRequirement], spec: SlotSpec) -> list[str]:
    lines: list[str] = []
    for req in reqs:
        rendered = _render_fact(req.cap, spec)
        dielectric = f" ({req.dielectric})" if req.dielectric else ""
        head = rendered[0] if rendered else "(no value stated)"
        lines.append(f"    {req.role}: {head}{dielectric}")
        lines.extend(f"    {extra}" for extra in rendered[1:])
        if req.note:
            lines.append(f"        note: {_note(req.note)}")
    return lines


def _render_vbus(rating: VbusRating) -> list[str]:
    bits: list[str] = []
    if rating.voltage_v is not None:
        bits.append(f"{_num(rating.voltage_v)} V")
    if rating.current_a is not None:
        bits.append(f"{_num(rating.current_a)} A")
    line = f"    VBUS rated {' / '.join(bits)}" if bits else "    VBUS rating stated"
    return [line, f"    note: {_note(rating.note)}"] if rating.note else [line]


def _render_payload(value: object, spec: SlotSpec) -> list[str]:
    """A structured slot payload as display lines."""
    if isinstance(value, DecouplingRule):
        return _render_decoupling(value, spec)
    if isinstance(value, ClockLayoutRule):
        return _render_clock_layout(value)
    if isinstance(value, ResetRule):
        return _render_reset(value)
    if isinstance(value, ThermalPadRule):
        return _render_thermal_pad(value)
    if isinstance(value, InductorRequirement):
        return _render_inductor(value, spec)
    if isinstance(value, VbusRating):
        return _render_vbus(value)
    if isinstance(value, list):
        if not value:
            return []
        first = value[0]
        if isinstance(first, SupplyRail):
            return _render_supply_rails(value)
        if isinstance(first, StrapPin):
            return _render_straps(value)
        if isinstance(first, MandatoryPeripheral):
            return _render_mandatory(value)
        if isinstance(first, PinFact):
            return _render_pins(value)
        if isinstance(first, CapRequirement):
            return _render_caps(value, spec)
        if isinstance(first, str):
            return [f"    {', '.join(value)}"]
    return []


def _cite(slot: Slot[Any], sheet: FactSheetBase) -> str:
    """Page-level provenance, with the document named only when it differs.

    Most slots are backed by the sheet's primary document, so repeating its
    title on every line would spend the budget on boilerplate. A slot backed by
    a DIFFERENT document — ST states VDDA decoupling in AN2586, not the
    datasheet — must keep its own title, or the citation would point at a
    document that does not contain the figure.
    """
    source: Source | None = slot.effective_source() or sheet.source
    if source is None:
        return ""
    if source.doc.strip() == sheet.source.doc.strip():
        return _shorten(source.ref.strip() or source.doc.strip(), _MAX_CITATION_CHARS)
    return _shorten(source.cite(), _MAX_CITATION_CHARS)


def render_slot(slot_name: str, slot: Slot[Any], sheet: FactSheetBase) -> list[str]:
    """An asserted slot as display lines, or ``[]`` when there is nothing to say."""
    spec = SLOT_SPECS.get(slot_name)
    if spec is None or not slot.asserted:
        return []
    body = _render_fact(slot.value, spec) or _render_payload(slot.value, spec)
    if not body:
        return []
    citation = _cite(slot, sheet)
    suffix = f"  [{citation}]" if citation else ""
    head, *rest = body
    if head.startswith("    "):
        # A multi-entry payload: the slot name gets its own line so the entries
        # stay aligned under it.
        return [f"  - {slot_name}:{suffix}", *body]
    return [f"  - {slot_name}: {head}{suffix}", *rest]


# --------------------------------------------------------------------------- #
# Step routing — derived from the registry, not written twice
# --------------------------------------------------------------------------- #


def _derive_step_slots() -> dict[str, tuple[str, ...]]:
    """``StepName -> slots that step consumes``, read off ``SLOT_SPECS``.

    ``SlotSpec.consumers`` already records entries shaped ``"StepName.check"``.
    Those names were written as a design target, and ``consumer_registry``'s
    docstring warns at length that most of them name work that is "planned, not
    wired" — a declaration nothing enforces, which is the same disease the fact
    base was built to cure one level down.

    Deriving the routing table from those strings is what makes them true. A slot
    reaches a step because its spec says so, so the declaration and the wiring
    cannot drift apart, and adding a consumer to a spec is enough to change what
    a step sees.
    """
    out: dict[str, list[str]] = {}
    for name, spec in SLOT_SPECS.items():
        for consumer in spec.consumers:
            step = consumer.split(".", 1)[0].strip()
            if step:
                out.setdefault(step, []).append(name)
    return {step: tuple(slots) for step, slots in out.items()}


STEP_SLOTS: dict[str, tuple[str, ...]] = _derive_step_slots()


# Steps that actually inject a brief into an LLM prompt. This is NOT every step
# named in ``SlotSpec.consumers``: ``SchPinMapStep`` and ``LayoutCriticalStep``
# both return ``(artifact, False)`` from ``propose`` — they are deterministic and
# never call a model, so text handed to them would be read by nobody. Slots whose
# only declared consumer is one of those two cannot be reached by this module at
# all, and ``test_factbrief_contract`` requires each of them to be registered
# with a reason rather than quietly going unread.
#
# Kept here rather than in the test so the claim has one home: a step that starts
# overriding ``fact_sheets_for_step`` must appear in this set, and a contract test
# checks the two agree.
BRIEFED_STEPS: frozenset[str] = frozenset({
    "TopologyStep",
    "SelectionStep",
    "SchConnectionsStep",
})


# Ordering: what a violation COSTS decides what gets said first, and what
# survives a budget cut. Same principle as
# ``SlotSpec.severity_on_violation`` — consequence, not data shape.
_CONSEQUENCE_RANK: dict[Consequence, int] = {
    Consequence.BURN: 0,
    Consequence.MALFUNCTION: 1,
    Consequence.MARGIN: 2,
}


def slots_for_step(step: str, sheet: FactSheetBase) -> list[str]:
    """Slots this step consumes that this device actually answers, ordered.

    Ordered by consequence first so the facts that destroy a board are stated
    before the ones that cost headroom, then by questionnaire position for a
    stable, reviewable sequence.
    """
    questionnaire = sheet.questionnaire()
    position = {name: index for index, name in enumerate(questionnaire)}
    wanted = [name for name in STEP_SLOTS.get(step, ()) if name in position]
    return sorted(
        dict.fromkeys(wanted),
        key=lambda name: (
            _CONSEQUENCE_RANK.get(SLOT_SPECS[name].consequence, 3),
            position[name],
        ),
    )


# --------------------------------------------------------------------------- #
# Gaps — an unanswered slot is not an absent limit
# --------------------------------------------------------------------------- #

_MAX_GAP_REASON_CHARS = 160


def render_gap(slot_name: str, slot: Slot[Any]) -> list[str]:
    """A slot with no answer, rendered only when the silence is dangerous.

    Four states, three of which say nothing here:

    * ``asserted`` — handled by :func:`render_slot`.
    * ``not_applicable`` — the device genuinely has no such limit, so there is
      no constraint to report and printing one would spend budget on nothing.
    * ``not_asserted`` / ``blocked`` on a ``margin`` slot — the worst case is
      thin headroom, which is not worth the space it would cost.
    * ``not_asserted`` / ``blocked`` on a ``burn`` or ``malfunction`` slot —
      **stated**. This is the case that matters: a reader who sees no entry for
      "absolute maximum input voltage" concludes there is no maximum. The
      four-state ``Status`` enum exists precisely so that "nobody extracted it"
      and "no limit exists" cannot be confused, and dropping the gap line would
      hand that confusion straight back.
    """
    spec = SLOT_SPECS.get(slot_name)
    if spec is None or slot.status not in (Status.NOT_ASSERTED, Status.BLOCKED):
        return []
    if spec.consequence is Consequence.MARGIN:
        return []
    line = (
        f"  - {slot_name}: NOT STATED by the sources consulted — unknown, "
        f"NOT unlimited. Do not substitute a value."
    )
    reason = _shorten(slot.reason, _MAX_GAP_REASON_CHARS)
    return [line, f"    why: {reason}"] if reason else [line]


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #

_GAP_LEGEND = (
    "A slot marked NOT STATED is unknown, not unlimited: no source consulted "
    "gives a figure, so do not infer one."
)

# Default ceiling for one step's fact block. Sized against the existing prompt
# budget conventions in the pipeline (see ``_MAX_REPAIR_ARTIFACT_CHARS``): large
# enough that a two-device design renders whole, small enough that a five-device
# design cannot crowd out the rest of the prompt.
_MAX_BRIEF_CHARS = 4_000


def _device_header(sheet: FactSheetBase, refs: Sequence[str]) -> str:
    where = f" ({', '.join(refs)})" if refs else ""
    return f"{sheet.device}{where} — {sheet.source.doc}"


def _device_block(
    step: str, sheet: FactSheetBase, refs: Sequence[str]
) -> tuple[list[tuple[int, list[str]]], bool]:
    """``[(consequence_rank, lines)]`` for one device, plus whether a gap appears.

    Entries stay separate rather than pre-joined so a budget cut can drop whole
    facts by consequence instead of severing a line in half.
    """
    entries: list[tuple[int, list[str]]] = []
    has_gap = False
    for slot_name in slots_for_step(step, sheet):
        slot = sheet.slot(slot_name)
        if slot is None:
            continue
        rank = _CONSEQUENCE_RANK.get(SLOT_SPECS[slot_name].consequence, 3)
        lines = render_slot(slot_name, slot, sheet)
        if not lines:
            lines = render_gap(slot_name, slot)
            if lines:
                has_gap = True
        if lines:
            entries.append((rank, lines))
    return entries, has_gap


def brief(
    step: str,
    entries: SheetRefs,
    *,
    uncovered: Sequence[str] = (),
    budget: int | None = None,
) -> str:
    """Datasheet facts this step needs, cited, ordered by consequence.

    ``step`` is a pipeline step CLASS NAME ("SelectionStep") rather than a
    :class:`~ratsnestpro.orchestration.pipeline.PipelineStep` member, because
    this module must not import the pipeline — the pipeline imports it.

    Returns ``""`` when there is nothing grounded to say, so a caller can omit
    the whole block rather than emit an empty heading that reads like "this
    device has no requirements".

    ``budget`` caps the rendered length. FACTS are droppable and their loss is
    announced; the two SAFETY statements are not. The legend explaining that
    ``NOT STATED`` means unknown, the note counting what was omitted, and the
    note naming parts with no sheet at all exist precisely to stop absent text
    from reading as permission, so sacrificing them to fit a number would defeat
    the budget's own purpose. A budget smaller than those statements therefore
    yields them alone, with every fact dropped.
    """
    pairs = dedupe(entries)
    blocks: list[tuple[str, list[tuple[int, list[str]]]]] = []
    any_gap = False
    for sheet in unique_devices(pairs):
        device_entries, has_gap = _device_block(step, sheet, refs_for(pairs, sheet.device))
        any_gap = any_gap or has_gap
        if device_entries:
            blocks.append((_device_header(sheet, refs_for(pairs, sheet.device)), device_entries))

    uncovered_note = (
        f"No datasheet facts are available for: {', '.join(uncovered)}. "
        f"That is missing evidence, not permission — do not infer limits for "
        f"these parts, and prefer a part that is covered where the choice is free."
        if uncovered
        else ""
    )

    # The legend and the uncovered note are part of what the caller pays for, so
    # they come out of the budget rather than being appended past it. A budget
    # that the output quietly exceeds is not a budget.
    limit = _MAX_BRIEF_CHARS if budget is None else budget
    overhead = len(uncovered_note) + 2 if uncovered_note else 0
    if any_gap:
        overhead += len(_GAP_LEGEND) + 2

    sections: list[str] = []
    if blocks:
        rendered = _apply_budget(blocks, max(limit - overhead, 0))
        body = "\n\n".join(rendered)
        if "NOT STATED" in body:
            sections.append(_GAP_LEGEND)
        sections.extend(rendered)
    if uncovered_note:
        sections.append(uncovered_note)
    return "\n\n".join(section for section in sections if section).strip()


def _apply_budget(
    blocks: Sequence[tuple[str, list[tuple[int, list[str]]]]],
    budget: int,
) -> list[str]:
    """Join device blocks, dropping the cheapest facts first when over budget.

    Prompts are finite and a single MCU sheet renders to several thousand
    characters, so something has to give. What gives is decided by
    :class:`~ratsnestpro.eda.factsheet.Consequence`: a ``margin`` fact costs
    headroom if ignored, a ``burn`` fact costs the board, so margin facts leave
    first and burn facts leave last. Within one consequence rank the later device
    and the later slot go first, which makes the result a pure function of the
    input — two runs on the same design produce byte-identical text, so a diff
    between runs means the design changed.

    Whatever is cut is COUNTED and announced. Silently shortening the list would
    turn a budget into the same lie the ``NOT STATED`` line exists to prevent: a
    reader cannot tell a fact that was omitted from a fact that does not exist.
    """
    items = [
        (block_index, entry_index, rank)
        for block_index, (_, entries) in enumerate(blocks)
        for entry_index, (rank, _) in enumerate(entries)
    ]
    kept: set[tuple[int, int]] = {(b, e) for b, e, _ in items}

    def render(selection: set[tuple[int, int]]) -> list[str]:
        out: list[str] = []
        for block_index, (header, entries) in enumerate(blocks):
            lines = [
                line
                for entry_index, (_, entry_lines) in enumerate(entries)
                if (block_index, entry_index) in selection
                for line in entry_lines
            ]
            if lines:
                out.append("\n".join([header, *lines]))
        return out

    def size(sections: Sequence[str], omitted: int) -> int:
        total = sum(len(section) for section in sections)
        total += 2 * max(len(sections) - 1, 0)          # blank line between blocks
        if omitted:
            total += len(_omission_note(omitted)) + 2
        return total

    # Sacrifice order: cheapest consequence first, then from the end of the brief.
    sacrifice = sorted(items, key=lambda item: (-item[2], -item[0], -item[1]))
    omitted = 0
    for block_index, entry_index, _ in sacrifice:
        if size(render(kept), omitted) <= budget:
            break
        kept.discard((block_index, entry_index))
        omitted += 1

    sections = render(kept)
    if omitted:
        sections.append(_omission_note(omitted))
    return sections


def _omission_note(count: int) -> str:
    return (
        f"[{count} lower-consequence fact(s) omitted to fit the prompt budget. "
        f"They are recorded in the fact sheets, not absent from them — treat them "
        f"as unread rather than unconstrained.]"
    )

