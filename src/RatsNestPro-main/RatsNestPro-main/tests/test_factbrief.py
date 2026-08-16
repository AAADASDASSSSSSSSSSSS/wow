"""Tests for :mod:`ratsnestpro.eda.factbrief` — facts rendered for one step.

Organised by the task that introduced each section so a failure points at the
behaviour it belongs to.
"""

from __future__ import annotations

from dataclasses import dataclass

from ratsnestpro.eda import factbrief
from ratsnestpro.eda.factsheet import (
    SLOT_SPECS,
    Consequence,
    QualitativeFact,
    Status,
    all_fact_sheets,
    consumer_registry,
    fact_sheet,
)


@dataclass
class _Part:
    ref: str
    value: str
    role: str = ""
    symbol: str = ""
    footprint: str = ""


# --------------------------------------------------------------------------- #
# Task 1 — device resolution
# --------------------------------------------------------------------------- #


def test_resolve_sheets_maps_selected_parts_to_their_sheets() -> None:
    parts = [
        _Part("U1", "STM32F103C8T6", "mcu"),
        _Part("U2", "AMS1117-3.3", "ldo regulator"),
        _Part("Y1", "X322512MSB4SI", "crystal"),
        _Part("R1", "10k", "pullup"),
    ]
    resolved = factbrief.resolve_sheets(parts)
    assert [(ref, sheet.device) for ref, sheet in resolved] == [
        ("U1", "STM32F103"),
        ("U2", "AMS1117-3.3"),
        ("Y1", "X322512MSB4SI"),
    ]


def test_resolve_sheets_falls_back_to_the_symbol() -> None:
    parts = [_Part("U1", "", "mcu", symbol="MCU_ST_STM32F1:STM32F103C8Tx")]
    resolved = factbrief.resolve_sheets(parts)
    assert [sheet.device for _, sheet in resolved] == ["STM32F103"]


def test_sheets_mentioned_finds_every_device_in_a_requirement() -> None:
    text = (
        "Build a two-layer board with an STM32F103C8T6 fed from an AMS1117-3.3, "
        "an 8 MHz X322512MSB4SI crystal and a USBLC6-2 on the USB pair."
    )
    devices = [sheet.device for _, sheet in factbrief.sheets_mentioned(text)]
    assert devices == ["STM32F103", "AMS1117-3.3", "X322512MSB4SI", "USBLC6-2"]


def test_sheets_mentioned_returns_empty_refs() -> None:
    """Topology runs before selection, so no reference designator exists yet."""
    entries = factbrief.sheets_mentioned("an RP2040 board")
    assert entries and all(ref == "" for ref, _ in entries)


def test_sheets_mentioned_keeps_esp32_variants_apart() -> None:
    c3_only = [s.device for _, s in factbrief.sheets_mentioned("use an ESP32-C3FH4")]
    assert c3_only == ["ESP32-C3"], "the plain ESP32 sheet must not ride along"

    both = [s.device for _, s in factbrief.sheets_mentioned("compare ESP32 with ESP32-C3")]
    assert both == ["ESP32", "ESP32-C3"], "each mention resolves independently"

    assert factbrief.sheets_mentioned("use an ESP32-S3-WROOM-1 module") == [], (
        "an uncovered variant must contribute nothing rather than the ESP32 sheet"
    )


def test_sheets_mentioned_handles_chinese_prose() -> None:
    text = "用 STM32F103C8T6,LQFP48 封装,搭配 AMS1117-3.3 稳压器"
    devices = [sheet.device for _, sheet in factbrief.sheets_mentioned(text)]
    assert devices == ["STM32F103", "AMS1117-3.3"]


def test_sheets_mentioned_on_unknown_text_is_empty() -> None:
    assert factbrief.sheets_mentioned("a generic two-layer board") == []
    assert factbrief.sheets_mentioned("") == []


def test_unique_devices_collapses_repeated_sheets() -> None:
    parts = [
        _Part("D1", "USBLC6-2SC6", "usb_esd"),
        _Part("D2", "USBLC6-2SC6", "usb_esd"),
        _Part("U1", "RP2040", "mcu"),
    ]
    entries = factbrief.resolve_sheets(parts)
    assert len(entries) == 3
    assert [s.device for s in factbrief.unique_devices(entries)] == ["USBLC6-2", "RP2040"]
    assert factbrief.refs_for(entries, "USBLC6-2") == ["D1", "D2"]


