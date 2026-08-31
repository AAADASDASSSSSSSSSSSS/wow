"""Catalog contracts and optional procurement-provider adapters.

The deterministic core can run entirely from the local JLCPCB cache. Remote
providers are deliberately optional: missing credentials become an evidence
gap, never a fabricated candidate and never an early design blocker.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

from ratsnestpro.eda.vendor import jlcpcb


@dataclass(frozen=True)
class ProcurementContext:
    """Preferences used when ranking otherwise compatible candidates."""

    region: str = "CN"
    preferred_providers: tuple[str, ...] = ("jlcpcb", "digikey", "mouser")
    basic_preferred: bool = True
    quantity: int = 1
    currency: str = "CNY"


@dataclass(frozen=True)
class PartConstraint:
    """Hard and soft requirements for one functional component role."""

    role: str = ""
    value: str = ""
    footprint: str = ""
    package: str = ""
    manufacturer: str = ""
    exact_mpn: str = ""
    min_stock: int = 0
    max_price: float | None = None
    max_lead_days: int | None = None
    required: bool = True
    quantity: int = 1
    hard_constraints: tuple[str, ...] = ()
    soft_preferences: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogCandidate:
    """Provider-neutral candidate with explicit evidence provenance."""

    provider: str
    provider_part_id: str
    lcsc: str
    mpn: str
    description: str
    package: str
    category: str
    value: str
    stock: int
    price: float
    manufacturer: str = ""
    currency: str = "CNY"
    lead_days: int | None = None
    datasheet: str = ""
    basic: bool = False
    package_match: str = "unknown"
    asset_status: str = "unverified"
    source_url: str = ""
    fetched_at: str = ""
    snapshot_id: str = ""
    lifecycle: str = ""
    rohs: str = ""
    constraint_gaps: tuple[str, ...] = ()
    preference_score: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def manufacturability_score(self) -> int:
        """Prefer parts whose package and KiCad assets are already grounded."""
        return {
            "exact": 4,
            "compatible": 3,
            "unknown": 1,
            "mismatch": 0,
        }.get(self.package_match, 0) + {
            "verified": 2,
            "compatible": 1,
            "unverified": 0,
            "missing": 0,
        }.get(self.asset_status, 0)


@dataclass(frozen=True)
class ProviderIssue:
    provider: str
    code: str
    message: str
    retryable: bool = False
    blocking: bool = False


@dataclass(frozen=True)
class ProviderSearchResult:
    provider: str
    candidates: tuple[CatalogCandidate, ...] = ()
    issues: tuple[ProviderIssue, ...] = ()
    queried_at: str = ""
    available: bool = True


class CatalogCache:
    """Persistent JSON snapshots for remote catalogue responses."""

    def __init__(self, path: str | None = None, ttl_seconds: int = 86_400) -> None:
        default = jlcpcb.db_path().with_name("catalog_cache.sqlite")
        self.path = default if path is None else Path(path)
        self.ttl_seconds = max(0, ttl_seconds)

    def get(self, key: str) -> tuple[list[dict[str, Any]], str] | None:
        if not self.path.is_file():
            return None
        try:
            connection = sqlite3.connect(str(self.path))
            row = connection.execute(
                "SELECT fetched_at, payload FROM catalog_snapshots WHERE cache_key = ?",
                (key,),
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            try:
                connection.close()
            except UnboundLocalError:
                pass
        if row is None or time.time() - float(row[0]) > self.ttl_seconds:
            return None
        try:
            payload = json.loads(row[1])
        except (TypeError, ValueError):
            return None
        return (
            payload,
            _cache_snapshot_id(key, float(row[0])),
        ) if isinstance(payload, list) else None

    def put(self, key: str, payload: list[dict[str, Any]]) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fetched_at = time.time()
        connection = sqlite3.connect(str(self.path))
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS catalog_snapshots ("
                "cache_key TEXT PRIMARY KEY, fetched_at REAL NOT NULL, payload TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT OR REPLACE INTO catalog_snapshots(cache_key, fetched_at, payload) "
                "VALUES (?, ?, ?)",
                (key, fetched_at, json.dumps(payload, ensure_ascii=False)),
            )
            connection.commit()
        finally:
            connection.close()
        return _cache_snapshot_id(key, fetched_at)


class PartCatalogProvider(Protocol):
    name: str

    def available(self) -> bool:
        """Whether this provider can be queried in the current environment."""

    def search(
        self,
        constraint: PartConstraint,
        context: ProcurementContext,
        limit: int = 10,
    ) -> ProviderSearchResult:
        """Return grounded candidates and explicit provider issues."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _snapshot_id(provider: str, query: str) -> str:
    stamp = time.time_ns()
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    return f"{provider}:{digest}:{stamp}"


