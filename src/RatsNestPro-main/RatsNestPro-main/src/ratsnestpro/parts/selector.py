"""Grounded part selection over local and optional remote catalogues.

Hard-fact layer: every MPN/LCSC value returned here comes from the real local
catalogue. When the cache is missing this reports ``available() is False`` and
returns empty results rather than inventing part numbers, so the agents and the
pipeline keep running with an explicit evidence gap instead of
fabricated procurement data.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, replace

from ratsnestpro.domain.contracts import CircuitIR
from ratsnestpro.eda.vendor import jlcpcb
from ratsnestpro.parts.catalog import (
    CatalogCandidate,
    PartCatalogProvider,
    PartConstraint,
    ProcurementContext,
    ProviderIssue,
    ProviderSearchResult,
    candidate_satisfies,
    decorate_candidate,
    providers_from_environment,
    rank_candidates,
)

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
    manufacturer: str = ""
    provider: str = "jlcpcb"
    provider_part_id: str = ""
    package_match: str = "unknown"
    asset_status: str = "unverified"
    lead_days: int | None = None
    currency: str = "CNY"
    source_url: str = ""
    fetched_at: str = ""
    snapshot_id: str = ""
    lifecycle: str = ""
    rohs: str = ""
    constraint_gaps: tuple[str, ...] = ()

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
        raw_gaps = row.get("constraint_gaps")
        constraint_gaps = (
            tuple(str(item) for item in raw_gaps)
            if isinstance(raw_gaps, (list, tuple))
            else ()
        )
        return cls(
            lcsc=text("lcsc"),
            mpn=text("mpn"),
            manufacturer=text("manufacturer"),
            description=text("description"),
            package=text("package"),
            category=text("category"),
            value=text("value"),
            stock=stock,
            price=price,
            datasheet=text("datasheet"),
            basic=basic,
            provider=text("provider") or "jlcpcb",
            provider_part_id=text("provider_part_id") or text("lcsc") or text("mpn"),
            package_match=text("package_match") or "unknown",
            asset_status=text("asset_status") or "unverified",
            lead_days=_optional_int(number("lead_days")),
            currency=text("currency") or "CNY",
            source_url=text("source_url"),
            fetched_at=text("fetched_at"),
            snapshot_id=text("snapshot_id"),
            lifecycle=text("lifecycle"),
            rohs=text("rohs"),
            constraint_gaps=constraint_gaps,
        )

    @classmethod
    def from_catalog(cls, candidate: CatalogCandidate) -> PartCandidate:
        return cls(
            lcsc=candidate.lcsc,
            mpn=candidate.mpn,
            manufacturer=candidate.manufacturer,
            description=candidate.description,
            package=candidate.package,
            category=candidate.category,
            value=candidate.value,
            stock=candidate.stock,
            price=candidate.price,
            datasheet=candidate.datasheet,
            basic=candidate.basic,
            provider=candidate.provider,
            provider_part_id=candidate.provider_part_id,
            package_match=candidate.package_match,
            asset_status=candidate.asset_status,
            lead_days=candidate.lead_days,
            currency=candidate.currency,
            source_url=candidate.source_url,
            fetched_at=candidate.fetched_at,
            snapshot_id=candidate.snapshot_id,
            lifecycle=candidate.lifecycle,
            rohs=candidate.rohs,
            constraint_gaps=candidate.constraint_gaps,
        )


def _optional_int(value: str) -> int | None:
    try:
        return int(float(value)) if value else None
    except (TypeError, ValueError):
        return None


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

    def __init__(self, providers: tuple[PartCatalogProvider, ...] | None = None) -> None:
        self.providers = providers or providers_from_environment()

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

    def search_catalog(
        self,
        constraint: PartConstraint,
        context: ProcurementContext | None = None,
        limit: int = 10,
    ) -> tuple[list[PartCandidate], list[ProviderIssue]]:
        """Search every configured provider and return ranked candidates."""
        context = context or ProcurementContext(quantity=max(1, constraint.quantity))
        if context.quantity < constraint.quantity:
            context = replace(context, quantity=constraint.quantity)
        all_candidates: list[CatalogCandidate] = []
        issues: list[ProviderIssue] = []
        for provider in self.providers:
            result: ProviderSearchResult = provider.search(constraint, context, limit=limit)
            all_candidates.extend(result.candidates)
            issues.extend(result.issues)
        eligible = [
            decorate_candidate(candidate, constraint)
            for candidate in all_candidates
            if candidate_satisfies(candidate, constraint)
        ]
        deduplicated: dict[tuple[str, str], CatalogCandidate] = {}
        for candidate in eligible:
            deduplicated.setdefault(
                (candidate.provider, candidate.provider_part_id), candidate
            )
        ranked = rank_candidates(list(deduplicated.values()), context)[: max(1, limit)]
        return [PartCandidate.from_catalog(candidate) for candidate in ranked], issues

    def select_for_role(
        self,
        role: str,
        value: str,
        footprint: str = "",
        *,
        package: str = "",
        context: ProcurementContext | None = None,
        limit: int = 5,
    ) -> tuple[list[PartCandidate], list[ProviderIssue]]:
        """Select candidates from a frozen role specification."""
        constraint = PartConstraint(
            role=role,
            value=value,
            footprint=footprint,
            package=package,
            quantity=(context.quantity if context else 1),
        )
        return self.search_catalog(constraint, context=context, limit=limit)

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