def test_uncovered_names_lists_parts_without_a_sheet() -> None:
    parts = [
        _Part("U1", "STM32F407VGT6", "mcu"),
        _Part("U2", "AMS1117-3.3", "ldo"),
        _Part("U3", "STM32F407VGT6", "mcu"),
    ]
    assert factbrief.uncovered_names(parts) == ["STM32F407VGT6"]


# --------------------------------------------------------------------------- #
# Task 2 — fact-shape renderers
# --------------------------------------------------------------------------- #


def _lines(device: str, slot_name: str) -> list[str]:
    sheet = fact_sheet(device)
    assert sheet is not None, device
    slot = sheet.slot(slot_name)
    assert slot is not None, f"{device}:{slot_name}"
    return factbrief.render_slot(slot_name, slot, sheet)


def _text(device: str, slot_name: str) -> str:
    return "\n".join(_lines(device, slot_name))


def test_range_fact_renders_its_interval_with_a_citation() -> None:
    rendered = _text("STM32F103", "supply_range")
    assert "supply_range" in rendered
    assert "2-3.6 V" in rendered or "2.0-3.6 V" in rendered
    assert "p.13" in rendered or "Table" in rendered, rendered


def test_fixed_fact_renders_the_comparison_direction() -> None:
    """A maximum must read as a maximum, taken from the slot's comparison."""
    rendered = _text("AMS1117-3.3", "abs_max_vin")
    assert "<=" in rendered and "15" in rendered, rendered


def test_fixed_fact_renders_its_tolerance() -> None:
    """USB-C Rd is 5.1 kOhm +/-20%.

    The nominal must lead: it is the value a designer specifies and orders. The
    acceptance window follows, because rendering only "4080-6120 ohm" would hide
    the 5.1 kOhm the datasheet actually names.
    """
    rendered = _text("USB-C 16P", "cc_pulldown_ohm")
    assert "5100" in rendered, rendered
    assert "+/-20%" in rendered, rendered
    assert "accepts" in rendered and "4080-6120" in rendered, rendered
    assert rendered.index("5100") < rendered.index("4080"), rendered


def test_exact_fact_without_a_tolerance_shows_no_window() -> None:
    """A pin count has no tolerance, so "(accepts 35.99-36.01 pins)" is noise."""
    rendered = _text("STM32F103", "pin_count")
    assert "36 pins" in rendered, rendered
    assert "accepts" not in rendered, rendered


def test_conditional_fact_lists_every_premise_and_never_flattens_them() -> None:
    """The AVR's speed grades are 4 / 10 / 20 MHz, each with its own voltage floor.

    Flattening them to "<= 20 MHz" would authorise 20 MHz at 1.8 V, which is the
    canonical way an AVR board ends up unstable.
    """
    rendered = _text("ATmega328P", "freq_vs_supply")
    assert "varies with supply_v" in rendered
    assert rendered.count("when ") == 3, rendered
    for grade, floor in (("4", "1.8"), ("10", "2.7"), ("20", "4.5")):
        assert f"<= {grade} MHz" in rendered, (grade, rendered)
        assert floor in rendered, (floor, rendered)
    assert "STRICTEST" in rendered, "the undetermined-selector policy must be stated"


def test_conflicting_fact_names_both_sources_and_the_safe_edge() -> None:
    """ST publishes 10 nF and 100 nF for the same VDDA pin; both must appear."""
    rendered = _text("STM32F103", "decoupling")
    assert "DISAGREE" in rendered, rendered
    assert "conservative choice" in rendered
    assert "->" in rendered, "each reading must be attributed to its document"


def test_qualitative_fact_is_verbatim_and_carries_no_number() -> None:
    import re

    for device, slot_name in (("ESP32", "clock_layout"), ("RP2040", "decoupling")):
        sheet = fact_sheet(device)
        assert sheet is not None
        slot = sheet.slot(slot_name)
        assert slot is not None
        if not isinstance(slot.value, QualitativeFact):
            continue
        rendered = "\n".join(factbrief.render_slot(slot_name, slot, sheet))
        assert "stated without a number" in rendered
        quoted = rendered.split('"')[1]
        assert quoted == slot.value.text
        assert not re.search(r"<=|>=", rendered), "no threshold may be invented"