def _cache_snapshot_id(key: str, fetched_at: float) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"cache:{digest}:{int(fetched_at)}"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value or default))
    except (TypeError, ValueError):
        match = re.search(r"\d[\d,]*", str(value or ""))
        return int(match.group(0).replace(",", "")) if match else default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
        return float(match.group(0)) if match else default


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "basic"}
    return bool(value)


def _nested_text(value: Any, *keys: str) -> str:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return str(current or "")


def _first_price(row: dict[str, Any]) -> float:
    direct = row.get("price") or row.get("unitPrice") or row.get("Price")
    if direct:
        return _as_float(direct)
    for key in ("StandardPricing", "PriceBreaks"):
        breaks = row.get(key)
        if not isinstance(breaks, list):
            continue
        for item in breaks:
            if isinstance(item, dict):
                price = item.get("UnitPrice") or item.get("Price")
                if price:
                    return _as_float(price)
    return 0.0


def _candidate_from_row(
    row: dict[str, Any],
    *,
    provider: str,
    package_match: str,
    context: ProcurementContext,
) -> CatalogCandidate:
    lcsc = str(
        row.get("lcsc")
        or row.get("id")
        or row.get("partNumber")
        or row.get("DigiKeyPartNumber")
        or row.get("MouserPartNumber")
        or ""
    )
    mpn = str(
        row.get("mpn")
        or row.get("manufacturerPartNumber")
        or row.get("ManufacturerPartNumber")
        or row.get("ManufacturerProductNumber")
        or row.get("MouserPartNumber")
        or ""
    )
    raw_package = row.get("package") or row.get("Package") or row.get("Packaging")
    if not raw_package:
        raw_package = row.get("PackageType")
    package = (
        str(raw_package.get("Name") or "")
        if isinstance(raw_package, dict)
        else str(raw_package or "")
    )
    fetched_at = str(row.get("fetched_at") or _now())
    currency = str(row.get("currency") or context.currency)
    asset_status = str(row.get("asset_status") or "unverified")
    attributes = dict(row.get("attributes") or {})
    raw_manufacturer = (
        row.get("manufacturer")
        or row.get("Manufacturer")
        or row.get("ManufacturerName")
        or _nested_text(row.get("ManufacturerInfo"), "Name")
        or ""
    )
    manufacturer = (
        str(raw_manufacturer.get("Name") or "")
        if isinstance(raw_manufacturer, dict)
        else str(raw_manufacturer)
    )
    lifecycle = str(row.get("lifecycle") or row.get("LifecycleStatus") or "")
    rohs = str(row.get("rohs") or row.get("ROHSStatus") or "")
    attributes.update(
        {
            key: value
            for key, value in {
                "manufacturer": manufacturer,
                "package": package,
                "lifecycle": lifecycle,
                "rohs": rohs,
            }.items()
            if value
        }
    )
    return CatalogCandidate(
        provider=provider,
        provider_part_id=lcsc or mpn,
        lcsc=lcsc,
        mpn=mpn,
        manufacturer=manufacturer,
        description=str(
            row.get("description")
            or row.get("Description")
            or row.get("ProductDescription")
            or ""
        ),
        package=package,
        category=str(row.get("category") or row.get("Category") or ""),
        value=str(row.get("value") or row.get("Value") or ""),
        stock=_as_int(
            row.get("stock")
            or row.get("quantityAvailable")
            or row.get("QuantityAvailable")
            or row.get("Availability")
        ),
        price=_first_price(row),
        currency=currency,
        lead_days=(
            _as_int(
                row.get("lead_days")
                or row.get("leadTimeDays")
                or row.get("LeadTime"),
                0,
            )
            or None
        ),
        datasheet=str(
            row.get("datasheet")
            or row.get("datasheetUrl")
            or row.get("DatasheetUrl")
            or row.get("DataSheetUrl")
            or ""
        ),
        basic=_as_bool(row.get("basic") or row.get("basicPart") or row.get("IsBasic")),
        package_match=package_match,
        asset_status=asset_status,
        source_url=str(
            row.get("source_url")
            or row.get("productUrl")
            or row.get("ProductUrl")
            or row.get("ProductDetailUrl")
            or ""
        ),
        fetched_at=fetched_at,
        snapshot_id=str(row.get("snapshot_id") or _snapshot_id(provider, mpn or lcsc)),
        lifecycle=lifecycle,
        rohs=rohs,
        attributes=attributes,
    )


