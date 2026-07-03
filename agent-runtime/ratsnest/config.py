"""Runtime configuration, resolved from environment variables with local defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# agent-runtime/ratsnest/config.py -> repo root is two levels up from package dir
REPO_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_KICAD_CLI = Path(r"E:\KiCad\10.0\bin\kicad-cli.exe")


@dataclass
class Config:
    kicad_happy_root: Path
    kicad_cli: Path | None
    runs_dir: Path
    strategies_dir: Path
    benchmarks_dir: Path
    control_plane_url: str | None
    mcp_server_dir: Path | None = None
    kicad_python: Path | None = None

    @property
    def kicad_scripts(self) -> Path:
        return self.kicad_happy_root / "skills" / "kicad" / "scripts"

    @property
    def bom_scripts(self) -> Path:
        return self.kicad_happy_root / "skills" / "bom" / "scripts"

    @classmethod
    def load(cls) -> "Config":
        kh_root = Path(
            os.environ.get(
                "RATSNEST_KICAD_HAPPY_ROOT",
                str(REPO_ROOT.parent / "kicad-happy-main"),
            )
        )
        cli_env = os.environ.get("RATSNEST_KICAD_CLI")
        if cli_env:
            kicad_cli: Path | None = Path(cli_env)
        elif _DEFAULT_KICAD_CLI.exists():
            kicad_cli = _DEFAULT_KICAD_CLI
        else:
            kicad_cli = None
        return cls(
            kicad_happy_root=kh_root,
            kicad_cli=kicad_cli,
            runs_dir=Path(os.environ.get("RATSNEST_RUNS_DIR", str(REPO_ROOT / "runs"))),
            strategies_dir=Path(
                os.environ.get(
                    "RATSNEST_STRATEGIES_DIR",
                    str(REPO_ROOT / "agent-runtime" / "strategies"),
                )
            ),
            benchmarks_dir=Path(
                os.environ.get("RATSNEST_BENCHMARKS_DIR", str(REPO_ROOT / "benchmarks"))
            ),
            control_plane_url=os.environ.get("RATSNEST_CONTROL_PLANE_URL"),
            mcp_server_dir=_first_existing(
                os.environ.get("RATSNEST_MCP_SERVER"),
                REPO_ROOT.parent / "KiCAD-MCP-Server-main",
            ),
            kicad_python=_first_existing(
                os.environ.get("RATSNEST_KICAD_PYTHON"),
                Path(r"E:\KiCad\10.0\bin\python.exe"),
            ),
        )


def _first_existing(*candidates) -> Path | None:
    for c in candidates:
        if c and Path(c).exists():
            return Path(c)
    return None