def test_non_asserted_slot_renders_nothing_here() -> None:
    """Gap wording is the gap rule's job (Task 4), not the renderer's."""
    assert _lines("AMS1117-3.3", "vin_range") == []
    assert _lines("X322512MSB4SI", "drive_level_uw") == []


def test_citation_names_a_differing_document_in_full() -> None:
    """A slot backed by an application note must not cite the datasheet's title."""
    sheet = fact_sheet("STM32F103")
    assert sheet is not None
    rendered = _text("STM32F103", "decoupling")
    # The conflict readings carry doc + ref per observation.
    assert "AN2586" in rendered or sheet.source.doc.split()[0] in rendered, rendered


# --------------------------------------------------------------------------- #
# Task 3 — structured payload renderers
# --------------------------------------------------------------------------- #


def test_internal_ldo_rail_is_marked_as_not_board_driven() -> None:
    """The distinction that damages boards when missed."""
    rendered = _text("RP2040", "supply_rails")
    assert "on-chip LDO" in rendered
    assert "MUST NOT be driven by the board" in rendered, rendered


def test_supply_rails_list_every_domain() -> None:
    rendered = _text("RP2040", "supply_rails")
    sheet = fact_sheet("RP2040")
    assert sheet is not None
    rails = sheet.slot("supply_rails").value  # type: ignore[union-attr]
    for rail in rails:
        assert rail.name in rendered


def test_decoupling_count_requirement_states_no_capacitance() -> None:
    """A vendor may mandate the COUNT without naming a value.

    Rendering that as a per-pin capacitance would mean inventing the value;
    omitting it would lose an enforceable rule.
    """
    shown = False
    for device in ("RP2040", "ESP32", "ESP32-C3", "STM32F103", "ATmega328P"):
        sheet = fact_sheet(device)
        assert sheet is not None
        slot = sheet.slot("decoupling")
        assert slot is not None
        if not slot.asserted or slot.value.per_supply_pair_required is None:  # type: ignore[union-attr]
            continue
        rendered = "\n".join(factbrief.render_slot("decoupling", slot, sheet))
        assert "per supply/ground PAIR" in rendered
        assert "capacitance not" in rendered
        shown = True
    assert shown, "no sheet exercises the count-without-value path"


def test_clock_layout_renders_the_esr_ceiling_as_a_number() -> None:
    """RP2040 gives 50 ohm; a number buried in prose cannot be compared."""
    rendered = _text("RP2040", "clock_layout")
    assert "ESR" in rendered and "50" in rendered, rendered


def test_reset_and_pins_and_straps_render() -> None:
    assert "external reset circuit is" in _text("STM32F103", "reset")
    pins = _text("RP2040", "pins")
    assert pins.strip(), "the RP2040 pin table must render"
    straps = _text("ESP32", "boot_strapping")
    assert "must be held" in straps, straps


def test_mandatory_peripheral_renders_for_the_rp2040_flash() -> None:
    rendered = _text("RP2040", "mandatory_peripherals")
    assert "mandatory" in rendered
    assert "flash" in rendered.lower(), rendered


def test_thermal_pad_names_the_packages_it_applies_to() -> None:
    rendered = _text("RP2040", "thermal_pad")
    assert "exposed pad grounding is" in rendered


def test_dcdc_inductor_and_caps_render() -> None:
    inductor = _text("TPS563201", "required_inductor")
    assert "inductance" in inductor, inductor
    caps = _text("TPS563201", "required_caps")
    assert caps.strip(), caps


def test_connector_vbus_and_packages_render() -> None:
    assert "VBUS" in _text("USB-C 16P", "vbus_rating")
    packages = _text("STM32F103", "packages")
    assert "LQFP48" in packages, packages


def test_every_asserted_slot_of_every_sheet_renders_without_error() -> None:
    """A payload type with no renderer would silently drop a whole slot."""
    missing: list[str] = []
    for sheet in all_fact_sheets():
        for slot_name in sheet.questionnaire():
            slot = sheet.slot(slot_name)
            if slot is None or not slot.asserted:
                continue
            if not factbrief.render_slot(slot_name, slot, sheet):
                missing.append(f"{sheet.device}:{slot_name}")
    assert not missing, f"asserted slots that render to nothing: {missing}"


