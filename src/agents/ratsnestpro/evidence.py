"""Object-level distillation of kicad-cli ERC/DRC reports.

The repair loop used to receive only a count ("ERC reported 49 errors"), which
gives a model nothing to act on: in a real run it responded by deleting nets
(4 -> 3 -> 2) and the error count grew (110 -> 124 -> 136). A raw report is the
opposite problem — 211 violations is far too much prompt.

This module follows the layering used by the reference Agentic Harness
Engineering harness: an aggregated overview, per-object detail, and a pointer
back to the raw report so any claim can be drilled down. Every field here comes
from the deterministic report, never from model prose.

Real kicad-cli ERC schema this parses::

    {"sheets": [{"violations": [
        {"type": "pin_not_connected", "severity": "error",
         "description": "Pin not connected",
         "items": [{"description": "Symbol U1 Pin 7 [NRST, Input, Line]", ...}]}]}]}
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ReportKind = Literal["erc", "drc"]

# "Symbol U1 Pin 7 [NRST, Input, Line]"
_PIN_ITEM_RE = re.compile(
    r"^Symbol\s+(?P<ref>\S+)\s+Pin\s+(?P<pin>\S+)\s*"
    r"\[(?P<pin_name>[^,\]]*)(?:,\s*(?P<electrical>[^,\]]*))?(?:,\s*[^\]]*)?\]",
    re.IGNORECASE,
)
# "Symbol C3 [C]"
_SYMBOL_ITEM_RE = re.compile(r"^Symbol\s+(?P<ref>\S+)\s*\[(?P<value>[^\]]*)\]", re.IGNORECASE)
# "Label 'GND'"
_LABEL_ITEM_RE = re.compile(r"^Label\s+'(?P<label>[^']*)'", re.IGNORECASE)
# DRC wording, e.g. "Footprint U1 pad 7" / "Net 'VBUS'"
_PAD_ITEM_RE = re.compile(r"^Footprint\s+(?P<ref>\S+)\s+pad\s+(?P<pin>\S+)", re.IGNORECASE)
_NET_ITEM_RE = re.compile(r"Net\s+'(?P<net>[^']*)'", re.IGNORECASE)


@dataclass(frozen=True)
class ViolationFinding:
    """One violation occurrence, resolved to the object it names."""

    kind: ReportKind
    rule_type: str
    severity: str
    description: str
    ref: str = ""
    pin: str = ""
    pin_name: str = ""
    electrical_type: str = ""
    net: str = ""
    label: str = ""
    item_description: str = ""

    @property
    def object_key(self) -> str:
        """Stable identity of the offending object, for before/after comparison."""
        if self.ref and self.pin:
            return f"{self.ref}:{self.pin}"
        if self.ref:
            return self.ref
        if self.net:
            return f"net:{self.net}"
        if self.label:
            return f"label:{self.label}"
        return self.item_description[:60] or self.rule_type

    @property
    def signature(self) -> str:
        """Rule + object, the unit a repair is expected to eliminate."""
        return f"{self.rule_type}:{self.object_key}"


def _parse_item(item: object) -> dict[str, str]:
    """Pull the object identity out of one report item description."""
    if isinstance(item, dict):
        text = str(item.get("description", ""))
    else:
        text = str(item)
    parsed: dict[str, str] = {"item_description": text}
    pin_match = _PIN_ITEM_RE.match(text) or _PAD_ITEM_RE.match(text)
    if pin_match:
        groups = pin_match.groupdict()
        parsed["ref"] = groups.get("ref") or ""
        parsed["pin"] = groups.get("pin") or ""
        parsed["pin_name"] = (groups.get("pin_name") or "").strip()
        parsed["electrical_type"] = (groups.get("electrical") or "").strip()
    else:
        symbol_match = _SYMBOL_ITEM_RE.match(text)
        if symbol_match:
            parsed["ref"] = symbol_match.group("ref")
        label_match = _LABEL_ITEM_RE.match(text)
        if label_match:
            parsed["label"] = label_match.group("label")
    net_match = _NET_ITEM_RE.search(text)
    if net_match:
        parsed["net"] = net_match.group("net")
    return parsed


def _findings_from_violations(
    violations: list[Any],
    kind: ReportKind,
    default_severity: str = "error",
) -> list[ViolationFinding]:
    findings: list[ViolationFinding] = []
    for violation in violations:
        if not isinstance(violation, dict):
            continue
        rule_type = str(violation.get("type", "unknown"))
        severity = str(violation.get("severity", default_severity))
        description = str(violation.get("description", ""))
        items = violation.get("items")
        items = items if isinstance(items, list) and items else [{}]
        for item in items:
            parsed = _parse_item(item)
            findings.append(
                ViolationFinding(
                    kind=kind,
                    rule_type=rule_type,
                    severity=severity,
                    description=description,
                    **parsed,
                )
            )
    return findings


def parse_erc_report(path: str | Path) -> list[ViolationFinding]:
    """Parse a kicad-cli ERC JSON report into object-level findings."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    violations: list[Any] = []
    for sheet in data.get("sheets", []) if isinstance(data, dict) else []:
        if isinstance(sheet, dict):
            violations.extend(sheet.get("violations", []) or [])
    return _findings_from_violations(violations, "erc")


