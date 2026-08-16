"""Gates on the fact sheets that actually ship in ``data/fact_sheets``.

The contract tests prove the *shape* is sound; these prove the *data* is. Three
things are enforced:

1. **Provenance.** Every asserted slot — including every fact nested inside a
   structured payload — carries a document plus a section/table/page reference.
   A value without a page is exactly the kind of half-remembered number this
   layer exists to keep out.
2. **No silent gaps.** Every roster device that has a sheet answers every slot of
   its questionnaire, and any remaining gap is listed explicitly.
3. **The verdicts that matter.** The classic per-family mistakes are asserted
   directly, so a future data edit that quietly loosens a limit fails here.
"""

from __future__ import annotations

import pytest

from ratsnestpro.domain.contracts import Severity
from ratsnestpro.eda.factsheet import (
    QUESTIONNAIRE,
    ConditionalFact,
    ConflictingRangeFact,
    DeviceClass,
    FactSheetBase,
    QualitativeFact,
    Slot,
    Source,
    Status,
    all_fact_sheets,
    coverage,
    evaluate,
    fact_sheet,
    open_gaps,
    slot_spec,
)

SHEETS = all_fact_sheets()
MCU_DEVICES = [s.device for s in SHEETS if DeviceClass(s.device_class) is DeviceClass.MCU]


def _nested_sources(obj: object) -> list[Source]:
    """Every Source reachable from a slot value, however deeply nested."""
    found: list[Source] = []
    if isinstance(obj, Source):
        return [obj]
    if isinstance(obj, list):
        for item in obj:
            found.extend(_nested_sources(item))
        return found
    fields = getattr(type(obj), "model_fields", None)
    if fields:
        for name in fields:
            found.extend(_nested_sources(getattr(obj, name, None)))
    return found


def test_sheets_are_present() -> None:
    assert SHEETS, "no fact sheets shipped in data/fact_sheets"
    assert len(MCU_DEVICES) == 5, f"expected the five migrated MCUs, got {MCU_DEVICES}"
    by_class: dict[DeviceClass, int] = {}
    for sheet in SHEETS:
        cls = DeviceClass(sheet.device_class)
        by_class[cls] = by_class.get(cls, 0) + 1
    assert by_class[DeviceClass.LDO] == 4, f"expected four LDOs, got {by_class.get(DeviceClass.LDO)}"
    assert by_class[DeviceClass.DCDC] == 2, (
        f"expected two DC-DC converters, got {by_class.get(DeviceClass.DCDC)}"
    )
    assert by_class[DeviceClass.TVS] == 2, f"expected two TVS parts, got {by_class.get(DeviceClass.TVS)}"
    assert by_class[DeviceClass.CONNECTOR] == 1, (
        f"expected one connector, got {by_class.get(DeviceClass.CONNECTOR)}"
    )
    assert by_class[DeviceClass.CRYSTAL] == 3, (
        f"expected three crystals (12/16/40 MHz), got {by_class.get(DeviceClass.CRYSTAL)}"
    )


@pytest.mark.parametrize("sheet", SHEETS, ids=lambda s: s.device)
def test_every_slot_is_answered(sheet: FactSheetBase) -> None:
    for name in QUESTIONNAIRE[DeviceClass(sheet.device_class)]:
        slot = sheet.slot(name)
        assert slot is not None, f"{sheet.device}: {name} missing"
        assert slot.status in set(Status), f"{sheet.device}: {name} has no settled status"


@pytest.mark.parametrize("sheet", SHEETS, ids=lambda s: s.device)
def test_asserted_slots_cite_a_page(sheet: FactSheetBase) -> None:
    for name in QUESTIONNAIRE[DeviceClass(sheet.device_class)]:
        slot = sheet.slot(name)
        assert slot is not None
        if slot.status is not Status.ASSERTED:
            continue
        sources = [*slot.sources(), *_nested_sources(slot.value)]
        assert sources, f"{sheet.device}.{name}: asserted with no source at all"
        for src in sources:
            assert src.doc.strip(), f"{sheet.device}.{name}: a source has no document"
            assert src.is_page_level(), (
                f"{sheet.device}.{name}: source {src.doc!r} has no section/table/page ref"
            )
            assert src.url.startswith("http"), f"{sheet.device}.{name}: {src.doc!r} has no URL"


