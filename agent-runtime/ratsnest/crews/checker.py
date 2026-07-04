"""Checker crew — kicad-happy disassembled into analyst agents.

Each agent owns ONE analyzer module, mounted in-process via khlib (the code
stays vendored and pullable; the architecture stops treating it as a single
black box). Every agent emits its own ATDP events, and per-agent knobs live
in the strategy's `analysts` slice — making individual checkers evolvable
(enable/disable, stage, future thresholds) instead of all-or-nothing.
"""

from __future__ import annotations

from pathlib import Path

from ratsnest.agents.base import Agent
from ratsnest.config import Config
from ratsnest.data_proxy import Recorder
from ratsnest.kh_adapter.runner import find_root_schematic
from ratsnest.khlib import load_kh_module
from ratsnest.schemas import AnalyzerOutput, StrategyBundle


class SchematicAnalyst(Agent):
    crew = "checker"
    name = "schematic_analyst"

    def __init__(self, config: Config, **kw):
        super().__init__(**kw)
        self.config = config

    def analyze(self, project_dir: Path) -> AnalyzerOutput:
        sch = find_root_schematic(project_dir)
        module = load_kh_module("analyze_schematic", self.config.kicad_scripts)
        raw = self.act(
            "analyze_schematic",
            lambda: module.analyze_schematic(str(sch)),
            observation={"file": sch.name},
            action_detail={"module": "kicad-happy/analyze_schematic"},
        )
        return AnalyzerOutput.model_validate(raw)


class PcbAnalyst(Agent):
    crew = "checker"
    name = "pcb_analyst"

    def __init__(self, config: Config, **kw):
        super().__init__(**kw)
        self.config = config

    def analyze(self, project_dir: Path) -> AnalyzerOutput | None:
        pcbs = sorted(Path(project_dir).glob("*.kicad_pcb"))
        if not pcbs:
            return None
        module = load_kh_module("analyze_pcb", self.config.kicad_scripts)
        try:
            raw = self.act(
                "analyze_pcb",
                lambda: module.analyze_pcb(str(pcbs[0])),
                observation={"file": pcbs[0].name},
                action_detail={"module": "kicad-happy/analyze_pcb"},
            )
        except Exception:
            return None  # boards with no layout content yet
        return AnalyzerOutput.model_validate(raw)


class CheckerCrew:
    """Fan-out over analyst agents; roster is strategy-governed."""

    def __init__(self, config: Config | None = None,
                 strategy: StrategyBundle | None = None,
                 recorder: Recorder | None = None, iteration: int = 0):
        self.config = config or Config.load()
        analysts_cfg = (strategy.solver_params.get("analysts", {})
                        if strategy else {})
        common = dict(recorder=recorder, iteration=iteration)
        self.agents: list = []
        if analysts_cfg.get("schematic", True):
            self.agents.append(SchematicAnalyst(
                self.config, strategy_slice=analysts_cfg, **common))
        if analysts_cfg.get("pcb", True):
            self.agents.append(PcbAnalyst(
                self.config, strategy_slice=analysts_cfg, **common))

    def evaluate(self, project_dir: Path) -> dict[str, AnalyzerOutput]:
        outputs: dict[str, AnalyzerOutput] = {}
        for agent in self.agents:
            result = agent.analyze(Path(project_dir))
            if result is not None:
                key = result.analyzer_type or agent.name
                outputs[key] = result
        return outputs
