"""Open-world hardware capability resolution for RatsNestPro.

Replaces the brand-prefix MCU whitelist (``STM32|RP\\d{4}|ESP32|ATMEGA|...``),
which silently missed whole families such as ATSAME/SAME, LPC, EFM32, GD32,
MSP430, Renesas RA, PSoC and PIC32. Part identity is decided by three grounded
sources instead of a fixed enumeration:

1. the user's own constraint clause ("主控必须是 X", "the MCU must be X"),
2. the installed KiCad symbol libraries (exact name, family, or order-code
   wildcard), and
3. the local catalogue (:class:`ratsnestpro.parts.PartSelector`).

Resolution is deliberately separate from *availability*: a part the user pinned
still resolves even when no symbol exists for it, so the Symbol Acquisition
ladder can report an honest ``symbol_unavailable`` instead of quietly
substituting a near-neighbour with a different pin count.

See ``docs/Intent_Routing_and_AHE_EHE.md`` sections 2.3 and 4.4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agents.ratsnestpro.intent import is_negated_mention

SubstitutionPolicy = Literal["forbidden", "family_equivalent", "allowed"]
ResolutionSource = Literal[
    "symbol_exact",
    "symbol_wildcard",
    "symbol_family",
    "catalog",
    "user_constraint",
]

# A manufacturer order code: starts with a letter, mixes letters and digits, and
# may carry hyphenated suffixes (ATSAME54P20A-AU, STM32F405RGT6, W25Q128JVSIQ).
_PART_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*(?:-[A-Za-z0-9]+)*\b")

# Structured "key: value" run/project names must not be mined for part numbers;
# their slugs look exactly like order codes (same54-industrial-edge-gateway).
_STRUCTURED_NAME_RE = re.compile(
    r"\b(?:run_name|project_name|report_name|out|output)\b\s*[:=：]?\s*[\r\n]*\s*"
    r"[\"']?[A-Za-z0-9_.-]+[\"']?",
    re.IGNORECASE,
)
# Evidence appended by the runtime is not user intent (docs note in the SAME54
# case: GROUNDED ARCHITECT EVIDENCE must never redefine the fixed MCU).
_RUNTIME_EVIDENCE_MARKERS = (
    "GROUNDED ARCHITECT EVIDENCE",
    "INDEPENDENT REVIEW FEEDBACK",
)

# Package/footprint codes, interface instances, and quantities are not parts.
_PACKAGE_CODE_RE = re.compile(
    r"^(?:[A-Z]*(?:QFP|QFN|BGA|SOIC|SSOP|TSSOP|MSOP|VSSOP|DFN|SOT|SOD|SOP|TO|"
    r"WLCSP|LGA|PLCC|DIP|SIP)[A-Z]*-?\d+(?:-\d+)?|\d{4}|\d{3,4}METRIC)$",
    re.IGNORECASE,
)
_INTERFACE_INSTANCE_RE = re.compile(
    r"^(?:USB[0-9.]*|SPI\d*|I2C\d*|I2S\d*|UART\d*|USART\d*|CAN\d*|CANFD|SDIO\d*|"
    r"SDMMC\d*|QSPI\d*|SWD|JTAG|RMII\d*|MII|RGMII|MDIO|MDC|ADC\d*|DAC\d*|PWM\d*|"
    r"GPIO\d*|DMA\d*|TIM\d*|RTC\d*|PHY\d*|RJ45|SD|SDHC|SDXC|MICROSD|TVS|ESD|LED\d*|"
    r"DAT\d|CMD|CLK|CS|SCK|MOSI|MISO|SDA|SCL|TXD\d*|RXD\d*|CANH|CANL|VBUS|VCC|VDD|"
    r"VSS|GND|CC\d|DP|DM|L\d|M\d|IO\d+|REF_?CLK\d*|CRS_?DV|TX_?EN)$",
    re.IGNORECASE,
)
_QUANTITY_RE = re.compile(
    r"^(?:\d+(?:\.\d+)?(?:V|MV|KV|A|MA|UA|W|MW|HZ|KHZ|MHZ|GHZ|F|MF|UF|NF|PF|R|K|M|"
    r"OHM|BIT|MBIT|GBIT|KB|MB|GB|MM|CM|PIN|PINS|LAYER|LAYERS|X|%)|"
    r"\d+V\d+|L\d|LAYER\d+)$",
    re.IGNORECASE,
)
# Common English/Chinese words that pass the shape test (e.g. "USB4", "RS485"
# are interfaces; "SAME54" alone is a family, not an order code).
_ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "mcu": ("mcu", "microcontroller", "主控", "单片机", "处理器", "soc"),
    "ethernet_phy": ("phy", "ethernet", "以太网"),
    "can_transceiver": ("can", "can-fd", "canfd", "收发器", "transceiver"),
    "buck_regulator": ("buck", "降压", "step-down", "dc-dc", "dcdc"),
    "ldo_regulator": ("ldo", "线性稳压", "linear regulator"),
    "flash": ("flash", "nor", "qspi", "存储"),
    "sensor": ("sensor", "传感器", "温湿度", "humidity", "temperature"),
    "usb_esd": ("esd", "tvs", "静电", "浪涌"),
}
_MCU_CONSTRAINT_RE = re.compile(
    r"(?:主控(?:器)?(?:必须)?(?:是|为|采用|使用)|"
    r"\bMCU\b[^\n。.]{0,20}?(?:must\s+be|is|shall\s+be|=)|"
    r"(?:main|primary)\s+(?:mcu|microcontroller)[^\n。.]{0,20}?"
    r"(?:must\s+be|is|shall\s+be|=)|"
    r"固定(?:为|主控)?)",
    re.IGNORECASE,
)
_FORBID_SUBSTITUTION_RE = re.compile(
    r"(?:不得替换|不能替换|禁止替换|不允许替换|不得使用其他|固定|"
    r"\bmust\s+not\s+be\s+(?:replaced|substituted)\b|\bno\s+substitution\b|"
    r"\bdo\s+not\s+substitute\b|\bexact\s+part\b)",
    re.IGNORECASE,
)
_PACKAGE_MENTION_RE = re.compile(
    r"\b((?:[A-Z]{0,2}QFP|QFN|BGA|SOIC|SSOP|TSSOP|MSOP|VSSOP|DFN|SOT|SOD|WLCSP|"
    r"LGA|DIP)-?\d+(?:-\d+)?)\b",
    re.IGNORECASE,
)


def strip_runtime_evidence(text: str) -> str:
    """Drop runtime-appended evidence so only user intent is parsed."""
    for marker in _RUNTIME_EVIDENCE_MARKERS:
        text = text.partition(marker)[0]
    return text


# Requirements are often written as a label on one line and its value on the
# next ("固定使用：\n\nSTM32F103C8T6"). Collapsing that join keeps the cue and the
# order code in one clause so constraint detection still binds them.
_LABEL_VALUE_BREAK_RE = re.compile(r"([：:])[ \t]*[\r\n]+[ \t]*")


def _user_intent_text(requirement: str) -> str:
    text = _STRUCTURED_NAME_RE.sub(" ", strip_runtime_evidence(requirement or ""))
    return _LABEL_VALUE_BREAK_RE.sub(r"\1 ", text)


def normalize_order_code(value: str) -> str:
    """Alphanumeric lowercase form used for order-code comparison."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _is_part_shaped(token: str) -> bool:
    normalized = normalize_order_code(token)
    if len(normalized) < 5:
        return False
    if not (any(c.isalpha() for c in normalized) and any(c.isdigit() for c in normalized)):
        return False
    bare = token.upper()
    if _PACKAGE_CODE_RE.match(bare) or _INTERFACE_INSTANCE_RE.match(bare):
        return False
    return not _QUANTITY_RE.match(bare)