class JlcSqliteProvider:
    """Read-only provider backed by the existing local JLCPCB database."""

    name = "jlcpcb"

    def available(self) -> bool:
        path = jlcpcb.db_path()
        if not path.is_file():
            return False
        try:
            import sqlite3

            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            connection.execute("SELECT 1 FROM parts LIMIT 1").fetchone()
        except Exception:
            return False
        finally:
            try:
                connection.close()
            except UnboundLocalError:
                pass
        return True

    def search(
        self,
        constraint: PartConstraint,
        context: ProcurementContext,
        limit: int = 10,
    ) -> ProviderSearchResult:
        queried_at = _now()
        if not self.available():
            return ProviderSearchResult(
                self.name,
                issues=(
                    ProviderIssue(
                        self.name, "cache_unavailable", "JLCPCB local cache is unavailable"
                    ),
                ),
                queried_at=queried_at,
                available=False,
            )
        try:
            package = constraint.package or _package_from_footprint(constraint.footprint)
            if constraint.role == "free_text":
                rows = jlcpcb.search(constraint.value, limit=max(1, limit))
                candidates = tuple(
                    _candidate_from_row(
                        dict(row),
                        provider=self.name,
                        package_match=_package_match(dict(row), package),
                        context=context,
                    )
                    for row in rows
                )
            elif constraint.exact_mpn:
                rows = jlcpcb.search(constraint.exact_mpn, limit=max(1, limit))
                candidates = tuple(
                    _candidate_from_row(
                        dict(row),
                        provider=self.name,
                        package_match=_package_match(dict(row), package),
                        context=context,
                    )
                    for row in rows
                )
            else:
                rows = jlcpcb.suggest_alternatives(
                    constraint.value, package or None, limit=max(1, limit)
                )
                if not rows and package:
                    rows = jlcpcb.suggest_alternatives(
                        constraint.value, None, limit=max(1, limit)
                    )
                candidates = tuple(
                    _candidate_from_row(
                        dict(row),
                        provider=self.name,
                        package_match=_package_match(dict(row), package),
                        context=context,
                    )
                    for row in rows
                )
            return ProviderSearchResult(self.name, candidates, queried_at=queried_at)
        except Exception as exc:
            return ProviderSearchResult(
                self.name,
                issues=(ProviderIssue(self.name, "query_failed", str(exc), retryable=True),),
                queried_at=queried_at,
            )


