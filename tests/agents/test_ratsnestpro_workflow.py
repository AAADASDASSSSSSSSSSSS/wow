import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from ratsnestpro.orchestration.pipeline import PIPELINE_TOTAL_STEPS

from agents.ratsnestpro.ratsnestpro_agent import (
    _after_architect,
    _after_hardware,
    _after_parts,
    _after_review,
    _component_queries,
    _configured_name,
    _hardware_requirement,
    _primary_mcu_mention,
    _validate_hardware_result,
    architect_phase,
    final_report,
    ratsnestpro_multi_agent,
)


def test_gate_driven_workflow_has_required_phases() -> None:
    nodes = set(ratsnestpro_multi_agent.get_graph().nodes)

    assert {
        "architect_phase",
        "parts_phase",
        "hardware_phase",
        "reviewer_phase",
        "hardware_repair_phase",
        "final_report",
    } <= nodes


def test_primary_mcu_ignores_project_slug_and_forbidden_alternatives() -> None:
    requirement = """
    项目名称为：
    stm32f405-industrial-controller
    主控必须是 STM32F405RGT6，禁止替换为其他 STM32、RP2040、ESP32 或 ATmega。
    """

    assert _primary_mcu_mention(requirement) == "STM32F405RGT6"


def test_configured_names_accept_chinese_labels_and_multiline_values() -> None:
    requirement = """
    项目名称为：

    stm32f405-industrial-controller
    run_name 使用：

    stm32f405-industrial-controller-e2e
    """

    assert _configured_name(requirement, {}, "project_name", "fallback") == (
        "stm32f405-industrial-controller"
    )
    assert _configured_name(requirement, {}, "run_name", "fallback") == (
        "stm32f405-industrial-controller-e2e"
    )


def test_part_queries_prioritize_required_mcu_and_skip_forbidden_mcus() -> None:
    requirement = """
    项目名称为：stm32f405-industrial-controller
    主控必须是 STM32F405RGT6，禁止替换为其他 STM32、RP2040、ESP32 或 ATmega。
    使用 W25Q64 SPI Flash。
    """

    queries = _component_queries(requirement)

    assert queries[0] == "STM32F405RGT6"
    assert "RP2040" not in queries
    assert "ESP32" not in queries


@pytest.mark.asyncio
async def test_architect_uses_exact_mcu_symbol_datasheet_before_search_results(
    monkeypatch,
) -> None:
    calls: dict[str, object] = {}

    def lookup(query: str, limit: int, required_package: str = "") -> str:
        calls["lookup"] = {
            "query": query,
            "limit": limit,
            "required_package": required_package,
        }
        return json.dumps(
            {
                "status": "ok",
                "exact_match": True,
                "candidates": [
                    {
                        "lib_id": "MCU_ST_STM32F4:STM32F405RGTx",
                        "usable": True,
                        "properties": {
                            "Datasheet": (
                                "https://www.st.com/resource/en/datasheet/stm32f405rg.pdf"
                            )
                        },
                    }
                ],
            }
        )

    class FakeTool:
        def __init__(self, response):
            self.response = response
            self.calls = []

        def invoke(self, args):
            self.calls.append(args)
            return json.dumps(self.response)

    class FakeModel:
        def with_config(self, **_kwargs):
            return self

        async def ainvoke(self, _messages, _config):
            return AIMessage(content="grounded design basis")

    search = FakeTool(
        {
            "status": "ok",
            "results": [{"href": ("https://www.st.com/resource/en/datasheet/stm32f405og.pdf")}],
        }
    )
    datasheet = FakeTool({"status": "ok", "matched_pages": [], "document_pages": 200})
    monkeypatch.setattr(
        "agents.ratsnestpro.ratsnestpro_agent.ratsnest_lookup_kicad_symbol",
        lookup,
    )
    monkeypatch.setattr(
        "agents.ratsnestpro.ratsnestpro_agent.web_search",
        search,
    )
    monkeypatch.setattr(
        "agents.ratsnestpro.ratsnestpro_agent.fetch_datasheet",
        datasheet,
    )
    monkeypatch.setattr(
        "agents.ratsnestpro.ratsnestpro_agent.get_model",
        lambda _model: FakeModel(),
    )

    result = await architect_phase(
        {
            "requirement": (
                "项目名称为：stm32f405-industrial-controller\n主控必须是 STM32F405RGT6。"
            ),
            "trace": [],
        },
        {"configurable": {"model": "test"}},
    )

    assert calls["lookup"] == {
        "query": "STM32F405RGT6",
        "limit": 3,
        "required_package": "",
    }
    assert datasheet.calls[0]["url"].endswith("stm32f405rg.pdf")
    assert datasheet.calls[0]["max_pages"] == 8
    assert "VCAP CEXT" in datasheet.calls[0]["query"]
    assert result["architecture"]["status"] == "ok"