def order_code_matches(first: str, second: str) -> bool:
    """Compare order codes, treating KiCad's ``x`` placeholder as a wildcard.

    Mirrors the pipeline's ``_mcu_model_matches`` so the agent and pipeline
    layers agree on identity.
    """
    left, right = normalize_order_code(first), normalize_order_code(second)
    if not left or not right:
        return False
    if left == right:
        return True

    def pattern(value: str) -> str:
        return re.escape(value).replace("x", "[a-z0-9]")

    return bool(re.fullmatch(pattern(left), right) or re.fullmatch(pattern(right), left))


def order_code_base(value: str) -> str:
    """Order code without its trailing packaging/temperature suffix.

    Vendors append tray/tape and grade suffixes that EDA libraries omit:
    ``ATSAME54P20A-AU`` (tray) is the same die/pinout as a library
    ``ATSAME54P20A-A``. Comparing bases prevents a spurious mismatch while the
    full code stays authoritative for procurement.
    """
    token = (value or "").strip()
    if "-" in token:
        head, _, tail = token.rpartition("-")
        if head and len(tail) <= 3:
            return normalize_order_code(head)
    return normalize_order_code(token)


@lru_cache(maxsize=1)
def _symbol_names() -> tuple[tuple[str, str, str], ...]:
    """(lib_id, library nick, normalized symbol name) for every installed symbol."""
    try:
        from ratsnestpro.eda import grounding

        index = grounding.symbol_index()
    except Exception:
        return ()
    return tuple(
        (lib_id, lib_id.partition(":")[0], normalize_order_code(lib_id.partition(":")[2]))
        for lib_id in index
    )


