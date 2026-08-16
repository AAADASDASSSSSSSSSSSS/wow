"""FactSheet contract gates.

Four properties are load-bearing and are proven here rather than documented:

1. **Every slot has a consumer.** A hard fact with nobody reading it is how the
   previous schema accumulated four dead fields.
2. **No blank cells.** Questionnaire slots are required model fields, so an
   unanswered question cannot parse as "no limit".
3. **A single-source range has no soft band.** Not "does not currently warn" —
   *cannot* warn, for any input.
4. **Consequence beats data shape.** On ``burn``/``malfunction`` slots a
   disputed band is still an ERROR at the strict edge; only ``margin`` slots go
   soft.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ratsnestpro.domain.contracts import Severity
from ratsnestpro.eda.factsheet import (
    DEVICE_ROSTER,
    QUESTIONNAIRE,
    SLOT_SPECS,
    Branch,
    Comparison,
    ConditionalFact,
    ConflictingRangeFact,
    Consequence,
    DeviceClass,
    FixedFact,
    Observation,
    QualitativeFact,
    RangeFact,
    Slot,
    Source,
    Status,
    consumer_registry,
    coverage,
    coverage_table,
    evaluate,
    sheet_model,
    slot_spec,
)

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

SRC_A = Source(doc="Datasheet A", ref="Table 1 / p.10", url="https://a.example/ds.pdf",
               accessed="2026-07-30")
SRC_B = Source(doc="Application note B", ref="§2.2", url="https://b.example/an.pdf",
               accessed="2026-07-30")


def asserted(value: object, source: Source | None = None) -> Slot:
    """An asserted slot. Structured payloads carry no source of their own, so one
    is attached at slot level; fact shapes bring their own."""
    if source is None and not callable(getattr(value, "provenance", None)):
        source = SRC_A
    return Slot(status=Status.ASSERTED, value=value, source=source)


def conflict(low: float, high: float, bound: str) -> ConflictingRangeFact:
    return ConflictingRangeFact(
        observations=[Observation(value=low, source=SRC_A), Observation(value=high, source=SRC_B)],
        bound=bound,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# 1. registry: no slot without a consumer, no consumer without a slot
# --------------------------------------------------------------------------- #


def test_every_questionnaire_slot_has_a_spec_and_a_consumer() -> None:
    registry = consumer_registry()
    for device_class, slots in QUESTIONNAIRE.items():
        for name in slots:
            assert name in SLOT_SPECS, f"{device_class}:{name} has no SlotSpec"
            assert registry[name], f"{name} is a hard fact with no declared consumer"


def test_no_orphan_slot_specs() -> None:
    used = {name for slots in QUESTIONNAIRE.values() for name in slots}
    orphans = sorted(set(SLOT_SPECS) - used)
    assert not orphans, f"slot specs nobody asks for: {orphans}"


def test_every_questionnaire_slot_is_a_required_field() -> None:
    """This is what makes a blank cell unrepresentable rather than merely tested."""
    for device_class, slots in QUESTIONNAIRE.items():
        model = sheet_model(device_class)
        for name in slots:
            field = model.model_fields.get(name)
            assert field is not None, f"{device_class}:{name} missing from the model"
            assert field.is_required(), f"{device_class}:{name} must not have a default"


def test_incomplete_sheet_does_not_parse() -> None:
    with pytest.raises(ValidationError):
        sheet_model(DeviceClass.LDO)(
            device="Nope",
            source=SRC_A,
            packages=asserted(["SOT-23-5"]),
            pin_count=asserted(5),
            # vin_range / vout / dropout_v / ... deliberately omitted
        )


# --------------------------------------------------------------------------- #
# 2. three-state slot discipline
# --------------------------------------------------------------------------- #


def test_asserted_slot_needs_a_page_level_source() -> None:
    with pytest.raises(ValidationError):
        Slot(status=Status.ASSERTED, value=3.3)  # no source at all
    with pytest.raises(ValidationError):
        Slot(status=Status.ASSERTED, value=3.3, source=Source(doc="Some doc"))  # no ref


def test_asserted_slot_accepts_a_source_carried_by_the_fact() -> None:
    slot = Slot(status=Status.ASSERTED, value=FixedFact(value=40, unit="MHz", source=SRC_A))
    assert slot.effective_source() is not None
    assert slot.effective_source().ref  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "status", [Status.NOT_APPLICABLE, Status.NOT_ASSERTED, Status.BLOCKED]
)
def test_non_asserted_slot_must_explain_itself(status: Status) -> None:
    with pytest.raises(ValidationError):
        Slot(status=status)
    assert Slot(status=status, reason="datasheet states no such limit").reason


# --------------------------------------------------------------------------- #
# 3. conflict cannot be faked
# --------------------------------------------------------------------------- #


def test_conflicting_range_needs_two_observations() -> None:
    with pytest.raises(ValidationError):
        ConflictingRangeFact(observations=[Observation(value=1.0, source=SRC_A)])


def test_conflicting_range_needs_two_distinct_documents() -> None:
    """A single document's interval is a RangeFact — it may not become a conflict."""
    same = Source(doc="Datasheet A", ref="p.11", url="https://a.example/ds.pdf")
    with pytest.raises(ValidationError):
        ConflictingRangeFact(
            observations=[Observation(value=1.0, source=SRC_A), Observation(value=2.2, source=same)]
        )


