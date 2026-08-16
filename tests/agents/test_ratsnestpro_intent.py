"""Regression matrix for structured intent routing.

Covers docs/Intent_Routing_and_AHE_EHE.md section 3.7 plus the SAME54 industrial
edge gateway case that previously misrouted a long build request to `review`
because it mentioned Reviewer/ERC/DRC and output filenames.
"""

import pytest
from langchain_core.messages import HumanMessage

from agents.ratsnestpro.intent import (
    IntentDecision,
    classify_intent,
    classify_requirement,
    detect_requested_outputs,
    extract_source_project_path,
    parse_request,
)
from agents.ratsnestpro.ratsnestpro_agent import (
    _after_initialize,
    initialize,
    ratsnestpro_multi_agent,
)

BUILD_THEN_REVIEW = "请设计一块 PCB，完成后由 Reviewer 审查。"
GENERATE_WITH_CHECKS = "请生成 KiCad 原理图和 PCB，并执行 ERC/DRC。"
REVIEW_WITH_POSIX_PATH = "请审查 /data/ratsnest/board.kicad_pcb 里的现有 PCB 工程。"
REVIEW_WITH_KICAD_PRO = "Review /data/ratsnest/board.kicad_pro and run ERC/DRC"
REVIEW_WITH_WINDOWS_PATH = 'Review "E:\\projects\\existing-board\\main.kicad_pcb".'
REVIEW_WITH_RUNS_DIR = "审查 runs/demo-pcb 里的 KiCad 工程并生成 Markdown 报告。"
REVIEW_NO_PATH = "请审查这个板子，但我没有提供任何路径或附件。"
PARTS_ONLY = "只查询某 MCU 的可采购型号。"
RESEARCH_ONLY = "研究 USB-C CC 电阻要求，不要生成 PCB。"
NEGATED_BUILD = "Research the STM32F405RGT6 datasheet. Do not design or generate a PCB."

# The real failure prompt (steering case), embedded verbatim: a long new-build
# request dense with Reviewer/审查/ERC/DRC/DSN/SES noise and explicit "not a
# review" directives. It must route to build.
SAME54_PROMPT = """This is a new PCB design-and-build request. Route this request to build mode.
There is no existing KiCad project to review.

请从需求开始设计一块新的工业以太网与 CAN-FD 数据采集网关。

project_name:
same54-industrial-edge-gateway

run_name:
same54-industrial-edge-gateway-e2e-v2

llm_mode:
required

必须执行完整的新建 PCB 流程，不得把本任务识别为“审查已有工程”。

禁止调用、复制、重命名或回退到 ATmega328P、STM32F405、RP2040 等已有案例或离线模板。

一、多智能体工作流

必须执行：

Supervisor
→ Architect
→ Parts Specialist
→ Hardware Engineer
→ Reviewer
→ 必要时 Hardware Engineer 修复
→ Reviewer 复审
→ Supervisor 汇总

十二、成功验收条件

成功至少要求：

- 主 MCU 确实为 ATSAME54P20A-AU；
- 产生实际 KiCad 原理图文件；
- 产生实际 KiCad PCB 文件；
- KiCad ERC error 为 0；
- KiCad DRC error 为 0；
- Freerouting 真实执行；
- 产生 DSN 和 SES；
- SES 成功导回 PCB；
- unconnected 为 0；
- 输出 BOM、CPL 和 Gerber；
- Reviewer 对本次新生成的工程完成独立审查。
"""


# --- doc section 3.7 matrix --------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [BUILD_THEN_REVIEW, GENERATE_WITH_CHECKS],
)
def test_create_actions_route_to_build(prompt: str) -> None:
    assert classify_requirement(prompt).primary_intent == "build"


def test_build_then_review_records_review_as_post_action() -> None:
    decision = classify_requirement(BUILD_THEN_REVIEW)
    assert decision.primary_intent == "build"
    assert "review" in decision.post_actions


@pytest.mark.parametrize(
    ("prompt", "expected_suffix"),
    [
        (REVIEW_WITH_POSIX_PATH, "board.kicad_pcb"),
        (REVIEW_WITH_KICAD_PRO, "board.kicad_pro"),
        (REVIEW_WITH_WINDOWS_PATH, "main.kicad_pcb"),
        (REVIEW_WITH_RUNS_DIR, "runs/demo-pcb"),
    ],
)
def test_review_with_source_path_routes_to_review(prompt: str, expected_suffix: str) -> None:
    decision = classify_requirement(prompt)
    assert decision.primary_intent == "review"
    assert decision.needs_clarification is False
    assert decision.source_project_path is not None
    assert decision.source_project_path.endswith(expected_suffix)


def test_review_without_path_requests_clarification() -> None:
    decision = classify_requirement(REVIEW_NO_PATH)
    assert decision.needs_clarification is True
    assert decision.clarification


def test_parts_only_routes_to_parts() -> None:
    assert classify_requirement(PARTS_ONLY).primary_intent == "parts"


@pytest.mark.parametrize("prompt", [RESEARCH_ONLY, NEGATED_BUILD])
def test_advisory_requests_route_to_research(prompt: str) -> None:
    assert classify_requirement(prompt).primary_intent == "research"


