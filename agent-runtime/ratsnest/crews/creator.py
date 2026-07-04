"""Creator crew — KiCAD-MCP-Server disassembled into designer agents.

The server's TypeScript/Node/MCP layer is DISCARDED for internal use: its
Python command interface (KiCADInterface.handle_command) is hosted in-process
inside our runtime (pcbnew imports fine — the venv shares KiCad's
interpreter). Each agent owns one command family; identity + ATDP telemetry +
strategy come from the Agent base. The MCP stdio transport remains available
only as an external integration path (Claude Desktop etc.).

  ProjectAgent    create_project / save_project / close_project
  SymbolAgent     add_schematic_component (real KiCad symbol libraries)
  WiringAgent     add_schematic_net_label / connect_to_net
"""

from __future__ import annotations

import sys
from pathlib import Path

from ratsnest.agents.base import Agent, AgentError
from ratsnest.config import Config
from ratsnest.data_proxy import Recorder
from ratsnest.design_gen.generator import GenerationError, solve_board_values
from ratsnest.design_gen.templates import rail_name
from ratsnest.kicad_env import bootstrap_kicad
from ratsnest.schemas import DesignSpec, StrategyBundle

# real KiCad 10 library symbol (verified present in Regulator_Linear.kicad_sym);
# AP1117 datasheet pinout: ADJ=1, VOUT=2, VIN=3 — and it is already the part
# the strategy's Vref table and MPN map are built around
REGULATOR_PART = "AP1117-ADJ"
REGULATOR_SYMBOL = "Regulator_Linear:AP1117-ADJ"

_host = None


def _get_host(config: Config):
    """Singleton in-process KiCADInterface from the vendored server."""
    global _host
    if _host is not None:
        return _host
    if not bootstrap_kicad(config.kicad_python):
        raise AgentError("pcbnew unavailable — cannot host KiCad skills in-process")
    if not config.mcp_server_dir:
        raise AgentError("KiCAD-MCP-Server dir not found (RATSNEST_MCP_SERVER)")
    py_dir = str(config.mcp_server_dir / "python")
    if py_dir not in sys.path:
        sys.path.insert(0, py_dir)
    import importlib
    module = importlib.import_module("kicad_interface")
    _host = module.KiCADInterface()
    return _host


class KiCadSkillAgent(Agent):
    """Base for creator agents: skills = vendored command handlers."""

    crew = "creator"
    commands: tuple[str, ...] = ()

    def __init__(self, config: Config, **kw):
        super().__init__(**kw)
        self.config = config

    def call(self, command: str, params: dict) -> dict:
        if command not in self.commands:
            raise AgentError(f"{self.name} does not own command {command!r}")
        host = _get_host(self.config)
        result = self.act(
            command, lambda: host.handle_command(command, params),
            action_detail={"params": {k: v for k, v in params.items()
                                      if k != "schematicPath"}},
        )
        if isinstance(result, dict) and result.get("success") is False:
            raise AgentError(f"{command} failed: "
                             f"{str(result.get('message') or result)[:200]}")
        return result if isinstance(result, dict) else {"value": result}


class ProjectAgent(KiCadSkillAgent):
    name = "project_agent"
    commands = ("create_project", "save_project", "close_project", "open_project")


class SymbolAgent(KiCadSkillAgent):
    name = "symbol_agent"
    commands = ("add_schematic_component",)


class WiringAgent(KiCadSkillAgent):
    name = "wiring_agent"
    commands = ("add_schematic_net_label", "connect_to_net", "add_schematic_wire")


class CreatorCrew:
    def __init__(self, config: Config | None = None,
                 recorder: Recorder | None = None, iteration: int = 0):
        self.config = config or Config.load()
        common = dict(recorder=recorder, iteration=iteration)
        self.project = ProjectAgent(self.config, **common)
        self.symbols = SymbolAgent(self.config, **common)
        self.wiring = WiringAgent(self.config, **common)

    def generate(self, spec: DesignSpec, out_dir: Path,
                 strategy: StrategyBundle) -> Path:
        out_dir = Path(out_dir).resolve()
        values, mpns, include_led = solve_board_values(
            spec, strategy, self.config, regulator_part=REGULATOR_PART)
        vin, vout = rail_name(spec.input_voltage), rail_name(spec.output_voltage)
        name = out_dir.name
        sch = out_dir / f"{name}.kicad_sch"
        out_dir.mkdir(parents=True, exist_ok=True)

        # both key spellings: the TS layer passed {name}, the python schema
        # documents {projectName} — the handler is fed directly in-process
        self.project.call("create_project", {"projectName": name, "name": name,
                                             "path": str(out_dir)})

        placements = [
            ("J1", "Connector_Generic:Conn_01x02", "Conn_01x02", 75, 60),
            ("U1", REGULATOR_SYMBOL, values["U1"], 100, 60),
            ("R1", "Device:R", values["R1"], 130, 55),
            ("R2", "Device:R", values["R2"], 130, 80),
        ]
        if include_led:
            placements += [("R3", "Device:R", values["R3"], 155, 55),
                           ("D1", "Device:LED", values["D1"], 155, 80)]
        for ref, symbol, value, x, y in placements:
            library, sym_type = symbol.split(":")
            self.symbols.call("add_schematic_component", {
                "schematicPath": str(sch),
                "component": {"library": library, "type": sym_type,
                              "reference": ref, "value": value,
                              "footprint": "", "x": x, "y": y,
                              "unit": 1, "angle": 0, "mirrorY": False},
            })

        nets = [(vin, "J1", "1"), ("GND", "J1", "2"),
                (vin, "U1", "3"), (vout, "U1", "2"), (vout, "R1", "1"),
                ("FB", "U1", "1"), ("FB", "R1", "2"), ("FB", "R2", "1"),
                ("GND", "R2", "2")]
        if include_led:
            nets += [(vout, "R3", "1"), ("LED_A", "R3", "2"),
                     ("LED_A", "D1", "2"), ("GND", "D1", "1")]
        for net, ref, pin in nets:
            self.wiring.call("add_schematic_net_label", {
                "schematicPath": str(sch), "netName": net,
                "componentRef": ref, "pinNumber": pin,
            })

        self.project.call("save_project", {"force": True})
        self.project.call("close_project", {"save": False})

        if not sch.exists():
            raise GenerationError(f"creator crew finished but {sch} missing")
        (out_dir / "designspec.json").write_text(
            spec.model_dump_json(indent=2), encoding="utf-8")
        return out_dir