def test_conflicting_range_edges_are_derived_not_authored() -> None:
    fact = conflict(1.0, 2.2, "lower")
    assert (fact.low, fact.high) == (1.0, 2.2)
    # The edges are computed from the observations: not inputs, not settable.
    assert "low" not in ConflictingRangeFact.model_fields
    assert "high" not in ConflictingRangeFact.model_fields
    with pytest.raises(AttributeError):
        fact.low = 0.5  # type: ignore[misc]
    # Authoring an edge directly is ignored rather than honoured.
    forged = ConflictingRangeFact(
        observations=[Observation(value=1.0, source=SRC_A), Observation(value=2.2, source=SRC_B)],
        bound="lower",
        low=0.1,  # type: ignore[call-arg]
    )
    assert forged.low == 1.0


def test_conditional_selector_must_be_used_by_its_branches() -> None:
    with pytest.raises(ValidationError):
        ConditionalFact(
            selector="supply_v",
            branches=[Branch(when={"adc_used": True}, value=FixedFact(value=1, source=SRC_A))],
            source=SRC_A,
        )


# --------------------------------------------------------------------------- #
# 4. a single-source range CANNOT produce a soft band
# --------------------------------------------------------------------------- #


def test_single_source_range_never_warns() -> None:
    spec = slot_spec("clock_external")          # WITHIN · malfunction
    slot = asserted(RangeFact(min=4, max=16, unit="MHz", source=SRC_A))
    warnings = 0
    for tenth in range(0, 301):                 # 0.0 .. 30.0 MHz
        verdict = evaluate(spec, slot, tenth / 10.0)
        assert verdict is not None
        if verdict.severity is Severity.WARNING:
            warnings += 1
        assert not verdict.disputed
    assert warnings == 0, "a single-source interval must have no disputed middle"


def test_single_source_range_blocks_outside_both_edges() -> None:
    spec = slot_spec("clock_external")
    slot = asserted(RangeFact(min=4, max=16, unit="MHz", source=SRC_A))
    assert evaluate(spec, slot, 25.0).ok is False        # type: ignore[union-attr]
    assert evaluate(spec, slot, 3.5).ok is False         # type: ignore[union-attr]
    assert evaluate(spec, slot, 8.0).ok is True          # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# 5. consequence decides severity, not data shape
# --------------------------------------------------------------------------- #

_BOUND_FOR = {
    Comparison.MAX_ALLOWED: "upper",
    Comparison.MIN_REQUIRED: "lower",
    Comparison.EXACT: "value",
    Comparison.WITHIN: "value",
}

_HARD_CHECKABLE = sorted(
    name
    for name, spec in SLOT_SPECS.items()
    if spec.comparison is not Comparison.NONE
    and spec.consequence in (Consequence.BURN, Consequence.MALFUNCTION)
)


@pytest.mark.parametrize("name", _HARD_CHECKABLE)
def test_burn_and_malfunction_slots_have_no_soft_band(name: str) -> None:
    """Sources may disagree; a board-damaging or function-breaking slot may not go soft."""
    spec = slot_spec(name)
    slot = asserted(conflict(10.0, 20.0, _BOUND_FOR[spec.comparison]))
    verdict = evaluate(spec, slot, 15.0)        # squarely inside the disputed band
    assert verdict is not None
    assert verdict.disputed, "15 should be recognised as disputed"
    assert verdict.severity is Severity.ERROR, f"{name} degraded a hard limit to a warning"
    assert verdict.ok is False


def test_margin_slot_disputed_band_warns_with_both_citations() -> None:
    spec = slot_spec("required_cin")            # MIN_REQUIRED · margin
    slot = asserted(conflict(1.0, 2.2, "lower"))
    verdict = evaluate(spec, slot, 1.5)
    assert verdict is not None
    assert verdict.disputed and verdict.severity is Severity.WARNING and verdict.ok
    assert "Datasheet A" in verdict.message and "Application note B" in verdict.message


