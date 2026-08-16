"""INF1 — reading the numbers a USER asked for, and arbitrating them.

The gap this fills
------------------
:mod:`ratsnestpro.eda.factgate` reads design values off ARTIFACTS — the selected
parts, the topology's rails, the netlist. By the time those exist, a requirement
that says "power the STM32F103 from 5 V" has already been turned into a rail list
and several LLM decisions. The gate does eventually catch it, at selection time,
as one ``datasheet_limits`` failure among many.

That is late and it is silent about intent. The user asked for something the
datasheet forbids, and the right response is not to quietly re-plan around it —
it is to say which figure is being violated, on which page, and let the user
decide. This module reads the request itself.

What it does NOT do
-------------------
It never resolves the conflict on its own. ``arbitrate`` produces verdicts;
whether a verdict blocks, warns, or is waived is decided by the acknowledgement
mechanism and enforced by ``RequirementsStep.check``. And a user's number is
never written back into ``data/fact_sheets`` — a fact sheet records what a
document says, and no amount of user insistence changes that.

Extraction discipline
---------------------
A false positive here is expensive: it interrupts the user to argue about a
number they never gave. Two guards do most of the work.

**Masking.** Part numbers are full of digits and voltage-like fragments —
"AMS1117-3.3", "PESD5V0L1BA", "ESP32-C3", "LQFP48", "USB2.0", "C2765186". Every
identifier that mixes letters and digits without being a plain quantity is blanked
out before any number is read, so the "3.3" in a regulator's order code can never
be mistaken for a requested rail.

**Context.** A bare number with a unit is not a claim about anything. "5 V" earns
a ``supply_range`` claim only when a supply word sits near it, and a frequency
earns a clock claim only near a clock word. Negations ("not 5 V", "instead of
16 MHz", "不要 5V") suppress the match entirely.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

from pydantic import BaseModel, ConfigDict

from ratsnestpro.domain.contracts import Severity
from ratsnestpro.eda.factsheet import (
    QUESTIONNAIRE,
    SLOT_SPECS,
    Comparison,
    Consequence,
    DeviceClass,
    FactSheetBase,
    evaluate,
    fact_sheets_named,
)

__all__ = [
    "ACK_PREFIX",
    "EXPERIENCE_SYSTEM",
    "Arbitration",
    "ClaimVerdict",
    "ExperienceOpinion",
    "UserClaim",
    "ack_token",
    "arbitrate",
    "experience_prompt",
    "extract_claims",
    "judge_by_experience",
    "parse_acks",
]


# --------------------------------------------------------------------------- #
# The claim
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UserClaim:
    """One numeric value the requirement asks for, and where it came from.

    ``slots`` is a tuple because a single request is often a statement about more
    than one datasheet figure: "24 V input" has to clear both the regulator's
    recommended ``vin_range`` and its ``abs_max_vin``, and those are different
    slots with different consequences.
    """

    value: float
    unit: str
    slots: tuple[str, ...]
    quote: str
    device_hint: str = ""

    def describe(self) -> str:
        where = f" (near {self.device_hint})" if self.device_hint else ""
        return f'{self.value:g} {self.unit} from "{self.quote}"{where}'


# --------------------------------------------------------------------------- #
# Masking part numbers before any number is read
# --------------------------------------------------------------------------- #

_IDENTIFIER = re.compile(r"[0-9A-Za-z._-]+")

# Shapes that are a QUANTITY rather than a part number, and so must survive
# masking. "3V3" is included because it is how a rail is written in half this
# codebase's net names.
_QUANTITY_SHAPES = (
    re.compile(r"^\d+(?:\.\d+)?(?:v|mhz|khz|hz|uf|nf|pf|ohm|r|k|m)$", re.IGNORECASE),
    re.compile(r"^\d{1,3}v\d{1,2}$", re.IGNORECASE),      # 3V3, 1V8
    re.compile(r"^\d+(?:\.\d+)?$"),                        # a bare number
)


def _is_quantity(token: str) -> bool:
    return any(shape.match(token) for shape in _QUANTITY_SHAPES)


def _mask_part_numbers(text: str) -> str:
    """Blank every identifier that mixes letters and digits without being a unit.

    Replaced with spaces rather than deleted so character offsets stay aligned
    with the original text — the quote shown to the user must be the words they
    actually wrote, not a rewritten version.
    """
    out = list(text)
    for match in _IDENTIFIER.finditer(text):
        token = match.group(0)
        if _is_quantity(token):
            continue
        has_letter = any(ch.isalpha() for ch in token)
        has_digit = any(ch.isdigit() for ch in token)
        if has_letter and has_digit:
            for index in range(match.start(), match.end()):
                out[index] = " "
    return "".join(out)


def _mask_acknowledgements(text: str) -> str:
    """Blank ``ACK-RISK:`` spans before any claim is read.

    An acknowledgement is metadata about a previous conversation, not a design
    statement, and reading it as one produces a specific wrong answer: the token
    "ACK-RISK: vin_range=5" contains "vin", so a nearby "5 V" was attributed to a
    regulator's INPUT limits instead of the MCU's supply range — and the MCU was
    then never checked at all. Spaces rather than deletion, to keep offsets.
    """
    out = list(text)
    for match in _ACK.finditer(text):
        for index in range(match.start(), match.end()):
            out[index] = " "
    return "".join(out)


# --------------------------------------------------------------------------- #
# Quantities
# --------------------------------------------------------------------------- #

# A number immediately followed by a unit. The left guard rejects a digit or
# letter directly before the number so a masked-but-adjacent fragment cannot
# bleed in; the right guard rejects a trailing alphanumeric so "5VDC" and
# "16MHzX" are not read as bare quantities.
_VOLTAGE = re.compile(
    r"(?<![0-9A-Za-z.])(\d{1,3}(?:\.\d{1,2})?)\s*[Vv](?![0-9A-Za-z])"
)
_VOLTAGE_SPLIT = re.compile(
    r"(?<![0-9A-Za-z.])(\d{1,3})[Vv](\d{1,2})(?![0-9A-Za-z])"
)
_FREQ_MHZ = re.compile(
    r"(?<![0-9A-Za-z.])(\d{1,4}(?:\.\d{1,3})?)\s*M(?:Hz)?(?![0-9A-Za-z])"
)
_CAP = re.compile(
    r"(?<![0-9A-Za-z.])(\d{1,4}(?:\.\d{1,3})?)\s*([unpµ])F(?![0-9A-Za-z])",
    re.IGNORECASE,
)
_RES = re.compile(
    r"(?<![0-9A-Za-z.])(\d{1,4}(?:\.\d{1,3})?)\s*"
    r"(?:([kKmM])(?:ohm|Ω|R)?|ohm|Ω)(?![0-9A-Za-z])"
)

_CAP_SCALE = {"u": 1.0, "µ": 1.0, "n": 1e-3, "p": 1e-6}


# --------------------------------------------------------------------------- #
# Context — what a number is a claim ABOUT
# --------------------------------------------------------------------------- #

# How far either side of a number a context word may sit. One clause, roughly.
_CONTEXT_WINDOW = 42

_NEGATIONS = (
    "not ", "no ", "without", "instead of", "rather than", "avoid", "never",
    "don't", "do not", "unless",
    "不", "非", "无需", "不要", "不用", "避免", "而不是", "禁止",
)


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Match a context word without matching inside a longer one.

    Learned from a real false positive: the resistance family listed "rd " to
    catch USB-C's Rd pulldown, and "boa**rd** " matched it, so "supply the board
    with 5 somethings" produced a 5 ohm CC-pulldown claim. ASCII keywords
    therefore get alphanumeric boundaries. CJK keywords are matched as plain
    substrings because Chinese has no word boundary to anchor on, and the CJK
    keywords used here are specific enough not to need one.
    """
    body = re.escape(keyword.strip())
    if keyword.isascii():
        return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])")
    return re.compile(body)