@pytest.mark.parametrize("sheet", SHEETS, ids=lambda s: s.device)
def test_non_asserted_slots_explain_themselves(sheet: FactSheetBase) -> None:
    for name in QUESTIONNAIRE[DeviceClass(sheet.device_class)]:
        slot = sheet.slot(name)
        assert slot is not None
        if slot.status is Status.ASSERTED:
            continue
        assert len(slot.reason.strip()) > 20, (
            f"{sheet.device}.{name}: {slot.status} needs a real reason, got {slot.reason!r}"
        )


def test_conflicts_come_from_distinct_documents_and_are_justified() -> None:
    """A softened disputed band must name why, and cite two different documents."""
    for sheet in SHEETS:
        for name in QUESTIONNAIRE[DeviceClass(sheet.device_class)]:
            slot = sheet.slot(name)
            assert slot is not None
            for fact in _conflicts(slot):
                docs = {o.source.doc for o in fact.observations}
                assert len(docs) >= 2, f"{sheet.device}.{name}: conflict from one document"
                if fact.disputed_consequence is not None:
                    assert len(fact.disputed_reason.strip()) > 20, (
                        f"{sheet.device}.{name}: softened without a real reason"
                    )


def _conflicts(obj: object) -> list[ConflictingRangeFact]:
    out: list[ConflictingRangeFact] = []
    if isinstance(obj, ConflictingRangeFact):
        return [obj]
    if isinstance(obj, list):
        for item in obj:
            out.extend(_conflicts(item))
        return out
    fields = getattr(type(obj), "model_fields", None)
    if fields:
        for name in fields:
            out.extend(_conflicts(getattr(obj, name, None)))
    return out


def test_gaps_are_listed_not_hidden() -> None:
    """Whatever gaps remain must be enumerable — that is the point of the status."""
    gaps = open_gaps()
    for device, slot_name, reason in gaps:
        assert reason.strip(), f"{device}.{slot_name}: gap with no reason"
    # Batch 1 closed every MCU gap; this pins that so a regression is visible.
    mcu_gaps = [g for g in gaps if g[0] in MCU_DEVICES]
    assert not mcu_gaps, f"MCU gaps reappeared: {mcu_gaps}"


def test_every_slot_is_accounted_for() -> None:
    """The invariant is that no slot goes missing — not that no gap exists.

    An earlier version of this test demanded ``answered == total`` for every
    sheet, which quietly turned "this batch happens to be complete" into a rule.
    The AMS1117 then failed it for an entirely legitimate reason: its datasheet
    publishes no recommended input-voltage range and no input-capacitor
    requirement, so two slots are honest ``not_asserted`` gaps. Demanding zero
    gaps would push an author to invent values — the exact failure mode this
    layer exists to prevent. What must hold is that every slot lands in exactly
    one bucket, and that every gap carries a reason.
    """
    for row in coverage():
        if not row.present:
            continue
        total = len(QUESTIONNAIRE[row.device_class])
        assert row.answered + len(row.gaps) == total, (
            f"{row.device}: {row.answered} answered + {len(row.gaps)} gaps != {total} slots"
        )


def test_gap_inventory_is_small_and_explained() -> None:
    """Gaps are acceptable but must stay visible and actionable.

    The requirement on a gap reason is not a particular turn of phrase but that it
    leaves the next person able to act: either it lists what was already searched
    ("Sources tried: ...") or it names the next move ("Next step: ..."), ideally
    both. An earlier version of this test grepped for the words "tried"/"no ",
    which rejected a perfectly good explanation that happened to be worded
    differently.
    """
    gaps = open_gaps()
    for device, slot_name, reason in gaps:
        assert len(reason.strip()) > 40, f"{device}.{slot_name}: gap reason too thin"
        low = reason.lower()
        assert "sources tried" in low or "next step" in low, (
            f"{device}.{slot_name}: a gap reason must record what was searched "
            f"('Sources tried: ...') or what to do next ('Next step: ...')"
        )
    # Pin the current inventory so a new gap has to be a deliberate, reviewed act.
    assert {(d, s) for d, s, _ in gaps} == {
        # AMS1117: its datasheet publishes neither a recommended input range nor an
        # input-capacitor requirement. Both are real absences, not missed extractions.
        ("AMS1117-3.3", "vin_range"),
        ("AMS1117-3.3", "required_cin"),
        # Crystals: neither the LCSC specification table nor the JLCPCB catalog record
        # publishes a drive level, and the LCSC datasheet links return an HTML
        # interstitial rather than the manufacturer PDF.
        ("X322512MSB4SI", "drive_level_uw"),
        ("X322516MLB4SI", "drive_level_uw"),
        ("TXM40M0004252HBCEO00T", "drive_level_uw"),
    }, f"gap inventory changed: {sorted((d, s) for d, s, _ in gaps)}"