def clear_symbol_cache() -> None:
    """Drop the cached symbol view (call after changing KICAD_SYMBOL_DIR)."""
    _symbol_names.cache_clear()


def order_code_family(normalized: str) -> str:
    """Longest leading letters+digits run, e.g. atsame54p20aau -> atsame54."""
    match = re.match(r"^([a-z]+\d+)", normalized)
    return match.group(1) if match else normalized[:6]


@dataclass(frozen=True)
class SymbolMatch:
    lib_id: str
    library: str
    relation: Literal["exact", "wildcard", "family"]


@dataclass
class ResolvedPart:
    """A part token resolved from user intent and grounded evidence."""

    token: str
    normalized: str
    role: str = ""
    sources: tuple[ResolutionSource, ...] = ()
    symbol_matches: tuple[SymbolMatch, ...] = ()
    catalog_mpns: tuple[str, ...] = ()
    package: str = ""
    substitution: SubstitutionPolicy = "allowed"
    evidence: tuple[str, ...] = ()

    @property
    def has_exact_symbol(self) -> bool:
        return any(match.relation == "exact" for match in self.symbol_matches)

    @property
    def confidence(self) -> float:
        if "symbol_exact" in self.sources:
            return 1.0
        if "symbol_wildcard" in self.sources:
            return 0.9
        if "catalog" in self.sources:
            return 0.8
        if "user_constraint" in self.sources:
            return 0.7
        if "symbol_family" in self.sources:
            return 0.6
        return 0.3


def _symbol_matches_for(normalized: str) -> tuple[SymbolMatch, ...]:
    matches: list[SymbolMatch] = []
    base = order_code_base(normalized)
    family = order_code_family(normalized)
    for lib_id, library, symbol_name in _symbol_names():
        if symbol_name == normalized or (base and symbol_name == base):
            matches.append(SymbolMatch(lib_id, library, "exact"))
        elif order_code_matches(normalized, symbol_name) or (
            base and order_code_matches(base, symbol_name)
        ):
            matches.append(SymbolMatch(lib_id, library, "wildcard"))
        elif family and symbol_name.startswith(family):
            matches.append(SymbolMatch(lib_id, library, "family"))
    order = {"exact": 0, "wildcard": 1, "family": 2}
    matches.sort(key=lambda m: (order[m.relation], m.lib_id))
    return tuple(matches[:24])


def _catalog_mpns(token: str) -> tuple[str, ...]:
    try:
        from ratsnestpro.parts import PartSelector

        selector = PartSelector()
        if not selector.available():
            return ()
        hits = selector.search(token, limit=5)
    except Exception:
        return ()
    return tuple(hit.mpn for hit in hits if hit.mpn)


def _clause_around(text: str, start: int) -> str:
    separators = ".!?。！？;\n"
    begin = max((text.rfind(sep, 0, start) for sep in separators), default=-1) + 1
    end_candidates = [pos for pos in (text.find(sep, start) for sep in separators) if pos > 0]
    end = min(end_candidates) if end_candidates else len(text)
    return text[begin:end]


def _role_for(clause: str, token: str) -> str:
    lowered = clause.lower()
    for role, keywords in _ROLE_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return role
    return ""