class _HttpCatalogProvider:
    """Small, dependency-free JSON adapter for providers with optional APIs."""

    name = "remote"
    api_key_env = ""
    client_id_env = ""
    endpoint_env = ""
    default_endpoint = ""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client_id: str | None = None,
        endpoint: str | None = None,
        requester: Callable[[str, dict[str, str], dict[str, Any]], Any] | None = None,
        cache: CatalogCache | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get(self.api_key_env, "")
        self.client_id = (
            client_id
            if client_id is not None
            else os.environ.get(self.client_id_env, "")
        )
        self.endpoint = endpoint or os.environ.get(self.endpoint_env, self.default_endpoint)
        self._requester = requester or _request_json
        self.cache = cache or CatalogCache()

    def available(self) -> bool:
        return bool(
            self.api_key
            and self.endpoint
            and (not self.client_id_env or self.client_id)
        )

    def request_endpoint(self) -> str:
        return self.endpoint

    def request_headers(self, context: ProcurementContext) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def request_payload(
        self,
        payload: dict[str, Any],
        context: ProcurementContext,
    ) -> dict[str, Any]:
        return payload

    def normalize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        return dict(item)

    def search(
        self,
        constraint: PartConstraint,
        context: ProcurementContext,
        limit: int = 10,
    ) -> ProviderSearchResult:
        queried_at = _now()
        if not self.api_key or (
            self.client_id_env and not self.client_id
        ):
            return ProviderSearchResult(
                self.name,
                issues=(
                    ProviderIssue(
                        self.name,
                        "credentials_missing",
                        f"{self.api_key_env} and {self.client_id_env} are not configured",
                    ),
                ),
                queried_at=queried_at,
                available=False,
            )
        if not self.endpoint:
            return ProviderSearchResult(
                self.name,
                issues=(
                    ProviderIssue(
                        self.name, "endpoint_missing", "provider endpoint is not configured"
                    ),
                ),
                queried_at=queried_at,
                available=False,
            )
        payload = {
            "query": constraint.value,
            "mpn": constraint.exact_mpn,
            "package": constraint.package or _package_from_footprint(constraint.footprint),
            "limit": max(1, limit),
            "region": context.region,
        }
        request_endpoint = self.request_endpoint()
        request_payload = self.request_payload(payload, context)
        cache_key = json.dumps(
            {"provider": self.name, "endpoint": self.endpoint, **request_payload},
            sort_keys=True,
        )
        try:
            cached = self.cache.get(cache_key)
            if cached is not None:
                raw_items, snapshot_id = cached
            else:
                data = self._requester(
                    request_endpoint,
                    self.request_headers(context),
                    request_payload,
                )
                raw_items = (
                    data.get("results")
                    or data.get("products")
                    or data.get("Products")
                    or data.get("items")
                    or []
                )
                search_results = data.get("SearchResults")
                if not raw_items and isinstance(search_results, dict):
                    raw_items = search_results.get("Parts") or []
                raw_items = [item for item in raw_items if isinstance(item, dict)]
                snapshot_id = self.cache.put(cache_key, raw_items)
            candidates = tuple(
                _candidate_from_row(
                    {
                        **self.normalize_item(dict(item)),
                        "value": item.get("value") or item.get("Value") or payload["query"],
                        "snapshot_id": snapshot_id,
                    },
                    provider=self.name,
                    package_match=_package_match(
                        self.normalize_item(dict(item)),
                        str(payload.get("package") or ""),
                    ),
                    context=context,
                )
                for item in raw_items[: max(1, limit)]
            )
            return ProviderSearchResult(self.name, candidates, queried_at=queried_at)
        except Exception as exc:
            return ProviderSearchResult(
                self.name,
                issues=(ProviderIssue(self.name, "query_failed", str(exc), retryable=True),),
                queried_at=queried_at,
            )