# --------------------------------------------------------------------------- #
# The verdicts that motivated the whole exercise
# --------------------------------------------------------------------------- #


def _verdict(device: str, slot_name: str, actual: float, ctx: dict[str, object] | None = None):
    sheet = fact_sheet(device)
    assert sheet is not None, f"{device} not found"
    slot = sheet.slot(slot_name)
    assert slot is not None
    return evaluate(slot_spec(slot_name), slot, actual, ctx or {})


def test_atmega_16mhz_on_3v3_is_blocked() -> None:
    """The canonical mistake: 16 MHz needs >= 4.5 V on an ATmega328P."""
    bad = _verdict("ATmega328P-AU", "freq_vs_supply", 16.0, {"supply_v": 3.3})
    assert bad is not None and not bad.ok and bad.severity is Severity.ERROR
    ok = _verdict("ATmega328P-AU", "freq_vs_supply", 16.0, {"supply_v": 5.0})
    assert ok is not None and ok.ok
    assert _verdict("ATmega328P-AU", "freq_vs_supply", 8.0, {"supply_v": 3.3}).ok  # type: ignore[union-attr]


def test_stm32_hse_out_of_range_is_blocked() -> None:
    bad = _verdict("STM32F103C8T6", "clock_external", 25.0)
    assert bad is not None and not bad.ok and bad.severity is Severity.ERROR
    assert _verdict("STM32F103C8T6", "clock_external", 8.0).ok  # type: ignore[union-attr]


def test_stm32_vdd_range_is_unconditional() -> None:
    """Verified against Table 9: the ADC condition constrains VDDA, not VDD."""
    sheet = fact_sheet("STM32F103C8T6")
    assert sheet is not None
    slot = sheet.slot("supply_range")
    assert slot is not None
    assert not isinstance(slot.value, ConditionalFact), (
        "VDD's operating range has no conditions in DS5319 Table 9 — do not re-add the "
        "ADC arm here; it belongs to VDDA in supply_rails"
    )
    assert _verdict("STM32F103C8T6", "supply_range", 2.2).ok      # type: ignore[union-attr]
    assert not _verdict("STM32F103C8T6", "supply_range", 3.8).ok  # type: ignore[union-attr]


def test_esp32_crystal_must_be_40mhz() -> None:
    bad = _verdict("ESP32-WROOM-32", "clock_external", 16.0)
    assert bad is not None and not bad.ok and bad.severity is Severity.ERROR
    assert _verdict("ESP32-WROOM-32", "clock_external", 40.0).ok  # type: ignore[union-attr]


def test_esp32_supply_floor_depends_on_in_package_flash() -> None:
    """2.3 V and 3.0 V are both correct — for different variants (DS Table 5-2 note 2)."""
    plain = _verdict("ESP32-D0WD", "supply_range", 2.5, {"in_package_flash_or_psram": False})
    assert plain is not None and plain.ok
    with_flash = _verdict("ESP32-D0WD", "supply_range", 2.5, {"in_package_flash_or_psram": True})
    assert with_flash is not None and not with_flash.ok
    unknown = _verdict("ESP32-D0WD", "supply_range", 2.5)
    assert unknown is not None and not unknown.ok, "undeclared variant must take the 3.0 V floor"
    assert "undetermined" in unknown.message


def test_esp32c3_and_esp32_do_not_share_crystal_layout_numbers() -> None:
    """2.0 mm vs 2.7 mm — same vendor, same family, different devices."""
    c3 = fact_sheet("ESP32-C3-MINI-1")
    esp = fact_sheet("ESP32-WROOM-32")
    assert c3 is not None and esp is not None
    assert c3.device == "ESP32-C3" and esp.device == "ESP32"
    c3_gap = c3.slot("clock_layout").value.min_gap_to_clock_pin_mm    # type: ignore[union-attr]
    esp_gap = esp.slot("clock_layout").value.min_gap_to_clock_pin_mm  # type: ignore[union-attr]
    assert (c3_gap, esp_gap) == (2.0, 2.7)