@dataclass(frozen=True)
class _Family:
    """One kind of claim: how to find its number and what makes it that claim."""

    name: str
    slots: tuple[str, ...]
    unit: str
    keywords: tuple[str, ...]
    exclusions: tuple[str, ...] = field(default=())

    def patterns(self) -> list[re.Pattern[str]]:
        return [_keyword_pattern(keyword) for keyword in self.keywords]


_VOLTAGE_FAMILIES: tuple[_Family, ...] = (
    _Family(
        name="regulator_input",
        # A stated input voltage is a claim against BOTH the recommended range
        # and the absolute maximum. They differ in consequence and in which
        # sheets publish them (the AMS1117 has only the latter), so a claim that
        # named just one would silently pass the part that documents the other.
        #
        # Keywords are deliberately narrow. An earlier version included "from a",
        # which made "power the STM32F103 from a 5 V rail" read as a REGULATOR
        # INPUT claim and never checked the MCU's supply range at all.
        slots=("vin_range", "abs_max_vin"),
        unit="V",
        keywords=(
            "input", "vin", "upstream", "barrel", "adapter", "wall wart",
            "输入", "接入", "外部电源",
        ),
    ),
    _Family(
        name="logic_supply",
        slots=("supply_range",),
        unit="V",
        keywords=(
            "supply", "supplies", "power", "powered", "rail", "vcc", "vdd",
            "vddio", "logic", "run at", "operate at", "core",
            "供电", "电源", "供给", "工作电压", "驱动",
        ),
    ),
    _Family(
        name="protected_rail",
        slots=("vrwm_v",),
        unit="V",
        keywords=("standoff", "clamp", "tvs", "esd", "钳位", "保护"),
    ),
)