# --------------------------------------------------------------------------- #
# Task 4 — step routing and the gap rule
# --------------------------------------------------------------------------- #


def test_step_slots_is_derived_from_the_consumer_registry() -> None:
    """The routing table must not be a second, hand-written copy."""
    registry = consumer_registry()
    for slot_name, consumers in registry.items():
        for consumer in consumers:
            step = consumer.split(".", 1)[0]
            assert step in factbrief.STEP_SLOTS, step
            assert slot_name in factbrief.STEP_SLOTS[step], (step, slot_name)
    # And nothing appears that no spec asked for.
    declared = {c.split(".", 1)[0] for cs in registry.values() for c in cs}
    assert set(factbrief.STEP_SLOTS) == declared


def test_step_slots_are_ordered_by_consequence() -> None:
    sheet = fact_sheet("STM32F103")
    assert sheet is not None
    ordered = factbrief.slots_for_step("SelectionStep", sheet)
    ranks = [
        {Consequence.BURN: 0, Consequence.MALFUNCTION: 1, Consequence.MARGIN: 2}[
            SLOT_SPECS[name].consequence
        ]
        for name in ordered
    ]
    assert ranks == sorted(ranks), ordered


def _brief_slots(text: str) -> set[str]:
    """Slot names a brief actually emitted.

    Matches the "  - name:" marker rather than searching the text, because slot
    notes quote datasheet prose that contains other slot names verbatim: the
    STM32's supply_rails note mentions "reset blocks", which a substring check
    would read as the reset slot.
    """
    return {
        line.split(":", 1)[0].removeprefix("  - ").strip()
        for line in text.splitlines()
        if line.startswith("  - ")
    }


def _entry_count(text: str) -> int:
    """Number of rendered facts. Distinct from ``len(_brief_slots(...))``: two
    devices legitimately answer the same slot, and a set would merge them."""
    return sum(1 for line in text.splitlines() if line.startswith("  - "))


def test_each_step_sees_only_its_own_slots() -> None:
    entries = factbrief.resolve_sheets([_Part("U1", "STM32F103C8T6", "mcu")])
    selection = _brief_slots(factbrief.brief("SelectionStep", entries))
    connections = _brief_slots(factbrief.brief("SchConnectionsStep", entries))

    assert "supply_range" in selection and "supply_range" not in connections
    assert "reset" in connections and "reset" not in selection


def test_brief_slots_match_exactly_what_the_registry_routes() -> None:
    """The rendered set must be the registry's set, intersected with the sheet.

    This is the property that makes the derived routing table trustworthy: a slot
    appears in a step's brief if and only if its spec named that step.
    """
    sheet = fact_sheet("STM32F103")
    assert sheet is not None
    entries = factbrief.resolve_sheets([_Part("U1", "STM32F103C8T6", "mcu")])
    for step in ("TopologyStep", "SelectionStep", "SchConnectionsStep"):
        emitted = _brief_slots(factbrief.brief(step, entries))
        routed = set(factbrief.slots_for_step(step, sheet))
        # Slots that are not_applicable or margin-gaps render nothing, so the
        # emitted set is a subset of the routed set and never exceeds it.
        assert emitted <= routed, (step, emitted - routed)
        assert emitted, step


def test_steps_share_a_slot_only_where_the_registry_says_so() -> None:
    """Overlap between steps is deliberate, not a routing bug.

    ``supply_rails`` declares both ``TopologyStep.rail_feasibility`` and
    ``SelectionStep.regulator_adequacy``: an MCU's supply domains constrain the
    power tree AND whether the chosen regulator can feed it. The test therefore
    pins overlap to the registry rather than forbidding it.
    """
    registry = consumer_registry()

    def steps_of(slot_name: str) -> set[str]:
        return {c.split(".", 1)[0] for c in registry[slot_name]}

    assert {"TopologyStep", "SelectionStep"} <= steps_of("supply_rails")

    entries = factbrief.resolve_sheets([_Part("U1", "STM32F103C8T6", "mcu")])
    topology = _brief_slots(factbrief.brief("TopologyStep", entries))
    selection = _brief_slots(factbrief.brief("SelectionStep", entries))
    for shared in topology & selection:
        assert {"TopologyStep", "SelectionStep"} <= steps_of(shared), shared
    # The two steps must still differ, or the routing would be doing nothing.
    assert topology != selection