class DigiKeyProvider(_HttpCatalogProvider):
    name = "digikey"
    api_key_env = "DIGIKEY_ACCESS_TOKEN"
    client_id_env = "DIGIKEY_CLIENT_ID"
    endpoint_env = "DIGIKEY_SEARCH_ENDPOINT"
    default_endpoint = "https://api.digikey.com/products/v4/search/keyword"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        endpoint: str | None = None,
        token_endpoint: str | None = None,
        requester: Callable[[str, dict[str, str], dict[str, Any]], Any] | None = None,
        token_requester: Callable[[str, dict[str, str]], Any] | None = None,
        cache: CatalogCache | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            client_id=client_id,
            endpoint=endpoint,
            requester=requester,
            cache=cache,
        )
        self.client_secret = (
            client_secret
            if client_secret is not None
            else os.environ.get("DIGIKEY_CLIENT_SECRET", "")
        )
        self.token_endpoint = token_endpoint or os.environ.get(
            "DIGIKEY_TOKEN_ENDPOINT",
            "https://api.digikey.com/v1/oauth2/token",
        )
        self._token_requester = token_requester or _request_form_json
        self._token_expires_at = 0.0

    def available(self) -> bool:
        return bool(
            self.client_id
            and self.endpoint
            and (self.api_key or self.client_secret)
        )

    def _ensure_access_token(self) -> None:
        if self.api_key and (
            not self._token_expires_at or time.monotonic() < self._token_expires_at
        ):
            return
        if not self.client_id or not self.client_secret:
            raise RuntimeError(
                "DIGIKEY_ACCESS_TOKEN or DIGIKEY_CLIENT_SECRET is required"
            )
        data = self._token_requester(
            self.token_endpoint,
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
        )
        token = str(data.get("access_token") or "")
        if not token:
            raise RuntimeError("DigiKey token response contains no access_token")
        self.api_key = token
        expires_in = max(60, _as_int(data.get("expires_in"), 600))
        self._token_expires_at = time.monotonic() + expires_in - 30

    def search(
        self,
        constraint: PartConstraint,
        context: ProcurementContext,
        limit: int = 10,
    ) -> ProviderSearchResult:
        if self.available():
            try:
                self._ensure_access_token()
            except Exception as exc:
                return ProviderSearchResult(
                    self.name,
                    issues=(
                        ProviderIssue(
                            self.name,
                            "authentication_failed",
                            str(exc),
                            retryable=True,
                        ),
                    ),
                    queried_at=_now(),
                    available=False,
                )
        return super().search(constraint, context, limit)

    def request_headers(self, context: ProcurementContext) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-DIGIKEY-Client-Id": self.client_id,
            "X-DIGIKEY-Locale-Site": context.region.upper(),
            "X-DIGIKEY-Locale-Language": "zhs" if context.region.upper() == "CN" else "en",
            "X-DIGIKEY-Locale-Currency": context.currency.upper(),
        }

    def request_payload(
        self,
        payload: dict[str, Any],
        context: ProcurementContext,
    ) -> dict[str, Any]:
        del context
        return {
            "Keywords": payload["mpn"] or payload["query"],
            "Limit": min(50, max(1, int(payload["limit"]))),
            "Offset": 0,
        }

    def normalize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        row = dict(item)
        description = row.get("Description")
        if isinstance(description, dict):
            row["description"] = (
                description.get("ProductDescription")
                or description.get("DetailedDescription")
                or ""
            )
        variations = row.get("ProductVariations")
        if isinstance(variations, list):
            usable = [entry for entry in variations if isinstance(entry, dict)]
            if usable:
                variation = max(
                    usable,
                    key=lambda entry: _as_int(entry.get("QuantityAvailable")),
                )
                for source, target in (
                    ("DigiKeyProductNumber", "DigiKeyPartNumber"),
                    ("QuantityAvailable", "QuantityAvailable"),
                    ("StandardPricing", "StandardPricing"),
                    ("PackageType", "PackageType"),
                ):
                    if not row.get(target) and variation.get(source) is not None:
                        row[target] = variation[source]
        return row


class MouserProvider(_HttpCatalogProvider):
    name = "mouser"
    api_key_env = "MOUSER_API_KEY"
    endpoint_env = "MOUSER_SEARCH_ENDPOINT"
    default_endpoint = "https://api.mouser.com/api/v1/search/partnumber"

    def request_endpoint(self) -> str:
        parts = urlsplit(self.endpoint)
        query = urlencode({"apiKey": self.api_key})
        existing = f"{parts.query}&" if parts.query else ""
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, existing + query, parts.fragment)
        )

    def request_headers(self, context: ProcurementContext) -> dict[str, str]:
        del context
        return {"Content-Type": "application/json"}

    def request_payload(
        self,
        payload: dict[str, Any],
        context: ProcurementContext,
    ) -> dict[str, Any]:
        del context
        return {
            "SearchByPartRequest": {
                "mouserPartNumber": payload["mpn"] or payload["query"],
                "partSearchOptions": "None",
            }
        }


