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

from pathlib import Path

from ratsnest.agents.base import Agent, AgentError
from ratsnest.config import Config
from ratsnest.data_proxy import Recorder
from ratsnest.design_gen.generator import GenerationError, solve_board_values
from ratsnest.design_gen.templates import rail_name
from ratsnest.kicad_host import KicadHostError, get_host
from ratsnest.schemas import DesignSpec, StrategyBundle

# real KiCad 10 library symbol (verified present in Regulator_Linear.kicad_sym);
# AP1117 datasheet pinout: ADJ=1, VOUT=2, VIN=3 — and it is already the part
# the strategy's Vref table and MPN map are built around
REGULATOR_PART = "AP1117-ADJ"
REGULATOR_SYMBOL = "Regulator_Linear:AP1117-ADJ"


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
        try:
            host = get_host(self.config)
        except KicadHostError as exc:
            raise AgentError(str(exc)) from exc
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


class LayoutAgent(KiCadSkillAgent):
    name = "layout_agent"
    commands = ("sync_schematic_to_board", "set_board_size",
                "add_board_outline", "place_component", "suggest_placement",
                "move_component")


class RoutingAgent(KiCadSkillAgent):
    name = "routing_agent"
    commands = ("route_pad_to_pad",)


_FOREMAN_PROMPT = """You are the tool-use foreman of a KiCad schematic crew. \
You are given the solved component set (references, symbols, values) and the \
required net connections. You decide PLACEMENT GEOMETRY on an A4 schematic \
sheet: x in [70,180], y in [40,110] (mm). Spread components logically \
(power flow left to right, feedback below the regulator, LED chain on the \
right) with at least 12mm between component centers.

Return ONLY JSON: {"placements": [{"ref": str, "x": number, "y": number}...],
"rationale": str}. Include every reference exactly once. Do not add, remove,
rename or revalue components — electrical topology is fixed by contract."""


