from __future__ import annotations

from ratsnestpro.orchestration.pipeline import (
    ComponentPrepareStep,
    PipelineContext,
    PipelineState,
    PipelineStep,
)
from ratsnestpro.orchestration.pipeline_contracts import SelectedPart, SelectionPlan
from ratsnestpro.parts import (
    CatalogCache,
    CatalogCandidate,
    DigiKeyProvider,
    MouserProvider,
    PartConstraint,
    ProcurementContext,
    candidate_constraint_gaps,
    packages_compatible,
    rank_candidates,
)


def _candidate(**changes: object) -> CatalogCandidate:
    values: dict[str, object] = {
        "provider": "jlcpcb",
        "provider_part_id": "C1",
        "lcsc": "C1",
        "mpn": "part",
        "description": "part",
        "package": "0603",
        "category": "capacitor",
        "value": "100nF",
        "stock": 10,
        "price": 1.0,
        "package_match": "exact",
        "asset_status": "verified",
        "basic": True,
    }
    values.update(changes)
    return CatalogCandidate(**values)


def test_ranking_prefers_manufacturability_before_price() -> None:
    expensive_grounded = _candidate(price=100.0, stock=1)
    cheap_unverified = _candidate(
        provider="mouser",
        provider_part_id="M1",
        lcsc="",
        price=0.01,
        stock=100_000,
        package_match="mismatch",
        asset_status="unverified",
        basic=False,
    )

    ranked = rank_candidates([cheap_unverified, expensive_grounded], ProcurementContext())

    assert ranked[0] == expensive_grounded


def test_remote_provider_without_credentials_is_an_evidence_gap() -> None:
    provider = DigiKeyProvider(api_key="", endpoint="https://example.invalid/search")
    result = provider.search(
        constraint=PartConstraint(value="STM32F103C8T6"),
        context=ProcurementContext(),
    )

    assert result.available is False
    assert result.candidates == ()
    assert result.issues[0].code == "credentials_missing"
    assert result.issues[0].blocking is False


def test_remote_catalog_response_is_reused_from_snapshot_cache(tmp_path) -> None:
    calls: list[str] = []

    def requester(endpoint, headers, payload):
        calls.append(endpoint)
        return {
            "results": [
                {
                    "mpn": "LM1117",
                    "description": "regulator",
                    "package": "SOT-223",
                    "quantityAvailable": 12,
                }
            ]
        }

    provider = DigiKeyProvider(
        api_key="test-token",
        client_id="test-client",
        endpoint="https://example.invalid/search",
        requester=requester,
        cache=CatalogCache(str(tmp_path / "catalog.sqlite")),
    )
    constraint = PartConstraint(value="LM1117", package="SOT-223")

    first = provider.search(constraint, ProcurementContext())
    second = provider.search(constraint, ProcurementContext())

    assert len(calls) == 1
    assert first.candidates[0].mpn == second.candidates[0].mpn == "LM1117"
    assert first.candidates[0].snapshot_id == second.candidates[0].snapshot_id


def test_digikey_v4_uses_vendor_request_and_response_contract(tmp_path) -> None:
    captured: dict[str, object] = {}

    def requester(endpoint, headers, payload):
        captured.update(endpoint=endpoint, headers=headers, payload=payload)
        return {
            "Products": [
                {
                    "DigiKeyProductNumber": "296-LM1117IMPX-ND",
                    "ManufacturerProductNumber": "LM1117IMPX-3.3/NOPB",
                    "Manufacturer": {"Name": "Texas Instruments"},
                    "Description": {"ProductDescription": "800 mA LDO"},
                    "PackageType": {"Name": "SOT-223"},
                    "QuantityAvailable": 42,
                    "DatasheetUrl": "https://example.test/lm1117.pdf",
                    "ProductUrl": "https://example.test/lm1117",
                    "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 0.42}],
                }
            ]
        }

    provider = DigiKeyProvider(
        api_key="token",
        client_id="client",
        requester=requester,
        cache=CatalogCache(str(tmp_path / "digikey.sqlite")),
    )
    result = provider.search(
        PartConstraint(value="LM1117", package="SOT-223"),
        ProcurementContext(region="CN", currency="CNY"),
    )

    assert captured["endpoint"] == "https://api.digikey.com/products/v4/search/keyword"
    assert captured["payload"] == {"Keywords": "LM1117", "Limit": 10, "Offset": 0}
    assert captured["headers"]["X-DIGIKEY-Locale-Site"] == "CN"
    assert result.candidates[0].mpn == "LM1117IMPX-3.3/NOPB"
    assert result.candidates[0].manufacturer == "Texas Instruments"
    assert result.candidates[0].stock == 42
    assert result.candidates[0].price == 0.42