def resolve_parts(requirement: str) -> list[ResolvedPart]:
    """Resolve every grounded part token mentioned positively in ``requirement``.

    Ordered by descending confidence then first appearance. Negated mentions
    ("禁止替换为 RP2040") are excluded by the shared intent negation logic.
    """
    text = _user_intent_text(requirement)
    resolved: dict[str, ResolvedPart] = {}
    for match in _PART_TOKEN_RE.finditer(text):
        token = match.group(0)
        if not _is_part_shaped(token) or is_negated_mention(text, match.start()):
            continue
        normalized = normalize_order_code(token)
        if normalized in resolved:
            continue
        clause = _clause_around(text, match.start())
        sources: list[ResolutionSource] = []
        symbol_matches = _symbol_matches_for(normalized)
        relations = {item.relation for item in symbol_matches}
        if "exact" in relations:
            sources.append("symbol_exact")
        elif "wildcard" in relations:
            sources.append("symbol_wildcard")
        catalog = _catalog_mpns(token)
        if catalog:
            sources.append("catalog")
        pinned = bool(_MCU_CONSTRAINT_RE.search(clause))
        if pinned:
            sources.append("user_constraint")
        if "family" in relations and not {
            "symbol_exact",
            "symbol_wildcard",
        } & set(sources):
            sources.append("symbol_family")
        if not sources:
            continue
        package_match = _PACKAGE_MENTION_RE.search(clause)
        if package_match is None and pinned:
            # A pinned part often states its package in a following requirement
            # line rather than the same clause. Search a bounded window so the
            # package cannot leak onto an unrelated later component.
            window = text[match.end() : match.end() + 400]
            package_match = _PACKAGE_MENTION_RE.search(window)
        role = _role_for(clause, token)
        if pinned and not role:
            role = "mcu"
        resolved[normalized] = ResolvedPart(
            token=token,
            normalized=normalized,
            role=role,
            sources=tuple(sources),
            symbol_matches=symbol_matches,
            catalog_mpns=catalog,
            package=package_match.group(1).upper() if package_match else "",
            substitution=(
                "forbidden" if pinned or _FORBID_SUBSTITUTION_RE.search(clause) else "allowed"
            ),
            evidence=(f"clause: {clause.strip()[:200]}",),
        )
    return sorted(
        resolved.values(),
        key=lambda part: (-part.confidence, part.token),
    )


def resolve_primary_mcu(requirement: str) -> ResolvedPart | None:
    """The MCU the user pinned, or the best MCU-library-grounded candidate."""
    parts = resolve_parts(requirement)
    pinned = [part for part in parts if part.role == "mcu" and "user_constraint" in part.sources]
    if pinned:
        return pinned[0]
    mcu_library = [
        part
        for part in parts
        if any(match.library.upper().startswith("MCU_") for match in part.symbol_matches)
    ]
    if mcu_library:
        return mcu_library[0]
    return parts[0] if parts else None


class ComponentConstraint(BaseModel):
    """A hard component requirement, created once and referenced downstream.

    Section 4.4: a fixed MCU must not be re-parsed from a long prompt at every
    step. Requirements builds these; selection, pin mapping, materialize and
    review all read the same structured object.
    """

    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, max_length=64)
    manufacturer_part_number: str = Field(min_length=1, max_length=160)
    substitution: SubstitutionPolicy = "allowed"
    package: str = Field(default="", max_length=64)
    resolution_sources: list[str] = Field(default_factory=list)
    symbol_candidates: list[str] = Field(default_factory=list)
    catalog_mpns: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)

    @property
    def normalized_mpn(self) -> str:
        return normalize_order_code(self.manufacturer_part_number)

    def allows(self, candidate_mpn: str) -> bool:
        """Whether ``candidate_mpn`` satisfies this constraint's identity."""
        if self.substitution == "allowed":
            return True
        if order_code_matches(self.manufacturer_part_number, candidate_mpn) or (
            order_code_base(self.manufacturer_part_number) == order_code_base(candidate_mpn)
        ):
            return True
        if self.substitution == "family_equivalent":
            return order_code_family(self.normalized_mpn) == order_code_family(
                normalize_order_code(candidate_mpn)
            )
        return False


def build_component_constraints(requirement: str) -> list[ComponentConstraint]:
    """Build the structured constraint set once from the original requirement."""
    constraints: list[ComponentConstraint] = []
    for part in resolve_parts(requirement):
        if part.substitution == "allowed" and not part.role:
            continue
        constraints.append(
            ComponentConstraint(
                role=part.role or "component",
                manufacturer_part_number=part.token,
                substitution=part.substitution,
                package=part.package,
                resolution_sources=list(part.sources),
                symbol_candidates=[match.lib_id for match in part.symbol_matches[:8]],
                catalog_mpns=list(part.catalog_mpns),
                evidence=list(part.evidence),
            )
        )
    return constraints