@pytest.mark.asyncio
async def test_architect_defers_identity_lookup_for_capability_only_requirement(
    monkeypatch,
) -> None:
    def unexpected(*_args, **_kwargs):
        pytest.fail("device-specific lookup ran before MCU selection")

    class UnexpectedTool:
        def invoke(self, *_args, **_kwargs):
            return unexpected()

    class FakeModel:
        def with_config(self, **_kwargs):
            return self

        async def ainvoke(self, _messages, _config):
            return AIMessage(content="capability design basis")

    monkeypatch.setattr(
        "agents.ratsnestpro.ratsnestpro_agent.ratsnest_lookup_kicad_symbol",
        unexpected,
    )
    monkeypatch.setattr(
        "agents.ratsnestpro.ratsnestpro_agent.web_search",
        UnexpectedTool(),
    )
    monkeypatch.setattr(
        "agents.ratsnestpro.ratsnestpro_agent.fetch_datasheet",
        UnexpectedTool(),
    )
    monkeypatch.setattr(
        "agents.ratsnestpro.ratsnestpro_agent.get_model",
        lambda _model: FakeModel(),
    )

    result = await architect_phase(
        {
            "requirement": (
                "需要 Wi-Fi、BLE、2 路 UART、12 路 GPIO 和低功耗，请按需求选择主控。"
            ),
            "capability": {
                "selection_mode": "capability_only",
                "required_capabilities": ["wifi", "bluetooth_le", "mcu_core"],
            },
            "trace": [],
        },
        {"configurable": {"model": "test"}},
    )

    assert result["architecture"]["status"] == "ok"
    assert result["architecture"]["selection_mode"] == "capability_only"
    assert result["architecture"]["symbol"]["status"] == "deferred"


def test_hardware_requirement_carries_grounded_architect_evidence() -> None:
    requirement = _hardware_requirement(
        {
            "requirement": "Build an STM32F405RGT6 controller.",
            "architecture": {
                "symbol": {
                    "candidates": [
                        {
                            "lib_id": "MCU_ST_STM32F4:STM32F405RGTx",
                            "pin_count": 64,
                        },
                        {
                            "lib_id": "MCU_ST_STM32F4:STM32F415RGTx",
                            "pin_count": 64,
                        },
                    ]
                },
                "search": {
                    "results": [
                        {"href": ("https://www.st.com/resource/en/datasheet/stm32f405rg.pdf")}
                    ]
                },
                "datasheet": {
                    "status": "partial",
                    "matched_pages": [{"page": 84, "text": "CEXT is 2.2 uF for each VCAP pin."}],
                },
            },
        }
    )

    assert "MCU_ST_STM32F4:STM32F405RGTx" in requirement
    assert "MCU_ST_STM32F4:STM32F415RGTx" not in requirement
    assert "stm32f405rg.pdf" in requirement
    assert "CEXT is 2.2 uF" in requirement


def test_hardware_gate_rejects_claims_without_real_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(tmp_path))

    result = _validate_hardware_result(
        {
            "status": "ok",
            "run_directory": str(tmp_path / "missing"),
            "completed_steps": 17,
            "total_steps": 17,
            "routing": {"method": "freerouting", "unconnected": 0},
            "verification": {
                "erc": {
                    "applicable": True,
                    "available": True,
                    "ran": True,
                    "errors": 0,
                },
                "drc": {
                    "applicable": True,
                    "available": True,
                    "ran": True,
                    "errors": 0,
                    "unconnected": 0,
                },
            },
            "artifacts": [
                "runs/missing/expected.kicad_sch",
                "runs/missing/expected.kicad_pcb",
            ],
        }
    )

    assert not result["release_ready"]
    assert not result["project_available"]
    assert result["actual_files"] == []
    assert "no actual .kicad_sch artifact" in result["release_blockers"]


