"""INF1 — device fact sheets: systematic, source-cited hard knowledge.

Why this module exists
---------------------
The retired ``eda.hardfacts`` layer grew as a *union* of whatever each datasheet
happened to state: ``speed_grades`` only for the AVR, ``crystal_range_mhz`` only
for the STM32, ``required_crystal_mhz`` only for the Espressif/RP2040 parts. The
consequence was not cosmetic — each branch of the datasheet gate only fired for
the subset of devices that had its field, so the *same* design error was blocked
on one MCU and passed silently on another. And ``None`` meant two different
things at once: "this device genuinely has no such limit" and "nobody extracted
it yet"; both failed open, indistinguishably.

This module replaces the union with a **questionnaire**: every device of a given
class must answer the same slot list. Each slot is one of four explicit states
(:class:`Status`) and every asserted value carries page-level provenance. A
missing answer is impossible to express — the questionnaire slots are required
model fields, so an incomplete sheet fails to parse.

Three ideas carry the design
----------------------------
1. **Three-state slots.** ``asserted`` needs a value *and* a source with a
   section/page reference. ``not_applicable`` / ``not_asserted`` / ``blocked``
   need a reason. Nothing is ever a silent default.

2. **Discriminated fact shapes.** A single authoritative interval
   (:class:`RangeFact`) is a different *type* from an interval derived from
   disagreeing sources (:class:`ConflictingRangeFact`), which is again different
   from a value that varies with a design condition (:class:`ConditionalFact`).
   Construction-time validators make it impossible to dress a single-source
   range up as a conflict, or to hide a conditional fact inside a conflict —
   that mislabelling is exactly how a hard limit would silently decay into a
   warning.

3. **Consequence decides severity, not data shape.** Each slot declares
   ``burn`` / ``malfunction`` / ``margin``. For ``burn`` and ``malfunction``
   slots the disputed band of a conflict is judged at its *strictest* edge and
   reported as an ERROR — those slots have no soft band at all. Only ``margin``
   slots produce a WARNING, because there the worst case is thin headroom
   rather than a dead board. The asymmetry is deliberate: a false block costs a
   parameter edit, a missed block costs the board.

Every evaluation **fails open**: an unknown device, a non-asserted slot, or an
undeterminable design value yields no verdict at all. A limit is never invented.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    computed_field,
    model_validator,
)

from ratsnestpro.domain.contracts import Severity

# Absolute tolerance for "exact value" comparisons (MHz, V, µF ...). Matches the
# tolerance the previous crystal check used, so migrated behaviour is unchanged.
_EXACT_TOL = 0.01


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class Status(StrEnum):
    """Why a slot has (or has not) a value. See module docstring."""

    ASSERTED = "asserted"
    NOT_APPLICABLE = "not_applicable"   # the device genuinely has no such limit
    NOT_ASSERTED = "not_asserted"       # not extracted yet — an open gap
    BLOCKED = "blocked"                 # source ladder exhausted, still unknown


class Consequence(StrEnum):
    """What happens when a slot's limit is violated — this drives severity."""

    BURN = "burn"                 # damages the device / board
    MALFUNCTION = "malfunction"   # does not damage, but does not work
    MARGIN = "margin"             # only headroom / cost


class Comparison(StrEnum):
    """How a design value is compared against the slot's fact."""

    MAX_ALLOWED = "max_allowed"     # actual must be <= fact
    MIN_REQUIRED = "min_required"   # actual must be >= fact
    EXACT = "exact"                 # actual must equal fact (within tolerance)
    WITHIN = "within"               # actual must lie inside [min, max]
    NONE = "none"                   # not numerically checkable at slot level


class DeviceClass(StrEnum):
    MCU = "mcu"
    LDO = "ldo"
    DCDC = "dcdc"
    CRYSTAL = "crystal"
    TVS = "tvs"
    CONNECTOR = "connector"


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #


class Source(BaseModel):
    """Page-level provenance. No asserted fact may exist without one."""

    model_config = ConfigDict(extra="ignore")

    doc: str = Field(min_length=1)      # title + revision / document number
    ref: str = ""                       # section / table / page — required when asserted
    url: str = ""
    accessed: str = ""

    def cite(self) -> str:
        return f"{self.doc} {self.ref}".strip() if self.ref else self.doc

    def is_page_level(self) -> bool:
        """True when the citation points at a section / table / page."""
        return bool(self.ref.strip())


# --------------------------------------------------------------------------- #
# Fact shapes (discriminated union on ``kind``)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Bound:
    """An inclusive allowed interval; ``None`` means unbounded on that side."""

    lo: float | None = None
    hi: float | None = None

    def contains(self, actual: float) -> bool:
        if self.lo is not None and actual < self.lo - _EXACT_TOL:
            return False
        if self.hi is not None and actual > self.hi + _EXACT_TOL:
            return False
        return True

    def describe(self, unit: str = "") -> str:
        suffix = f" {unit}" if unit else ""
        if self.lo is not None and self.hi is not None:
            if abs(self.hi - self.lo) <= _EXACT_TOL:
                return f"{self.lo:g}{suffix}"
            return f"{self.lo:g}-{self.hi:g}{suffix}"
        if self.hi is not None:
            return f"<= {self.hi:g}{suffix}"
        if self.lo is not None:
            return f">= {self.lo:g}{suffix}"
        return "unbounded"


def _combine(bounds: Sequence[Bound], *, permissive: bool) -> Bound:
    """Merge bounds into the widest (permissive) or narrowest (strict) one."""
    los = [b.lo for b in bounds if b.lo is not None]
    his = [b.hi for b in bounds if b.hi is not None]
    if permissive:
        lo = min(los) if len(los) == len(bounds) and los else None
        hi = max(his) if len(his) == len(bounds) and his else None
    else:
        lo = max(los) if los else None
        hi = min(his) if his else None
    return Bound(lo=lo, hi=hi)


class _SingleSourceFact(BaseModel):
    """Shared behaviour for fact shapes backed by exactly one document."""

    model_config = ConfigDict(extra="ignore")

    source: Source

    def provenance(self) -> list[Source]:
        return [self.source]


class FixedFact(_SingleSourceFact):
    """One authoritative scalar (a maximum, a minimum, or an exact value)."""

    kind: Literal["fixed"] = "fixed"
    value: float
    unit: str = ""
    tolerance_ppm: float | None = None
    tolerance_pct: float | None = None
    note: str = ""

    def _tolerance(self) -> float:
        """Half-width of the acceptance band around ``value``.

        A datasheet "exact" value almost always carries a tolerance, and honouring
        it is not cosmetic: USB Type-C specifies Rd as 5.1 kOhm +/-20%, so a
        perfectly compliant 4.7 kOhm resistor would be rejected if only the
        internal float-comparison epsilon were used. The widest asserted
        tolerance wins; the epsilon is the floor.
        """
        widths = [_EXACT_TOL]
        if self.tolerance_pct is not None:
            widths.append(abs(self.value) * self.tolerance_pct / 100.0)
        if self.tolerance_ppm is not None:
            widths.append(abs(self.value) * self.tolerance_ppm / 1_000_000.0)
        return max(widths)

    def bound(self, comparison: Comparison) -> Bound:
        match comparison:
            case Comparison.MAX_ALLOWED:
                return Bound(hi=self.value)
            case Comparison.MIN_REQUIRED:
                return Bound(lo=self.value)
            case Comparison.EXACT | Comparison.WITHIN:
                tol = self._tolerance()
                return Bound(lo=self.value - tol, hi=self.value + tol)
            case Comparison.NONE:
                return Bound()