_FREQ_FAMILY = _Family(
    name="external_clock",
    # freq_mhz is the crystal's own fact and clock_external is the MCU's
    # requirement; a requested frequency is a claim against both sides.
    slots=("clock_external", "freq_mhz"),
    unit="MHz",
    keywords=(
        "crystal", "xtal", "oscillator", "resonator", "hse", "clock", "mclk",
        "晶振", "晶体", "时钟", "振荡",
    ),
)

_CAP_FAMILIES: tuple[_Family, ...] = (
    _Family(
        name="input_cap",
        slots=("required_cin",),
        unit="uF",
        keywords=("input cap", "input capacitor", "cin", "输入电容"),
    ),
    _Family(
        name="output_cap",
        slots=("required_cout",),
        unit="uF",
        keywords=("output cap", "output capacitor", "cout", "输出电容"),
    ),
)

_RES_FAMILY = _Family(
    name="cc_pulldown",
    slots=("cc_pulldown_ohm",),
    unit="ohm",
    keywords=("cc1", "cc2", "cc", "pulldown", "pull-down", "rd", "下拉"),
)


def _is_negated(text: str, start: int) -> bool:
    """True when a negation sits close before the number.

    Deliberately narrow: only the text preceding the value, and only within one
    clause. "Use 3.3 V, not 5 V" must suppress the 5 V and keep the 3.3 V.
    """
    prefix = text[max(0, start - 28) : start].lower()
    return any(marker in prefix for marker in _NEGATIONS)


def _quote(text: str, start: int, end: int) -> str:
    left = max(0, start - 24)
    right = min(len(text), end + 24)
    return " ".join(text[left:right].split())


