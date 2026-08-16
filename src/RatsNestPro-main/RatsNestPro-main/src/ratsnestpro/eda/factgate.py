"""INF1 - the consumption layer that turns fact sheets into verdicts.

:mod:`ratsnestpro.eda.factsheet` answers "what does the datasheet say?" for 17
devices across 37 slots. This module answers the other half - "what does THIS
design actually do?" - and puts the two together.

Why a separate layer exists
---------------------------
A fact alone gates nothing. ``AMS1117 absolute maximum VIN = 15 V`` blocks a
design only once something observes that the design feeds it 24 V. The old
``hardfacts.verify_clock_supply`` hard-coded three such observations for one
device (the MCU) and therefore could never reach the other sixteen sheets. The
mapping from *(device class, slot) -> how to read that value off the design* is
the actual work, and it lives here.

Two kinds of check
------------------
**Slot gates** compare a device fact against an observed design value via
:func:`ratsnestpro.eda.factsheet.evaluate` - supply voltage against the MCU's
rated range, upstream rail against an LDO's absolute maximum, footprint pad
count against the datasheet's contact count.

**Cross-device gates** compare a fact on one sheet against a fact on another,
which no single-slot comparison can express. Three of the sharpest findings in
the fact base have this shape:

* the most-stocked 12 MHz crystal has 80 ohm ESR while the RP2040 - the device
  that *mandates* 12 MHz - specifies a 50 ohm ceiling;
* every stocked 40 MHz crystal carries +/-10 ppm tolerance *plus* +/-20 to
  +/-30 ppm temperature drift, so the sum misses the ESP32's +/-10 ppm
  requirement by three to four times even though the part is advertised as
  "+/-10 ppm";
* a crystal's frequency must match what the MCU mandates, and "mandates" is a
  fixed value on one device (ESP32: exactly 40 MHz) and a range on another
  (STM32 HSE: 4-16 MHz).

None of these is visible from one sheet. All are ordinary comparisons once both
sheets are loaded.

A fourth cross-device shape needs the design's connectivity as well as two
sheets: a device's supply pin must not sit on the net feeding its own
regulator's input. Both operands are datasheet identities, but the violation is
a wire, so :func:`supply_pin_conflicts` takes ``pin_nets`` and ``pins`` and is
called from the connections step rather than from :func:`gate_findings`.

Fail-open discipline
--------------------
Every observer returns ``None`` when it cannot determine a value, and
``evaluate`` yields no verdict for an unknown device or an unanswered slot. An
unrecognised part, a missing artifact, or an unparseable rail name produces
silence rather than a fabricated limit. The gate blocks only what it can prove
from a cited datasheet figure.

Severity follows the slot's declared consequence, not the data shape: ``burn``
and ``malfunction`` block, ``margin`` reports. That mapping is the whole reason
:class:`~ratsnestpro.eda.factsheet.Consequence` exists.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from ratsnestpro.domain.contracts import Severity
from ratsnestpro.eda import footprints
from ratsnestpro.eda.factsheet import (
    QUESTIONNAIRE,
    SLOT_SPECS,
    Comparison,
    Consequence,
    DeviceClass,
    FactSheetBase,
    evaluate,
    fact_sheet,
)
from ratsnestpro.eda.vendor.library import footprint_roots

__all__ = [
    "DesignObservation",
    "GateFinding",
    "CoverageGap",
    "observe",
    "part_name",
    "resolve_sheets",
    "slot_verdicts",
    "cross_device_verdicts",
    "supply_pin_conflicts",
    "crystal_channel_conflicts",
    "coverage_gaps",
    "gate_findings",
]


# --------------------------------------------------------------------------- #
# Part -> sheet resolution (one source of truth)
# --------------------------------------------------------------------------- #


def part_name(part: Any) -> str:
    """The string a part is identified by, value first and symbol as fallback.

    The value carries the order code the design actually buys
    ("STM32F103C8T6"); the symbol carries a library-generic name
    ("MCU_ST_STM32F1:STM32F103C8Tx") that may stand for a whole family. Value
    first therefore resolves to the most specific sheet available.
    """
    return getattr(part, "value", "") or getattr(part, "symbol", "")


def resolve_sheets(parts: list[Any]) -> list[tuple[str, FactSheetBase]]:
    """``(ref, sheet)`` for every part a fact sheet answers for.

    Exported because the gate layer and the prompt layer must never disagree
    about which datasheet applies to a part: two copies of this three-line loop
    is exactly how one of them would keep citing a document the other had
    stopped trusting.
    """
    out: list[tuple[str, FactSheetBase]] = []
    for part in parts:
        sheet = fact_sheet(part_name(part))
        if sheet is not None:
            out.append((getattr(part, "ref", ""), sheet))
    return out


# --------------------------------------------------------------------------- #
# Reading values off the design
# --------------------------------------------------------------------------- #

_FREQ_RE = re.compile(r"(\d+(?:\.\d+)?)\s*M(?:Hz)?\b", re.IGNORECASE)
_VOLT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*V\b", re.IGNORECASE)
_CAP_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(u|\u00b5|n|p)F", re.IGNORECASE)

# Rail names carry their voltage in one of two conventions, and real net names
# wrap them in prefixes and suffixes: this codebase alone uses "3V3", "5V",
# "3.3V", "+3V3", "VCORE_1V8" and "Power_5V". Anchoring on the whole string (the
# first version of this) silently returned None for every decorated name, which
# in a fail-open gate means the check quietly stops existing.
#
# The left-hand guard excludes an alphanumeric character before the digits so a
# PART NUMBER can never be misread as a rail: "PESD5V0L1BA" contains "5V0" but
# it is preceded by "D", so it does not match.
_RAIL_DIGIT_V_DIGIT = re.compile(
    r"(?:^|[^A-Za-z0-9.])(\d{1,3})V(\d{1,2})(?![A-Za-z0-9])", re.IGNORECASE
)
_RAIL_NUMBER_V = re.compile(
    r"(?:^|[^A-Za-z0-9.])(\d{1,3}(?:\.\d{1,2})?)V(?![A-Za-z0-9])", re.IGNORECASE
)

# Role fragments that identify what a part is FOR. Roles are free text written
# by an LLM, so matching is deliberately loose and always lower-cased.
_ROLE_HINTS: dict[str, tuple[str, ...]] = {
    "mcu": ("mcu", "microcontroller", "processor", "soc"),
    "regulator": ("ldo", "regulator", "dc_dc", "dcdc", "buck", "boost", "converter"),
    "crystal": ("crystal", "xtal", "oscillator", "resonator"),
    "tvs": ("tvs", "esd", "protection", "clamp", "suppressor"),
    "connector": ("connector", "usb", "receptacle", "socket", "header"),
}


def _rail_voltage(name: str) -> float | None:
    """Voltage implied by a rail or net NAME. Rails carry only names, not values.

    Handles both conventions and tolerates decoration: ``3V3``, ``1V8``, ``5V``,
    ``3.3V``, ``+3V3``, ``VCORE_1V8``, ``Power_5V``. Returns ``None`` for names
    carrying no voltage (``VBUS``, ``VBAT``, ``GND``) rather than guessing,
    because a fabricated rail voltage would gate against a number nobody stated.

    ``VBUS`` deserves note: it is a real 5 V rail in practice, but its name does
    not say so and its tolerance band is wider than 5.0 nominal. Inferring 5.0
    from the name would understate the worst case, so it abstains instead.
    """
    text = name.strip()
    m = _RAIL_DIGIT_V_DIGIT.search(text)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = _RAIL_NUMBER_V.search(text)
    if m:
        return float(m.group(1))
    return None


def _role_is(part: Any, kind: str) -> bool:
    role = (getattr(part, "role", "") or "").lower()
    return any(h in role for h in _ROLE_HINTS.get(kind, ()))


# Role fragments naming a SUPPORT component rather than a device. Roles are free
# text and are routinely written as "<device>_<function>", so the device's own
# name leaks into the roles of its satellites: "mcu_vdd_decoupling_1" is a
# capacitor, "buck_input_cap" is a capacitor, "crystal_load_cap" is a capacitor.
# Substring role matching classifies all three as the device they serve, which
# would attribute a device questionnaire — and, in _conditional_context, a
# footprint — to a two-terminal passive.
_SUPPORT_ROLE_HINTS: tuple[str, ...] = (
    "decoupling", "cap", "resistor", "pullup", "pulldown", "pull_up", "pull_down",
    "inductor", "choke", "ferrite", "bead", "led", "button", "jumper", "hole",
    "test_point", "testpoint", "divider", "feedback", "bootstrap", "termination",
    "bulk", "diode",
)


def _is_support_part(part: Any) -> bool:
    """True when the role names a passive serving a device, not the device."""
    role = (getattr(part, "role", "") or "").lower()
    return any(hint in role for hint in _SUPPORT_ROLE_HINTS)


def _regulator_output_v(part: Any) -> float | None:
    """Output voltage a regulator part number encodes, e.g. AP2112K-3.3 -> 3.3.

    Requires an explicit decimal point or a ``-3V3`` style suffix, so digits
    inside a part number (``TPS563201``) are never mistaken for a voltage.
    """
    value = getattr(part, "value", "") or ""
    m = re.search(r"-(\d)V(\d)\b", value, re.IGNORECASE)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"-(\d\.\d)\b", value)
    if m:
        return float(m.group(1))
    m = _VOLT_RE.search(value)
    return float(m.group(1)) if m else None


def _capacitance_uf(value: str) -> float | None:
    m = _CAP_RE.search(value or "")
    if not m:
        return None
    scale = {"u": 1.0, "\u00b5": 1.0, "n": 1e-3, "p": 1e-6}[m.group(2).lower()]
    return float(m.group(1)) * scale


def _resistance_ohm(value: str) -> float | None:
    """Resistance from a schematic value string: ``5.1k``, ``5100``, ``5k1``."""
    text = (value or "").strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([kKmM])?\s*(?:ohm|R|\u03a9)?$", text)
    if m:
        mult = {"k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, None: 1.0}[m.group(2)]
        return float(m.group(1)) * mult
    m = re.match(r"^(\d+)[kK](\d+)$", text)     # 5k1 -> 5100
    if m:
        return float(f"{m.group(1)}.{m.group(2)}") * 1e3
    return None


@lru_cache(maxsize=2048)
def _pad_count_in_roots(footprint: str, roots: tuple[str, ...]) -> int | None:
    """Distinct pad numbers for ``footprint``, resolved against ``roots``.

    Counts DISTINCT pad numbers rather than pad objects: a thermal tab shares
    its number with an electrical pad on some footprints, and a connector's
    shield pads are often unnumbered. Returns ``None`` when the footprint is not
    installed, so a missing library never manufactures a mismatch.

    Cached because the underlying lookup is expensive and pure: measured at
    ~98 ms per call, since ``resolve_footprint`` walks the library roots (with a
    ``*.pretty`` glob fallback) and ``load_footprint_node`` re-parses the
    s-expression file, neither of which is memoised. Without this cache the gate
    cost ~270 ms per observation for a four-part design and dominated the whole
    test suite.

    ``roots`` is part of the key, not decoration. A footprint id means nothing on
    its own — it names a file inside whatever ``KICAD_FOOTPRINT_DIR`` and the
    discovered KiCad installs currently point at. Keyed on the id alone, the
    first answer wins for the life of the process, so a library that was
    unreachable when the first caller asked stays ``None`` after it becomes
    reachable, and every pad-count comparison downstream silently fails open.
    The sibling indexes in :mod:`ratsnestpro.eda.grounding` are keyed the same
    way for the same reason.
    """
    del roots  # key only; the resolver reads the live roots itself
    pads = footprints.footprint_pads(footprint)
    if not pads:
        return None
    numbers = {str(p.get("number", "")).strip() for p in pads}
    numbers.discard("")
    numbers.discard("~")
    return len(numbers) or None


def _footprint_pad_count(footprint: str) -> int | None:
    """Electrical terminal count from the real footprint library."""
    if not footprint:
        return None
    return _pad_count_in_roots(
        footprint, tuple(str(r) for r in footprint_roots())
    )


@dataclass(slots=True)
class DesignObservation:
    """What the design actually does, as far as the available artifacts show.

    Every field is optional. A ``None`` or missing entry means "not determinable
    from the artifacts produced so far", which makes the corresponding slot gate
    fail open instead of comparing against a placeholder.
    """

    logic_supply_v: float | None = None
    clock_mhz: float | None = None
    crystal_mhz: float | None = None
    rail_voltages: tuple[float, ...] = ()
    highest_rail_v: float | None = None
    # ref -> observed value, for per-part slots.
    pad_counts: dict[str, int] = field(default_factory=dict)
    upstream_v: dict[str, float] = field(default_factory=dict)
    protected_rail_v: dict[str, float] = field(default_factory=dict)
    cc_pulldown_ohm: dict[str, float] = field(default_factory=dict)
    input_cap_uf: dict[str, float] = field(default_factory=dict)
    output_cap_uf: dict[str, float] = field(default_factory=dict)
    # Design context handed to conditional facts (selectors like `package`).
    context: dict[str, Any] = field(default_factory=dict)


def observe(
    parts: list[Any],
    *,
    rails: list[str] | None = None,
    netlist: Any | None = None,
    requirement_text: str = "",
) -> DesignObservation:
    """Read every gateable value the current artifacts expose.

    ``parts`` is the selection, ``rails`` the topology's rail names, ``netlist``
    the connection intent once it exists. Later artifacts unlock more
    observations - associating a capacitor with a regulator terminal needs the
    netlist - so this is called again with more inputs as the pipeline advances.
    """
    obs = DesignObservation()
    volts = tuple(
        v for v in (_rail_voltage(r) for r in (rails or [])) if v is not None
    )
    obs.rail_voltages = volts
    obs.highest_rail_v = max(volts) if volts else None

    regulator_outputs: list[float] = []
    for part in parts:
        if _role_is(part, "regulator"):
            out_v = _regulator_output_v(part)
            if out_v is not None:
                regulator_outputs.append(out_v)
        if _role_is(part, "crystal"):
            m = _FREQ_RE.search(getattr(part, "value", "") or "")
            if m:
                obs.crystal_mhz = float(m.group(1))

    # The logic supply the MCU is INTENDED to see, read off the regulator's order
    # code. Note what this is not: evidence about where the MCU is wired. It
    # cannot be, at the step that calls this — the netlist does not exist yet, so
    # a part number is the only thing there is to read. ``ratsnest-370639d2``
    # shows the gap: its AMS1117-3.3 makes this 3.3 V and the selection gate
    # passes, while the MCU's VBAT pin actually sat on the regulator's unregulated
    # INPUT net. Once connectivity exists, ``supply_pin_conflicts`` judges the
    # wiring instead of the intent.
    if regulator_outputs:
        obs.logic_supply_v = min(regulator_outputs)
    elif volts:
        obs.logic_supply_v = min(volts)

    # The clock the MCU runs at. A crystal is the only clock source the current
    # artifacts name explicitly; an internal oscillator leaves this None, which
    # is correct - there is then no external clock to gate.
    obs.clock_mhz = obs.crystal_mhz

    for part in parts:
        ref = getattr(part, "ref", "")
        pads = _footprint_pad_count(getattr(part, "footprint", "") or "")
        if pads is not None:
            obs.pad_counts[ref] = pads
        # A regulator's input sees the highest rail on the board: an upstream
        # supply is by definition not below a rail derived from it.
        if _role_is(part, "regulator") and obs.highest_rail_v is not None:
            obs.upstream_v[ref] = obs.highest_rail_v
        # A protection device stands on the rail it protects. Without a netlist
        # the best available assumption is the highest rail, which is also the
        # worst case for a standoff-voltage check.
        if _role_is(part, "tvs") and obs.highest_rail_v is not None:
            obs.protected_rail_v[ref] = obs.highest_rail_v

    if netlist is not None:
        _observe_from_netlist(obs, parts, netlist)

    obs.context.update(_conditional_context(parts, obs, requirement_text))
    return obs


def _observe_from_netlist(
    obs: DesignObservation, parts: list[Any], netlist: Any
) -> None:
    """Refine observations that need real connectivity.

    Before the netlist exists a capacitor cannot be attributed to a specific
    regulator terminal. Once it does, input and output capacitors and the CC
    pulldown resistors become readable - which is what turns ``required_cin``,
    ``required_cout`` and ``cc_pulldown_ohm`` from data into gates.
    """
    by_ref = {getattr(p, "ref", ""): p for p in parts}
    touches: dict[str, set[str]] = {}
    for net in getattr(netlist, "nets", None) or []:
        name = str(getattr(net, "name", "") or "")
        for pin in getattr(net, "pins", None) or []:
            ref = getattr(pin, "ref", "")
            if ref:
                touches.setdefault(ref, set()).add(name)

    for part in parts:
        ref = getattr(part, "ref", "")
        if not _role_is(part, "regulator"):
            continue
        out_v = _regulator_output_v(part)
        reg_nets = touches.get(ref, set())
        out_nets = {
            n for n in reg_nets
            if out_v is not None and _rail_voltage(n) == out_v
        }
        in_nets = {
            n for n in reg_nets
            if _rail_voltage(n) is not None and _rail_voltage(n) != out_v
        }
        # Capacitors in parallel add, and a datasheet minimum is a minimum for
        # the NODE, not for one component.
        c_in = _total_cap_on(by_ref, touches, in_nets)
        c_out = _total_cap_on(by_ref, touches, out_nets)
        if c_in is not None:
            obs.input_cap_uf[ref] = c_in
        if c_out is not None:
            obs.output_cap_uf[ref] = c_out
        upstream = [v for v in (_rail_voltage(n) for n in in_nets) if v is not None]
        if upstream:
            obs.upstream_v[ref] = max(upstream)

    for part in parts:
        ref = getattr(part, "ref", "")
        if not _role_is(part, "connector"):
            continue
        cc_nets = {n for n in touches.get(ref, set()) if "CC" in n.upper()}
        for other_ref, other_nets in touches.items():
            if other_ref == ref or not (other_nets & cc_nets):
                continue
            if not other_ref.upper().startswith("R"):
                continue
            other = by_ref.get(other_ref)
            if other is None:
                continue
            ohms = _resistance_ohm(getattr(other, "value", "") or "")
            if ohms is not None:
                obs.cc_pulldown_ohm[ref] = ohms

    for part in parts:
        ref = getattr(part, "ref", "")
        if not _role_is(part, "tvs"):
            continue
        volts = [
            v for v in (_rail_voltage(n) for n in touches.get(ref, set()))
            if v is not None
        ]
        if volts:
            obs.protected_rail_v[ref] = max(volts)


def _total_cap_on(
    by_ref: dict[str, Any], touches: dict[str, set[str]], nets: set[str]
) -> float | None:
    if not nets:
        return None
    total = 0.0
    found = False
    for ref, ref_nets in touches.items():
        if not (ref_nets & nets) or not ref.upper().startswith("C"):
            continue
        part = by_ref.get(ref)
        if part is None:
            continue
        uf = _capacitance_uf(getattr(part, "value", "") or "")
        if uf is not None:
            total += uf
            found = True
    return total if found else None


def _conditional_context(
    parts: list[Any], obs: DesignObservation, requirement_text: str
) -> dict[str, Any]:
    """Selector values that conditional facts need in order to resolve.

    Missing keys are the normal case and are safe: a conditional fact with an
    unresolvable selector applies its ``on_unknown`` policy, which for every
    burn-class slot in the fact base is ``strictest``.
    """
    ctx: dict[str, Any] = {}
    if obs.logic_supply_v is not None:
        ctx["supply_v"] = obs.logic_supply_v
        ctx["core_supply_v"] = obs.logic_supply_v
    for part in parts:
        # ``_is_support_part`` matters here, not just cosmetically: roles are
        # written "<device>_<function>", so "mcu_vdd_decoupling_1" matches the
        # "mcu" hint. Without the guard the first such capacitor in the list
        # would set ``package`` from its own 0402 footprint, and every
        # ConditionalFact keyed on ``package`` — pin_count, the AVR speed grades
        # — would resolve against a capacitor.
        if _role_is(part, "mcu") and not _is_support_part(part):
            fp = getattr(part, "footprint", "") or ""
            if fp:
                ctx["package"] = fp.split(":")[-1]
            break
    text = (requirement_text or "").lower()
    if "psram" in text or "in-package flash" in text:
        ctx["in_package_flash_or_psram"] = True
    return ctx


# --------------------------------------------------------------------------- #
# Turning facts + observations into findings
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class GateFinding:
    """One provable datasheet violation, with the citation that proves it."""

    ref: str
    device: str
    slot: str
    severity: Severity
    message: str
    citation: str = ""
    # Objects a repair would have to touch, beyond the part itself: net names,
    # a second device. Left empty when the violation is about one part's own
    # parameter, where ``ref`` already says everything.
    targets: tuple[str, ...] = ()

    def as_text(self) -> str:
        cite = f" [{self.citation}]" if self.citation else ""
        return f"{self.ref} ({self.device}) {self.slot}: {self.message}{cite}"

    def all_targets(self) -> tuple[str, ...]:
        """``ref`` first, then whatever else a repair must look at."""
        seen = dict.fromkeys((self.ref, *self.targets))
        return tuple(t for t in seen if t)


def _severity_for(consequence: Consequence) -> Severity:
    """Consequence decides severity - the contract's central rule.

    Slot gates do NOT call this: :func:`ratsnestpro.eda.factsheet.evaluate`
    already stamps each verdict with a severity, and re-deriving it here would
    create a second source of truth that could drift - and would discard the
    softening that a documented source conflict is allowed to apply. This exists
    only for cross-device findings, which produce no ``Verdict``.
    """
    return Severity.WARNING if consequence is Consequence.MARGIN else Severity.ERROR


def _citation(sheet: FactSheetBase, slot_name: str) -> str:
    """Page-level provenance for the specific fact that was violated.

    The legacy gate cited ``facts.source.ref`` - the SHEET's source - for every
    violation regardless of which fact it was, so a speed-grade violation was
    attributed to whatever document the sheet header named. Per-slot sources fix
    that.
    """
    slot = sheet.slot(slot_name)
    src = slot.effective_source() if slot is not None else None
    if src is None:
        src = sheet.source
    return " / ".join(p for p in (src.doc, src.ref) if p)


def _observed_for(
    slot: str, sheet: FactSheetBase, ref: str, obs: DesignObservation
) -> float | int | None:
    """The design value a slot must be compared against, or ``None``.

    This mapping is what makes the fact base consumable. A slot absent from here
    is data-only by design: it reaches the LLM as grounded context but does not
    gate.
    """
    cls = sheet.device_class
    if slot == "pin_count":
        return obs.pad_counts.get(ref)
    if cls is DeviceClass.MCU:
        if slot == "supply_range":
            return obs.logic_supply_v
        if slot == "freq_vs_supply":
            return obs.clock_mhz
        if slot == "clock_external":
            return obs.crystal_mhz
    if cls in (DeviceClass.LDO, DeviceClass.DCDC):
        if slot in ("vin_range", "abs_max_vin"):
            return obs.upstream_v.get(ref)
        if slot == "required_cin":
            return obs.input_cap_uf.get(ref)
        if slot == "required_cout":
            return obs.output_cap_uf.get(ref)
    if cls is DeviceClass.CRYSTAL:
        # freq_mhz is deliberately NOT gated here. A crystal's frequency is a
        # fact, and what it must match is another device's fact (the MCU's
        # requirement), so it is handled in cross_device_verdicts where the
        # message can name both parts. Gating it here would report the same
        # problem twice, with the operands the wrong way round.
        return None
    if cls is DeviceClass.TVS and slot == "vrwm_v":
        return obs.protected_rail_v.get(ref)
    if cls is DeviceClass.CONNECTOR and slot == "cc_pulldown_ohm":
        return obs.cc_pulldown_ohm.get(ref)
    return None


def slot_verdicts(parts: list[Any], obs: DesignObservation) -> list[GateFinding]:
    """Every slot gate, for every part that has a fact sheet.

    This replaces the single-device, three-fact ``verify_clock_supply``: each
    part resolves to its sheet, and each slot in that device class's
    questionnaire is evaluated against the observation. Unknown parts and
    unobservable slots are skipped silently.
    """
    out: list[GateFinding] = []
    for ref, sheet in resolve_sheets(parts):
        for slot_name in QUESTIONNAIRE.get(sheet.device_class, ()):
            spec = SLOT_SPECS.get(slot_name)
            if spec is None or spec.comparison is Comparison.NONE:
                continue
            slot = sheet.slot(slot_name)
            if slot is None:
                continue
            actual = _observed_for(slot_name, sheet, ref, obs)
            if actual is None:
                continue
            verdict = evaluate(spec, slot, float(actual), obs.context)
            if verdict is None or verdict.ok:
                continue
            out.append(GateFinding(
                ref=ref, device=sheet.device, slot=slot_name,
                severity=verdict.severity, message=verdict.message,
                citation=_citation(sheet, slot_name),
            ))
    return out


def cross_device_verdicts(parts: list[Any]) -> list[GateFinding]:
    """Checks comparing a fact on one sheet against a fact on another.

    No single-slot comparison can express these, because both operands are
    datasheet figures rather than design choices.
    """
    out: list[GateFinding] = []
    mcu = _first_sheet(parts, DeviceClass.MCU)
    crystal = _first_sheet(parts, DeviceClass.CRYSTAL)
    if mcu is None or crystal is None:
        return out

    mcu_sheet, _ = mcu
    xtal_sheet, xtal_ref = crystal
    freq = _slot_number(xtal_sheet, "freq_mhz")

    # Frequency: an MCU states it as a fixed requirement (ESP32: exactly
    # 40 MHz) or as an acceptable range (STM32 HSE: 4-16 MHz). Both shapes are
    # real; treating a range as a required value would reject every legal part.
    required, low, high = _clock_requirement(mcu_sheet)
    if freq is not None:
        if required is not None and abs(freq - required) > 0.01:
            out.append(GateFinding(
                ref=xtal_ref, device=xtal_sheet.device, slot="freq_mhz",
                severity=Severity.ERROR,
                message=(
                    f"{mcu_sheet.device} requires a {required:g} MHz crystal but "
                    f"{xtal_sheet.device} is {freq:g} MHz"
                ),
                citation=_citation(mcu_sheet, "clock_external"),
            ))
        elif low is not None and high is not None and not (low <= freq <= high):
            out.append(GateFinding(
                ref=xtal_ref, device=xtal_sheet.device, slot="freq_mhz",
                severity=Severity.ERROR,
                message=(
                    f"{mcu_sheet.device} accepts a {low:g}-{high:g} MHz external "
                    f"crystal but {xtal_sheet.device} is {freq:g} MHz"
                ),
                citation=_citation(mcu_sheet, "clock_external"),
            ))

    # ESR: the ceiling belongs to the oscillator, the value to the crystal.
    esr = _slot_number(xtal_sheet, "esr_max_ohm")
    esr_limit = _mcu_esr_ceiling(mcu_sheet)
    if esr_limit is not None and esr is not None and esr > esr_limit:
        out.append(GateFinding(
            ref=xtal_ref, device=xtal_sheet.device, slot="esr_max_ohm",
            severity=Severity.ERROR,
            message=(
                f"{xtal_sheet.device} ESR is {esr:g} ohm but {mcu_sheet.device} "
                f"specifies a {esr_limit:g} ohm maximum - the oscillator loses "
                f"drive margin and may fail to start"
            ),
            citation=_citation(mcu_sheet, "clock_layout"),
        ))

    # Accuracy: tolerance and drift ADD. A part sold as "+/-10 ppm" carries a
    # separate temperature-stability figure, and only the sum is comparable to a
    # system requirement.
    tol_limit = _clock_tolerance_ppm(mcu_sheet)
    tol = _slot_number(xtal_sheet, "frequency_tolerance_ppm")
    stab = _slot_number(xtal_sheet, "frequency_stability_ppm")
    if tol_limit is not None and tol is not None and stab is not None:
        worst = tol + stab
        if worst > tol_limit:
            out.append(GateFinding(
                ref=xtal_ref, device=xtal_sheet.device,
                slot="frequency_tolerance_ppm", severity=Severity.ERROR,
                message=(
                    f"{mcu_sheet.device} requires +/-{tol_limit:g} ppm but "
                    f"{xtal_sheet.device} is +/-{tol:g} ppm at room temperature "
                    f"PLUS +/-{stab:g} ppm drift = +/-{worst:g} ppm worst case. "
                    f"The part is advertised as +/-{tol:g} ppm; the figures add"
                ),
                citation=_citation(mcu_sheet, "clock_external"),
            ))
    return out


# --------------------------------------------------------------------------- #
# Cross-device gates that need the actual connectivity
# --------------------------------------------------------------------------- #


def _is_ground_pin_name(name: str) -> bool:
    """Whether a symbol's own pin NAME denotes ground.

    Matched on the pin name, not on the net name, because the electrical type
    cannot separate the two: a linear regulator's ground pin is ``power_in``
    exactly like its supply input, and an MCU's ``VSS`` / ``VSSA`` are too.
    Without this every LDO would appear to have two input nets and ground would
    be treated as a supply rail.
    """
    upper = re.sub(r"[^A-Z0-9]", "", (name or "").upper())
    return "GND" in upper or "VSS" in upper or upper == "VEE"


def _supply_pins_of(
    ref: str,
    pins: Mapping[str, Sequence[Mapping[str, object]]],
    pin_nets: Mapping[tuple[str, str], str],
    *,
    kind: str,
) -> dict[str, str]:
    """``pin number -> net`` for each wired, non-ground pin of one electrical type.

    Unwired pins are omitted rather than mapped to a placeholder: a pin with no
    net makes no claim about connectivity, and inventing one would turn silence
    into evidence.
    """
    out: dict[str, str] = {}
    for pin in pins.get(ref) or ():
        if str(pin.get("type", "")).strip().lower() != kind:
            continue
        if _is_ground_pin_name(str(pin.get("name", ""))):
            continue
        number = str(pin.get("number", "")).strip()
        net = pin_nets.get((ref, number))
        if number and net:
            out[number] = net
    return out


def _range_max(sheet: FactSheetBase, slot_name: str) -> float | None:
    slot = sheet.slot(slot_name)
    value = slot.value if slot is not None else None
    high = getattr(value, "max", None) if value is not None else None
    return float(high) if isinstance(high, (int, float)) else None


def supply_pin_conflicts(
    parts: list[Any],
    *,
    pin_nets: Mapping[tuple[str, str], str],
    pins: Mapping[str, Sequence[Mapping[str, object]]],
) -> list[GateFinding]:
    """A device's supply pin must not sit on the net that feeds its own regulator.

    The defect this exists for
    --------------------------
    In ``ratsnest-370639d2`` the STM32F103's ``VBAT`` (pin 1, ``power_in``)
    landed on ``/REG_IN`` together with the AMS1117's ``VI`` (pin 3), while the
    regulator's ``VO`` fed ``/VDD33`` where the same MCU's ``VDD`` pins sit.
    ``/REG_IN`` comes from USB ``VBUS`` through a ferrite bead, so the part rated
    to 3.6 V was wired to the unregulated input. KiCad ERC cannot see this - no
    net is shorted to another - and the selection step could not either, because
    it reads the supply voltage off the part number.

    Why this is topological rather than a voltage comparison
    -------------------------------------------------------
    ``observe`` used to answer "what supply does the MCU see?" with
    ``min(regulator_outputs)`` - the regulator's *stated output*, inferred from
    its order code, never from where the MCU is actually wired. That answer is
    right for a correct board and silent on exactly the board that is wrong.

    Reading the net's voltage instead is not available: ``/REG_IN`` carries no
    voltage in its name, and the upstream ``/VBUS`` only carries one if USB VBUS
    is assumed to be 5 V - which USB-C PD makes false, in the direction that
    makes the defect worse. So the verdict rests on topology, where the evidence
    actually is: the regulator's input side is by construction not at the
    regulated output voltage, and every pin gated by one ``supply_range`` slot
    must sit at one voltage. A device cannot straddle both sides of its own
    regulator whatever the input voltage turns out to be.

    Three conditions, each one closing a false positive
    ---------------------------------------------------
    1. The regulator must have exactly one distinct ``power_out`` net. A buck
       converter's symbol has no ``power_out`` pin at all - its output is
       downstream of the inductor, and ``SW`` is typed ``output`` - so switchers
       yield no verdict here. That is the correct fail-open outcome rather than
       a guess at which pin is the output.
    2. That output net must carry another supply pin of the same device, which
       is what establishes "this regulator feeds this device". Without it the
       check would fire on any unrelated rail the device happens to touch.
    3. Nets driven by *any* regulator's output are excluded. A device with two
       supply domains (3.3 V logic plus a core rail from a second regulator)
       legitimately has pins on the 3.3 V net that is also the core regulator's
       input; condition 3 is what keeps that design silent.

    Fail-open, as everywhere in this module: a part with no fact sheet is not
    judged, on either side. Both identities come from
    :func:`resolve_sheets` - never from ``role``, which is free text a model
    wrote and which reads "regulator" on a part that is not one.
    """
    out: list[GateFinding] = []
    sheets = resolve_sheets(parts)
    regulators = [
        (ref, sheet) for ref, sheet in sheets
        if sheet.device_class in (DeviceClass.LDO, DeviceClass.DCDC)
    ]
    if not regulators:
        return out

    driven_nets = {
        net
        for ref, _sheet in regulators
        for net in _supply_pins_of(ref, pins, pin_nets, kind="power_out").values()
    }
    spec = SLOT_SPECS.get("supply_range")
    severity = _severity_for(
        spec.consequence if spec is not None else Consequence.BURN
    )

    for dev_ref, dev_sheet in sheets:
        if dev_sheet.device_class is not DeviceClass.MCU:
            continue
        supply = _supply_pins_of(dev_ref, pins, pin_nets, kind="power_in")
        if not supply:
            continue
        supply_nets = set(supply.values())
        pin_name = {
            str(p.get("number", "")).strip(): str(p.get("name", ""))
            for p in pins.get(dev_ref) or ()
        }
        for reg_ref, reg_sheet in regulators:
            outputs = set(
                _supply_pins_of(reg_ref, pins, pin_nets, kind="power_out").values()
            )
            if len(outputs) != 1:
                continue
            output_net = next(iter(outputs))
            if output_net not in supply_nets:
                continue
            input_nets = set(
                _supply_pins_of(reg_ref, pins, pin_nets, kind="power_in").values()
            ) - driven_nets
            offenders = sorted(
                (number, net)
                for number, net in supply.items()
                if net in input_nets
            )
            if not offenders:
                continue
            listed = ", ".join(
                f"{dev_ref}:{number} ({pin_name.get(number) or '?'})"
                for number, _net in offenders
            )
            on_nets = ", ".join(sorted({net for _number, net in offenders}))
            limit = _range_max(dev_sheet, "supply_range")
            rated = (
                f" {dev_sheet.device} is rated to {limit:g} V maximum."
                if limit is not None
                else ""
            )
            out.append(GateFinding(
                ref=dev_ref,
                device=dev_sheet.device,
                slot="supply_range",
                severity=severity,
                message=(
                    f"{listed} sits on {on_nets}, which feeds the INPUT of "
                    f"{reg_ref} ({reg_sheet.device}) - the same regulator whose "
                    f"output {output_net} supplies this device's other supply "
                    f"pins. The input side is by construction not at the "
                    f"regulated output voltage.{rated} Move the pin to "
                    f"{output_net}"
                ),
                citation=_citation(dev_sheet, "supply_range"),
                targets=(
                    reg_ref,
                    *sorted({net for _number, net in offenders}),
                    output_net,
                ),
            ))
    return out


# Alternate-function names that mark an oscillator channel. The high-speed and
# low-speed channels are different pins with different rated frequency ranges,
# and the default pin name says nothing about either: an STM32's LSE pins are
# named ``PC14`` / ``PC15`` and its HSE pins ``PD0`` / ``PD1``.
_HSE_ALTERNATES = ("RCC_OSC_IN", "RCC_OSC_OUT", "OSC_IN", "OSC_OUT")
_LSE_ALTERNATES = ("RCC_OSC32_IN", "RCC_OSC32_OUT", "OSC32_IN", "OSC32_OUT")


def _channel_pin_numbers(part: Any, wanted: Sequence[str]) -> list[str]:
    """Pin numbers offering one of ``wanted`` alternates, wired or not.

    Which pins *are* the channel is a static fact about the symbol. The repair
    hint needs it precisely when those pins are unconnected, which is the whole
    shape of the defect, so this must not filter on connectivity.
    """
    from ratsnestpro.eda import symbols

    targets = {name.upper() for name in wanted}
    out: list[str] = []
    for pin in symbols.symbol_pins(str(getattr(part, "symbol", ""))) or []:
        alternates = pin.get("alternates") or ()
        if not isinstance(alternates, (tuple, list)):
            continue
        if not ({str(a).upper() for a in alternates} & targets):
            continue
        number = str(pin.get("number", "")).strip()
        if number:
            out.append(number)
    return out


def _channel_pins(
    part: Any,
    pin_nets: Mapping[tuple[str, str], str],
    wanted: Sequence[str],
) -> dict[str, str]:
    """``pin number -> net`` for wired pins offering one of ``wanted`` alternates.

    Read from the symbol library, not from the caller's pin table. That table
    comes from the exported netlist's ``libparts`` section, which lists a pin's
    number, name and electrical type but not its alternate functions -- and the
    alternate name is the only place the oscillator channel is stated. The
    dependency is therefore essential here rather than a convenience, which is
    the opposite of the bridged-capacitor correction, where the library lookup
    was removed precisely because it was avoidable.

    Returns nothing when the library is unavailable or declares no alternates,
    which is the fail-open outcome: many older libraries declare none.

    Exact match on the alternate name, not a substring: ``RCC_OSC32_IN``
    contains ``OSC_IN`` and so would match a loose test for the high-speed
    channel, which is the exact confusion this check exists to catch.
    """
    from ratsnestpro.eda import symbols

    ref = str(getattr(part, "ref", ""))
    lib_pins = symbols.symbol_pins(str(getattr(part, "symbol", ""))) or []
    targets = {name.upper() for name in wanted}
    out: dict[str, str] = {}
    for pin in lib_pins:
        alternates = pin.get("alternates") or ()
        if not isinstance(alternates, (tuple, list)):
            continue
        if not ({str(a).upper() for a in alternates} & targets):
            continue
        number = str(pin.get("number", "")).strip()
        net = pin_nets.get((ref, number))
        if number and net:
            out[number] = net
    return out


def crystal_channel_conflicts(
    parts: list[Any],
    *,
    pin_nets: Mapping[tuple[str, str], str],
) -> list[GateFinding]:
    """A crystal must sit on the oscillator channel rated for its frequency.

    The defect this exists for
    --------------------------
    In ``ratsnest-370639d2`` an 8 MHz crystal was wired to the STM32F103's pins
    3 and 4. Those are ``PC14`` / ``PC15``, whose alternates are
    ``RCC_OSC32_IN`` / ``RCC_OSC32_OUT`` -- the 32.768 kHz low-speed channel. The
    high-speed channel, pins 5 and 6 (``RCC_OSC_IN`` / ``RCC_OSC_OUT``), was
    never connected. The nets were even named ``HSE_OSC_IN`` / ``HSE_OSC_OUT``:
    the intent was right and the pins were wrong, which is why no name-based
    check could see it. ``crystal_two_distinct_signal_nets`` passes too -- the two
    terminals do land on two distinct non-power nets.

    Where each fact comes from
    --------------------------
    The channel is read from the symbol library's alternate-function names, which
    is the only place it is stated; the frequency comes from the crystal's value;
    the acceptable range comes from the MCU's ``clock_external`` fact sheet slot.
    None of the three is a model's opinion.

    Fail-open, deliberately, in three ways: a symbol declaring no alternates
    yields no verdict (many older libraries declare none), an MCU with no fact
    sheet yields no verdict, and a crystal whose value carries no parseable
    frequency yields no verdict.
    """
    out: list[GateFinding] = []
    spec = SLOT_SPECS.get("clock_external")
    severity = _severity_for(
        spec.consequence if spec is not None else Consequence.MALFUNCTION
    )
    by_ref = {getattr(p, "ref", ""): p for p in parts}
    # ref -> nets, for finding a two-terminal part bridging two oscillator pins
    nets_of: dict[str, set[str]] = {}
    for (ref, _number), net in pin_nets.items():
        nets_of.setdefault(ref, set()).add(net)

    for mcu_ref, mcu_sheet in resolve_sheets(parts):
        if mcu_sheet.device_class is not DeviceClass.MCU:
            continue
        required, low, high = _clock_requirement(mcu_sheet)
        if required is None and low is None:
            continue
        mcu_part = by_ref.get(mcu_ref)
        if mcu_part is None:
            continue
        hse_numbers = _channel_pin_numbers(mcu_part, _HSE_ALTERNATES)
        lse = _channel_pins(mcu_part, pin_nets, _LSE_ALTERNATES)
        if not hse_numbers and not lse:
            continue
        lse_nets = set(lse.values())
        if not lse_nets:
            continue
        for ref, nets in nets_of.items():
            if ref == mcu_ref or len(nets) != 2 or not nets <= lse_nets:
                continue
            part = by_ref.get(ref)
            if part is None:
                continue
            match = _FREQ_RE.search(str(getattr(part, "value", "") or ""))
            if match is None:
                continue
            freq = float(match.group(1))
            # A megahertz-range crystal on the low-speed channel. The low-speed
            # channel is specified for a 32.768 kHz tuning-fork crystal; the
            # oscillator will not start, and the MCU falls back to its internal
            # RC clock if it runs at all.
            wanted = (
                f"{required:g} MHz" if required is not None
                else f"{low:g}-{high:g} MHz" if low is not None and high is not None
                else "its rated range"
            )
            channel = ", ".join(
                f"{mcu_ref}:{number}" for number in sorted(lse)
            )
            hse_hint = (
                f" The high-speed channel is "
                f"{', '.join(f'{mcu_ref}:{n}' for n in sorted(hse_numbers))}."
                if hse_numbers
                else ""
            )
            out.append(GateFinding(
                ref=ref,
                device=mcu_sheet.device,
                slot="clock_external",
                severity=severity,
                message=(
                    f"{ref} is {freq:g} MHz and sits on {channel}, which the "
                    f"symbol declares as the 32.768 kHz low-speed oscillator "
                    f"channel ({'/'.join(_LSE_ALTERNATES[:2])}). "
                    f"{mcu_sheet.device} expects {wanted} on its high-speed "
                    f"channel.{hse_hint} Naming the nets after HSE does not move "
                    f"them"
                ),
                citation=_citation(mcu_sheet, "clock_external"),
                targets=(mcu_ref, *sorted(nets)),
            ))
    return out


def _clock_requirement(
    sheet: FactSheetBase,
) -> tuple[float | None, float | None, float | None]:
    """``(required, min, max)`` for a device's external clock.

    A fixed fact means one frequency is mandated (ESP32 firmware supports only
    40 MHz); a range fact means any frequency inside it is legal (STM32 HSE).
    At most one of the two forms is populated.
    """
    slot = sheet.slot("clock_external")
    value = slot.value if slot is not None else None
    if value is None:
        return None, None, None
    kind = getattr(value, "kind", "")
    if kind == "fixed":
        v = getattr(value, "value", None)
        return (float(v) if isinstance(v, (int, float)) else None), None, None
    if kind == "range":
        lo, hi = getattr(value, "min", None), getattr(value, "max", None)
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
            return None, float(lo), float(hi)
    return None, None, None


def _clock_tolerance_ppm(sheet: FactSheetBase) -> float | None:
    slot = sheet.slot("clock_external")
    value = slot.value if slot is not None else None
    ppm = getattr(value, "tolerance_ppm", None) if value is not None else None
    return float(ppm) if isinstance(ppm, (int, float)) else None


def _mcu_esr_ceiling(sheet: FactSheetBase) -> float | None:
    slot = sheet.slot("clock_layout")
    value = slot.value if slot is not None else None
    esr = getattr(value, "max_crystal_esr_ohm", None) if value is not None else None
    return float(esr) if isinstance(esr, (int, float)) else None


def _slot_number(sheet: FactSheetBase, slot_name: str) -> float | None:
    slot = sheet.slot(slot_name)
    if slot is None or slot.value is None:
        return None
    value = getattr(slot.value, "value", None)
    return float(value) if isinstance(value, (int, float)) else None


def _first_sheet(
    parts: list[Any], cls: DeviceClass
) -> tuple[FactSheetBase, str] | None:
    for ref, sheet in resolve_sheets(parts):
        if sheet.device_class is cls:
            return sheet, ref
    return None


# --------------------------------------------------------------------------- #
# Coverage — the silence at the sheet BOUNDARY
# --------------------------------------------------------------------------- #
#
# :mod:`ratsnestpro.eda.factsheet` spends a four-state ``Status`` enum making
# sure a blank cell INSIDE a sheet cannot masquerade as "no limit". That
# discipline stops at the sheet boundary: :func:`slot_verdicts` skips a part with
# no sheet at all, and a skipped part looks exactly like a clean one. Failing
# open there is correct and deliberate — this project selects parts open-world,
# so blocking every device outside the 17-sheet roster would break it, and
# inventing a limit is worse than not having one. What was missing is the
# ANNOUNCEMENT: nothing told anyone that a burn-class check had not run.


@dataclass(slots=True)
class CoverageGap:
    """A selected part that no fact sheet answers for.

    Not a violation — an absence of evidence. Carried separately from
    :class:`GateFinding` (whose docstring promises "one provable datasheet
    violation") so the two can never be confused in a report.
    """

    ref: str
    name: str
    device_class: DeviceClass
    unchecked_gates: tuple[str, ...]

    @property
    def severe_gates(self) -> tuple[str, ...]:
        """Unchecked gates whose violation damages or disables the part."""
        return tuple(
            slot for slot in self.unchecked_gates
            if (spec := SLOT_SPECS.get(slot)) is not None
            and spec.consequence is not Consequence.MARGIN
        )

    def as_text(self) -> str:
        severe = self.severe_gates
        return (
            f"{self.ref} ({self.name}) has no datasheet fact sheet, so none of the "
            f"{len(self.unchecked_gates)} datasheet limits for a "
            f"{self.device_class} was checked — including {len(severe)} whose "
            f"violation damages or disables the part ({', '.join(severe)}). "
            f"Absence of a finding here is absence of evidence, not proof the "
            f"design is within limits."
        )


# Role fragments that pin a regulator to one of the two regulator questionnaires.
# The two overlap on vin_range / abs_max_vin / vout / current_rating_ma, so a
# wrong guess between them barely moves the count; guessing at all is only to
# name the class in the message.
_SWITCHING_HINTS = ("dc_dc", "dcdc", "buck", "boost", "converter", "switching")

# A connector is only worth reporting when its bus imposes electrical
# requirements this questionnaire models (Rd pulldowns, VBUS rating, shield
# bonding). A 2.54 mm breakout header matches the "header" role hint but has no
# such limits, and flagging one on every board would bury the MCU and regulator
# gaps that matter.
_GATED_CONNECTOR_HINTS = ("usb", "type-c", "typec", "type_c", "receptacle")


def _infer_device_class(part: Any) -> DeviceClass | None:
    """The questionnaire a part would answer, guessed from its free-text role.

    Returns ``None`` when the role names nothing the fact base models (a
    resistor, an LED, a mounting hole), which is how the report stays about
    parts whose datasheet limits could actually have blocked the board.
    """
    role = (getattr(part, "role", "") or "").lower()
    if _is_support_part(part):
        return None
    if _role_is(part, "mcu"):
        return DeviceClass.MCU
    if _role_is(part, "regulator"):
        return (
            DeviceClass.DCDC
            if any(hint in role for hint in _SWITCHING_HINTS)
            else DeviceClass.LDO
        )
    if _role_is(part, "crystal"):
        return DeviceClass.CRYSTAL
    if _role_is(part, "tvs"):
        return DeviceClass.TVS
    if _role_is(part, "connector") and any(
        hint in role for hint in _GATED_CONNECTOR_HINTS
    ):
        return DeviceClass.CONNECTOR
    return None


def coverage_gaps(parts: list[Any]) -> list[CoverageGap]:
    """Selected parts whose datasheet limits could not be checked at all.

    Fails open exactly as before — this reports the gap, it does not block on
    it. The count is of GATEABLE slots (``comparison != NONE``) in that device
    class's questionnaire: those are the checks a sheet would have made
    reachable. Data-only slots are excluded because they never produced a
    verdict even for a covered part.
    """
    out: list[CoverageGap] = []
    for part in parts:
        name = part_name(part)
        if fact_sheet(name) is not None:
            continue
        device_class = _infer_device_class(part)
        if device_class is None:
            continue
        gates = tuple(
            slot for slot in QUESTIONNAIRE.get(device_class, ())
            if (spec := SLOT_SPECS.get(slot)) is not None
            and spec.comparison is not Comparison.NONE
        )
        if not gates:
            continue
        out.append(CoverageGap(
            ref=getattr(part, "ref", ""),
            name=name or "unnamed part",
            device_class=device_class,
            unchecked_gates=gates,
        ))
    return out


def gate_findings(
    parts: list[Any],
    *,
    rails: list[str] | None = None,
    netlist: Any | None = None,
    requirement_text: str = "",
) -> list[GateFinding]:
    """All datasheet findings for a design - the single entry point for steps.

    Call it with whatever artifacts exist: at selection time ``rails`` alone
    already gates supply, clock and regulator input voltages; once the netlist
    exists the capacitor and CC-pulldown gates begin firing too.
    """
    obs = observe(
        parts, rails=rails, netlist=netlist, requirement_text=requirement_text
    )
    return slot_verdicts(parts, obs) + cross_device_verdicts(parts)