def test_rp2040_200mhz_needs_an_elevated_core_supply() -> None:
    """Inverted direction: the higher clock demands the higher voltage."""
    assert not _verdict("RP2040", "freq_vs_supply", 200.0, {"core_supply_v": 1.10}).ok  # type: ignore[union-attr]
    assert _verdict("RP2040", "freq_vs_supply", 200.0, {"core_supply_v": 1.15}).ok      # type: ignore[union-attr]
    undeclared = _verdict("RP2040", "freq_vs_supply", 200.0)
    assert undeclared is not None and not undeclared.ok, "must fall back to 133 MHz"


def test_rp2040_accepts_3v6_because_the_limit_is_3v63() -> None:
    """Guards the corrected bound: an earlier reading wrongly capped IOVDD at 3.3 V."""
    assert _verdict("RP2040", "supply_range", 3.6).ok       # type: ignore[union-attr]
    assert not _verdict("RP2040", "supply_range", 5.0).ok   # type: ignore[union-attr]


def test_ap2112_has_no_5v_variant() -> None:
    """The exact part number a model once invented: AP2112K-5.0 does not exist."""
    sheet = fact_sheet("AP2112K-3.3")
    assert sheet is not None
    vout = sheet.slot("vout")
    assert vout is not None and vout.value is not None
    note = getattr(vout.value, "note", "")
    assert "NO 5.0 V variant" in note or "no 5.0 v variant" in note.lower(), (
        "the absence of a 5 V AP2112 must stay recorded — it is what makes the "
        "hallucinated AP2112K-5.0 provably wrong rather than merely unlikely"
    )
    assert getattr(vout.value, "value", None) == 3.3


def test_regulator_input_limits_catch_overvoltage() -> None:
    """abs_max_vin exists precisely so a part with no recommended range can still gate."""
    # AMS1117 publishes only an absolute maximum; 24 V must still be caught.
    bad = _verdict("AMS1117-3.3", "abs_max_vin", 24.0)
    assert bad is not None and not bad.ok and bad.severity is Severity.ERROR
    assert _verdict("AMS1117-3.3", "abs_max_vin", 12.0).ok        # type: ignore[union-attr]
    # The AP2112's recommended range gates both edges.
    assert _verdict("AP2112K-3.3", "vin_range", 5.0).ok           # type: ignore[union-attr]
    assert not _verdict("AP2112K-3.3", "vin_range", 12.0).ok      # type: ignore[union-attr]
    assert not _verdict("AP2112K-3.3", "vin_range", 2.0).ok       # type: ignore[union-attr]


def test_ldo_output_capacitors_are_not_interchangeable() -> None:
    """22 uF tantalum vs 1 uF ceramic — a 20x spread that must never be averaged."""
    ams = _verdict("AMS1117-3.3", "required_cout", 1.0)
    assert ams is not None and not ams.ok, "1 uF is not enough for an AMS1117"
    assert _verdict("AMS1117-3.3", "required_cout", 22.0).ok      # type: ignore[union-attr]
    assert _verdict("AP2112K-3.3", "required_cout", 1.0).ok       # type: ignore[union-attr]


def test_mic5219_output_capacitor_depends_on_the_bypass_capacitor() -> None:
    """Fitting the 470 pF bypass capacitor doubles the required output capacitance."""
    assert _verdict("MIC5219", "required_cout", 1.0, {"cbyp_pf": 0}).ok        # type: ignore[union-attr]
    with_byp = _verdict("MIC5219", "required_cout", 1.0, {"cbyp_pf": 470})
    assert with_byp is not None and not with_byp.ok
    assert _verdict("MIC5219", "required_cout", 2.2, {"cbyp_pf": 470}).ok      # type: ignore[union-attr]
    # Bypass state unknown -> the stricter 2.2 uF floor applies, and says so.
    unknown = _verdict("MIC5219", "required_cout", 1.0)
    assert unknown is not None and not unknown.ok and "undetermined" in unknown.message


def test_lp2985_dropout_depends_on_load_and_silicon_revision() -> None:
    """Two condition variables at once: 575 mV legacy vs 254 mV new at full load."""
    sheet = fact_sheet("LP2985")
    assert sheet is not None
    slot = sheet.slot("dropout_v")
    assert slot is not None
    fact = slot.value
    assert isinstance(fact, ConditionalFact)
    selectors = {key for branch in fact.branches for key in branch.when}
    assert selectors == {"chip_revision", "load_ma"}, (
        "collapsing either condition would misstate the dropout by more than 2x"
    )