class CreatorCrew:
    def __init__(self, config: Config | None = None,
                 recorder: Recorder | None = None, iteration: int = 0,
                 llm=None):
        self.config = config or Config.load()
        self.llm = llm
        self.recorder = recorder
        self._step_n = 0
        common = dict(recorder=recorder, iteration=iteration)
        self.project = ProjectAgent(self.config, **common)
        self.symbols = SymbolAgent(self.config, **common)
        self.wiring = WiringAgent(self.config, **common)
        self.layout = LayoutAgent(self.config, **common)
        self.routing = RoutingAgent(self.config, **common)

    def _foreman_positions(self, placements: list) -> tuple[dict, str] | None:
        """Brain path: LLM proposes schematic positions. Contract-validated;
        None on any violation (caller keeps the deterministic layout)."""
        if self.llm is None or not self.llm.available:
            return None
        payload = {
            "components": [{"ref": p[0], "symbol": p[1], "value": p[2]}
                           for p in placements],
        }
        import json as _json
        raw = self.llm.complete_json("creator_foreman", _FOREMAN_PROMPT,
                                     _json.dumps(payload), max_tokens=800)
        if not raw or not isinstance(raw.get("placements"), list):
            return None
        want = {p[0] for p in placements}
        got: dict[str, tuple[float, float]] = {}
        for item in raw["placements"]:
            try:
                ref = str(item["ref"])
                x, y = float(item["x"]), float(item["y"])
            except (KeyError, TypeError, ValueError):
                return None
            if ref not in want or ref in got:
                return None
            if not (70 <= x <= 180 and 40 <= y <= 110):
                return None
            got[ref] = (x, y)
        if set(got) != want:
            return None
        return got, str(raw.get("rationale", ""))[:500]

    def _snapshot(self, out_dir: Path, label: str) -> None:
        """Timeline frame after an agent action: SVG of the current schematic
        state + an ATDP event the frontend renders as a step."""
        if self.recorder is None:
            return
        from ratsnest.preview import snapshot_schematic
        self._step_n += 1
        tag = f"step_{self._step_n:02d}_{label}"
        path = snapshot_schematic(out_dir, tag, self.config)
        self.recorder.emit(
            "creator.step", 0,
            action={"step": self._step_n, "label": label.replace('_', ' ')},
            outcome={"ok": True,
                     "preview": (f"preview/steps/{path.name}"
                                 if path else None)},
            metadata={"agent": "timeline", "crew": "creator"},
        )

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
        self._snapshot(out_dir, "create_project")

        placements = [
            ("J1", "Connector_Generic:Conn_01x02", "Conn_01x02", 75, 60),
            ("U1", REGULATOR_SYMBOL, values["U1"], 100, 60),
            ("R1", "Device:R", values["R1"], 130, 55),
            ("R2", "Device:R", values["R2"], 130, 80),
        ]
        if include_led:
            placements += [("R3", "Device:R", values["R3"], 155, 55),
                           ("D1", "Device:LED", values["D1"], 155, 80)]

        # brain: the foreman may re-plan schematic geometry (contract-checked;
        # topology, refs and values are immutable — LLM proposes, tools execute)
        foreman = self._foreman_positions(placements)
        if foreman is not None:
            positions, _rationale = foreman
            placements = [(ref, sym, val, *positions[ref])
                          for ref, sym, val, _x, _y in placements]

        footprint_map: dict = strategy.solver_params.get("footprint_map", {})
        for ref, symbol, value, x, y in placements:
            library, sym_type = symbol.split(":")
            self.symbols.call("add_schematic_component", {
                "schematicPath": str(sch),
                "component": {"library": library, "type": sym_type,
                              "reference": ref, "value": value,
                              "footprint": str(footprint_map.get(symbol, "")),
                              "x": x, "y": y,
                              "unit": 1, "angle": 0, "mirrorY": False},
            })
            self._snapshot(out_dir, f"place_{ref}")
        # the vendored placement handler drops the footprint field — stamp
        # footprints with OUR ops-only editor so board sync can import them
        from ratsnest.design_edit.sexp_edit import apply_property_updates
        fp_updates: dict[str, dict[str, str]] = {}
        for ref, symbol, *_ in placements:
            fp = str(footprint_map.get(symbol, ""))
            if fp:
                fp_updates[ref] = {"Footprint": fp}
        if fp_updates:
            new_text, _log = apply_property_updates(
                sch.read_text(encoding="utf-8"), fp_updates, self.config)
            sch.write_text(new_text, encoding="utf-8")

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
        self._snapshot(out_dir, "connect_nets")

        # project fp-lib-table so the vendored LibraryManager (and KiCad
        # itself) can resolve the stock footprint libraries we reference
        self._write_fp_lib_table(out_dir, footprint_map)

        # --- LayoutAgent: schematic -> board (F8), outline, placement --------
        board = out_dir / f"{name}.kicad_pcb"
        outline: dict = strategy.solver_params.get(
            "board_outline", {"width": 50, "height": 35})
        routed = 0
        try:
            self.layout.call("sync_schematic_to_board", {
                "schematicPath": str(sch), "boardPath": str(board)})
            self.layout.call("set_board_size", {
                "width": float(outline.get("width", 50)),
                "height": float(outline.get("height", 35)), "unit": "mm"})
            # deterministic row placement, width-aware so courtyards clear
            # (PM-001/PM-002 taught us: edge margins + per-package widths)
            width_by_lib = {"Package_TO_SOT_SMD": 9.0,
                            "Connector_PinHeader_2.54mm": 7.0}
            cursor = 8.0
            prev_half = 0.0
            for ref, symbol, *_rest in placements:
                fp = str(footprint_map.get(symbol, ""))
                half = width_by_lib.get(fp.split(":", 1)[0], 4.0) / 2
                cursor += prev_half + half + 1.5
                prev_half = half
                try:
                    self.layout.call("move_component", {
                        "reference": ref,
                        "position": {"x": round(cursor, 2), "y": 15.0,
                                     "unit": "mm"}})
                except AgentError:
                    pass
            # --- RoutingAgent: chain-route every net's pins -------------------
            by_net: dict[str, list[tuple[str, str]]] = {}
            for net, ref, pin in nets:
                by_net.setdefault(net, []).append((ref, pin))
            for net, pins in by_net.items():
                for (r1, p1), (r2, p2) in zip(pins, pins[1:]):
                    try:
                        self.routing.call("route_pad_to_pad", {
                            "fromRef": r1, "fromPad": p1,
                            "toRef": r2, "toPad": p2})
                        routed += 1
                    except AgentError:
                        pass  # partial routing is acceptable in gen v1
        except AgentError:
            pass  # board work is best-effort; the schematic is authoritative

        self.project.call("save_project", {"force": True})
        self.project.call("close_project", {"save": False})
        self._snapshot(out_dir, "board_complete")

        if not sch.exists():
            raise GenerationError(f"creator crew finished but {sch} missing")
        (out_dir / "designspec.json").write_text(
            spec.model_dump_json(indent=2), encoding="utf-8")
        return out_dir

    def _write_fp_lib_table(self, out_dir: Path,
                            footprint_map: dict) -> None:
        if not self.config.kicad_python:
            return
        fp_root = (Path(self.config.kicad_python).parent.parent
                   / "share" / "kicad" / "footprints")
        libs = sorted({str(fp).split(":", 1)[0]
                       for fp in footprint_map.values() if ":" in str(fp)})
        rows = []
        for lib in libs:
            pretty = fp_root / f"{lib}.pretty"
            if pretty.exists():
                uri = str(pretty).replace("\\", "/")
                rows.append(f'  (lib (name "{lib}")(type "KiCad")'
                            f'(uri "{uri}")(options "")(descr ""))')
        if rows:
            (out_dir / "fp-lib-table").write_text(
                "(fp_lib_table\n  (version 7)\n" + "\n".join(rows) + "\n)\n",
                encoding="utf-8")
