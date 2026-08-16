"""Grounded part selection over the local JLCPCB SQLite cache.

Hard-fact layer: every MPN/LCSC value returned here comes from the real local
catalogue. When the cache is missing this reports ``available() is False`` and
returns empty results rather than inventing part numbers, so the agents and the
17-step pipeline keep running with an explicit evidence gap instead of
fabricated procurement data.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from ratsnestpro.domain.contracts import CircuitIR
from ratsnestpro.eda.vendor import jlcpcb

# Roles that are mechanical-only and never carry a purchasable catalogue value.
_UNGROUNDED_ROLES = frozenset({"mounting_hole", "test_point", "fiducial"})

# Imperial chip size embedded in a KiCad footprint name, e.g.
# "Capacitor_SMD:C_0603_1608Metric" -> 0603. Anchored on both sides so the
# metric twin (1608) is not picked instead.
_IMPERIAL_SIZE_RE = re.compile(r"_(\d{4})_")
_PACKAGE_TOKEN_RE = re.compile(
    r"\b((?:SOT|SOIC|TSSOP|SSOP|MSOP|VSSOP|QFN|DFN|TQFP|LQFP|QFP|SOD|SOP|TO)"
    r"-?\d+(?:-\d+)?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PartCandidate:
    """One catalogue row. Every field is grounded in the local cache."""

    lcsc: str
    mpn: str
    description: str
    package: str
    category: str
    value: str
    stock: int
    price: float
    datasheet: str
    basic: bool

    @classmethod
    def from_row(cls, row: dict[str, object]) -> PartCandidate:
        def text(key: str) -> str:
            value = row.get(key)
            return "" if value is None else str(value)

        def number(key: str) -> str:
            """Row values arrive as ``object`` from sqlite; normalize to text."""
            value = row.get(key)
            return "" if value is None else str(value)

        try:
            stock = int(float(number("stock") or 0))
        except ValueError:
            stock = 0
        try:
            price = float(number("price") or 0.0)
        except ValueError:
            price = 0.0
        raw_basic = number("basic")
        try:
            basic = bool(float(raw_basic or 0))
        except ValueError:
            basic = raw_basic.strip().lower() in {"true", "yes", "basic"}
        return cls(
            lcsc=text("lcsc"),
            mpn=text("mpn"),
            description=text("description"),
            package=text("package"),
            category=text("category"),
            value=text("value"),
            stock=stock,
            price=price,
            datasheet=text("datasheet"),
            basic=basic,
        )


def package_from_footprint(footprint: str) -> str:
    """Best-effort catalogue package name for a KiCad footprint lib_id.

    Returns "" when nothing recognizable is present; callers then query by value
    alone rather than filtering on a guess.
    """
    if not footprint:
        return ""
    name = footprint.partition(":")[2] or footprint
    imperial = _IMPERIAL_SIZE_RE.search(name)
    if imperial:
        return imperial.group(1)
    token = _PACKAGE_TOKEN_RE.search(name)
    if token:
        return token.group(1).upper()
    return ""


class PartSelector:
    """Read-only, grounded view of the local JLCPCB cache.

    The cache path is resolved per call through the vendored ``jlcpcb.db_path``,
    so changing ``KICAD_MCP_HOME`` takes effect without rebuilding the selector.
    """

    def available(self) -> bool:
        """True only when a real cache file exists and is queryable.

        Deliberately avoids the vendored ``_connect`` helper, which would create
        an empty database as a side effect and make an absent cache look present.
        """
        path = jlcpcb.db_path()
        if not path.is_file():
            return False
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            return False
        try:
            connection.execute("SELECT 1 FROM parts LIMIT 1").fetchone()
        except sqlite3.Error:
            return False
        finally:
            connection.close()
        return True

    def search(self, query: str, limit: int = 25) -> list[PartCandidate]:
        """Free-text search over MPN, description and value."""
        if not query or not self.available():
            return []
        try:
            rows = jlcpcb.search(query, limit=max(1, limit))
        except sqlite3.Error:
            return []
        return [PartCandidate.from_row(row) for row in rows]

    def suggest(
        self,
        value: str,
        footprint: str = "",
        limit: int = 10,
    ) -> list[PartCandidate]:
        """Candidates for an exact value, preferring the footprint's package.

        Falls back to a value-only query when the derived package yields nothing,
        so an unrecognized footprint name degrades instead of hiding real parts.
        """
        if not value or not self.available():
            return []
        bounded = max(1, limit)
        package = package_from_footprint(footprint)
        try:
            rows = jlcpcb.suggest_alternatives(value, package or None, limit=bounded)
            if not rows and package:
                rows = jlcpcb.suggest_alternatives(value, None, limit=bounded)
        except sqlite3.Error:
            return []
        return [PartCandidate.from_row(row) for row in rows]

    def ground_ir(
        self,
        ir: CircuitIR,
        limit: int = 3,
    ) -> dict[str, list[PartCandidate]]:
        """Map component refs to grounded candidates by value and footprint.

        Only refs with at least one real match appear; mechanical parts and
        components without a value are skipped. Returns {} with no cache.
        """
        if not self.available():
            return {}
        grounded: dict[str, list[PartCandidate]] = {}
        for component in ir.components:
            if component.role in _UNGROUNDED_ROLES or not component.value:
                continue
            candidates = self.suggest(component.value, component.footprint, limit=limit)
            if candidates:
                grounded[component.ref] = candidates
        return grounded