class RangeFact(_SingleSourceFact):
    """An interval stated by a **single** authoritative source.

    Both edges are hard: outside is an ERROR, and there is no disputed middle.
    The singular ``source`` field is what enforces that — an interval assembled
    from two disagreeing documents cannot be expressed here, it must use
    :class:`ConflictingRangeFact`.
    """

    kind: Literal["range"] = "range"
    min: float
    max: float
    unit: str = ""
    note: str = ""

    @model_validator(mode="after")
    def _ordered(self) -> RangeFact:
        if self.min > self.max:
            raise ValueError("RangeFact min must be <= max")
        return self

    def bound(self, comparison: Comparison) -> Bound:
        match comparison:
            case Comparison.MAX_ALLOWED:
                return Bound(hi=self.max)
            case Comparison.MIN_REQUIRED:
                return Bound(lo=self.min)
            case Comparison.EXACT | Comparison.WITHIN:
                return Bound(lo=self.min, hi=self.max)
            case Comparison.NONE:
                return Bound()


class QualitativeFact(_SingleSourceFact):
    """A datasheet statement with no number ("place as close as possible").

    Stored verbatim with its source so it can still be injected as grounded
    guidance. It is deliberately **not** thresholdable — inventing "<= 5 mm"
    here would be exactly the fabrication this layer forbids.
    """

    kind: Literal["qualitative"] = "qualitative"
    text: str = Field(min_length=1)


class Branch(BaseModel):
    """One condition arm of a :class:`ConditionalFact`.

    ``when`` maps a selector name to a condition: a two-element list is an
    inclusive interval, anything else is compared for equality (booleans and
    strings included, strings case-insensitively).
    """

    model_config = ConfigDict(extra="ignore")

    when: dict[str, object] = Field(min_length=1)
    value: FixedFact | RangeFact = Field(discriminator="kind")
    note: str = ""

    def matches(self, ctx: Mapping[str, object]) -> bool | None:
        """``True``/``False`` when decidable, ``None`` when a selector is unknown."""
        decided = True
        for key, cond in self.when.items():
            actual = ctx.get(key)
            if actual is None:
                decided = False
                continue
            if not _condition_holds(cond, actual):
                return False
        return True if decided else None


def _condition_holds(cond: object, actual: object) -> bool:
    if isinstance(cond, list | tuple) and len(cond) == 2:
        try:
            lo, hi = float(cond[0]), float(cond[1])  # type: ignore[arg-type]
            val = float(actual)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return lo - _EXACT_TOL <= val <= hi + _EXACT_TOL
    if isinstance(cond, bool) or isinstance(actual, bool):
        return bool(cond) is bool(actual)
    if isinstance(cond, str) or isinstance(actual, str):
        return str(cond).strip().lower() == str(actual).strip().lower()
    try:
        return abs(float(cond) - float(actual)) <= _EXACT_TOL  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


class ConditionalFact(_SingleSourceFact):
    """Different hard values under different premises — **not** a conflict.

    The classic instances are the AVR's voltage-derated speed grades and the
    STM32's "VDD 2.0-3.6 V, but 2.4-3.6 V when the ADC is used". Each arm is
    hard on its own; the only question is which arm applies. When the selector
    cannot be determined from the design, ``on_unknown`` decides: ``strictest``
    judges against the narrowest arm (and says so in the message), ``skip``
    fails open.
    """

    kind: Literal["conditional"] = "conditional"
    selector: str = Field(min_length=1)
    branches: list[Branch] = Field(min_length=1)
    combine: Literal["max_of_matching", "min_of_matching", "first_match"] = "max_of_matching"
    on_unknown: Literal["strictest", "skip"] = "strictest"
    unit: str = ""
    note: str = ""

    @model_validator(mode="after")
    def _selector_is_used(self) -> ConditionalFact:
        keys = {k for b in self.branches for k in b.when}
        if self.selector not in keys:
            raise ValueError(
                f"ConditionalFact.selector {self.selector!r} is not used by any branch "
                f"(branch selectors: {sorted(keys)})"
            )
        return self

    def resolve(self, comparison: Comparison, ctx: Mapping[str, object]) -> tuple[Bound, bool]:
        """Return ``(bound, selector_known)``.

        ``selector_known`` is False when the arm could not be determined and the
        ``strictest`` fallback was used, so callers can say so in the message.
        """
        matching = [b for b in self.branches if b.matches(ctx) is True]
        if matching:
            bounds = [b.value.bound(comparison) for b in matching]
            if self.combine == "first_match":
                return bounds[0], True
            return _combine(bounds, permissive=self.combine == "max_of_matching"), True
        all_bounds = [b.value.bound(comparison) for b in self.branches]
        return _combine(all_bounds, permissive=False), False

    def branch_values(self) -> list[float]:
        out: list[float] = []
        for b in self.branches:
            if isinstance(b.value, FixedFact):
                out.append(b.value.value)
            else:
                out.extend([b.value.min, b.value.max])
        return out


class Observation(BaseModel):
    """One source's reading of a quantity, used to express a genuine conflict."""

    model_config = ConfigDict(extra="ignore")

    value: float
    source: Source


class ConflictingRangeFact(BaseModel):
    """A quantity two or more authoritative sources disagree about.

    ``bound`` says what the observations constrain: the upper edge of an allowed
    interval, the lower edge, or a target value. The permissive edge is where
    *every* source agrees the design is wrong (always an ERROR); between the
    edges is the disputed band, whose severity is decided by the slot's
    :class:`Consequence` — for ``burn``/``malfunction`` slots it is still an
    ERROR judged at the strict edge, so those slots have no soft band.

    Two validators keep this type honest: at least two observations, and their
    source documents must differ. A single document's interval therefore cannot
    be laundered into a "conflict", and the edges are computed rather than
    hand-written so a disputed band cannot be fabricated.
    """

    model_config = ConfigDict(extra="ignore")

    kind: Literal["conflicting_range"] = "conflicting_range"
    observations: list[Observation] = Field(min_length=2)
    bound: Literal["upper", "lower", "value"] = "value"
    unit: str = ""
    note: str = ""
    # Narrow, auditable escape hatch. On a `malfunction` slot a disagreement is
    # sometimes genuinely a headroom choice rather than a functional break — ST
    # publishes both 10 nF (datasheet supply figure) and 100 nF (AN2586) for the
    # same VDDA pin, so blocking the datasheet's own figure would be a false
    # positive. Setting this downgrades ONLY the disputed band, only on a real
    # multi-source conflict, and only with a recorded reason. `burn` slots ignore
    # it entirely (see :func:`evaluate`) — a board-damaging limit is never soft.
    disputed_consequence: Literal["margin"] | None = None
    disputed_reason: str = ""

    @model_validator(mode="after")
    def _override_is_justified(self) -> ConflictingRangeFact:
        if self.disputed_consequence is not None and not self.disputed_reason.strip():
            raise ValueError(
                "softening a disputed band requires disputed_reason explaining why the "
                "disagreement is a headroom choice rather than a functional break"
            )
        return self

    @model_validator(mode="after")
    def _distinct_documents(self) -> ConflictingRangeFact:
        docs = [o.source.doc.strip().lower() for o in self.observations]
        if len(set(docs)) < 2:
            raise ValueError(
                "ConflictingRangeFact needs observations from at least two distinct "
                "source documents; a single source's interval is a RangeFact, and "
                "values that hold under different premises are a ConditionalFact"
            )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def low(self) -> float:
        return min(o.value for o in self.observations)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def high(self) -> float:
        return max(o.value for o in self.observations)

    def bounds(self) -> tuple[Bound, Bound]:
        """Return ``(permissive, strict)`` bounds."""
        match self.bound:
            case "upper":
                return Bound(hi=self.high), Bound(hi=self.low)
            case "lower":
                return Bound(lo=self.low), Bound(lo=self.high)
            case "value":
                return Bound(lo=self.low, hi=self.high), Bound(lo=self.high, hi=self.low)

    def cite_all(self) -> str:
        return "; ".join(f"{o.source.cite()} -> {o.value:g}" for o in self.observations)

    def provenance(self) -> list[Source]:
        return [o.source for o in self.observations]