def test_digikey_can_acquire_two_legged_oauth_token(tmp_path) -> None:
    token_payloads: list[dict[str, str]] = []

    def token_requester(_endpoint, payload):
        token_payloads.append(payload)
        return {"access_token": "fresh-token", "expires_in": 600}

    def requester(_endpoint, headers, _payload):
        assert headers["Authorization"] == "Bearer fresh-token"
        return {"Products": []}

    provider = DigiKeyProvider(
        api_key="",
        client_id="client",
        client_secret="secret",
        requester=requester,
        token_requester=token_requester,
        cache=CatalogCache(str(tmp_path / "oauth.sqlite")),
    )

    provider.search(PartConstraint(value="STM32"), ProcurementContext())

    assert token_payloads == [
        {
            "client_id": "client",
            "client_secret": "secret",
            "grant_type": "client_credentials",
        }
    ]


def test_mouser_uses_search_by_part_contract(tmp_path) -> None:
    captured: dict[str, object] = {}

    def requester(endpoint, _headers, payload):
        captured.update(endpoint=endpoint, payload=payload)
        return {
            "SearchResults": {
                "Parts": [
                    {
                        "MouserPartNumber": "595-LM1117IMPX33",
                        "ManufacturerPartNumber": "LM1117IMPX-3.3/NOPB",
                        "Manufacturer": "Texas Instruments",
                        "Description": "800 mA LDO",
                        "Package": "SOT-223",
                        "Availability": "125 In Stock",
                        "DataSheetUrl": "https://example.test/lm1117.pdf",
                        "PriceBreaks": [{"Quantity": 1, "Price": "$0.51"}],
                    }
                ]
            }
        }

    provider = MouserProvider(
        api_key="key",
        requester=requester,
        cache=CatalogCache(str(tmp_path / "mouser.sqlite")),
    )
    result = provider.search(
        PartConstraint(value="LM1117", package="SOT-223"),
        ProcurementContext(),
    )

    assert str(captured["endpoint"]).endswith("/partnumber?apiKey=key")
    assert captured["payload"] == {
        "SearchByPartRequest": {
            "mouserPartNumber": "LM1117",
            "partSearchOptions": "None",
        }
    }
    assert result.candidates[0].stock == 125
    assert result.candidates[0].price == 0.51


def test_package_aliases_and_missing_hard_constraint_evidence() -> None:
    candidate = _candidate(
        manufacturer="Texas Instruments",
        package="SOT23-5",
        attributes={"output_current_a": 0.6},
    )
    constraint = PartConstraint(
        manufacturer="Texas Instruments",
        package="SOT-23-5",
        quantity=20,
        hard_constraints=("output_current_a>=0.5", "dropout_v<=0.3"),
    )

    assert packages_compatible(candidate.package, constraint.package)
    assert candidate_constraint_gaps(candidate, constraint) == (
        "stock 10 is below required quantity 20",
        "hard constraint is unverified: dropout_v<=0.3",
    )


def test_component_preparation_does_not_block_when_libraries_are_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr("ratsnestpro.orchestration.pipeline.config.symbol_dir", lambda: None)
    monkeypatch.setattr("ratsnestpro.orchestration.pipeline.config.footprint_dir", lambda: None)
    state = PipelineState(requirement_text="Build a simple 3V3 board")
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(
        parts=[
            SelectedPart(
                ref="R1",
                symbol="Device:R",
                value="10k",
                footprint="Resistor_SMD:R_0603_1608Metric",
                role="pullup",
            )
        ]
    )

    result = ComponentPrepareStep().run(state, PipelineContext())

    assert result.blocked is False
    preparation = state.artifact(PipelineStep.COMPONENT_PREPARE)
    assert preparation is not None
    assert preparation.release_ready is False
    assert any(check.name == "component_release_ready" for check in result.checks)


def test_component_preparation_requires_positive_release_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.config.symbol_dir", lambda: tmp_path
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.config.footprint_dir", lambda: tmp_path
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.resolve_symbol", lambda _value: object()
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.footprints.footprint_pads",
        lambda _value: [{"number": "1"}],
    )
    state = PipelineState(requirement_text="Build a production-ready 3V3 board")
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(
        parts=[
            SelectedPart(
                ref="U1",
                symbol="Regulator_Linear:LM1117-3.3",
                value="LM1117-3.3",
                footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2",
                role="regulator",
                mpn="LM1117IMPX-3.3/NOPB",
                catalog_provider="digikey",
                provider_part_id="296-LM1117IMPX-ND",
                package_match="exact",
                datasheet="https://example.test/lm1117.pdf",
                catalog_snapshot_id="cache:abc:123",
            )
        ]
    )

    ComponentPrepareStep().run(state, PipelineContext())

    preparation = state.artifact(PipelineStep.COMPONENT_PREPARE)
    assert preparation.release_ready is True
    assert preparation.components[0].status == "installed_exact"
    assert preparation.components[0].unresolved is False