def test_boost_and_buck_input_ranges_do_not_overlap_in_meaning() -> None:
    """A buck needs VIN above VOUT; a boost is specified below it and may pass through."""
    assert not _verdict("TPS563201", "vin_range", 3.3).ok    # type: ignore[union-attr]
    assert _verdict("TPS563201", "vin_range", 12.0).ok       # type: ignore[union-attr]
    assert _verdict("TPS61023", "vin_range", 0.9).ok         # type: ignore[union-attr]
    assert not _verdict("TPS61023", "vin_range", 12.0).ok    # type: ignore[union-attr]


def test_tps61023_switching_frequency_follows_input_voltage() -> None:
    sheet = fact_sheet("TPS61023")
    assert sheet is not None
    slot = sheet.slot("switching_freq_khz")
    assert slot is not None and isinstance(slot.value, ConditionalFact)
    assert slot.value.selector == "vin_v"


def test_esd_diode_capacitance_separates_high_speed_from_low_speed_parts() -> None:
    """A part named 'Low capacitance' can still be 21x too slow for USB high speed."""
    usblc = fact_sheet("USBLC6-2")
    pesd = fact_sheet("PESD5V0L1BA")
    assert usblc is not None and pesd is not None
    usblc_c = usblc.slot("capacitance_pf").value.value    # type: ignore[union-attr]
    pesd_c = pesd.slot("capacitance_pf").value.value      # type: ignore[union-attr]
    assert usblc_c == 3.5 and pesd_c == 75.0, (
        "these two figures are the whole point of the slot: 3.5 pF max suits USB 2.0 "
        "high speed, 75 pF typ does not, despite the PESD part being titled "
        "'Low capacitance bidirectional ESD protection diode'"
    )
    # It is a datum, not a self-comparison: evaluating it must not pretend to judge.
    assert _verdict("USBLC6-2", "capacitance_pf", 3.0) is None


def test_esd_standoff_voltage_is_compared_against_the_protected_rail() -> None:
    """vrwm is MAX_ALLOWED: the standing rail must not exceed the standoff."""
    assert _verdict("USBLC6-2", "vrwm_v", 5.0).ok            # type: ignore[union-attr]
    assert not _verdict("USBLC6-2", "vrwm_v", 12.0).ok       # type: ignore[union-attr]
    # A useful real catch: USB VBUS at its 5.25 V upper tolerance exceeds the
    # PESD5V0L1BA's 5 V standoff, while the USB-rated USBLC6-2 accommodates it.
    assert _verdict("USBLC6-2", "vrwm_v", 5.25).ok           # type: ignore[union-attr]
    assert not _verdict("PESD5V0L1BA", "vrwm_v", 5.25).ok    # type: ignore[union-attr]


def test_usb_c_cc_pulldown_accepts_the_whole_tolerance_band() -> None:
    """Rd is 5.1k +/-20%; demanding exactly 5100 ohm would reject compliant parts."""
    for ohms in (4080.0, 4700.0, 5100.0, 5600.0, 6120.0):
        verdict = _verdict("USB-C 16P", "cc_pulldown_ohm", ohms)
        assert verdict is not None and verdict.ok, f"{ohms} ohm is within +/-20% of 5.1k"
    for ohms in (4000.0, 10000.0, 1000.0):
        verdict = _verdict("USB-C 16P", "cc_pulldown_ohm", ohms)
        assert verdict is not None and not verdict.ok, f"{ohms} ohm is outside the band"


def test_crystal_load_capacitance_is_per_part_not_per_package() -> None:
    """Same vendor, same 3225 outline, load capacitance differs by more than 2x."""
    twelve = fact_sheet("X322512MSB4SI")
    sixteen = fact_sheet("X322516MLB4SI")
    forty = fact_sheet("TXM40M0004252HBCEO00T")
    assert twelve is not None and sixteen is not None and forty is not None
    cls = tuple(
        s.slot("load_capacitance_pf").value.value  # type: ignore[union-attr]
        for s in (twelve, sixteen, forty)
    )
    assert cls == (20.0, 9.0, 15.0), (
        "these three values are why CL is recorded per part: a habitual 22 pF load "
        "capacitor pair is wrong for all three, and badly wrong for the 9 pF part"
    )


