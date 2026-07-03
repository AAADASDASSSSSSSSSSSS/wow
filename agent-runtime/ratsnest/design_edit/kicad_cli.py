"""kicad-cli ERC wrapper — feature-gated: returns None when unavailable."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from ratsnest.config import Config
from ratsnest.kh_adapter.runner import find_root_schematic


def run_erc(project_dir: Path, config: Config | None = None) -> bool | None:
    """Run KiCad ERC. Returns True (pass), False (errors found), or None (n/a)."""
    config = config or Config.load()
    if not config.kicad_cli or not Path(config.kicad_cli).exists():
        return None
    sch = find_root_schematic(Path(project_dir))
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "erc.json"
        try:
            subprocess.run(
                [str(config.kicad_cli), "sch", "erc", "--format", "json",
                 "--output", str(out), "--severity-error", str(sch)],
                capture_output=True, text=True, timeout=120,
            )
            if not out.exists():
                return None
            report = json.loads(out.read_text(encoding="utf-8"))
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
            return None
    violations = 0
    for sheet in report.get("sheets", []):
        violations += len(sheet.get("violations", []))
    return violations == 0