def test_hardware_gate_accepts_real_routed_project(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(tmp_path))
    run = tmp_path / "runs" / "complete"
    run.mkdir(parents=True)
    (run / "board.kicad_sch").write_text("schematic", encoding="utf-8")
    (run / "board.kicad_pcb").write_text("pcb", encoding="utf-8")
    (run / "board.dsn").write_text("dsn", encoding="utf-8")
    (run / "board.ses").write_text("ses", encoding="utf-8")

    result = _validate_hardware_result(
        {
            "status": "ok",
            "run_directory": str(run),
            "completed_steps": 17,
            "total_steps": 17,
            "routing": {"method": "freerouting", "unconnected": 0},
            "verification": {
                "erc": {
                    "applicable": True,
                    "available": True,
                    "ran": True,
                    "errors": 0,
                },
                "drc": {
                    "applicable": True,
                    "available": True,
                    "ran": True,
                    "errors": 0,
                    "unconnected": 0,
                },
            },
            "artifacts": [
                "runs/complete/board.kicad_sch",
                "runs/complete/board.kicad_pcb",
            ],
        }
    )

    assert result["release_ready"]
    assert result["project_available"]
    assert len(result["actual_files"]) == 4


def test_missing_procurement_proof_does_not_undo_technical_completion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(tmp_path))
    run = tmp_path / "runs" / "electrically-complete"
    run.mkdir(parents=True)
    for name, content in (
        ("board.kicad_sch", "schematic"),
        ("board.kicad_pcb", "pcb"),
        ("board.dsn", "dsn"),
        ("board.ses", "ses"),
    ):
        (run / name).write_text(content, encoding="utf-8")

    result = _validate_hardware_result(
        {
            "status": "ok",
            "run_directory": str(run),
            "completed_steps": PIPELINE_TOTAL_STEPS,
            "total_steps": PIPELINE_TOTAL_STEPS,
            "component_release_ready": False,
            "component_release_blockers": ["catalog API not configured"],
            "routing": {"method": "freerouting", "unconnected": 0},
            "verification": {
                "erc": {
                    "applicable": True,
                    "available": True,
                    "ran": True,
                    "errors": 0,
                },
                "drc": {
                    "applicable": True,
                    "available": True,
                    "ran": True,
                    "errors": 0,
                    "unconnected": 0,
                },
            },
            "artifacts": [],
        }
    )

    assert result["design_complete"]
    assert not result["release_ready"]
    assert "catalog API not configured" in result["release_blockers"]


def test_hardware_gate_rejects_nonzero_kicad_cli_erc(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(tmp_path))
    run = tmp_path / "runs" / "bad-erc"
    run.mkdir(parents=True)
    (run / "board.kicad_sch").write_text("schematic", encoding="utf-8")
    (run / "board.kicad_pcb").write_text("pcb", encoding="utf-8")

    result = _validate_hardware_result(
        {
            "status": "blocked",
            "run_directory": str(run),
            "completed_steps": 17,
            "total_steps": 17,
            "routing": {"method": "freerouting", "unconnected": 0},
            "verification": {
                "erc": {
                    "applicable": True,
                    "available": True,
                    "ran": True,
                    "errors": 59,
                },
                "drc": {
                    "applicable": True,
                    "available": True,
                    "ran": True,
                    "errors": 0,
                    "unconnected": 0,
                },
            },
            "artifacts": [
                "runs/bad-erc/board.kicad_sch",
                "runs/bad-erc/board.kicad_pcb",
            ],
        }
    )

    assert not result["release_ready"]
    assert result["project_available"]
    assert "kicad-cli ERC reported 59 error(s)" in result["release_blockers"]


def test_reviewer_block_routes_to_bounded_repair_rounds() -> None:
    assert (
        _after_review(
            {
                "workflow_mode": "build",
                "review": {"status": "blocked"},
                "review_round": 0,
                "max_review_rounds": 2,
            }
        )
        == "hardware_repair_phase"
    )
    assert (
        _after_review(
            {
                "workflow_mode": "build",
                "review": {"status": "blocked"},
                "review_round": 2,
                "max_review_rounds": 2,
            }
        )
        == "final_report"
    )


def test_reviewer_is_skipped_when_hardware_created_no_project() -> None:
    assert _after_hardware({"hardware": {"project_available": False}}) == "final_report"


def test_reviewer_runs_when_hardware_created_a_project() -> None:
    assert _after_hardware({"hardware": {"project_available": True}}) == "reviewer_phase"