def test_most_stocked_12mhz_crystal_exceeds_the_rp2040_esr_limit() -> None:
    """The cheapest Basic 12 MHz part is out of spec for the MCU that mandates 12 MHz.

    RP2040's hardware design guide selects a crystal with a maximum ESR of 50 ohm.
    The most stocked 12 MHz crystal in the JLCPCB catalog is 80 ohm. Recording both
    facts is what makes the mismatch visible instead of shipping it.
    """
    xtal = fact_sheet("X322512MSB4SI")
    assert xtal is not None
    esr = xtal.slot("esr_max_ohm").value.value      # type: ignore[union-attr]
    assert esr == 80.0
    rp2040 = fact_sheet("RP2040")
    assert rp2040 is not None
    clock_layout = rp2040.slot("clock_layout")
    assert clock_layout is not None
    assert "50" in getattr(clock_layout.value, "note", ""), (
        "the RP2040 50 ohm ESR ceiling must stay recorded for this comparison to mean anything"
    )


def test_no_stocked_40mhz_crystal_meets_the_esp32_ppm_requirement() -> None:
    """ESP32 asks for +/-10 ppm; tolerance plus temperature drift blows past it."""
    xtal = fact_sheet("TXM40M0004252HBCEO00T")
    esp32 = fact_sheet("ESP32-WROOM-32")
    assert xtal is not None and esp32 is not None
    tol = xtal.slot("frequency_tolerance_ppm").value.value        # type: ignore[union-attr]
    stab = xtal.slot("frequency_stability_ppm").value.value       # type: ignore[union-attr]
    required = esp32.slot("clock_external").value.tolerance_ppm   # type: ignore[union-attr]
    assert required == 10.0, "ESP32's +/-10 ppm crystal requirement must stay asserted"
    assert tol + stab > required, (
        f"crystal worst case {tol} + {stab} ppm must be recognised as exceeding the "
        f"{required} ppm requirement — a part sold as '+/-10 ppm' does not meet it"
    )


def test_usb_c_vbus_rating_came_from_the_drawing_not_the_catalog_attribute() -> None:
    """The catalog says 3 A in one field and 5 A in another; the drawing says 3 A."""
    conn = fact_sheet("USB-C 16P")
    assert conn is not None
    rating = conn.slot("vbus_rating")
    assert rating is not None and rating.value is not None
    assert rating.value.current_a == 3.0 and rating.value.voltage_v == 5.0
    assert "DISCREPANCY" in rating.value.note.upper(), (
        "the conflicting catalog attribute must stay documented so nobody 'fixes' "
        "this back to 5 A from the distributor field"
    )


def test_unknown_device_never_produces_a_verdict() -> None:
    assert fact_sheet("TotallyUnknownMCU9000") is None
    assert fact_sheet("") is None


def test_lookup_resolves_part_numbers_and_kicad_lib_ids() -> None:
    """Parts reach their sheet as the design names them, not as the sheet titles it.

    The design refers to a device by ordering code (``ATmega328P-AU``) or by KiCad
    lib-id (``MCU_ST_STM32F1:STM32F103C8Tx``); the longest alias must win so the
    ESP32 sheet never answers for an ESP32-C3. Retired with ``test_hardfacts``,
    kept here because :func:`observe` feeds exactly these strings to the gate.
    """
    assert fact_sheet("ATmega328P-AU").device == "ATmega328P"            # type: ignore[union-attr]
    assert fact_sheet("MCU_Microchip_ATmega:ATmega328P-A").device == "ATmega328P"  # type: ignore[union-attr]
    assert fact_sheet("MCU_ST_STM32F1:STM32F103C8Tx").device == "STM32F103"       # type: ignore[union-attr]
    assert fact_sheet("ESP32").device == "ESP32"                          # type: ignore[union-attr]
    assert fact_sheet("ESP32-C3").device == "ESP32-C3"                    # type: ignore[union-attr]


def test_qualitative_facts_are_never_thresholded() -> None:
    """A 'as short as possible' rule must not acquire a number by accident."""
    for sheet in SHEETS:
        for name in QUESTIONNAIRE[DeviceClass(sheet.device_class)]:
            slot = sheet.slot(name)
            assert slot is not None
            if isinstance(slot.value, QualitativeFact):
                assert evaluate(slot_spec(name), slot, 1.0) is None


def _slot_of(sheet: FactSheetBase, name: str) -> Slot[object]:
    slot = sheet.slot(name)
    assert slot is not None
    return slot