def parse_drc_report(path: str | Path) -> list[ViolationFinding]:
    """Parse a kicad-cli DRC JSON report, including parity/unconnected groups."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    findings: list[ViolationFinding] = []
    for key in ("violations", "schematic_parity", "unconnected_items"):
        group = data.get(key)
        if isinstance(group, list):
            findings.extend(_findings_from_violations(group, "drc"))
    return findings


@dataclass
class ObjectSummary:
    """All violations attached to one object, e.g. a component reference."""

    object_key: str
    ref: str = ""
    total: int = 0
    errors: int = 0
    rule_types: Counter = field(default_factory=Counter)
    pins: list[str] = field(default_factory=list)

    def describe(self, max_pins: int = 8) -> str:
        rules = ", ".join(f"{name}x{count}" for name, count in self.rule_types.most_common(3))
        text = f"{self.object_key} ({self.errors} error(s); {rules})"
        if self.pins:
            shown = self.pins[:max_pins]
            more = len(self.pins) - len(shown)
            pins = ", ".join(shown) + (f", +{more} more" if more > 0 else "")
            text += f" pins: {pins}"
        return text


@dataclass
class ViolationDigest:
    """Aggregated, drill-downable view of one or two reports."""

    findings: list[ViolationFinding] = field(default_factory=list)
    erc_report_path: str = ""
    drc_report_path: str = ""

    @property
    def errors(self) -> list[ViolationFinding]:
        return [f for f in self.findings if f.severity.lower() == "error"]

    @property
    def warnings(self) -> list[ViolationFinding]:
        return [f for f in self.findings if f.severity.lower() == "warning"]

    @property
    def error_signatures(self) -> set[str]:
        """Error-level rule+object pairs a repair is expected to eliminate."""
        return {f.signature for f in self.errors}

    def by_rule(self) -> Counter:
        return Counter(f.rule_type for f in self.errors)

    def by_object(self) -> list[ObjectSummary]:
        """Error findings grouped per object, worst offender first."""
        grouped: dict[str, ObjectSummary] = {}
        for finding in self.errors:
            key = finding.ref or finding.object_key
            summary = grouped.setdefault(key, ObjectSummary(object_key=key, ref=finding.ref))
            summary.total += 1
            summary.errors += 1
            summary.rule_types[finding.rule_type] += 1
            if finding.pin:
                pin_label = (
                    f"{finding.pin}({finding.pin_name})" if finding.pin_name else finding.pin
                )
                if pin_label not in summary.pins:
                    summary.pins.append(pin_label)
        ordered = sorted(grouped.values(), key=lambda s: (-s.errors, s.object_key))
        return ordered

    def target_refs(self, limit: int = 12) -> list[str]:
        """Component references responsible for the errors, worst first."""
        refs = [s.ref for s in self.by_object() if s.ref]
        return refs[:limit]

    def to_prompt(
        self,
        max_rules: int = 8,
        max_objects: int = 10,
        max_pins: int = 10,
    ) -> str:
        """Compact, actionable digest for the repair prompt.

        Aggregated counts first, then the specific objects and pins to fix, then
        the raw report path so the full detail stays reachable.
        """
        if not self.findings:
            return ""
        lines = [
            f"Deterministic verification digest: {len(self.errors)} error(s), "
            f"{len(self.warnings)} warning(s).",
            "",
            "Errors by rule:",
        ]
        for rule, count in self.by_rule().most_common(max_rules):
            lines.append(f"- {rule}: {count}")
        objects = self.by_object()
        if objects:
            lines.extend(["", f"Objects to fix (worst first, {len(objects)} total):"])
            for summary in objects[:max_objects]:
                lines.append(f"- {summary.describe(max_pins=max_pins)}")
            remaining = len(objects) - min(len(objects), max_objects)
            if remaining > 0:
                lines.append(f"- ... and {remaining} more object(s) in the raw report")
        paths = [p for p in (self.erc_report_path, self.drc_report_path) if p]
        if paths:
            lines.extend(["", "Full detail (drill down for every occurrence):"])
            lines.extend(f"- {path}" for path in paths)
        return "\n".join(lines)


def distill_reports(
    erc_report_path: str | None = None,
    drc_report_path: str | None = None,
) -> ViolationDigest:
    """Build one digest from whichever reports exist."""
    findings: list[ViolationFinding] = []
    erc_path = str(erc_report_path or "")
    drc_path = str(drc_report_path or "")
    if erc_path and Path(erc_path).is_file():
        findings.extend(parse_erc_report(erc_path))
    else:
        erc_path = ""
    if drc_path and Path(drc_path).is_file():
        findings.extend(parse_drc_report(drc_path))
    else:
        drc_path = ""
    return ViolationDigest(
        findings=findings,
        erc_report_path=erc_path,
        drc_report_path=drc_path,
    )


def digest_from_pipeline_result(result: dict[str, Any]) -> ViolationDigest:
    """Distill the reports referenced by a pipeline result payload."""
    verification = result.get("verification")
    if not isinstance(verification, dict):
        return ViolationDigest()
    erc = verification.get("erc") if isinstance(verification.get("erc"), dict) else {}
    drc = verification.get("drc") if isinstance(verification.get("drc"), dict) else {}
    return distill_reports(erc.get("report_path"), drc.get("report_path"))


def compare_signatures(
    before: set[str] | list[str],
    after: set[str] | list[str],
) -> dict[str, list[str]]:
    """Object-level flips between two sets of error signatures.

    This is the inner-loop analogue of the reference harness's task-level
    flipped/regressed sets: ``fixed`` disappeared, ``introduced`` are new, and
    ``persisted`` survived the repair.
    """
    before_set = set(before)
    after_set = set(after)
    return {
        "fixed": sorted(before_set - after_set),
        "introduced": sorted(after_set - before_set),
        "persisted": sorted(before_set & after_set),
    }


def compare_digests(
    before: ViolationDigest,
    after: ViolationDigest,
) -> dict[str, list[str]]:
    """Object-level flips between two attempts."""
    return compare_signatures(before.error_signatures, after.error_signatures)