def test_burn_class_gap_is_stated_and_margin_gap_is_not() -> None:
    """AMS1117 has two recorded gaps with different consequences.

    ``vin_range`` is BURN — silence there reads as "any input voltage is fine".
    ``required_cin`` is MARGIN — the worst case is thin headroom, not a dead
    board, so it does not earn space.
    """
    entries = factbrief.resolve_sheets([_Part("U2", "AMS1117-3.3", "ldo regulator")])
    topology = factbrief.brief("TopologyStep", entries)
    connections = factbrief.brief("SchConnectionsStep", entries)

    assert "vin_range: NOT STATED" in topology, topology
    assert "unknown, NOT unlimited" in topology
    assert "Do not substitute a value" in topology
    assert "required_cin" not in connections, connections


def test_gap_line_carries_a_shortened_reason() -> None:
    entries = factbrief.resolve_sheets([_Part("U2", "AMS1117-3.3", "ldo regulator")])
    topology = factbrief.brief("TopologyStep", entries)
    why = [line for line in topology.splitlines() if line.strip().startswith("why:")]
    assert why, topology
    assert len(why[0]) <= 200
    assert "abs_max_vin" in why[0] or "Absolute Maximum" in why[0], why


def test_crystal_drive_level_gap_is_stated() -> None:
    entries = factbrief.resolve_sheets([_Part("Y1", "X322512MSB4SI", "crystal")])
    text = factbrief.brief("SelectionStep", entries)
    assert "drive_level_uw: NOT STATED" in text, text


def test_not_applicable_slots_are_omitted() -> None:
    """A device that genuinely has no such limit has no constraint to report."""
    sheet = fact_sheet("STM32F103")
    assert sheet is not None
    inapplicable = [
        name for name in sheet.questionnaire()
        if (slot := sheet.slot(name)) is not None and slot.status is Status.NOT_APPLICABLE
    ]
    assert inapplicable, "STM32F103 has not_applicable slots to exercise this"
    entries = factbrief.resolve_sheets([_Part("U1", "STM32F103C8T6", "mcu")])
    everything = "\n".join(
        factbrief.brief(step, entries) for step in factbrief.STEP_SLOTS
    )
    for name in inapplicable:
        assert f"- {name}:" not in everything, name


def test_brief_is_empty_when_nothing_is_grounded() -> None:
    assert factbrief.brief("SelectionStep", []) == ""
    assert factbrief.brief("SelectionStep", factbrief.resolve_sheets([
        _Part("R1", "10k", "pullup")
    ])) == ""


def test_uncovered_parts_are_named_as_missing_evidence() -> None:
    parts = [_Part("U1", "STM32F407VGT6", "mcu")]
    text = factbrief.brief(
        "SelectionStep",
        factbrief.resolve_sheets(parts),
        uncovered=factbrief.uncovered_names(parts),
    )
    assert "STM32F407VGT6" in text
    assert "missing evidence, not permission" in text


def test_device_header_names_the_document_and_the_reference() -> None:
    entries = factbrief.resolve_sheets([_Part("U1", "STM32F103C8T6", "mcu")])
    text = factbrief.brief("SelectionStep", entries)
    header = text.splitlines()[0]
    assert header.startswith("STM32F103 (U1) — "), header
    assert "DS5319" in header or "STM32F103" in header


def test_repeated_parts_share_one_device_block() -> None:
    parts = [_Part("D1", "USBLC6-2SC6", "usb_esd"), _Part("D2", "USBLC6-2SC6", "usb_esd")]
    text = factbrief.brief("SelectionStep", factbrief.resolve_sheets(parts))
    assert text.count("USBLC6-2 (") == 1, text
    assert "D1, D2" in text


# --------------------------------------------------------------------------- #
# Task 5 — budget and deterministic truncation
# --------------------------------------------------------------------------- #

_BUSY = [
    _Part("U1", "STM32F103C8T6", "mcu"),
    _Part("U2", "AMS1117-3.3", "ldo regulator"),
    _Part("Y1", "X322512MSB4SI", "crystal"),
    _Part("D1", "USBLC6-2SC6", "usb_esd"),
    _Part("J1", "USB-C 16P", "usb_c_connector"),
]