def _match_family(
    families: tuple[_Family, ...], text: str, start: int, end: int
) -> _Family | None:
    """The family whose context word sits CLOSEST to the number.

    Distance rather than declaration order, because a sentence often contains
    words belonging to several families and the nearest one is the one the number
    is about: in "12 V input feeds the 3.3 V supply rail" the first number is
    next to "input" and the second next to "supply", and an order-based rule
    would attribute both to whichever family happened to be declared first.
    """
    left = max(0, start - _CONTEXT_WINDOW)
    right = min(len(text), end + _CONTEXT_WINDOW)
    segment = text[left:right].lower()
    best: tuple[int, _Family] | None = None
    for family in families:
        if any(bad in segment for bad in family.exclusions):
            continue
        for pattern in family.patterns():
            for hit in pattern.finditer(segment):
                position = left + hit.start()
                distance = start - position if position < start else position - end
                if best is None or distance < best[0]:
                    best = (max(distance, 0), family)
    return best[1] if best else None


# Where one statement ends and the next begins. A voltage belongs to the device
# named in ITS clause, not to whichever device happens to be closest in
# characters: in "Feed the AMS1117-3.3 from 24 V and power the STM32F103C8T6 from
# 5 V" the STM32 is physically nearer to "24 V" than the AMS1117 is, so a
# distance rule attributes the 24 V to the MCU and never checks the regulator's
# absolute maximum — the single most damaging thing this module exists to catch.
#
# The lookarounds matter: a naive "[.,;]" split cuts "AMS1117-3.3" in half at its
# decimal point, and the resulting clause contains no device name at all — which
# silently restores the very failure the clause rule was added to fix.
_CLAUSE_SPLIT = re.compile(
    r"(?<!\d)[;,](?!\d)"        # punctuation, but not a thousands/decimal comma
    r"|\.(?=\s|$)"              # a sentence period, not a decimal point
    r"|\n"
    r"|\sand\s|\sthen\s|\swith\s"
    r"|[、，；。]"
)

# Which voltage family a device of each class implies. A number in a clause about
# a regulator is a claim about its INPUT; one in a clause about an MCU is a claim
# about its supply range.
_CLASS_VOLTAGE_FAMILY: dict[DeviceClass, str] = {
    DeviceClass.LDO: "regulator_input",
    DeviceClass.DCDC: "regulator_input",
    DeviceClass.MCU: "logic_supply",
    DeviceClass.TVS: "protected_rail",
}

# Words that mean a voltage near a regulator is its OUTPUT, not its input.
_OUTPUT_WORDS = ("output", "vout", "downstream", "输出")


def _clause(text: str, start: int, end: int) -> tuple[int, int]:
    boundaries = [0] + [m.end() for m in _CLAUSE_SPLIT.finditer(text)] + [len(text)]
    left = max((b for b in boundaries if b <= start), default=0)
    right = min((b for b in boundaries if b >= end), default=len(text))
    return left, right


def _clause_family(text: str, start: int, end: int) -> _Family | None:
    """The voltage family implied by the device named in this clause."""
    left, right = _clause(text, start, end)
    clause = text[left:right]
    if any(word in clause.lower() for word in _OUTPUT_WORDS):
        return None
    wanted: str | None = None
    for sheet in fact_sheets_named(clause):
        candidate = _CLASS_VOLTAGE_FAMILY.get(DeviceClass(sheet.device_class))
        if candidate is None:
            continue
        if wanted is not None and wanted != candidate:
            # Two device classes in one clause: too ambiguous to route by device,
            # so fall back to the wording.
            return None
        wanted = candidate
    return next(
        (f for f in _VOLTAGE_FAMILIES if f.name == wanted), None
    ) if wanted else None


def _nearest_device(text: str, position: int) -> str:
    """The device name closest to a claim, for the message only.

    Arbitration resolves the device by CLASS (a ``supply_range`` claim is about
    the MCU whichever words surround it), so this is presentation, not logic —
    which is why a wrong guess here cannot produce a wrong verdict.
    """
    best: tuple[int, str] | None = None
    for sheet in fact_sheets_named(text):
        for key in sheet.match_keys():
            index = text.lower().find(key.lower())
            if index < 0:
                continue
            distance = abs(index - position)
            if best is None or distance < best[0]:
                best = (distance, sheet.device)
    return best[1] if best else ""


