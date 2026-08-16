import json
from pathlib import Path

import pytest
from ratsnestpro.agents import LlmMode
from ratsnestpro.domain.contracts import RequirementSpec
from ratsnestpro.orchestration.pipeline import (
    CheckResult,
    PipelineState,
    PipelineStep,
    StepResult,
)

from agents.agents import agents
from agents.ratsnestpro.tools import (
    _is_atmega328_only,
    _load_pipeline_state,
    _paired_project_files,
    _pipeline_mode,
    _symbol_match_score,
    _write_pipeline_state,
    ratsnest_create_design_plan,
    ratsnest_review_kicad_project,
    ratsnest_run_pcb_pipeline,
)
from agents.ratsnestpro.web_tools import (
    _page_score,
    _proxy_sections,
    _query_terms,
    _validate_public_https_url,
    fetch_datasheet,
    web_search,
)


def test_ratsnestpro_multi_agent_is_registered() -> None:
    assert "ratsnestpro-multi-agent" in agents


def test_create_design_plan_stays_in_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(tmp_path))

    result = json.loads(
        ratsnest_create_design_plan(
            "ATmega328 USB-C 5V 16MHz development board",
            run_name="unit-plan",
            project_name="unit_board",
        )
    )

    assert result["status"] == "ok"
    plan_path = Path(result["plan_path"])
    assert plan_path == tmp_path / "runs" / "unit-plan" / "plan.json"
    assert plan_path.is_file()
    assert result["params"]["crystal_mhz"] == 16
    assert result["params"]["ldo_output_v"] == 5.0


def test_review_rejects_path_outside_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(tmp_path))

    result = json.loads(ratsnest_review_kicad_project(str(tmp_path.parent)))

    assert result["status"] == "error"
    assert "must stay inside" in result["error"]


def test_review_pairs_schematic_and_pcb_from_either_project_file(tmp_path) -> None:
    schematic = tmp_path / "board.kicad_sch"
    pcb = tmp_path / "board.kicad_pcb"
    project = tmp_path / "board.kicad_pro"
    for path in (schematic, pcb, project):
        path.write_text("", encoding="utf-8")

    assert _paired_project_files(project, None, None) == (schematic, pcb)
    assert _paired_project_files(pcb, None, pcb) == (schematic, pcb)


def test_non_atmega_pipeline_cannot_use_offline_template() -> None:
    assert _pipeline_mode("RP2040 USB-C development board", LlmMode.OFFLINE) == (
        LlmMode.REQUIRED
    )
    assert _pipeline_mode("ATmega328 USB-C development board", LlmMode.OFFLINE) == (
        LlmMode.OFFLINE
    )
    assert _pipeline_mode(
        "Use RP2040, not ATmega328", LlmMode.OFFLINE
    ) == LlmMode.REQUIRED


def test_atmega_template_family_detection() -> None:
    assert _is_atmega328_only("ATmega328P-AU development board")
    assert not _is_atmega328_only("RP2040 development board")
    assert not _is_atmega328_only("Use RP2040, not ATmega328")
    assert not _is_atmega328_only("Do not use ATmega328; choose a generic MCU")


def test_non_atmega_plan_tool_redirects_to_adaptive_pipeline() -> None:
    result = json.loads(ratsnest_create_design_plan("RP2040 development board"))

    assert result["status"] == "use_generic_pipeline"
    assert result["next_tool"] == "ratsnest_run_pcb_pipeline"


def test_blocked_pipeline_persists_intermediate_state(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(tmp_path))

    def stop_at_selection(_self, state, _context):
        state.results.extend(
            [
                StepResult(step=PipelineStep.REQUIREMENTS, summary="requirements"),
                StepResult(step=PipelineStep.TOPOLOGY, summary="topology"),
                StepResult(
                    step=PipelineStep.SELECTION,
                    blocked=True,
                    summary="selection",
                    checks=[
                        CheckResult(
                            name="pin_pad_compatibility:J6",
                            ok=False,
                            message="16 pins do not match 11 pads",
                        )
                    ],
                ),
            ]
        )

    monkeypatch.setattr("agents.ratsnestpro.tools.Pipeline.run", stop_at_selection)

    result = json.loads(
        ratsnest_run_pcb_pipeline(
            "ATmega328 test board",
            run_name="blocked-state",
            project_name="blocked_board",
            llm_mode="offline",
        )
    )

    assert result["status"] == "blocked"
    assert result["completed_steps"] == 3
    assert Path(result["pipeline_state_path"]).is_file()
    assert Path(result["pipeline_result_path"]).is_file()
    assert any(path.endswith("pipeline_state.json") for path in result["artifacts"])
    assert result["steps"][-1]["failed_checks"] == [
        {
            "name": "pin_pad_compatibility:J6",
            "message": "16 pins do not match 11 pads",
        }
    ]