def test_brief_never_exceeds_its_budget() -> None:
    """Facts are droppable; the safety statements that explain absence are not."""
    parts = _BUSY + [_Part("U9", "STM32F407VGT6", "mcu")]
    entries = factbrief.resolve_sheets(parts)
    uncovered = factbrief.uncovered_names(parts)
    for budget in (600, 1200, 2400, 4000, 8000):
        text = factbrief.brief(
            "SelectionStep", entries, uncovered=uncovered, budget=budget
        )
        assert len(text) <= budget, (budget, len(text))


def test_an_impossible_budget_keeps_the_safety_statements_and_drops_every_fact() -> None:
    """Shrinking a safety statement to fit a number would defeat its purpose.

    The omission count and the uncovered-parts note are the only things standing
    between "no text here" and "no constraints here", so they survive a budget
    that nothing else does.
    """
    parts = _BUSY + [_Part("U9", "STM32F407VGT6", "mcu")]
    entries = factbrief.resolve_sheets(parts)
    text = factbrief.brief(
        "SelectionStep",
        entries,
        uncovered=factbrief.uncovered_names(parts),
        budget=50,
    )
    assert _entry_count(text) == 0
    assert "omitted to fit the prompt budget" in text
    assert "STM32F407VGT6" in text


def test_truncation_sacrifices_margin_before_burn() -> None:
    entries = factbrief.resolve_sheets(_BUSY)
    full = _brief_slots(factbrief.brief("SelectionStep", entries, budget=10**6))
    tight = _brief_slots(factbrief.brief("SelectionStep", entries, budget=2400))

    assert tight, "a 2400-character budget must still fit several facts"
    assert tight < full, "a tight budget must actually drop facts"

    kept_ranks = {SLOT_SPECS[name].consequence for name in tight}
    dropped_ranks = {SLOT_SPECS[name].consequence for name in full - tight}
    if Consequence.BURN in dropped_ranks:
        assert Consequence.MARGIN not in kept_ranks, (tight, full - tight)


def test_burn_facts_are_the_last_to_go() -> None:
    """At a budget that fits only a couple of facts, they must be the costly ones."""
    entries = factbrief.resolve_sheets(_BUSY)
    survivors = _brief_slots(factbrief.brief("SelectionStep", entries, budget=1600))
    assert survivors, "1600 characters must fit at least one fact"
    assert all(
        SLOT_SPECS[name].consequence is Consequence.BURN for name in survivors
    ), survivors


def test_truncation_is_announced_and_counted() -> None:
    entries = factbrief.resolve_sheets(_BUSY)
    full = factbrief.brief("SelectionStep", entries, budget=10**6)
    tight = factbrief.brief("SelectionStep", entries, budget=2400)

    assert "omitted to fit the prompt budget" not in full
    assert "omitted to fit the prompt budget" in tight
    assert "unread rather than unconstrained" in tight, (
        "an omitted fact must not read as a nonexistent one"
    )

    # Count ENTRIES, not distinct slot names: two devices legitimately answer the
    # same slot, and a set would silently merge them.
    dropped = _entry_count(full) - _entry_count(tight)
    reported = int(tight.rsplit("[", 1)[1].split(" ", 1)[0])
    assert reported == dropped, (reported, dropped)


def test_brief_is_byte_identical_across_runs() -> None:
    entries = factbrief.resolve_sheets(_BUSY)
    for budget in (None, 600, 2400):
        first = factbrief.brief("SelectionStep", entries, budget=budget)
        second = factbrief.brief("SelectionStep", entries, budget=budget)
        assert first == second, budget


def test_default_budget_keeps_a_realistic_design_whole() -> None:
    """The common case — one MCU and one regulator — must not be truncated."""
    entries = factbrief.resolve_sheets([
        _Part("U1", "STM32F103C8T6", "mcu"),
        _Part("U2", "AMS1117-3.3", "ldo regulator"),
    ])
    for step in ("TopologyStep", "SelectionStep", "SchConnectionsStep"):
        text = factbrief.brief(step, entries)
        assert "omitted to fit the prompt budget" not in text, (step, len(text))