def test_documented_conflict_may_soften_a_malfunction_slot() -> None:
    """ST publishes both 10 nF and 100 nF for VDDA; blocking its own figure is wrong."""
    spec = slot_spec("required_cout")           # MIN_REQUIRED · malfunction
    fact = ConflictingRangeFact(
        observations=[Observation(value=0.01, source=SRC_A), Observation(value=0.1, source=SRC_B)],
        bound="lower",
        disputed_consequence="margin",
        disputed_reason="both values are published by the same vendor for the same pin",
    )
    verdict = evaluate(spec, asserted(fact), 0.01)
    assert verdict is not None
    assert verdict.disputed and verdict.severity is Severity.WARNING and verdict.ok
    assert "treated as headroom" in verdict.message


def test_override_cannot_soften_a_burn_slot() -> None:
    """The escape hatch stops at anything that could damage the board."""
    burn_slots = [
        name for name, spec in SLOT_SPECS.items()
        if spec.consequence is Consequence.BURN and spec.comparison is not Comparison.NONE
    ]
    assert burn_slots, "expected at least one checkable burn slot"
    for name in burn_slots:
        spec = slot_spec(name)
        fact = ConflictingRangeFact(
            observations=[
                Observation(value=10.0, source=SRC_A),
                Observation(value=20.0, source=SRC_B),
            ],
            bound=_BOUND_FOR[spec.comparison],  # type: ignore[arg-type]
            disputed_consequence="margin",
            disputed_reason="attempt to soften a burn slot",
        )
        verdict = evaluate(spec, asserted(fact), 15.0)
        assert verdict is not None
        assert verdict.severity is Severity.ERROR, f"{name} was softened despite being burn"


def test_softening_requires_a_recorded_reason() -> None:
    with pytest.raises(ValidationError):
        ConflictingRangeFact(
            observations=[
                Observation(value=0.01, source=SRC_A),
                Observation(value=0.1, source=SRC_B),
            ],
            bound="lower",
            disputed_consequence="margin",
        )


def test_conflict_outside_every_source_is_an_error_even_on_a_margin_slot() -> None:
    spec = slot_spec("required_cin")
    slot = asserted(conflict(1.0, 2.2, "lower"))
    verdict = evaluate(spec, slot, 0.47)        # below what *any* source allows
    assert verdict is not None and not verdict.ok and verdict.severity is Severity.WARNING
    assert "violates every source" in verdict.message


# --------------------------------------------------------------------------- #
# 6. conditional facts: different premises, each hard on its own
# --------------------------------------------------------------------------- #


def _atmega_speed_grades() -> ConditionalFact:
    """The real AVR table: <=4 MHz at 1.8 V, <=10 at 2.7 V, <=20 at 4.5 V."""
    return ConditionalFact(
        selector="supply_v",
        branches=[
            Branch(when={"supply_v": [1.8, 5.5]}, value=FixedFact(value=4, unit="MHz",
                                                                  source=SRC_A)),
            Branch(when={"supply_v": [2.7, 5.5]}, value=FixedFact(value=10, unit="MHz",
                                                                  source=SRC_A)),
            Branch(when={"supply_v": [4.5, 5.5]}, value=FixedFact(value=20, unit="MHz",
                                                                  source=SRC_A)),
        ],
        combine="max_of_matching",
        unit="MHz",
        source=SRC_A,
    )


@pytest.mark.parametrize(
    "supply_v,clock_mhz,expect_ok",
    [
        (3.3, 16.0, False),   # the classic mistake: 16 MHz on a 3.3 V AVR
        (3.3, 8.0, True),
        (5.0, 16.0, True),
        (5.0, 20.0, True),
        (1.8, 8.0, False),
    ],
)
def test_conditional_selects_the_applicable_arm(
    supply_v: float, clock_mhz: float, expect_ok: bool
) -> None:
    spec = slot_spec("freq_vs_supply")          # MAX_ALLOWED · burn
    slot = asserted(_atmega_speed_grades())
    verdict = evaluate(spec, slot, clock_mhz, {"supply_v": supply_v})
    assert verdict is not None
    assert verdict.ok is expect_ok
    if not expect_ok:
        assert verdict.severity is Severity.ERROR


def test_conditional_unknown_selector_falls_back_to_the_strictest_arm() -> None:
    spec = slot_spec("freq_vs_supply")
    slot = asserted(_atmega_speed_grades())
    verdict = evaluate(spec, slot, 8.0, {})     # supply unknown -> strictest arm is 4 MHz
    assert verdict is not None and not verdict.ok
    assert "undetermined" in verdict.message and "strictest" in verdict.message


def test_conditional_skip_policy_fails_open() -> None:
    spec = slot_spec("freq_vs_supply")
    fact = _atmega_speed_grades().model_copy(update={"on_unknown": "skip"})
    assert evaluate(spec, asserted(fact), 8.0, {}) is None