@dataclass
class ConstraintSet:
    """Constraints plus lookup helpers, carried in the workflow state."""

    constraints: list[ComponentConstraint] = field(default_factory=list)

    @classmethod
    def from_requirement(cls, requirement: str) -> ConstraintSet:
        return cls(build_component_constraints(requirement))

    @classmethod
    def from_state(cls, payload: object) -> ConstraintSet:
        if not isinstance(payload, list):
            return cls()
        restored: list[ComponentConstraint] = []
        for item in payload:
            try:
                restored.append(ComponentConstraint.model_validate(item))
            except Exception:
                continue
        return cls(restored)

    def to_state(self) -> list[dict[str, object]]:
        return [constraint.model_dump() for constraint in self.constraints]

    def for_role(self, role: str) -> ComponentConstraint | None:
        for constraint in self.constraints:
            if constraint.role == role:
                return constraint
        return None

    @property
    def mcu(self) -> ComponentConstraint | None:
        return self.for_role("mcu")

    @property
    def fixed(self) -> list[ComponentConstraint]:
        return [c for c in self.constraints if c.substitution == "forbidden"]


# --------------------------------------------------------------------------- #
# 2.5a Symbol Acquisition capability ladder (doc section 4.5)
# --------------------------------------------------------------------------- #

AcquisitionTier = Literal[
    "installed_exact",
    "installed_wildcard",
    "vendor_resource",
    "trusted_third_party",
    "generated_from_pin_table",
    "unavailable",
]

_PACKAGE_PIN_COUNT_RE = re.compile(r"(\d+)(?!.*\d)")


def package_pin_count(package: str) -> int | None:
    """Pin count declared by a package code (TQFP-128 -> 128)."""
    if not package:
        return None
    match = _PACKAGE_PIN_COUNT_RE.search(package)
    if not match:
        return None
    try:
        count = int(match.group(1))
    except ValueError:
        return None
    return count if 2 <= count <= 2_000 else None


def _symbol_pin_count(lib_id: str) -> int | None:
    try:
        from ratsnestpro.eda import symbols

        info = symbols.symbol_info(lib_id)
    except Exception:
        return None
    if not info:
        return None
    count = info.get("pin_count")
    return int(count) if isinstance(count, int | float) else None


@dataclass
class CandidateRejection:
    lib_id: str
    reason: str


@dataclass
class SymbolAcquisition:
    """Outcome of the acquisition ladder for one component constraint."""

    requested_mpn: str
    tier: AcquisitionTier
    lib_id: str = ""
    pin_count: int | None = None
    rejected: tuple[CandidateRejection, ...] = ()
    evidence: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.tier not in {"unavailable"} and bool(self.lib_id)

    @property
    def failure_class(self) -> str:
        """Failure taxonomy label when nothing usable was acquired."""
        if self.resolved:
            return ""
        return "symbol_mismatch" if self.rejected else "symbol_unavailable"


def _validate_candidate(
    constraint: ComponentConstraint,
    match: SymbolMatch,
    required_pins: int | None,
) -> str:
    """Reason a candidate must be rejected, or "" when it is acceptable.

    Enforces the doc's rule that a fuzzy candidate must agree on device family,
    order code and package/pin count. This is what stops ATSAME54N19A-A (100
    pins) from standing in for ATSAME54P20A-AU (TQFP-128).
    """
    symbol_name = match.lib_id.partition(":")[2]
    if not constraint.allows(symbol_name):
        return (
            f"order code {symbol_name} does not satisfy "
            f"{constraint.manufacturer_part_number} with substitution="
            f"{constraint.substitution}"
        )
    if required_pins is not None:
        pins = _symbol_pin_count(match.lib_id)
        if pins is not None and pins != required_pins:
            return (
                f"symbol has {pins} pins but package {constraint.package} requires {required_pins}"
            )
    return ""


