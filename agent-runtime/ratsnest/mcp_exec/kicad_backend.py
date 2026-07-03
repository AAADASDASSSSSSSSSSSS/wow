"""KiCad MCP execution backend: sub-agents that create designs in real KiCad.

Where the template backend writes S-expressions itself, this backend drives
the vendored KiCAD-MCP-Server (github.com/mixelpixx/KiCAD-MCP-Server, 122
tools over SWIG/IPC) so components come from KiCad's real symbol libraries.
Electrical values still come from `solve_board_values` — one evolvable
strategy governs both creation paths, and kicad-happy remains the judge of
whatever gets created.

Sub-agent split (design doc roster):
  ProjectAgent    create_project / save_project / close_project
  SchematicAgent  place components + bind pins to nets via pin-snapped labels
"""

from __future__ import annotations

from pathlib import Path

from ratsnest.config import Config
from ratsnest.data_proxy import Recorder
from ratsnest.design_gen.generator import GenerationError, solve_board_values
from ratsnest.design_gen.templates import rail_name
from ratsnest.mcp_exec.client import McpClient
from ratsnest.schemas import DesignSpec, StrategyBundle

# real KiCad library part (stock symbol libs): adjustable 1.25V regulator —
# present in the strategy vref_table, so the synthesizer can verify its divider
MCP_REGULATOR_PART = "LM317"
MCP_REGULATOR_SYMBOL = "Regulator_Linear:LM317_3PinPackage"
# LM317 TO-220: pin 1 = ADJ, pin 2 = VOUT, pin 3 = VIN


class KiCadMcpBackend:
    def __init__(self, config: Config | None = None,
                 recorder: Recorder | None = None):
        self.config = config or Config.load()
        self.recorder = recorder
        if not self.config.mcp_server_dir:
            raise GenerationError(
                "KiCAD-MCP-Server not found (set RATSNEST_MCP_SERVER)")
        self.dist = self.config.mcp_server_dir / "dist" / "index.js"
        if not self.dist.exists():
            raise GenerationError(
                f"{self.dist} missing — build the server with `npm run build`")

    def _client(self) -> McpClient:
        env = {"KICAD_AUTO_LAUNCH": "false", "NODE_ENV": "production"}
        if self.config.kicad_python:
            env["KICAD_PYTHON"] = str(self.config.kicad_python)
        return McpClient(["node", str(self.dist)],
                         cwd=self.config.mcp_server_dir, env=env,
                         recorder=self.recorder)

    def generate(self, spec: DesignSpec, out_dir: Path,
                 strategy: StrategyBundle) -> Path:
        out_dir = Path(out_dir).resolve()
        values, mpns, include_led = solve_board_values(
            spec, strategy, self.config, regulator_part=MCP_REGULATOR_PART)

        vin, vout = rail_name(spec.input_voltage), rail_name(spec.output_voltage)
        # the server creates <path>/<name>.kicad_sch (no subdirectory)
        name = out_dir.name
        sch_path = out_dir / f"{name}.kicad_sch"
        out_dir.mkdir(parents=True, exist_ok=True)

        with self._client() as mcp:
            # --- ProjectAgent -------------------------------------------------
            mcp.call_tool("create_project", {
                "name": name,
                "path": str(out_dir),
            })

            # --- SchematicAgent: place from real KiCad libraries ---------------
            placements = [
                ("U1", MCP_REGULATOR_SYMBOL, values["U1"], 100, 60),
                ("R1", "Device:R", values["R1"], 130, 55),
                ("R2", "Device:R", values["R2"], 130, 80),
            ]
            if include_led:
                placements += [
                    ("R3", "Device:R", values["R3"], 155, 55),
                    ("D1", "Device:LED", values["D1"], 155, 80),
                ]
            for ref, symbol, value, x, y in placements:
                mcp.call_tool("add_schematic_component", {
                    "schematicPath": str(sch_path),
                    "symbol": symbol, "reference": ref, "value": value,
                    "position": {"x": x, "y": y},
                })

            # --- SchematicAgent: connectivity via pin-snapped net labels -------
            # (componentRef+pinNumber guarantees the electrical connection)
            nets: list[tuple[str, str, str]] = [
                (vin, "U1", "3"),
                (vout, "U1", "2"), (vout, "R1", "1"),
                ("FB", "U1", "1"), ("FB", "R1", "2"), ("FB", "R2", "1"),
                ("GND", "R2", "2"),
            ]
            if include_led:
                nets += [
                    (vout, "R3", "1"),
                    ("LED_A", "R3", "2"), ("LED_A", "D1", "2"),
                    ("GND", "D1", "1"),
                ]
            for net, ref, pin in nets:
                mcp.call_tool("add_schematic_net_label", {
                    "schematicPath": str(sch_path),
                    "netName": net,
                    "componentRef": ref,
                    "pinNumber": pin,
                })

            # --- ProjectAgent: persist and release ------------------------------
            mcp.call_tool("save_project", {"force": True})
            mcp.call_tool("close_project", {"save": False})

        if not sch_path.exists():
            raise GenerationError(
                f"MCP run finished but {sch_path} was not created")
        (out_dir / "designspec.json").write_text(
            spec.model_dump_json(indent=2), encoding="utf-8")
        return out_dir