FactValue = Annotated[
    FixedFact | RangeFact | ConditionalFact | ConflictingRangeFact | QualitativeFact,
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- #
# Structured slot payloads
# --------------------------------------------------------------------------- #


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SupplyRail(_Payload):
    """One supply domain. ``origin`` distinguishes a rail the board must feed
    from one produced by an on-chip regulator (which must *not* be fed)."""

    name: str = Field(min_length=1)
    min_v: float | None = None
    max_v: float | None = None
    typ_v: float | None = None
    origin: Literal["external", "internal_ldo"] = "external"
    min_current_ma: float | None = None
    note: str = ""


class PerPinCap(_Payload):
    """A capacitor tied to a named pin group.

    ``cap`` is a full fact shape rather than a bare float so that a value the
    sources disagree about (ST's VDDA decoupling is 100 nF in AN2586 but 10 nF in
    the datasheet's supply figure) or one that depends on a condition stays
    machine-judgeable by the same :func:`evaluate` path as every other slot.
    """

    pin_group: str = Field(min_length=1)
    cap: FactValue
    count_per_pin: int = 1
    note: str = ""


class DecouplingRule(_Payload):
    """Vendor decoupling requirements.

    ``per_pin`` holds only the capacitors whose **value** a source actually
    states. ``per_supply_pair_required`` carries the frequent case where a
    vendor mandates the *count* ("one capacitor for every VCC/GND pair") without
    naming a capacitance — recording that as a per_pin entry would mean inventing
    the value, and dropping it would lose an enforceable rule. ``bulk`` may be a
    :class:`QualitativeFact` for the same reason: some vendors require bulk
    capacitance without giving a number.
    """

    per_pin: list[PerPinCap] = Field(default_factory=list)
    per_supply_pair_required: bool | None = None
    per_supply_pair_note: str = ""
    bulk: FactValue | None = None
    max_distance_mm: float | None = None
    placement_note: str = ""


class ResetRule(_Payload):
    external_required: bool
    details: str = ""


class StrapPin(_Payload):
    pin: str = Field(min_length=1)
    required_state: str = Field(min_length=1)
    note: str = ""


class MandatoryPeripheral(_Payload):
    kind: str = Field(min_length=1)
    note: str = ""


class PinFact(_Payload):
    """A pin whose function constrains what may be connected to it.

    Only pins that carry a real constraint are declared; everything else is
    ``unspecified`` and the pin-function gate simply does not judge it.
    """

    name: str = Field(min_length=1)
    kind: Literal[
        "power", "ground", "clock", "reset", "boot", "adc", "input_only", "nc", "unspecified"
    ]
    float_allowed: bool | None = None
    note: str = ""


class ClockLayoutRule(_Payload):
    """Oscillator placement constraints.

    Vendors state these in several shapes: a maximum trace length, a *minimum*
    separation from the clock pin (Espressif gives 2.7 mm), a ban on vias in the
    clock trace, an under-crystal routing keepout, or a ground guard. All are
    optional because most datasheets state only some of them.

    ``max_crystal_esr_ohm`` and ``series_resistor_ohm`` are the oscillator's
    demands ON the crystal rather than layout geometry, but they live here
    because that is the section vendors state them in — Raspberry Pi gives both
    in RP2040 §2.3. They are separate FIELDS rather than prose because a number
    buried in ``note`` cannot be compared against a candidate crystal: the
    80 ohm ESR of the most-stocked 12 MHz part only conflicts with the RP2040's
    50 ohm ceiling if something can read the 50.
    """

    max_trace_mm: float | None = None
    min_gap_to_clock_pin_mm: float | None = None
    vias_in_clock_trace_allowed: bool | None = None
    keepout_under: bool | None = None
    ground_guard: bool | None = None
    max_crystal_esr_ohm: float | None = None
    series_resistor_ohm: float | None = None
    note: str = ""


class ThermalPadRule(_Payload):
    """Exposed-pad requirements.

    ``applies_to_packages`` matters because a device is often sold in both a
    leaded package with no pad and a QFN with one; a bare ``required=True``
    would otherwise flag the leaded variant.
    """

    required: bool
    min_vias: int | None = None
    applies_to_packages: list[str] = Field(default_factory=list)
    note: str = ""


class InductorRequirement(_Payload):
    inductance: FactValue
    isat: FactValue | None = None
    note: str = ""


class CapRequirement(_Payload):
    role: str = Field(min_length=1)          # e.g. "bootstrap", "feedforward"
    cap: FactValue
    dielectric: str = ""
    note: str = ""


# --------------------------------------------------------------------------- #
# Three-state slot
# --------------------------------------------------------------------------- #

T = TypeVar("T")


class Slot(BaseModel, Generic[T]):
    """One questionnaire answer, in exactly one of four explicit states."""

    model_config = ConfigDict(extra="ignore")

    status: Status
    value: T | None = None
    reason: str = ""
    source: Source | None = None

    @model_validator(mode="after")
    def _state_is_complete(self) -> Slot[T]:
        if self.status is Status.ASSERTED:
            if self.value is None:
                raise ValueError("an asserted slot must carry a value")
            if not any(src.is_page_level() for src in self.sources()):
                raise ValueError(
                    "an asserted slot needs a source with a section/table/page ref "
                    "(no fact without page-level provenance)"
                )
        elif not self.reason.strip():
            raise ValueError(f"a {self.status} slot must explain itself in 'reason'")
        return self

    def sources(self) -> list[Source]:
        """Every document backing this slot.

        A slot may carry its own ``source`` (structured payloads such as a pin
        table) or delegate to the fact it holds — and a conflict fact carries
        one source *per observation*, which is why this returns a list.
        """
        out: list[Source] = []
        if self.source is not None:
            out.append(self.source)
        inner = getattr(self.value, "provenance", None)
        if callable(inner):
            out.extend(src for src in inner() if isinstance(src, Source))
        return out

    def effective_source(self) -> Source | None:
        """The primary document backing this slot, if any."""
        sources = self.sources()
        return sources[0] if sources else None

    @property
    def asserted(self) -> bool:
        return self.status is Status.ASSERTED


# --------------------------------------------------------------------------- #
# Verdicts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Verdict:
    """The outcome of judging one design value against one slot."""

    slot: str
    ok: bool
    severity: Severity
    message: str
    disputed: bool = False


def evaluate(
    spec: SlotSpec,
    slot: Slot[Any],
    actual: float | None,
    ctx: Mapping[str, object] | None = None,
) -> Verdict | None:
    """Judge ``actual`` against ``slot``, or return ``None`` to fail open.

    ``None`` is returned whenever a verdict would require inventing something:
    a non-asserted slot, a qualitative fact, a slot that is not numerically
    comparable, an undeterminable design value, or a conditional fact whose
    selector is unknown and whose policy is ``skip``.
    """
    if actual is None or not slot.asserted or spec.comparison is Comparison.NONE:
        return None
    fact = slot.value
    ctx = ctx or {}
    cite = _cite(slot)

    match fact:
        case QualitativeFact():
            return None

        case FixedFact() | RangeFact():
            bound = fact.bound(spec.comparison)
            ok = bound.contains(actual)
            return Verdict(
                slot=spec.name,
                ok=ok,
                severity=spec.severity_on_violation(),
                message=_msg(spec, actual, bound, cite, ok),
            )

        case ConditionalFact():
            bound, known = fact.resolve(spec.comparison, ctx)
            if not known and fact.on_unknown == "skip":
                return None
            if not known and spec.comparison is Comparison.EXACT:
                # Every arm disagrees with the design -> wrong under any premise.
                if any(abs(actual - v) <= _EXACT_TOL for v in fact.branch_values()):
                    return None
            ok = bound.contains(actual)
            note = "" if known else f" (selector {fact.selector} undetermined; strictest arm applied)"
            return Verdict(
                slot=spec.name,
                ok=ok,
                severity=spec.severity_on_violation(),
                message=_msg(spec, actual, bound, cite, ok) + note,
            )

        case ConflictingRangeFact():
            permissive, strict = fact.bounds()
            if not permissive.contains(actual):
                return Verdict(
                    slot=spec.name,
                    ok=False,
                    severity=spec.severity_on_violation(),
                    message=(
                        f"{spec.name}: {actual:g}{_unit(spec)} violates every source "
                        f"({fact.cite_all()})"
                    ),
                )
            if strict.contains(actual):
                return Verdict(
                    slot=spec.name, ok=True, severity=Severity.INFO,
                    message=f"{spec.name}: {actual:g}{_unit(spec)} agrees with all sources",
                )
            # Disputed band: sources disagree about this value. Severity comes from
            # the slot's consequence, not from the data shape. A `burn` slot is
            # hard unconditionally; a `malfunction` slot may be softened only by an
            # explicit, reasoned override recorded on the fact itself.
            softened = (
                spec.consequence is Consequence.MALFUNCTION
                and fact.disputed_consequence == "margin"
            )
            hard = spec.consequence is not Consequence.MARGIN and not softened
            if hard:
                broken = "damage the board" if spec.consequence is Consequence.BURN else (
                    "break function"
                )
                advice = f"judged at the strictest edge because a violation here would {broken}"
            elif softened:
                advice = f"treated as headroom: {fact.disputed_reason}"
            else:
                advice = "prefer the more conservative value"
            return Verdict(
                slot=spec.name,
                ok=not hard,
                severity=Severity.ERROR if hard else Severity.WARNING,
                disputed=True,
                message=(
                    f"{spec.name}: {actual:g}{_unit(spec)} falls in a disputed band "
                    f"{strict.describe(spec.unit)} vs {permissive.describe(spec.unit)} — "
                    f"{advice} ({fact.cite_all()})"
                ),
            )

        case _:
            return None


def _unit(spec: SlotSpec) -> str:
    return f" {spec.unit}" if spec.unit else ""


def _cite(slot: Slot[Any]) -> str:
    src = slot.effective_source()
    return src.cite() if src else ""


def _msg(spec: SlotSpec, actual: float, bound: Bound, cite: str, ok: bool) -> str:
    verb = "is within" if ok else "violates"
    tail = f" ({cite})" if cite else ""
    return (
        f"{spec.name}: {actual:g}{_unit(spec)} {verb} the datasheet limit "
        f"{bound.describe(spec.unit)}{tail}"
    )


# --------------------------------------------------------------------------- #
# Slot registry — a slot may not exist without a declared consumer
# --------------------------------------------------------------------------- #


class SlotSpec(BaseModel):
    """What a slot means, how it is judged, and who consumes it."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    comparison: Comparison
    consequence: Consequence
    unit: str = ""
    description: str = ""
    consumers: list[str] = Field(min_length=1)

    def severity_on_violation(self) -> Severity:
        return Severity.WARNING if self.consequence is Consequence.MARGIN else Severity.ERROR


def _spec(
    name: str,
    comparison: Comparison,
    consequence: Consequence,
    consumers: list[str],
    unit: str = "",
    description: str = "",
) -> SlotSpec:
    return SlotSpec(
        name=name,
        comparison=comparison,
        consequence=consequence,
        unit=unit,
        description=description,
        consumers=consumers,
    )


_C = Comparison
_Q = Consequence

SLOT_SPECS: dict[str, SlotSpec] = {
    # -- common to every device class ------------------------------------- #
    "packages": _spec(
        "packages", _C.NONE, _Q.MALFUNCTION,
        ["SelectionStep.mcu_package_identity", "KnowledgeBase.expand_query"],
        description="packages the device is actually sold in",
    ),
    "pin_count": _spec(
        "pin_count", _C.EXACT, _Q.MALFUNCTION,
        ["SelectionStep.mcu_package_identity"], unit="pins",
        description="authoritative pin count, cross-checked against the chosen symbol",
    ),
    # -- MCU ------------------------------------------------------------- #
    "supply_range": _spec(
        "supply_range", _C.WITHIN, _Q.BURN,
        ["SelectionStep.datasheet_limits"], unit="V",
        description="rated operating supply voltage",
    ),
    "supply_rails": _spec(
        "supply_rails", _C.NONE, _Q.BURN,
        ["TopologyStep.rail_feasibility", "SelectionStep.regulator_adequacy"],
        description="supply domains, their origin (external vs on-chip LDO) and current need",
    ),
    "freq_vs_supply": _spec(
        "freq_vs_supply", _C.MAX_ALLOWED, _Q.BURN,
        ["SelectionStep.datasheet_limits"], unit="MHz",
        description="voltage-derated maximum clock (speed grades)",
    ),
    "clock_external": _spec(
        "clock_external", _C.WITHIN, _Q.MALFUNCTION,
        ["SelectionStep.datasheet_limits"], unit="MHz",
        description="constraint on an external crystal / oscillator",
    ),
    "clock_layout": _spec(
        "clock_layout", _C.NONE, _Q.MALFUNCTION,
        ["LayoutCriticalStep.critical_placement"],
        description="oscillator placement and guarding requirements",
    ),
    "decoupling": _spec(
        "decoupling", _C.NONE, _Q.MALFUNCTION,
        ["SchConnectionsStep.design_policy", "LayoutCriticalStep.critical_placement"],
        description="per-supply-pin and bulk decoupling required by the vendor",
    ),
    "reset": _spec(
        "reset", _C.NONE, _Q.MALFUNCTION,
        ["SchConnectionsStep.design_policy"],
        description="whether an external reset circuit is required",
    ),
    "boot_strapping": _spec(
        "boot_strapping", _C.NONE, _Q.MALFUNCTION,
        ["SchConnectionsStep.design_policy", "SchPinMapStep.pin_function"],
        description="boot / strapping pins and the states they must be held in",
    ),
    "mandatory_peripherals": _spec(
        "mandatory_peripherals", _C.NONE, _Q.MALFUNCTION,
        ["SchConnectionsStep.design_policy"],
        description="parts without which the device cannot run (e.g. QSPI flash)",
    ),
    "pins": _spec(
        "pins", _C.NONE, _Q.BURN,
        ["SchPinMapStep.pin_function"],
        description="function-constrained pins (power/ground/reset/boot/input-only)",
    ),
    "thermal_pad": _spec(
        "thermal_pad", _C.NONE, _Q.BURN,
        ["LayoutCriticalStep.critical_placement"],
        description="exposed-pad grounding and via count",
    ),
    # -- regulators (LDO / DC-DC) ---------------------------------------- #
    "vin_range": _spec(
        "vin_range", _C.WITHIN, _Q.BURN,
        ["TopologyStep.rail_feasibility"], unit="V",
        description="recommended operating input voltage range",
    ),
    "abs_max_vin": _spec(
        "abs_max_vin", _C.MAX_ALLOWED, _Q.BURN,
        ["TopologyStep.rail_feasibility"], unit="V",
        description=(
            "absolute-maximum input voltage. Kept separate from vin_range because "
            "some regulators publish only one of the two: the AMS1117 datasheet gives "
            "an absolute maximum of 15 V and no recommended range, while the AP2112 "
            "gives a recommended 2.5-6.0 V range. Without this slot the AMS1117 could "
            "not gate a 24 V input at all"
        ),
    ),
    "vout": _spec(
        "vout", _C.NONE, _Q.MALFUNCTION,
        ["TopologyStep.rail_feasibility"], unit="V",
        description="regulated output voltage (fixed or adjustable range)",
    ),
    "dropout_v": _spec(
        "dropout_v", _C.NONE, _Q.MALFUNCTION,
        ["TopologyStep.rail_feasibility"], unit="V",
        description="dropout voltage — Vin must exceed Vout by at least this",
    ),
    "current_rating_ma": _spec(
        "current_rating_ma", _C.NONE, _Q.BURN,
        ["SelectionStep.regulator_adequacy"], unit="mA",
        description="maximum output current",
    ),
    "required_cin": _spec(
        "required_cin", _C.MIN_REQUIRED, _Q.MARGIN,
        ["SchConnectionsStep.datasheet_connection"], unit="uF",
        description="input capacitor the datasheet requires",
    ),
    "required_cout": _spec(
        "required_cout", _C.MIN_REQUIRED, _Q.MALFUNCTION,
        ["SchConnectionsStep.datasheet_connection"], unit="uF",
        description="output capacitor required for stability",
    ),
    "required_bypass_cap": _spec(
        "required_bypass_cap", _C.NONE, _Q.MALFUNCTION,
        ["SchConnectionsStep.design_policy"], unit="uF",
        description=(
            "reference-bypass / noise-reduction capacitor on a BYP or BYPASS pin. "
            "Not cosmetic: on the MIC5219 the minimum output capacitance depends on "
            "whether this capacitor is fitted (1 uF without it, 2.2 uF with 470 pF), "
            "and the LP2985's 30 uVRMS noise figure is specified with 10 nF here"
        ),
    ),
    "switching_freq_khz": _spec(
        "switching_freq_khz", _C.NONE, _Q.MARGIN,
        ["SelectionStep.regulator_adequacy"], unit="kHz",
        description="nominal switching frequency",
    ),
    "required_inductor": _spec(
        "required_inductor", _C.NONE, _Q.MALFUNCTION,
        ["SchConnectionsStep.design_policy"],
        description="inductance and saturation current for the power stage",
    ),
    "required_caps": _spec(
        "required_caps", _C.NONE, _Q.MALFUNCTION,
        ["SchConnectionsStep.design_policy"],
        description="additional capacitors the converter needs (bootstrap, feedforward)",
    ),
    # -- crystal ---------------------------------------------------------- #
    "freq_mhz": _spec(
        "freq_mhz", _C.EXACT, _Q.MALFUNCTION,
        ["SelectionStep.datasheet_limits"], unit="MHz",
        description="nominal frequency",
    ),
    "load_capacitance_pf": _spec(
        "load_capacitance_pf", _C.NONE, _Q.MALFUNCTION,
        ["SelectionStep.load_cap_calc"], unit="pF",
        description="CL — sets the two load capacitors",
    ),
    "frequency_tolerance_ppm": _spec(
        "frequency_tolerance_ppm", _C.NONE, _Q.MALFUNCTION,
        ["SelectionStep.datasheet_limits"], unit="ppm",
        description=(
            "room-temperature frequency tolerance. Kept separate from stability "
            "because datasheets state them separately and the errors ADD: a part "
            "listed as '+/-10 ppm' with '+/-20 ppm' stability does not meet a "
            "+/-10 ppm system requirement such as the ESP32's. "
            "NONE, not MAX_ALLOWED: this is the crystal's OWN guaranteed bound, so "
            "there is no design value that must stay under it. The real check is "
            "cross-device (tolerance + stability vs the MCU's stated requirement) and "
            "lives in factgate.cross_device_verdicts"
        ),
    ),
    "frequency_stability_ppm": _spec(
        "frequency_stability_ppm", _C.NONE, _Q.MALFUNCTION,
        ["SelectionStep.datasheet_limits"], unit="ppm",
        description=(
            "frequency drift over the operating temperature range. NONE for the same "
            "reason as frequency_tolerance_ppm — it is a part property, and only the "
            "sum of the two can be compared against an MCU requirement"
        ),
    ),
    "esr_max_ohm": _spec(
        "esr_max_ohm", _C.NONE, _Q.MALFUNCTION,
        ["SelectionStep.load_cap_calc"], unit="ohm",
        description=(
            "maximum equivalent series resistance. NONE, not MAX_ALLOWED: 'the "
            "crystal's ESR must not exceed the crystal's ESR' is tautological — the "
            "same fake-gate shape that was removed from capacitance_pf. What must "
            "tolerate this number is the OSCILLATOR, so the check is cross-device "
            "(crystal ESR vs the MCU's stated ESR ceiling, e.g. RP2040's 50 ohm) and "
            "lives in factgate.cross_device_verdicts"
        ),
    ),
    "drive_level_uw": _spec(
        "drive_level_uw", _C.MAX_ALLOWED, _Q.BURN,
        ["SelectionStep.load_cap_calc"], unit="uW",
        description="maximum drive level",
    ),
    # -- TVS / ESD -------------------------------------------------------- #
    "vrwm_v": _spec(
        "vrwm_v", _C.MAX_ALLOWED, _Q.BURN,
        ["SelectionStep.datasheet_limits"], unit="V",
        description=(
            "reverse standoff voltage. Compared as MAX_ALLOWED against the voltage of "
            "the protected rail: the standing voltage must not exceed the standoff, or "
            "the diode conducts continuously. MIN_REQUIRED would have the comparison "
            "backwards"
        ),
    ),
    "clamping_v": _spec(
        "clamping_v", _C.NONE, _Q.BURN,
        ["SelectionStep.datasheet_limits"], unit="V",
        description="clamping voltage at the rated surge — a datum for comparison against "
                    "the protected pin's own absolute maximum",
    ),
    "capacitance_pf": _spec(
        "capacitance_pf", _C.NONE, _Q.MALFUNCTION,
        ["SelectionStep.signal_integrity_guard"], unit="pF",
        description=(
            "junction capacitance — a DATUM, not a threshold. Comparing a part's "
            "capacitance against itself is tautological; the gate must compare this "
            "value against the protected interface's budget (USB 2.0 high speed wants "
            "a few pF, an I2C line tolerates tens). That budget is an interface fact "
            "and does not live in a device sheet, so this slot stays NONE until an "
            "interface questionnaire exists. Recording it still pays: it is what "
            "distinguishes the USBLC6-2 at 3.5 pF max from the PESD5V0L1BA at 75 pF typ"
        ),
    ),
    "channels": _spec(
        "channels", _C.NONE, _Q.MALFUNCTION,
        ["SchConnectionsStep.design_policy"], unit="lines",
        description="number of protected lines",
    ),
    # -- connector -------------------------------------------------------- #
    "cc_pulldown_ohm": _spec(
        "cc_pulldown_ohm", _C.EXACT, _Q.MALFUNCTION,
        ["SchConnectionsStep.datasheet_connection"], unit="ohm",
        description="Rd pulldown on each CC pin for a USB-C sink",
    ),
    "vbus_rating": _spec(
        "vbus_rating", _C.NONE, _Q.BURN,
        ["TopologyStep.rail_feasibility"],
        description="VBUS voltage / current rating of the connector",
    ),
    "shield_grounding": _spec(
        "shield_grounding", _C.NONE, _Q.MALFUNCTION,
        ["SchConnectionsStep.design_policy"],
        description="how the shell / shield must be tied",
    ),
}


COMMON_SLOTS: tuple[str, ...] = ("packages", "pin_count")

QUESTIONNAIRE: dict[DeviceClass, tuple[str, ...]] = {
    DeviceClass.MCU: COMMON_SLOTS + (
        "supply_range", "supply_rails", "freq_vs_supply", "clock_external", "clock_layout",
        "decoupling", "reset", "boot_strapping", "mandatory_peripherals", "pins", "thermal_pad",
    ),
    DeviceClass.LDO: COMMON_SLOTS + (
        "vin_range", "abs_max_vin", "vout", "dropout_v", "current_rating_ma",
        "required_cin", "required_cout", "required_bypass_cap",
    ),
    DeviceClass.DCDC: COMMON_SLOTS + (
        "vin_range", "abs_max_vin", "vout", "current_rating_ma", "switching_freq_khz",
        "required_inductor", "required_caps",
    ),
    DeviceClass.CRYSTAL: COMMON_SLOTS + (
        "freq_mhz", "load_capacitance_pf", "esr_max_ohm", "drive_level_uw",
        "frequency_tolerance_ppm", "frequency_stability_ppm",
    ),
    DeviceClass.TVS: COMMON_SLOTS + (
        "vrwm_v", "clamping_v", "capacitance_pf", "channels",
    ),
    DeviceClass.CONNECTOR: COMMON_SLOTS + (
        "cc_pulldown_ohm", "vbus_rating", "shield_grounding",
    ),
}


def slot_spec(name: str) -> SlotSpec:
    return SLOT_SPECS[name]


def consumer_registry() -> dict[str, list[str]]:
    """Slot name -> the pipeline checks that consume it.

    How to read this, and what changed
    ----------------------------------
    These names began as a design target, and for a long time most of them were
    exactly that: a declaration nothing enforced, which is the same disease this
    module exists to cure one level down. Two mechanisms now hold them to an
    implementation, so the entries divide into three groups.

    **Wired as gates.** ``SelectionStep.datasheet_limits`` and
    ``SchConnectionsStep.datasheet_connection`` are real check names the pipeline
    emits. A slot routed to one of them reaches a verdict.

    **Wired as briefs.** The step PREFIX of every entry is now load-bearing:
    :func:`ratsnestpro.eda.factbrief._derive_step_slots` reads these strings to
    decide which facts are injected into which step's prompt. For the steps in
    ``factbrief.BRIEFED_STEPS`` — ``TopologyStep``, ``SelectionStep``,
    ``SchConnectionsStep`` — adding a consumer here changes what the model is
    shown. This is what makes a ``Comparison.NONE`` slot able to influence a
    design at all: it cannot gate, so being read is the only effect available
    to it.

    **Still aspirational.** ``SchPinMapStep`` and ``LayoutCriticalStep`` name
    steps whose ``propose`` never calls a model (both return ``(artifact,
    False)``), so a brief routed there is read by nobody, and neither has a check
    that consumes facts yet. ``KnowledgeBase.expand_query`` names no pipeline
    step at all. Do not treat those entries as evidence that a slot is consumed.

    The enforceable statements live in tests:

    * ``test_factgate.test_every_comparable_slot_has_an_observer_or_a_stated_reason``
      proves every slot that declares a comparison can reach a verdict.
    * ``test_factbrief_contract.test_every_data_only_slot_is_briefed_or_states_why_not``
      proves every slot that declares NO comparison reaches a prompt, or is
      registered with the reason it cannot.
    * ``test_factbrief_contract.test_unbriefed_slots_are_only_routed_to_unwired_steps``
      proves the registered reasons are true rather than convenient.
    """
    return {name: list(spec.consumers) for name, spec in SLOT_SPECS.items()}



# --------------------------------------------------------------------------- #
# Fact sheets — one model per device class, questionnaire slots are REQUIRED
# --------------------------------------------------------------------------- #
#
# Every questionnaire slot below is a required field with no default. That is
# the enforcement of "no blank cells": a sheet that omits a slot does not parse,
# so an unanswered question cannot reach the gates disguised as "no limit". The
# author must instead say *why* — not_applicable, not_asserted or blocked.


class VbusRating(_Payload):
    voltage_v: float | None = None
    current_a: float | None = None
    note: str = ""


class FactSheetBase(BaseModel):
    """Fields shared by every device class."""

    model_config = ConfigDict(extra="ignore")

    device: str = Field(min_length=1)
    device_class: DeviceClass
    family: str = ""
    aliases: list[str] = Field(default_factory=list)
    # Order-code prefixes this sheet must NOT answer for. A sheet covers one
    # die in one density/revision band, but part numbers of neighbouring bands
    # share its leading characters: "ESP32-S3" starts with "ESP32" yet is a
    # different die in a different package, and "STM32F103RC" starts with
    # "STM32F103" yet is documented in DS5792 rather than this sheet's DS5319.
    # Listing the neighbours here makes the sheet refuse them outright, which is
    # what stops a confident, page-cited verdict from being issued against the
    # wrong document. This is a blocklist and therefore incomplete by
    # construction (next year's ESP32-X9 is not in it); the matcher's exact-key
    # rule is what keeps an unlisted newcomer safe by defaulting to no match.
    excludes: list[str] = Field(default_factory=list)
    source: Source                      # primary document for the sheet
    packages: Slot[list[str]]
    # A fact, not a plain int: a device sold in several packages has a pin count
    # per package, which is a ConditionalFact(selector="package").
    pin_count: Slot[FactValue]

    def match_keys(self) -> list[str]:
        return [self.device, *self.aliases]

    def slot(self, name: str) -> Slot[Any] | None:
        value = getattr(self, name, None)
        return value if isinstance(value, Slot) else None

    def questionnaire(self) -> tuple[str, ...]:
        return QUESTIONNAIRE[DeviceClass(self.device_class)]


class McuFactSheet(FactSheetBase):
    device_class: Literal[DeviceClass.MCU] = DeviceClass.MCU
    supply_range: Slot[FactValue]
    supply_rails: Slot[list[SupplyRail]]
    freq_vs_supply: Slot[FactValue]
    clock_external: Slot[FactValue]
    clock_layout: Slot[ClockLayoutRule]
    decoupling: Slot[DecouplingRule]
    reset: Slot[ResetRule]
    boot_strapping: Slot[list[StrapPin]]
    mandatory_peripherals: Slot[list[MandatoryPeripheral]]
    pins: Slot[list[PinFact]]
    thermal_pad: Slot[ThermalPadRule]


class LdoFactSheet(FactSheetBase):
    device_class: Literal[DeviceClass.LDO] = DeviceClass.LDO
    vin_range: Slot[FactValue]
    abs_max_vin: Slot[FactValue]
    vout: Slot[FactValue]
    dropout_v: Slot[FactValue]
    current_rating_ma: Slot[FactValue]
    required_cin: Slot[FactValue]
    required_cout: Slot[FactValue]
    required_bypass_cap: Slot[FactValue]


class DcdcFactSheet(FactSheetBase):
    device_class: Literal[DeviceClass.DCDC] = DeviceClass.DCDC
    vin_range: Slot[FactValue]
    abs_max_vin: Slot[FactValue]
    vout: Slot[FactValue]
    current_rating_ma: Slot[FactValue]
    switching_freq_khz: Slot[FactValue]
    required_inductor: Slot[InductorRequirement]
    required_caps: Slot[list[CapRequirement]]


class CrystalFactSheet(FactSheetBase):
    device_class: Literal[DeviceClass.CRYSTAL] = DeviceClass.CRYSTAL
    freq_mhz: Slot[FactValue]
    load_capacitance_pf: Slot[FactValue]
    esr_max_ohm: Slot[FactValue]
    drive_level_uw: Slot[FactValue]
    frequency_tolerance_ppm: Slot[FactValue]
    frequency_stability_ppm: Slot[FactValue]


class TvsFactSheet(FactSheetBase):
    device_class: Literal[DeviceClass.TVS] = DeviceClass.TVS
    vrwm_v: Slot[FactValue]
    clamping_v: Slot[FactValue]
    capacitance_pf: Slot[FactValue]
    channels: Slot[FactValue]


class ConnectorFactSheet(FactSheetBase):
    device_class: Literal[DeviceClass.CONNECTOR] = DeviceClass.CONNECTOR
    cc_pulldown_ohm: Slot[FactValue]
    vbus_rating: Slot[VbusRating]
    shield_grounding: Slot[FactValue]


AnyFactSheet = Annotated[
    McuFactSheet
    | LdoFactSheet
    | DcdcFactSheet
    | CrystalFactSheet
    | TvsFactSheet
    | ConnectorFactSheet,
    Field(discriminator="device_class"),
]

_SHEET_MODELS: dict[DeviceClass, type[FactSheetBase]] = {
    DeviceClass.MCU: McuFactSheet,
    DeviceClass.LDO: LdoFactSheet,
    DeviceClass.DCDC: DcdcFactSheet,
    DeviceClass.CRYSTAL: CrystalFactSheet,
    DeviceClass.TVS: TvsFactSheet,
    DeviceClass.CONNECTOR: ConnectorFactSheet,
}


def sheet_model(device_class: DeviceClass) -> type[FactSheetBase]:
    return _SHEET_MODELS[device_class]


# --------------------------------------------------------------------------- #
# Roster — the devices this round is expected to cover
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RosterEntry:
    slug: str                 # data/fact_sheets/<slug>.json
    device: str
    device_class: DeviceClass
    note: str = ""


DEVICE_ROSTER: tuple[RosterEntry, ...] = (
    # MCU — migrated from the retired device_facts JSON and extended to the
    # full questionnaire.
    RosterEntry("atmega328p", "ATmega328P", DeviceClass.MCU),
    RosterEntry("stm32f103", "STM32F103", DeviceClass.MCU),
    RosterEntry("esp32", "ESP32", DeviceClass.MCU),
    RosterEntry("esp32c3", "ESP32-C3", DeviceClass.MCU),
    RosterEntry("rp2040", "RP2040", DeviceClass.MCU),
    # Linear regulators — the parts the corpus already discusses in prose.
    RosterEntry("ams1117_33", "AMS1117-3.3", DeviceClass.LDO),
    RosterEntry("ap2112k_33", "AP2112K-3.3", DeviceClass.LDO),
    RosterEntry("mic5219", "MIC5219", DeviceClass.LDO),
    RosterEntry("lp2985", "LP2985", DeviceClass.LDO),
    # Switching regulators.
    RosterEntry("tps563201", "TPS563201", DeviceClass.DCDC, "step-down"),
    RosterEntry("tps61023", "TPS61023", DeviceClass.DCDC, "step-up"),
    # Protection.
    RosterEntry("usblc6_2", "USBLC6-2", DeviceClass.TVS),
    RosterEntry("pesd5v0l1ba", "PESD5V0L1BA", DeviceClass.TVS),
    # Connector.
    RosterEntry("usb_c_16p", "USB-C 16P", DeviceClass.CONNECTOR,
                "SHOU HAN TYPE-C 16PIN 2MD(073) / LCSC C2765186 — 946k in stock, "
                "the most widely stocked USB-C receptacle in the JLCPCB catalog"),
    # Crystals — chosen from JLCPCB catalog stock, not from recollection. The 12 and
    # 16 MHz parts are JLCPCB Basic (free assembly); 40 MHz has no Basic option.
    RosterEntry("xtal_12mhz_c9002", "X322512MSB4SI", DeviceClass.CRYSTAL,
                "12 MHz, LCSC C9002, Basic, 641k stock — mandatory frequency for RP2040"),
    RosterEntry("xtal_16mhz_c13738", "X322516MLB4SI", DeviceClass.CRYSTAL,
                "16 MHz, LCSC C13738, Basic, 444k stock — ATmega328P at 4.5 V and above"),
    RosterEntry("xtal_40mhz_c284176", "TXM40M0004252HBCEO00T", DeviceClass.CRYSTAL,
                "40 MHz, LCSC C284176, 45k stock — mandatory frequency for ESP32 and "
                "ESP32-C3; chosen over C9010 because its ESR is published"),
)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

_SHEETS_DIR = Path(__file__).resolve().parents[1] / "data" / "fact_sheets"


def _norm(text: str) -> str:
    """Lower-case ASCII alphanumerics only.

    ASCII-restricted rather than ``str.isalnum()`` because that predicate is
    True for CJK characters, so a Chinese requirement would fold its prose into
    the normalized form and change what a key is compared against.
    """
    return "".join(ch for ch in text.lower() if ch.isascii() and ch.isalnum())


# Everything that is NOT part of an identifier. "-", "_" and "." are kept because
# they live *inside* part numbers ("ESP32-S3", "ATmega328P-AU", "AMS1117-3.3"):
# splitting on them would tear "ESP32-S3" into "esp32" + "s3" and hand the bare
# "esp32" straight back to the ESP32 sheet, which is the exact mis-hit this
# matcher exists to prevent. Everything else splits, including CJK text and
# full-width punctuation, so "用 STM32F103C8T6,LQFP48 封装" yields the order code
# as its own identifier.
_IDENT_SPLIT = re.compile(r"[^0-9A-Za-z._-]+")
_IDENT_RUN = re.compile(r"[0-9A-Za-z._-]+")


def _query_forms(text: str) -> tuple[str, frozenset[str]]:
    """The identifier forms a query offers: the whole string and its identifiers.

    Two forms are needed because a device arrives named in two shapes. A KiCad
    lib-id ("MCU_ST_STM32F1:STM32F103C8Tx") is matched as a whole by an alias of
    the same shape, while a value string ("STM32F103C8T6 LQFP48") carries the
    part number as one identifier among several.
    """
    tokens = frozenset(
        form for form in (_norm(part) for part in _IDENT_SPLIT.split(text)) if form
    )
    return _norm(text), tokens


def _match_strength(
    sheet: FactSheetBase, whole: str, tokens: frozenset[str]
) -> int | None:
    """Length of the longest key identifying this sheet, or ``None`` for no match.

    Matching is **exact per identifier**, not substring. The substring rule this
    replaced accepted any query that merely contained a key, so "ESP32-S3"
    normalized to "esp32s3", contained "esp32", and was answered with the
    classic ESP32's sheet — QFN48 against the S3's QFN56, reported as a
    page-cited ERROR that blocked a legal design. Every legitimately covered
    order code is therefore enumerated in ``aliases``; an unlisted one resolves
    to ``None`` and is reported as missing coverage instead of being answered
    from a neighbouring die's datasheet. Defaulting to no-match is the safe
    direction in an open-world catalog, defaulting to a mis-hit is not.

    ``excludes`` is checked per identifier rather than against the whole query,
    because a requirement naming both "ESP32" and "ESP32-C3" must still resolve
    the plain ESP32: a whole-query check would see "esp32c" somewhere in the
    text and veto the sheet for both mentions.
    """
    excluded = [form for form in (_norm(item) for item in sheet.excludes) if form]
    keys = {form for form in (_norm(key) for key in sheet.match_keys()) if form}
    best: int | None = None
    for form in (whole, *tokens):
        if any(form.startswith(prefix) for prefix in excluded):
            continue
        if form in keys and (best is None or len(form) > best):
            best = len(form)
    return best


@lru_cache(maxsize=4)
def _index_for(sheets_dir: str) -> tuple[FactSheetBase, ...]:
    directory = Path(sheets_dir)
    out: list[FactSheetBase] = []
    if directory.is_dir():
        adapter: TypeAdapter[FactSheetBase] = TypeAdapter(AnyFactSheet)
        for jf in sorted(directory.glob("*.json")):
            out.append(adapter.validate_json(jf.read_text(encoding="utf-8")))
    return tuple(out)


def all_fact_sheets(sheets_dir: Path | None = None) -> tuple[FactSheetBase, ...]:
    return _index_for(str(sheets_dir or _SHEETS_DIR))


def fact_sheet(
    name: str,
    device_class: DeviceClass | None = None,
    sheets_dir: Path | None = None,
) -> FactSheetBase | None:
    """Look up a sheet by part number / alias / KiCad lib-id.

    An identifier of the query must equal a key exactly (see
    :func:`_match_strength` for why substring matching was removed). The longest
    matching key wins, which is what keeps "ESP32-C3" from being answered by the
    plain "ESP32" sheet even before its ``excludes`` entry rules that out.
    Returns ``None`` for an unknown device — never a fabricated sheet, and never
    a neighbouring variant's sheet.
    """
    if not name:
        return None
    whole, tokens = _query_forms(name)
    best: tuple[int, FactSheetBase] | None = None
    for sheet in all_fact_sheets(sheets_dir):
        if device_class is not None and DeviceClass(sheet.device_class) is not device_class:
            continue
        strength = _match_strength(sheet, whole, tokens)
        if strength is not None and (best is None or strength > best[0]):
            best = (strength, sheet)
    return best[1] if best else None


def fact_sheets_named(
    text: str, sheets_dir: Path | None = None
) -> tuple[FactSheetBase, ...]:
    """Every sheet named by an identifier in ``text``, in order of appearance.

    The plural of :func:`fact_sheet`. A requirement names several devices at
    once ("an STM32F103C8T6 fed from an AMS1117-3.3 with a 12 MHz crystal"), and
    a step that has not selected parts yet — topology runs before selection —
    has only this text to resolve them from.

    Each identifier is resolved independently and by the same exact-key rule, so
    a mention of "ESP32-C3" contributes the C3 sheet without the plain ESP32
    sheet riding along, and an unlisted variant contributes nothing at all.
    Ordering follows first appearance in the text so the caller's output is
    stable across runs.
    """
    if not text:
        return ()
    sheets = all_fact_sheets(sheets_dir)
    whole = _norm(text)
    first_seen: dict[str, int] = {}
    for match in _IDENT_RUN.finditer(text):
        form = _norm(match.group(0))
        if form and form not in first_seen:
            first_seen[form] = match.start()
    found: dict[str, tuple[int, FactSheetBase]] = {}
    for form, position in first_seen.items():
        singleton = frozenset({form})
        for sheet in sheets:
            if _match_strength(sheet, whole, singleton) is None:
                continue
            previous = found.get(sheet.device)
            if previous is None or position < previous[0]:
                found[sheet.device] = (position, sheet)
    return tuple(
        sheet for _, sheet in sorted(found.values(), key=lambda item: (item[0], item[1].device))
    )


# --------------------------------------------------------------------------- #
# Coverage report — makes gaps visible instead of silent
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SheetCoverage:
    slug: str
    device: str
    device_class: DeviceClass
    present: bool
    statuses: dict[str, Status]      # slot name -> status (empty when absent)
    note: str = ""

    @property
    def asserted(self) -> int:
        return sum(1 for s in self.statuses.values() if s is Status.ASSERTED)

    @property
    def not_applicable(self) -> int:
        return sum(1 for s in self.statuses.values() if s is Status.NOT_APPLICABLE)

    @property
    def answered(self) -> int:
        """Slots with a settled answer — asserted or explicitly inapplicable.

        Kept separate from :attr:`asserted` because a ``not_applicable`` slot is
        just as finished as an asserted one; conflating them would make a device
        that genuinely has no such limit look incomplete.
        """
        return self.asserted + self.not_applicable

    @property
    def gaps(self) -> list[str]:
        """Slots still owed an answer (an explicit, recorded gap)."""
        return sorted(
            name for name, s in self.statuses.items()
            if s in (Status.NOT_ASSERTED, Status.BLOCKED)
        )


def coverage(sheets_dir: Path | None = None) -> list[SheetCoverage]:
    """Per-roster-device slot status. A device with no sheet reports present=False."""
    directory = Path(sheets_dir or _SHEETS_DIR)
    by_slug = {p.stem: p for p in directory.glob("*.json")} if directory.is_dir() else {}
    sheets = {s.device: s for s in all_fact_sheets(sheets_dir)}
    report: list[SheetCoverage] = []
    for entry in DEVICE_ROSTER:
        sheet = sheets.get(entry.device)
        if sheet is None and entry.slug not in by_slug:
            report.append(
                SheetCoverage(entry.slug, entry.device, entry.device_class, False, {}, entry.note)
            )
            continue
        statuses: dict[str, Status] = {}
        if sheet is not None:
            for name in QUESTIONNAIRE[entry.device_class]:
                slot = sheet.slot(name)
                if slot is not None:
                    statuses[name] = slot.status
        report.append(
            SheetCoverage(entry.slug, entry.device, entry.device_class, True, statuses, entry.note)
        )
    return report


def coverage_table(sheets_dir: Path | None = None) -> str:
    """Human-readable matrix for the review batches and the roadmap."""
    rows = coverage(sheets_dir)
    lines = [f"{'device':<16} {'class':<10} {'answered':>9} {'asrt':>5} {'n/a':>4}  gaps"]
    lines.append("-" * 78)
    for row in rows:
        if not row.present:
            state = f"sheet missing{' — ' + row.note if row.note else ''}"
            lines.append(f"{row.device:<16} {row.device_class:<10} {'-':>9} {'-':>5} {'-':>4}  {state}")
            continue
        total = len(QUESTIONNAIRE[row.device_class])
        gaps = ", ".join(row.gaps) or "none"
        lines.append(
            f"{row.device:<16} {row.device_class:<10} "
            f"{f'{row.answered}/{total}':>9} {row.asserted:>5} {row.not_applicable:>4}  {gaps}"
        )
    return "\n".join(lines)


def open_gaps(sheets_dir: Path | None = None) -> list[tuple[str, str, str]]:
    """(device, slot, reason) for every not_asserted / blocked slot.

    This is the next collection round's worklist — the whole point of keeping
    gaps explicit rather than letting them look like "no limit".
    """
    out: list[tuple[str, str, str]] = []
    for sheet in all_fact_sheets(sheets_dir):
        for name in sheet.questionnaire():
            slot = sheet.slot(name)
            if slot is not None and slot.status in (Status.NOT_ASSERTED, Status.BLOCKED):
                out.append((sheet.device, name, slot.reason))
    return out