def acquire_symbol(constraint: ComponentConstraint) -> SymbolAcquisition:
    """Walk the acquisition ladder for one constraint, validating every rung.

    Only the rungs that can be verified offline are attempted here: exact and
    controlled-wildcard matches against installed libraries. Vendor and
    third-party rungs are reported as explicit next actions rather than being
    faked, and an unverifiable request ends as ``unavailable`` instead of
    silently substituting a near neighbour.
    """
    required_pins = package_pin_count(constraint.package)
    matches = _symbol_matches_for(constraint.normalized_mpn)
    if not matches:
        matches = _symbol_matches_for(order_code_base(constraint.manufacturer_part_number))
    rejections: list[CandidateRejection] = []
    for relation, tier in (
        ("exact", "installed_exact"),
        ("wildcard", "installed_wildcard"),
    ):
        for match in matches:
            if match.relation != relation:
                continue
            reason = _validate_candidate(constraint, match, required_pins)
            if reason:
                rejections.append(CandidateRejection(match.lib_id, reason))
                continue
            return SymbolAcquisition(
                requested_mpn=constraint.manufacturer_part_number,
                tier=tier,  # type: ignore[arg-type]
                lib_id=match.lib_id,
                pin_count=_symbol_pin_count(match.lib_id),
                rejected=tuple(rejections),
                evidence=(f"installed KiCad symbol matched by {relation}",),
            )
    # Family neighbours are never auto-accepted: they are recorded as rejected
    # evidence so the report can explain why the request is blocked.
    for match in matches:
        if match.relation != "family":
            continue
        reason = _validate_candidate(constraint, match, required_pins) or (
            "family neighbour is not the requested order code and substitution "
            f"is {constraint.substitution}"
        )
        rejections.append(CandidateRejection(match.lib_id, reason))
    return SymbolAcquisition(
        requested_mpn=constraint.manufacturer_part_number,
        tier="unavailable",
        rejected=tuple(rejections[:12]),
        evidence=(
            f"{len(matches)} library candidate(s) examined; none satisfied the "
            "identity and package requirements",
        ),
        next_actions=(
            f"fetch the official vendor EDA symbol for {constraint.manufacturer_part_number}",
            "or generate a project-level symbol from the official pin table and "
            "verify pin numbers, names, electrical types, package and pads",
        ),
    )


# --------------------------------------------------------------------------- #
# 2.5b Component dependency obligation graph + typed capability coverage
# --------------------------------------------------------------------------- #

# Required support networks per functional capability (doc section 4.6). These
# are structural obligations, not electrical values: the deterministic checks and
# datasheets still decide component values.
_OBLIGATIONS: dict[str, tuple[str, ...]] = {
    "buck_regulator": (
        "buck_input_capacitor",
        "buck_output_capacitor",
        "buck_inductor",
        "buck_bootstrap_capacitor",
        "buck_feedback_network",
        "buck_compensation_network",
    ),
    "ldo_regulator": ("ldo_input_capacitor", "ldo_output_capacitor"),
    "sdio_4bit": (
        "cmd_pullup",
        "dat0_pullup",
        "dat1_pullup",
        "dat2_pullup",
        "dat3_pullup",
        "sd_decoupling",
        "sd_esd",
    ),
    "qspi_flash": ("flash_decoupling", "flash_cs_pullup"),
    "can_fd": (
        "can_transceiver",
        "can_tvs",
        "can_common_mode_choke",
        "can_termination_option",
    ),
    "rmii_phy": (
        "phy_analog_decoupling",
        "phy_ref_clock",
        "phy_mdio_pullup",
        "phy_address_strap",
        "magnetics_center_tap",
        "ethernet_esd",
    ),
    "ethernet_connector_with_magnetics": ("ethernet_esd",),
    "usb2_device": (
        "usb_cc1_rd",
        "usb_cc2_rd",
        "usb_dp_series_resistor",
        "usb_dm_series_resistor",
        "usb_esd",
        "vbus_protection",
    ),
    "analog_input_0_10v": (
        "input_series_limit",
        "resistor_divider",
        "rc_low_pass",
        "adc_overvoltage_clamp",
        "input_connector",
    ),
    "industrial_dc_input": ("input_fuse", "reverse_polarity_protection", "input_tvs"),
    "i2c_bus": ("i2c_scl_pullup", "i2c_sda_pullup"),
    "mcu_core": ("core_decoupling", "core_regulator_capacitors", "reset_circuit"),
}

# Capabilities a part role actually provides. Prevents role-string overlap from
# implying coverage: an RJ45 with magnetics does not implement an RMII PHY.
_PROVIDED_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "ethernet_phy": ("rmii_phy",),
    "rj45_magjack": ("ethernet_connector_with_magnetics",),
    "can_transceiver": ("can_fd",),
    "buck_regulator": ("buck_regulator",),
    "ldo_regulator": ("ldo_regulator",),
    "flash": ("qspi_flash",),
    "microsd": ("sdio_4bit",),
    "mcu": ("mcu_core",),
    "sensor": ("i2c_bus",),
}