def test_pipeline_checkpoint_restores_only_completed_prefix(tmp_path) -> None:
    state = PipelineState(requirement_text="board", project_name="demo")
    state.artifacts[PipelineStep.REQUIREMENTS] = RequirementSpec(
        raw_text="board",
        project_name="demo",
    )
    state.results.extend([
        StepResult(step=PipelineStep.REQUIREMENTS, summary="requirements"),
        StepResult(step=PipelineStep.TOPOLOGY, blocked=True, summary="blocked"),
    ])
    checkpoint = tmp_path / "pipeline_state.json"
    _write_pipeline_state(checkpoint, "board", state)

    restored = _load_pipeline_state(checkpoint, "board", "demo")

    assert restored.completed == [PipelineStep.REQUIREMENTS]
    assert restored.artifact(PipelineStep.REQUIREMENTS) is not None
    assert restored.artifact(PipelineStep.TOPOLOGY) is None


def test_kicad_symbol_matching_accepts_order_code_wildcard() -> None:
    exact_family = _symbol_match_score(
        "STM32F405RGT6",
        "MCU_ST_STM32F4:STM32F405RGTx",
    )
    wrong_package = _symbol_match_score(
        "STM32F405RGT6",
        "MCU_ST_STM32F4:STM32F405VGTx",
    )

    assert exact_family > wrong_package


def test_web_search_provider_failure_does_not_abort(monkeypatch) -> None:
    def fail_search(*args, **kwargs):
        raise RuntimeError("TLS handshake failed")

    monkeypatch.setattr("agents.ratsnestpro.web_tools._provider_search", fail_search)

    result = json.loads(
        web_search.invoke(
            {
                "query": (
                    "st.com resource en datasheet dm00037051 "
                    "STM32F405xx STM32F407xx pinout LQFP64"
                )
            }
        )
    )

    assert result["status"] == "partial"
    assert result["results"][0]["href"] == (
        "https://www.st.com/resource/en/datasheet/dm00037051.pdf"
    )


def test_web_search_provider_failure_returns_recoverable_result(monkeypatch) -> None:
    def fail_search(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("agents.ratsnestpro.web_tools._provider_search", fail_search)

    result = json.loads(web_search.invoke({"query": "generic MCU reference design"}))

    assert result["status"] == "temporarily_unavailable"
    assert result["results"] == []


def test_datasheet_reader_rejects_local_network_url() -> None:
    with pytest.raises(ValueError, match="Local network"):
        _validate_public_https_url("https://localhost/private.pdf")


def test_datasheet_proxy_can_use_global_ipv4_with_reserved_ipv6(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "agents.ratsnestpro.web_tools.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("31.13.91.33", 443)),
            (10, 1, 6, "", ("2001::68f4:2bd0", 443, 0, 0)),
        ],
    )

    with pytest.raises(ValueError, match="Private or non-routable"):
        _validate_public_https_url("https://r.jina.ai/document")
    _validate_public_https_url(
        "https://r.jina.ai/document",
        allow_mixed_dns=True,
    )


def test_datasheet_pinout_page_outranks_revision_history() -> None:
    terms = _query_terms("LQFP64 pinout pin definitions")
    pinout_page = "Figure 12. STM32F40xxx LQFP64 pinout"
    revision_page = (
        "Revision history. Updated Figure 12. STM32F40xxx LQFP64 pinout."
    )

    assert _page_score(pinout_page, terms) > _page_score(revision_page, terms)


def test_datasheet_failure_does_not_abort(monkeypatch) -> None:
    def fail_read(*args, **kwargs):
        raise RuntimeError("PDF host unavailable")

    monkeypatch.setattr("agents.ratsnestpro.web_tools._read_datasheet", fail_read)
    monkeypatch.setattr(
        "agents.ratsnestpro.web_tools._read_datasheet_via_text_proxy",
        fail_read,
    )

    result = json.loads(
        fetch_datasheet.invoke(
            {
                "url": "https://www.st.com/resource/en/datasheet/dm00037051.pdf",
                "query": "LQFP64 pinout",
            }
        )
    )

    assert result["status"] == "temporarily_unavailable"
    assert result["matched_pages"] == []


def test_datasheet_uses_official_source_text_fallback(monkeypatch) -> None:
    def fail_read(*args, **kwargs):
        raise RuntimeError("PDF host unavailable")

    monkeypatch.setattr("agents.ratsnestpro.web_tools._read_datasheet", fail_read)
    monkeypatch.setattr(
        "agents.ratsnestpro.web_tools._read_datasheet_via_text_proxy",
        lambda url, query, max_pages: {
            "status": "partial",
            "source_url": url,
            "retrieval_method": "official_document_text_proxy",
            "query": query,
            "matched_pages": [{"page": 84, "text": "VCAP 2.2 uF"}][
                :max_pages
            ],
        },
    )

    result = json.loads(
        fetch_datasheet.invoke(
            {
                "url": "https://www.st.com/resource/en/datasheet/dm00037051.pdf",
                "query": "VCAP decoupling",
            }
        )
    )

    assert result["status"] == "partial"
    assert result["source_url"].endswith("dm00037051.pdf")
    assert result["retrieval_method"] == "official_document_text_proxy"
    assert "PDF host unavailable" in result["direct_fetch_error"]


def test_proxy_sections_preserve_datasheet_page_numbers() -> None:
    text = """
    Number of Pages: 206
    Markdown Content:
    DS8626 Rev 12 83/206
    General power text.
    DS8626 Rev 12 84/206
    VCAP external capacitor is 2.2 uF.
    """

    pages, sections = _proxy_sections(text)

    assert pages == 206
    assert dict(sections)[84].strip() == "VCAP external capacitor is 2.2 uF."