def test_conditional_supply_range_with_adc_condition() -> None:
    """STM32F103: VDD 2.0-3.6 V, but 2.4-3.6 V once the ADC is used."""
    spec = slot_spec("supply_range")            # WITHIN · burn
    fact = ConditionalFact(
        selector="adc_used",
        branches=[
            Branch(when={"adc_used": False}, value=RangeFact(min=2.0, max=3.6, unit="V",
                                                             source=SRC_A)),
            Branch(when={"adc_used": True}, value=RangeFact(min=2.4, max=3.6, unit="V",
                                                            source=SRC_A)),
        ],
        combine="first_match",
        unit="V",
        source=SRC_A,
    )
    slot = asserted(fact)
    assert evaluate(spec, slot, 2.2, {"adc_used": False}).ok is True   # type: ignore[union-attr]
    assert evaluate(spec, slot, 2.2, {"adc_used": True}).ok is False   # type: ignore[union-attr]
    unknown = evaluate(spec, slot, 2.2, {})
    assert unknown is not None and not unknown.ok   # strictest arm applied


# --------------------------------------------------------------------------- #
# 7. fail open everywhere a verdict would require inventing something
# --------------------------------------------------------------------------- #


def test_evaluate_fails_open() -> None:
    spec = slot_spec("clock_external")
    ranged = asserted(RangeFact(min=4, max=16, unit="MHz", source=SRC_A))
    assert evaluate(spec, ranged, None) is None                       # value unknown
    gap = Slot(status=Status.NOT_ASSERTED, reason="not extracted yet")
    assert evaluate(spec, gap, 25.0) is None                          # slot not asserted
    qualitative = asserted(QualitativeFact(text="as close as possible", source=SRC_A))
    assert evaluate(spec, qualitative, 25.0) is None                  # nothing to threshold
    assert evaluate(slot_spec("supply_rails"), asserted([]), 3.3) is None  # comparison NONE


# --------------------------------------------------------------------------- #
# 8. coverage report
# --------------------------------------------------------------------------- #


def test_coverage_report_covers_the_whole_roster() -> None:
    """One row per roster entry — asserted against the roster, not a magic number.

    This previously hardcoded 16 and broke the moment the crystal entries went from
    two placeholders to three real parts. The invariant is the correspondence, not
    the count.
    """
    rows = coverage()
    assert len(rows) == len(DEVICE_ROSTER)
    assert {r.slug for r in rows} == {e.slug for e in DEVICE_ROSTER}
    assert {r.device_class for r in rows} == set(DeviceClass)
    table = coverage_table()
    for row in rows:
        assert row.device in table



# Consumer names in SLOT_SPECS that correspond to checks the pipeline really
# emits. Everything else in consumer_registry() is a planned target.
WIRED_CONSUMERS: frozenset[str] = frozenset({
    "SelectionStep.datasheet_limits",
    "SchConnectionsStep.datasheet_connection",
})


def test_wired_consumers_are_real_check_names() -> None:
    """A consumer claimed as wired must name a check the pipeline emits.

    The registry was written as a design target and most of it still is. This
    keeps the WIRED set honest: the check name has to appear in the pipeline
    source, so a name cannot drift out of existence unnoticed - which is exactly
    what happened to ``SchConnectionsStep.design_policy``, a consumer declared on
    twelve slots that never existed.
    """
    import re
    from pathlib import Path

    import ratsnestpro.orchestration.pipeline as pipeline_module

    source = Path(pipeline_module.__file__).read_text(encoding="utf-8")
    emitted = set(re.findall(r'name=f?"([A-Za-z_][A-Za-z0-9_]*)', source))

    for consumer in WIRED_CONSUMERS:
        step, _, check_name = consumer.partition(".")
        assert check_name in emitted, (
            f"{consumer} is listed as wired but no check named {check_name!r} is "
            f"emitted by pipeline.py"
        )
        assert hasattr(pipeline_module, step), (
            f"{consumer} names step {step!r}, which does not exist"
        )


def test_declared_consumers_are_not_mistaken_for_wiring() -> None:
    """Document the gap between declared and wired, so nobody re-learns it.

    If this ratio ever reaches 1.0 the registry has become trustworthy and this
    test should be replaced by one asserting that.
    """
    declared = {c for cs in consumer_registry().values() for c in cs}
    assert WIRED_CONSUMERS <= declared, (
        "a name marked wired must at least appear in the registry"
    )
    assert len(declared) > len(WIRED_CONSUMERS), (
        "the registry no longer contains unwired targets - update this test and "
        "the consumer_registry docstring to say so"
    )
