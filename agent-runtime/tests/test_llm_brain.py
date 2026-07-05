"""LLM brain seams: typed contracts enforced, deterministic fallback proven.

FakeLlm exercises the full brain path without network — validation is the
product here: the LLM proposes, the contracts dispose.
"""

from ratsnest.llm import extract_json
from ratsnest.schemas import Finding, RepairHint, RepairOp, RepairOpType, StrategyBundle


class FakeLlm:
    """Injectable brain returning canned JSON per agent name."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[str] = []
        self.available = True

    def complete_json(self, agent, system, user, max_tokens=0):
        self.calls.append(agent)
        return self.responses.get(agent)


# -- json extraction ----------------------------------------------------------

def test_extract_json_plain_fenced_and_noise():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": {"b": 2}}\n```') == {"a": {"b": 2}}
    assert extract_json('Sure! Here is the plan:\n{"x": "y"} done') == {"x": "y"}
    assert extract_json("no json here") is None
    assert extract_json('{"broken": ') is None


# -- seam 1: requirement understanding ----------------------------------------

def test_requirement_agent_llm_valid_and_invalid():
    from ratsnest.design_gen.requirement_agent import parse_requirement_llm

    good = FakeLlm({"requirement_agent": {
        "project_name": "Car Dash Cam supply!",
        "input_voltage": 12, "output_voltage": 3.3,
        "output_current_a": 1.0, "led": "green"}})
    spec = parse_requirement_llm("给行车记录仪做一个12V转3.3V的供电板，绿灯指示", good)
    assert spec is not None
    assert spec.output_voltage == 3.3 and spec.led == "green"
    assert spec.project_name == "car_dash_cam_supply"  # slug normalized

    # contract gate: Vout >= Vin is impossible for this family -> fallback
    bad = FakeLlm({"requirement_agent": {
        "project_name": "x", "input_voltage": 3.3, "output_voltage": 12}})
    assert parse_requirement_llm("boost 3.3 to 12", bad) is None

    # no LLM -> None (caller uses deterministic extractor)
    off = FakeLlm({})
    off.available = False
    assert parse_requirement_llm("12V to 5V", off) is None


# -- seam 2: creator foreman ---------------------------------------------------

def _placements():
    return [("J1", "Connector_Generic:Conn_01x02", "Conn_01x02", 75, 60),
            ("U1", "Regulator_Linear:AP1117-ADJ", "AP1117-ADJ", 100, 60),
            ("R1", "Device:R", "3k", 130, 55)]


def _crew_with(llm):
    from ratsnest.crews.creator import CreatorCrew
    crew = CreatorCrew.__new__(CreatorCrew)  # no KiCad host needed for this
    crew.llm = llm
    return crew


def test_foreman_valid_positions_applied():
    llm = FakeLlm({"creator_foreman": {
        "placements": [{"ref": "J1", "x": 72, "y": 60},
                       {"ref": "U1", "x": 95, "y": 62},
                       {"ref": "R1", "x": 120, "y": 80}],
        "rationale": "power flows left to right"}})
    result = _crew_with(llm)._foreman_positions(_placements())
    assert result is not None
    positions, rationale = result
    assert positions["R1"] == (120.0, 80.0)
    assert "left to right" in rationale


def test_foreman_contract_violations_rejected():
    # unknown ref -> whole proposal dropped (fallback to deterministic layout)
    llm = FakeLlm({"creator_foreman": {
        "placements": [{"ref": "J1", "x": 72, "y": 60},
                       {"ref": "U1", "x": 95, "y": 62},
                       {"ref": "C9", "x": 120, "y": 80}]}})
    assert _crew_with(llm)._foreman_positions(_placements()) is None
    # out-of-bounds position -> dropped
    llm2 = FakeLlm({"creator_foreman": {
        "placements": [{"ref": "J1", "x": 5, "y": 60},
                       {"ref": "U1", "x": 95, "y": 62},
                       {"ref": "R1", "x": 120, "y": 80}]}})
    assert _crew_with(llm2)._foreman_positions(_placements()) is None
    # missing ref -> dropped
    llm3 = FakeLlm({"creator_foreman": {
        "placements": [{"ref": "J1", "x": 72, "y": 60}]}})
    assert _crew_with(llm3)._foreman_positions(_placements()) is None


# -- seam 3: repair reasoning ---------------------------------------------------

def _hints():
    op1 = RepairOp(op=RepairOpType.set_value, ref="R1",
                   params={"value": "3k"}, finding_id="RN-VOUT-001:R1")
    op2 = RepairOp(op=RepairOpType.set_value, ref="R3",
                   params={"value": "330"}, finding_id="LR-001:R3")
    return [RepairHint(finding_id="RN-VOUT-001:R1", repair_type="feedback_divider",
                       suggested_ops=[op1], explanation="divider"),
            RepairHint(finding_id="LR-001:R3", repair_type="led_resistor",
                       suggested_ops=[op2], explanation="led")]


def test_repair_reasoner_filters_and_explains():
    from ratsnest.agents.repair_planner import _reason_about_repairs
    llm = FakeLlm({"repair_reasoner": {
        "approve": ["RN-VOUT-001:R1"],
        "reject": [{"finding_id": "LR-001:R3", "reason": "LED brightness ok"}],
        "notes": {"RN-VOUT-001:R1": "restores the 5V rail"}}})
    decision = _reason_about_repairs(_hints(), [], llm)
    assert decision is not None
    assert decision["approve"] == {"RN-VOUT-001:R1"}
    assert "LED brightness" in decision["rejects"]["LR-001:R3"]


def test_repair_reasoner_bogus_ids_fail_open():
    from ratsnest.agents.repair_planner import _reason_about_repairs
    llm = FakeLlm({"repair_reasoner": {
        "approve": ["NOT-A-REAL-ID"], "reject": [], "notes": {}}})
    assert _reason_about_repairs(_hints(), [], llm) is None  # keep everything


# -- seam 5: evolution proposer --------------------------------------------------

def test_evolution_proposer_bounded_diff():
    from ratsnest.evolution.proposer import propose_candidate
    incumbent = StrategyBundle(name="v0",
                               solver_params={"vref_table": {"AP1117": 1.25}})
    llm = FakeLlm({"evolution_agent": {
        "vref_table_add": {"LM1117": 1.25, "EVIL": 99.0},
        "weight_updates": {"warning": 5, "error": 9999},
        "name_suffix": "lm1117 vref!!",
        "rationale": "3 runs escalated on LM1117 divider findings"}})
    result = propose_candidate(incumbent, {"runs": 5}, llm)
    assert result is not None
    name, bundle, rationale = result
    assert bundle.solver_params["vref_table"]["LM1117"] == 1.25
    assert "EVIL" not in bundle.solver_params["vref_table"]   # out of bounds
    assert bundle.scorecard_weights["warning"] == 5.0
    assert bundle.scorecard_weights["error"] == 30.0          # 9999 rejected
    assert name.startswith("candidate-llm-")
    assert bundle.version_id() != incumbent.version_id()


def test_evolution_proposer_empty_diff_is_none():
    from ratsnest.evolution.proposer import propose_candidate
    incumbent = StrategyBundle(name="v0")
    llm = FakeLlm({"evolution_agent": {"rationale": "no idea"}})
    assert propose_candidate(incumbent, {}, llm) is None