def test_build_stops_when_architect_grounding_is_blocked() -> None:
    assert (
        _after_architect({"workflow_mode": "build", "architecture": {"status": "blocked"}})
        == "final_report"
    )
    assert (
        _after_architect({"workflow_mode": "build", "architecture": {"status": "ok"}})
        == "parts_phase"
    )
    assert (
        _after_architect({"workflow_mode": "build", "architecture": {"status": "partial"}})
        == "parts_phase"
    )


def test_build_allows_unavailable_catalog_but_stops_on_grounded_part_failure() -> None:
    assert (
        _after_parts({"workflow_mode": "build", "parts": {"status": "unavailable"}})
        == "hardware_phase"
    )
    assert (
        _after_parts({"workflow_mode": "build", "parts": {"status": "partial"}}) == "hardware_phase"
    )
    assert (
        _after_parts({"workflow_mode": "build", "parts": {"status": "blocked"}}) == "final_report"
    )


def test_workflow_uses_real_phase_nodes_without_synthetic_handoff_nodes() -> None:
    nodes = set(ratsnestpro_multi_agent.get_graph().nodes)

    assert not any(node.startswith("route_") for node in nodes)


def test_final_report_does_not_promote_expected_files() -> None:
    result = final_report(
        {
            "workflow_mode": "build",
            "trace": [],
            "architecture": {"status": "ok"},
            "parts": {"status": "unavailable"},
            "hardware": {
                "release_ready": False,
                "completed_steps": 0,
                "routing": {"method": "not_reached", "unconnected": -1},
                "release_blockers": ["no actual .kicad_sch artifact"],
                "actual_files": [],
            },
            "review": {},
        }
    )
    content = result["messages"][0].content

    assert "Overall status: **BLOCKED**" in content
    assert "Expected filenames are not reported as completed" in content


def test_final_report_preserves_artifacts_from_failed_repair(
    tmp_path: Path,
) -> None:
    schematic = tmp_path / "first-attempt.kicad_sch"
    schematic.write_text("schematic", encoding="utf-8")
    result = final_report(
        {
            "workflow_mode": "build",
            "trace": [],
            "architecture": {"status": "ok"},
            "parts": {"status": "unavailable"},
            "hardware": {
                "release_ready": False,
                "completed_steps": 3,
                "routing": {"method": "not_reached", "unconnected": -1},
                "release_blockers": ["17-step pipeline did not complete"],
                "actual_files": [],
            },
            "hardware_attempts": [
                {"actual_files": [str(schematic)]},
                {"actual_files": []},
            ],
            "review": {},
        }
    )

    assert str(schematic) in result["messages"][0].content


def test_final_report_includes_exact_blocked_pipeline_checks() -> None:
    result = final_report(
        {
            "workflow_mode": "build",
            "trace": [],
            "architecture": {"status": "ok"},
            "parts": {"status": "unavailable"},
            "hardware": {
                "release_ready": False,
                "completed_steps": 3,
                "routing": {"method": "not_reached"},
                "verification": {},
                "release_blockers": ["17-step pipeline did not complete"],
                "actual_files": [],
                "steps": [
                    {
                        "name": "selection",
                        "blocked": True,
                        "summary": "70 parts",
                        "failed_checks": [
                            {
                                "name": "pin_pad_compatibility:J6",
                                "message": "16 symbol pins do not match 11 pads",
                            }
                        ],
                    }
                ],
            },
            "review": {},
        }
    )

    content = result["messages"][0].content
    assert "Pipeline stopped at `selection`" in content
    assert "`pin_pad_compatibility:J6`: 16 symbol pins do not match 11 pads" in content


def test_unavailable_optional_parts_cache_does_not_override_clean_release() -> None:
    result = final_report(
        {
            "workflow_mode": "build",
            "trace": [],
            "architecture": {"status": "ok"},
            "parts": {"status": "unavailable"},
            "hardware": {
                "release_ready": True,
                "completed_steps": 17,
                "routing": {"method": "freerouting", "unconnected": 0},
                "verification": {
                    "erc": {"errors": 0},
                    "drc": {"errors": 0, "unconnected": 0},
                },
                "release_blockers": [],
                "actual_files": [],
                "steps": [],
            },
            "review": {"status": "ok"},
        }
    )

    content = result["messages"][0].content
    assert "Overall status: **SUCCESS**" in content
    assert "Parts verification: unavailable" in content
