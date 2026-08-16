"""Task 6 — the contract that keeps the fact base from going unread.

Why this file is separate from ``test_factbrief.py``
---------------------------------------------------
That file tests behaviour: given this slot, is the rendering right? This one
tests a PROPERTY of the whole system: is every fact reachable by somebody?

The distinction matters because the failure it guards is invisible. Twenty-five
of the thirty-seven slots declare ``Comparison.NONE`` — they are data, not
thresholds — so no gate can fail when they are ignored, and no test noticed that
nothing read them. ``tests/test_factgate.py`` already carries the same shape of
guard for the twelve gateable slots (``UNOBSERVED_SLOTS``); this is its
counterpart for the other twenty-five, and it is written the same way on purpose:
an exception must be REGISTERED WITH A REASON, never merely tolerated.
"""

from __future__ import annotations

from dataclasses import dataclass

from ratsnestpro.eda import factbrief
from ratsnestpro.eda.factsheet import (
    QUESTIONNAIRE,
    SLOT_SPECS,
    Comparison,
    all_fact_sheets,
    consumer_registry,
)


@dataclass
class _Part:
    ref: str
    value: str
    role: str = ""
    symbol: str = ""
    footprint: str = ""


# Data-only slots that no briefed step can reach, each with its reason. A slot
# absent from here and absent from every briefed step's output is a fact nobody
# reads — the exact condition this module exists to make impossible.
#
# All three entries share one cause: their only declared consumer is a step whose
# ``propose`` returns ``(artifact, False)`` and never calls a model, so injecting
# text into it would change nothing. Reaching them needs a different mechanism
# (a deterministic ``check``, or the placement algorithm itself reading the
# fact), which is a separate piece of work from prompt injection.
UNBRIEFED_SLOTS: dict[str, str] = {
    "pins": (
        "Only consumer is SchPinMapStep.pin_function. SchPinMapStep.propose "
        "returns self._deterministic_map(state), False — the pin map is resolved "
        "from the real symbol library because a model cannot be allowed to invent "
        "pin numbers, so there is no prompt to inject into. Reaching this slot "
        "means teaching SchPinMapStep.check to compare mapped pins against the "
        "declared pin kinds (input_only, nc, boot) instead."
    ),
    "clock_layout": (
        "Only consumer is LayoutCriticalStep.critical_placement, whose propose "
        "returns plan, False — placement is a deterministic grid walk. The "
        "numbers in this slot (max_trace_mm, min_gap_to_clock_pin_mm) are "
        "geometry constraints the ALGORITHM would have to honour, or that "
        "LayoutCriticalStep.check would have to verify against real coordinates. "
        "Note that max_crystal_esr_ohm from this same slot IS already consumed, "
        "by factgate.cross_device_verdicts."
    ),
    "thermal_pad": (
        "Only consumer is LayoutCriticalStep.critical_placement, which is "
        "deterministic. min_vias and applies_to_packages are checkable facts "
        "about the finished board, so they belong in a layout check against the "
        "actual footprint and via placement, not in a proposal prompt."
    ),
}


def _briefed_slots() -> set[str]:
    """Every slot that some briefed step routes for some device in the roster."""
    out: set[str] = set()
    for step in factbrief.BRIEFED_STEPS:
        for sheet in all_fact_sheets():
            out.update(factbrief.slots_for_step(step, sheet))
    return out


# --------------------------------------------------------------------------- #
# The keystone
# --------------------------------------------------------------------------- #


def test_every_data_only_slot_is_briefed_or_states_why_not() -> None:
    """A ``Comparison.NONE`` slot must reach a prompt, or be registered here.

    Slots with a comparison are gates and are covered by
    ``test_factgate.test_every_comparable_slot_has_an_observer_or_a_stated_reason``.
    Slots without one can only ever influence a design by being READ, so if no
    step reads them they have no effect on anything at all.
    """
    data_only = {
        name
        for slots in QUESTIONNAIRE.values()
        for name in slots
        if SLOT_SPECS[name].comparison is Comparison.NONE
    }
    reachable = _briefed_slots()
    orphans = sorted(data_only - reachable - set(UNBRIEFED_SLOTS))
    assert not orphans, (
        f"data-only slots no briefed step reads and no reason explains: {orphans}. "
        f"Either route them by adding a briefed step to SlotSpec.consumers, or "
        f"register them in UNBRIEFED_SLOTS with the reason they cannot be reached."
    )