def extract_claims(text: str) -> list[UserClaim]:
    """Numeric design values the requirement asks for, in order of appearance.

    Returns ``[]`` for text that states no number this fact base can judge —
    which is the common case and is not a problem: a requirement that pins no
    values has nothing to conflict with.
    """
    if not text or not text.strip():
        return []
    masked = _mask_part_numbers(_mask_acknowledgements(text))
    claims: list[UserClaim] = []
    seen: set[tuple[str, float, int]] = set()

    def add(
        family: _Family, value: float, start: int, end: int
    ) -> None:
        key = (family.name, round(value, 4), start)
        if key in seen:
            return
        seen.add(key)
        claims.append(UserClaim(
            value=value,
            unit=family.unit,
            slots=family.slots,
            quote=_quote(text, start, end),
            device_hint=_nearest_device(text, start),
        ))

    def scan(
        pattern: re.Pattern[str],
        families: tuple[_Family, ...],
        read: object,
        *,
        route_by_device: bool = False,
    ) -> None:
        for match in pattern.finditer(masked):
            if _is_negated(masked, match.start()):
                continue
            family = None
            if route_by_device:
                # The device named in the clause is checked FIRST because it is
                # the more reliable signal: "feed", "from" and "power" are used
                # for both a regulator's input and an MCU's supply, but a clause
                # naming an LDO is unambiguously about that LDO.
                family = _clause_family(text, match.start(), match.end())
            if family is None:
                family = _match_family(
                    families, masked, match.start(), match.end()
                )
            if family is None:
                continue
            value = read(match)  # type: ignore[operator]
            if value is None:
                continue
            add(family, value, match.start(), match.end())

    scan(_VOLTAGE_SPLIT, _VOLTAGE_FAMILIES,
         lambda m: float(f"{m.group(1)}.{m.group(2)}"), route_by_device=True)
    scan(_VOLTAGE, _VOLTAGE_FAMILIES, lambda m: float(m.group(1)),
         route_by_device=True)
    scan(_FREQ_MHZ, (_FREQ_FAMILY,), lambda m: float(m.group(1)))
    scan(_CAP, _CAP_FAMILIES,
         lambda m: float(m.group(1)) * _CAP_SCALE[m.group(2).lower()])
    scan(_RES, (_RES_FAMILY,), _read_resistance)

    claims.sort(key=lambda claim: text.find(claim.quote.split()[0]) if claim.quote else 0)
    return claims