def _request_json(
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> dict[str, Any]:
    parsed: Any = None
    for attempt in range(3):
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
                parsed = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == 2:
                raise RuntimeError(f"HTTP {exc.code} from catalog provider") from exc
            retry_after = _as_float(exc.headers.get("Retry-After"), 0.25)
            time.sleep(min(2.0, max(0.05, retry_after)))
    if not isinstance(parsed, dict):
        raise RuntimeError("catalog provider returned a non-object JSON response")
    return parsed


def _request_form_json(endpoint: str, payload: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint,
        data=urlencode(payload).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            parsed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from DigiKey OAuth") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("DigiKey OAuth returned a non-object JSON response")
    return parsed


def _package_from_footprint(footprint: str) -> str:
    from ratsnestpro.parts.selector import package_from_footprint

    return package_from_footprint(footprint)


_PACKAGE_SIGNATURE_RE = re.compile(
    r"\b(SOT|SOIC|TSSOP|SSOP|MSOP|VSSOP|QFN|DFN|TQFP|LQFP|QFP|BGA|LGA|"
    r"CSP|WSON|SON|SOD|SOP|TSOP|DIP|TO)[- ]?(\d+)(?:[- ]?(\d+))?\b",
    re.IGNORECASE,
)
_PASSIVE_PACKAGE_RE = re.compile(r"\b(0[12468]\d{2}|1[02]\d{2})\b")


def normalize_package(value: str) -> str:
    """Normalize common distributor/KiCad package spelling differences."""
    text = value.upper().replace("_", "-").replace("/", "-")
    text = re.sub(r"\s+", "", text)
    passive = _PASSIVE_PACKAGE_RE.search(text)
    if passive:
        return passive.group(1)
    match = _PACKAGE_SIGNATURE_RE.search(text)
    if not match:
        return re.sub(r"[^A-Z0-9]", "", text)
    family, first, second = match.groups()
    return "-".join(token for token in (family.upper(), first, second) if token)


def packages_compatible(actual: str, requested: str) -> bool:
    if not actual or not requested:
        return False
    actual_normalized = normalize_package(actual)
    requested_normalized = normalize_package(requested)
    if actual_normalized == requested_normalized:
        return True
    actual_match = _PACKAGE_SIGNATURE_RE.search(actual_normalized)
    requested_match = _PACKAGE_SIGNATURE_RE.search(requested_normalized)
    if not actual_match or not requested_match:
        return False
    # Package-family and pin-count agreement is compatible even when one
    # catalogue omits a suffix such as exposed-pad count.
    return actual_match.group(1, 2) == requested_match.group(1, 2)


def _package_match(row: dict[str, Any], requested: str) -> str:
    if not requested:
        return "unknown"
    raw_actual = row.get("package") or row.get("Package") or row.get("Packaging")
    if not raw_actual:
        raw_actual = row.get("PackageType")
    actual = (
        str(raw_actual.get("Name") or "")
        if isinstance(raw_actual, dict)
        else str(raw_actual or "")
    )
    if not actual:
        return "unknown"
    if normalize_package(actual) == normalize_package(requested):
        return "exact"
    return "compatible" if packages_compatible(actual, requested) else "mismatch"


_ATTRIBUTE_CONSTRAINT_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9_. -]{0,80}?)\s*(<=|>=|==|=)\s*(.+?)\s*$"
)


def _attribute_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _candidate_attributes(candidate: CatalogCandidate) -> dict[str, Any]:
    values = {
        "manufacturer": candidate.manufacturer,
        "package": candidate.package,
        "lifecycle": candidate.lifecycle,
        "rohs": candidate.rohs,
        **candidate.attributes,
    }
    return {_attribute_key(str(key)): value for key, value in values.items()}