def test_long_build_prompt_with_review_noise_routes_to_build() -> None:
    decision = classify_requirement(SAME54_PROMPT)
    assert decision.primary_intent == "build"
    assert decision.needs_clarification is False
    # "There is no existing KiCad project to review" -> no source artifact.
    assert decision.source_project_path is None
    # Reviewer/ERC/DRC/output filenames are output evidence, not a review trigger.
    for artifact in ("KiCad schematic", "KiCad PCB", "DSN", "SES", "BOM", "CPL", "Gerber"):
        assert artifact in decision.requested_outputs


# --- source artifact vs requested output separation (the core fix) -----------


def test_requested_output_filename_is_not_a_review_trigger() -> None:
    decision = classify_requirement(
        "设计一块板子并产出 board.kicad_pcb 文件，然后做 ERC/DRC 审查。"
    )
    assert decision.primary_intent == "build"


def test_parse_request_separates_source_and_output() -> None:
    parsed = parse_request("设计一块板子并产出 board.kicad_pcb，然后 Reviewer 审查 ERC/DRC。")
    assert parsed.has_create_action is True
    assert parsed.has_review_action is True
    # A bare output filename is not a source project path.
    assert parsed.source_project_path is None
    assert "KiCad PCB" in parsed.requested_outputs
    decision = classify_intent(parsed)
    assert decision.primary_intent == "build"
    assert "review" in decision.post_actions


def test_extract_source_project_path_variants() -> None:
    assert (
        extract_source_project_path("Review /data/x/board.kicad_pro now")
        == "/data/x/board.kicad_pro"
    )
    assert extract_source_project_path('open "C:\\p\\b.kicad_sch" please') == "C:\\p\\b.kicad_sch"
    assert extract_source_project_path("审查 runs/demo-pcb 里的工程") == "runs/demo-pcb"
    assert extract_source_project_path("produce a board.kicad_pcb output") is None


def test_detect_requested_outputs() -> None:
    outputs = detect_requested_outputs("generate schematic, PCB, DSN, SES, BOM, CPL and Gerber")
    for artifact in ("KiCad schematic", "KiCad PCB", "DSN", "SES", "BOM", "CPL", "Gerber"):
        assert artifact in outputs


# --- explicit API workflow_mode + required-parameter gate --------------------


def test_explicit_mode_overrides_text_classification() -> None:
    decision = classify_requirement("Review /data/x/board.kicad_pcb", explicit_mode="build")
    assert decision.primary_intent == "build"
    assert decision.confidence == 1.0


def test_explicit_review_without_path_is_gated_to_clarification() -> None:
    decision = classify_requirement("audit the board", explicit_mode="review")
    assert decision.needs_clarification is True


def test_explicit_review_without_path_but_with_create_action_becomes_build() -> None:
    decision = classify_requirement("设计并生成一块新板子", explicit_mode="review")
    assert decision.primary_intent == "build"


# --- LLM tie-break for genuine ambiguity -------------------------------------


def test_ambiguous_request_uses_llm_classifier_when_provided() -> None:
    used: dict[str, bool] = {}

    def fake_llm(parsed: object) -> IntentDecision:
        used["called"] = True
        return IntentDecision(primary_intent="parts", confidence=0.66, evidence=["llm tie-break"])

    decision = classify_requirement("看看这个板子的整体情况。", llm_classifier=fake_llm)
    assert used.get("called") is True
    assert decision.primary_intent == "parts"


def test_ambiguous_request_without_llm_defaults_to_research() -> None:
    decision = classify_requirement("看看这个板子的整体情况。")
    assert decision.primary_intent == "research"
    assert decision.confidence <= 0.5


# --- graph integration -------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_routes_same54_prompt_to_build_with_configured_names() -> None:
    state = {"messages": [HumanMessage(content=SAME54_PROMPT)]}
    result = await initialize(state, {"configurable": {"thread_id": "t-1"}})
    assert result["workflow_mode"] == "build"
    assert result["intent"]["needs_clarification"] is False
    assert result["review_target"] == ""
    assert result["project_name"] == "same54-industrial-edge-gateway"
    assert result["run_name"] == "same54-industrial-edge-gateway-e2e-v2"


@pytest.mark.asyncio
async def test_initialize_explicit_workflow_mode_forces_build() -> None:
    state = {"messages": [HumanMessage(content="Review /data/x/board.kicad_pcb")]}
    result = await initialize(
        state, {"configurable": {"thread_id": "t-2", "workflow_mode": "build"}}
    )
    assert result["workflow_mode"] == "build"


@pytest.mark.asyncio
async def test_initialize_review_without_path_flags_clarification() -> None:
    state = {"messages": [HumanMessage(content="请审查这个板子，我没有给路径。")]}
    result = await initialize(state, {"configurable": {"thread_id": "t-3"}})
    assert result["workflow_mode"] == "review"
    assert result["intent"]["needs_clarification"] is True


def test_after_initialize_routes_clarification_to_clarify_node() -> None:
    assert (
        _after_initialize({"workflow_mode": "review", "intent": {"needs_clarification": True}})
        == "clarify"
    )
    assert (
        _after_initialize({"workflow_mode": "build", "intent": {"needs_clarification": False}})
        == "architect_phase"
    )


def test_clarify_node_is_registered() -> None:
    assert "clarify" in ratsnestpro_multi_agent.get_graph().nodes