# Interface keywords -> required capability, used to read obligations off a
# natural-language requirement.
_CAPABILITY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "rmii_phy": ("rmii", "ethernet phy", "以太网 phy", "以太网phy"),
    "ethernet_connector_with_magnetics": ("rj45", "magjack", "网口"),
    "can_fd": ("can-fd", "can fd", "canfd"),
    "sdio_4bit": ("microsd", "sdio", "sdhc", "sd 卡", "sd卡"),
    "qspi_flash": ("qspi", "nor flash", "spi flash"),
    "usb2_device": ("usb-c", "usb c", "usb type-c", "usb 2.0", "type-c"),
    "analog_input_0_10v": ("0-10 v", "0-10v", "0–10 v", "0–10v", "模拟输入"),
    "industrial_dc_input": ("9-36", "9–36", "工业直流", "工业输入"),
    "buck_regulator": ("buck", "降压"),
    "ldo_regulator": ("ldo", "线性稳压", "低压差"),
    "i2c_bus": ("i²c", "i2c"),
}


@dataclass(frozen=True)
class Obligation:
    """One required support element implied by a chosen capability."""

    capability: str
    role: str
    satisfied: bool = False

    @property
    def key(self) -> str:
        return f"{self.capability}:{self.role}"


def required_capabilities(requirement: str) -> list[str]:
    """Capabilities the requirement asks for, by interface keyword."""
    lowered = strip_runtime_evidence(requirement or "").lower()
    found = [
        capability
        for capability, keywords in _CAPABILITY_KEYWORDS.items()
        if any(keyword in lowered for keyword in keywords)
    ]
    if "mcu_core" not in found:
        found.append("mcu_core")
    return found


def expand_obligations(capabilities: list[str]) -> list[Obligation]:
    """Recursively expand capabilities into their required support roles.

    A capability may imply another capability (an RMII PHY board also needs the
    magnetics/connector obligations), so expansion follows those edges once per
    capability to keep the graph acyclic.
    """
    seen: set[str] = set()
    queue = list(capabilities)
    obligations: list[Obligation] = []
    while queue:
        capability = queue.pop(0)
        if capability in seen:
            continue
        seen.add(capability)
        for role in _OBLIGATIONS.get(capability, ()):
            obligations.append(Obligation(capability=capability, role=role))
            # A required role that is itself a capability pulls in its own
            # obligations (e.g. can_transceiver -> can_fd support network).
            if role in _OBLIGATIONS and role not in seen:
                queue.append(role)
    return obligations


def capability_is_implemented_by(capability: str, part_role: str) -> bool:
    """Typed coverage check (doc section 4.7), not a substring comparison."""
    return capability in _PROVIDED_CAPABILITIES.get(part_role, ())


@dataclass
class CoverageReport:
    """Which required capabilities and obligations are actually covered."""

    missing_capabilities: tuple[str, ...] = ()
    missing_obligations: tuple[Obligation, ...] = ()
    satisfied_obligations: tuple[Obligation, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing_capabilities and not self.missing_obligations

    @property
    def failure_class(self) -> str:
        if self.missing_capabilities:
            return "missing_component"
        if self.missing_obligations:
            return "missing_support_network"
        return ""


def evaluate_coverage(
    capabilities: list[str],
    part_roles: list[str],
    present_roles: list[str] | None = None,
) -> CoverageReport:
    """Compare required capabilities/obligations against what parts provide.

    ``part_roles`` are the roles of selected parts (used for typed capability
    coverage); ``present_roles`` additionally names support roles already
    instantiated, defaulting to ``part_roles``.
    """
    available = list(present_roles if present_roles is not None else part_roles)
    missing_caps = [
        capability
        for capability in capabilities
        if not any(capability_is_implemented_by(capability, role) for role in part_roles)
    ]
    covered_capabilities = [c for c in capabilities if c not in missing_caps]
    satisfied: list[Obligation] = []
    missing: list[Obligation] = []
    for obligation in expand_obligations(covered_capabilities):
        if obligation.role in available:
            satisfied.append(obligation)
        else:
            missing.append(obligation)
    return CoverageReport(
        missing_capabilities=tuple(missing_caps),
        missing_obligations=tuple(missing),
        satisfied_obligations=tuple(satisfied),
    )