def test_unbriefed_registry_has_no_stale_entries() -> None:
    """An entry that became reachable must be removed, not left as a false alibi."""
    reachable = _briefed_slots()
    stale = sorted(name for name in UNBRIEFED_SLOTS if name in reachable)
    assert not stale, (
        f"UNBRIEFED_SLOTS still excuses slots that are now briefed: {stale}"
    )


def test_unbriefed_entries_name_a_real_slot_and_give_a_reason() -> None:
    for name, reason in UNBRIEFED_SLOTS.items():
        assert name in SLOT_SPECS, f"{name} is not a slot"
        assert len(reason.split()) >= 15, f"{name} needs a reason, not a label"


def test_unbriefed_slots_are_only_routed_to_unwired_steps() -> None:
    """The stated cause must be the actual cause.

    Each registered slot must genuinely have no briefed consumer — otherwise the
    reason is fiction and the slot is simply not being rendered.
    """
    registry = consumer_registry()
    for name in UNBRIEFED_SLOTS:
        steps = {consumer.split(".", 1)[0] for consumer in registry[name]}
        assert not (steps & factbrief.BRIEFED_STEPS), (
            f"{name} IS routed to a briefed step ({steps & factbrief.BRIEFED_STEPS}); "
            f"its UNBRIEFED_SLOTS reason is wrong"
        )


def test_every_gateable_slot_is_also_briefed_where_it_makes_sense() -> None:
    """Gates benefit from arriving before the proposal, not only after it.

    Not an absolute requirement — a slot may be judged without being narrated —
    but a gate that is never shown to the model can only ever be discovered by
    being violated, which costs a repair round every time. This test records how
    many are in that position rather than forbidding it.
    """
    gateable = {
        name
        for slots in QUESTIONNAIRE.values()
        for name in slots
        if SLOT_SPECS[name].comparison is not Comparison.NONE
    }
    silent = sorted(gateable - _briefed_slots())
    # Only the two slots that have no observer either (see UNOBSERVED_SLOTS in
    # test_factgate) are allowed to be invisible on both sides.
    assert silent == [] or set(silent) <= {"drive_level_uw", "freq_mhz"}, silent


# --------------------------------------------------------------------------- #
# The report the plan asked for: what is read, and what is not
# --------------------------------------------------------------------------- #


def test_coverage_report_is_printable(capsys) -> None:
    """Emits the briefed / unbriefed inventory so a reviewer can see the state."""
    data_only = sorted({
        name
        for slots in QUESTIONNAIRE.values()
        for name in slots
        if SLOT_SPECS[name].comparison is Comparison.NONE
    })
    reachable = _briefed_slots()
    lines = ["", "slot                      briefed by"]
    lines.append("-" * 64)
    for name in data_only:
        steps = sorted(
            step for step in factbrief.BRIEFED_STEPS
            if any(name in factbrief.slots_for_step(step, s) for s in all_fact_sheets())
        )
        state = ", ".join(steps) if name in reachable else "NOBODY (registered)"
        lines.append(f"{name:<25} {state}")
    print("\n".join(lines))
    captured = capsys.readouterr().out
    assert "briefed by" in captured
    assert len([line for line in captured.splitlines() if line.strip()]) >= len(data_only)


def test_a_real_design_reaches_every_briefable_mcu_slot() -> None:
    """End to end: an MCU design must actually emit the slots the routing promises."""
    entries = factbrief.resolve_sheets([_Part("U1", "RP2040", "mcu")])
    emitted: set[str] = set()
    for step in factbrief.BRIEFED_STEPS:
        text = factbrief.brief(step, entries, budget=10**6)
        emitted |= {
            line.split(":", 1)[0].removeprefix("  - ").strip()
            for line in text.splitlines()
            if line.startswith("  - ")
        }
    sheet = next(s for _, s in entries)
    expected = {
        name
        for step in factbrief.BRIEFED_STEPS
        for name in factbrief.slots_for_step(step, sheet)
        if (slot := sheet.slot(name)) is not None and slot.asserted
    }
    assert expected <= emitted, expected - emitted