def _constraint_outcome(candidate: CatalogCandidate, expression: str) -> bool | None:
    match = _ATTRIBUTE_CONSTRAINT_RE.fullmatch(expression)
    if not match:
        return None
    key, operator, expected = match.groups()
    actual = _candidate_attributes(candidate).get(_attribute_key(key))
    if actual in (None, ""):
        return None
    actual_number = _as_float(actual, float("nan"))
    expected_number = _as_float(expected, float("nan"))
    numeric = actual_number == actual_number and expected_number == expected_number
    if operator == ">=":
        return actual_number >= expected_number if numeric else None
    if operator == "<=":
        return actual_number <= expected_number if numeric else None
    if numeric:
        return actual_number == expected_number
    return str(actual).strip().casefold() == expected.strip().casefold()


def candidate_constraint_gaps(
    candidate: CatalogCandidate,
    constraint: PartConstraint,
) -> tuple[str, ...]:
    gaps: list[str] = []
    if constraint.manufacturer and not candidate.manufacturer:
        gaps.append(f"manufacturer evidence missing ({constraint.manufacturer})")
    if constraint.package and candidate.package_match == "unknown":
        gaps.append(f"package evidence missing ({constraint.package})")
    if candidate.stock < max(1, constraint.quantity):
        gaps.append(
            f"stock {candidate.stock} is below required quantity {constraint.quantity}"
        )
    if constraint.max_price is not None and candidate.price <= 0:
        gaps.append("price evidence missing")
    if constraint.max_lead_days is not None and candidate.lead_days is None:
        gaps.append("lead-time evidence missing")
    for expression in constraint.hard_constraints:
        if _constraint_outcome(candidate, expression) is None:
            gaps.append(f"hard constraint is unverified: {expression}")
    return tuple(dict.fromkeys(gaps))


def decorate_candidate(
    candidate: CatalogCandidate,
    constraint: PartConstraint,
) -> CatalogCandidate:
    preference_score = sum(
        _constraint_outcome(candidate, expression) is True
        for expression in constraint.soft_preferences
    )
    return replace(
        candidate,
        constraint_gaps=candidate_constraint_gaps(candidate, constraint),
        preference_score=preference_score,
    )


def rank_candidates(
    candidates: list[CatalogCandidate],
    context: ProcurementContext,
) -> list[CatalogCandidate]:
    """Rank manufacturability first, then JLC basic/stock/lead time/price."""
    provider_order = {
        name: len(context.preferred_providers) - index
        for index, name in enumerate(context.preferred_providers)
    }

    def key(candidate: CatalogCandidate) -> tuple[Any, ...]:
        in_stock = candidate.stock >= max(0, context.quantity)
        lead = candidate.lead_days if candidate.lead_days is not None else 10_000
        price = candidate.price if candidate.price > 0 else 10_000_000.0
        return (
            candidate.manufacturability_score,
            int(candidate.package_match == "exact"),
            -len(candidate.constraint_gaps),
            int(candidate.basic and context.basic_preferred),
            provider_order.get(candidate.provider, 0),
            int(in_stock),
            candidate.stock,
            -lead,
            candidate.preference_score,
            -price,
        )

    return sorted(candidates, key=key, reverse=True)


def candidate_satisfies(candidate: CatalogCandidate, constraint: PartConstraint) -> bool:
    """Apply only explicit hard constraints; unknown evidence stays visible."""
    if constraint.exact_mpn and candidate.mpn.casefold() != constraint.exact_mpn.casefold():
        return False
    if (
        constraint.manufacturer
        and candidate.manufacturer
        and candidate.manufacturer.casefold() != constraint.manufacturer.casefold()
    ):
        return False
    if candidate.stock < constraint.min_stock:
        return False
    if constraint.max_price is not None and candidate.price > constraint.max_price:
        return False
    if (
        constraint.max_lead_days is not None
        and candidate.lead_days is not None
        and candidate.lead_days > constraint.max_lead_days
    ):
        return False
    if constraint.package and candidate.package_match == "mismatch":
        return False
    if any(
        _constraint_outcome(candidate, expression) is False
        for expression in constraint.hard_constraints
    ):
        return False
    return True


def providers_from_environment() -> tuple[PartCatalogProvider, ...]:
    """Construct the default provider chain without requiring remote secrets."""
    return (JlcSqliteProvider(), DigiKeyProvider(), MouserProvider())