def _read_resistance(match: re.Match[str]) -> float:
    multiplier = {None: 1.0, "": 1.0, "k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6}
    return float(match.group(1)) * multiplier[match.group(2)]


# --------------------------------------------------------------------------- #
# Acknowledgement — scoped to one slot at one value
# --------------------------------------------------------------------------- #

ACK_PREFIX = "ACK-RISK:"

_ACK = re.compile(
    rf"{re.escape(ACK_PREFIX)}\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _canonical(value: float) -> str:
    """A value's token form: ``5``, ``5.0`` and ``5.00`` agree, ``5.5`` differs.

    Without normalisation an acknowledgement would be defeated by formatting, and
    with too MUCH normalisation it would leak: rounding to whole volts would let
    an ack for 5 V waive a later 5.4 V. Three decimals is finer than any figure
    in the fact base and coarser than float noise.
    """
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def ack_token(slot: str, value: float) -> str:
    """The exact string a user must supply to accept ONE risk.

    Scoped to ``slot`` and ``value`` on purpose. A blanket "I accept the risks"
    would waive limits the user never saw, including ones introduced by a later
    edit; changing the number invalidates the token and the question is asked
    again. This is the mechanism behind the rule that an acknowledgement is never
    a global switch.
    """
    return f"{slot}={_canonical(value)}"


def parse_acks(text: str) -> frozenset[str]:
    """Tokens acknowledged in the requirement text.

    Machine-readable rather than free prose because the consequence of
    misreading is asymmetric: mistaking "I understand the 5 V risk is real, use
    3.3 V" for an acceptance would silently ship the damaging design. Turning a
    user's sentence into a token is a separate, supervised step at the agent
    layer; this function only recognises the token itself.
    """
    return frozenset(
        ack_token(match.group(1).lower(), float(match.group(2)))
        for match in _ACK.finditer(text or "")
    )


# --------------------------------------------------------------------------- #
# Arbitration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ClaimVerdict:
    """What the datasheets say about one requested value.

    ``tier`` records HOW it was judged, and the report must keep them apart:

    * ``hard`` — compared against an asserted slot by
      :func:`ratsnestpro.eda.factsheet.evaluate`, with a page-level citation.
    * ``no_fact`` — no asserted, comparable slot exists for this claim. Fails
      open; a candidate for the experience check.
    * ``advisory`` — judged against engineering experience rather than a
      document. Never an ERROR, and never written back to a fact sheet.
    """

    claim: UserClaim
    slot: str
    tier: str
    ok: bool
    severity: Severity
    message: str
    device: str = ""
    citation: str = ""
    ack_token: str = ""
    acknowledged: bool = False
    advisory_range: str = ""
    advisory_sources: tuple[str, ...] = ()

    @property
    def needs_ack(self) -> bool:
        """True when the user has to decide before the design can proceed."""
        return not self.ok and not self.acknowledged and bool(self.ack_token)

    @property
    def limit_text(self) -> str:
        return self.advisory_range or self.citation


@dataclass(frozen=True)
class Arbitration:
    verdicts: tuple[ClaimVerdict, ...] = ()

    @property
    def blocking(self) -> tuple[ClaimVerdict, ...]:
        return tuple(v for v in self.verdicts if v.needs_ack)

    @property
    def accepted(self) -> tuple[ClaimVerdict, ...]:
        """Conflicts the user acknowledged — the audit trail, not a silence."""
        return tuple(v for v in self.verdicts if not v.ok and v.acknowledged)

    @property
    def unresolved(self) -> tuple[ClaimVerdict, ...]:
        """Claims no document can judge; the experience check's input."""
        return tuple(v for v in self.verdicts if v.tier == "no_fact")


def _classes_for_slot(slot: str) -> tuple[DeviceClass, ...]:
    """Device classes whose questionnaire contains this slot.

    Read from ``QUESTIONNAIRE`` rather than hard-coded so a slot added to a class
    is routed without a second edit here. This is also why arbitration resolves
    the device by CLASS and not by proximity in the text: "5 V" is a claim about
    the MCU's supply range regardless of which words happen to surround it.
    """
    return tuple(
        device_class
        for device_class, slots in QUESTIONNAIRE.items()
        if slot in slots
    )


def _consequence_phrase(consequence: Consequence) -> str:
    match consequence:
        case Consequence.BURN:
            return "exceeding it can DAMAGE the device or the board"
        case Consequence.MALFUNCTION:
            return "violating it will prevent the design from working"
        case _:
            return "it reduces design margin"


def arbitrate(
    claims: Sequence[UserClaim],
    sheets: Sequence[FactSheetBase],
    *,
    acks: frozenset[str] = frozenset(),
) -> Arbitration:
    """Judge each requested value against the datasheets, or record that it cannot be.

    Produces verdicts only. Whether a verdict blocks is decided by
    ``RequirementsStep.check``, and whether it is waived is decided by the user's
    acknowledgement — kept separate so this function stays a pure comparison and
    the policy lives where it can be seen.
    """
    verdicts: list[ClaimVerdict] = []
    for claim in claims:
        for slot_name in claim.slots:
            verdicts.append(_judge(claim, slot_name, sheets, acks))
    return Arbitration(verdicts=tuple(verdicts))


def _judge(
    claim: UserClaim,
    slot_name: str,
    sheets: Sequence[FactSheetBase],
    acks: frozenset[str],
) -> ClaimVerdict:
    spec = SLOT_SPECS.get(slot_name)
    token = ack_token(slot_name, claim.value)
    acknowledged = token in acks
    wanted = _classes_for_slot(slot_name)
    sheet = next(
        (s for s in sheets if DeviceClass(s.device_class) in wanted), None
    )
    slot = sheet.slot(slot_name) if sheet is not None else None

    if (
        spec is None
        or sheet is None
        or slot is None
        or not slot.asserted
        or spec.comparison is Comparison.NONE
    ):
        why = (
            "no part of the required class is named"
            if sheet is None
            else f"{sheet.device} does not state this figure"
        )
        return ClaimVerdict(
            claim=claim,
            slot=slot_name,
            tier="no_fact",
            ok=True,
            severity=Severity.INFO,
            message=(
                f"{slot_name}: {claim.value:g} {claim.unit} requested "
                f'("{claim.quote}") — {why}, so no datasheet limit applies. '
                f"This is missing evidence, not approval."
            ),
            device=sheet.device if sheet is not None else "",
            ack_token="",
        )

    verdict = evaluate(spec, slot, claim.value, {})
    citation = _slot_citation(sheet, slot_name)
    if verdict is None or verdict.ok:
        return ClaimVerdict(
            claim=claim,
            slot=slot_name,
            tier="hard",
            ok=True,
            severity=Severity.INFO,
            message=(
                f"{slot_name}: {claim.value:g} {claim.unit} is within "
                f"{sheet.device}'s datasheet limit"
            ),
            device=sheet.device,
            citation=citation,
            ack_token="",
        )

    return ClaimVerdict(
        claim=claim,
        slot=slot_name,
        tier="hard",
        ok=False,
        severity=verdict.severity,
        message=(
            f"{slot_name}: you asked for {claim.value:g} {claim.unit} "
            f'("{claim.quote}") but {sheet.device} {verdict.message} — '
            f"{_consequence_phrase(spec.consequence)}."
        ),
        device=sheet.device,
        citation=citation,
        ack_token=token,
        acknowledged=acknowledged,
    )


def _slot_citation(sheet: FactSheetBase, slot_name: str) -> str:
    slot = sheet.slot(slot_name)
    source = slot.effective_source() if slot is not None else None
    if source is None:
        source = sheet.source
    return " / ".join(part for part in (source.doc, source.ref) if part)


# --------------------------------------------------------------------------- #
# Tier 2 — experience, for values no document can judge
# --------------------------------------------------------------------------- #


class ExperienceOpinion(BaseModel):
    """A model's read on whether a value is ordinary engineering practice.

    Deliberately NOT a fact. It carries no page reference because there is no
    page: it is the soft tier of the two-tier knowledge stance
    (:mod:`ratsnestpro.knowledge.store`), and the rules in
    :func:`judge_by_experience` keep it from ever hardening into a block.
    """

    model_config = ConfigDict(extra="ignore")

    within_norm: bool
    typical_range: str = ""
    reason: str = ""


EXPERIENCE_SYSTEM = (
    "You judge whether ONE requested value is within normal engineering practice "
    "for the stated purpose. You are NOT quoting a datasheet — no datasheet "
    "figure exists for this value, which is why you were asked. Reply with a "
    "single minified JSON object: within_norm (bool), typical_range (short "
    "string, e.g. '1-10 uF'), reason (under 200 characters). Say within_norm "
    "false only when the value is outside what a competent engineer would "
    "choose, not merely unusual. If you cannot tell, say within_norm true and "
    "explain the uncertainty in reason - a guess presented as a limit is worse "
    "than no opinion."
)


def experience_prompt(verdict: ClaimVerdict, knowledge: str = "") -> str:
    """The question put to the model for one unjudgeable value."""
    spec = SLOT_SPECS.get(verdict.slot)
    purpose = spec.description if spec is not None else verdict.slot
    device = f" on a {verdict.device}" if verdict.device else ""
    body = (
        f"Requested value: {verdict.claim.value:g} {verdict.claim.unit}\n"
        f"What it is for: {purpose}{device}\n"
        f'The user wrote: "{verdict.claim.quote}"\n'
        f"No datasheet in the fact base states a limit for this."
    )
    return f"{body}\n\nRelevant design practice:\n{knowledge}" if knowledge else body


# ``None`` means "no opinion available" and MUST fail open.
ExperienceAsk = Callable[[ClaimVerdict], ExperienceOpinion | None]


def judge_by_experience(
    arbitration: Arbitration,
    *,
    ask: ExperienceAsk,
    acks: frozenset[str] = frozenset(),
    corpus_ids: Sequence[str] = (),
) -> Arbitration:
    """Re-judge the ``no_fact`` verdicts against experience, and nothing else.

    Three outcomes, and the third is the one that matters most:

    * within normal practice — adopted silently, but RECORDED with the range the
      model gave and the corpus documents it drew on. Silent must not mean
      untraceable: if a value later turns out to have been wrong, the record is
      how anyone finds out what was assumed.
    * outside normal practice — reported as a WARNING that can be acknowledged.
    * no opinion (the model is unavailable, errored, or declined) — FAILS OPEN.
      Offline operation is a supported mode of this project, and blocking here
      would make it unusable over what is, by construction, a soft judgement.

    An advisory verdict is never an ERROR. It has no page-level provenance, so it
    cannot carry the authority that blocking a design requires.
    """
    rejudged: list[ClaimVerdict] = []
    for verdict in arbitration.verdicts:
        if verdict.tier != "no_fact":
            rejudged.append(verdict)
            continue
        opinion = _safe_ask(ask, verdict)
        if opinion is None:
            rejudged.append(replace(
                verdict,
                message=(
                    f"{verdict.message} No experience check was available, so "
                    f"this value was neither confirmed nor questioned."
                ),
            ))
            continue
        rejudged.append(_from_opinion(verdict, opinion, acks, corpus_ids))
    return Arbitration(verdicts=tuple(rejudged))


def _safe_ask(ask: ExperienceAsk, verdict: ClaimVerdict) -> ExperienceOpinion | None:
    """Any failure is "no opinion". A broken advisor must not stop a design."""
    try:
        return ask(verdict)
    except Exception:  # noqa: BLE001 - advisory boundary, deliberately total
        return None


def _from_opinion(
    verdict: ClaimVerdict,
    opinion: ExperienceOpinion,
    acks: frozenset[str],
    corpus_ids: Sequence[str],
) -> ClaimVerdict:
    basis = f" (design practice: {', '.join(corpus_ids)})" if corpus_ids else ""
    shown = f" Typical: {opinion.typical_range}." if opinion.typical_range else ""
    reason = f" {opinion.reason}" if opinion.reason else ""
    if opinion.within_norm:
        return replace(
            verdict,
            tier="advisory",
            ok=True,
            severity=Severity.INFO,
            message=(
                f"{verdict.slot}: {verdict.claim.value:g} {verdict.claim.unit} has "
                f"no datasheet limit and is within normal practice, so it was "
                f"adopted as asked.{shown}{reason}{basis}"
            ),
            advisory_range=opinion.typical_range,
            advisory_sources=tuple(corpus_ids),
            ack_token="",
        )
    token = ack_token(verdict.slot, verdict.claim.value)
    return replace(
        verdict,
        tier="advisory",
        ok=False,
        # WARNING and never ERROR: an opinion without a page reference has no
        # standing to block a board.
        severity=Severity.WARNING,
        message=(
            f"{verdict.slot}: {verdict.claim.value:g} {verdict.claim.unit} is "
            f"outside normal engineering practice. No datasheet states a limit, "
            f"so this is EXPERIENCE, not a manual figure.{shown}{reason}{basis}"
        ),
        advisory_range=opinion.typical_range,
        advisory_sources=tuple(corpus_ids),
        ack_token=token,
        acknowledged=token in acks,
    )
