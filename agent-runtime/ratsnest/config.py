"""Runtime configuration, resolved from environment variables with local defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# agent-runtime/ratsnest/config.py -> repo root is two levels up from package dir
REPO_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_KICAD_CLI = Path(r"E:\KiCad\10.0\bin\kicad-cli.exe")


def _apply_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE defaults from .env (repo root). Real environment
    variables always win — docker-compose / the control plane inject vars
    that must never be overridden. utf-8-sig tolerates PowerShell BOMs."""
    path = path or REPO_ROOT / ".env"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


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
    llm_api_key: str | None = None
    llm_provider: str = "anthropic"  # anthropic|openai|deepseek|qwen|moonshot|zhipu|ollama
    llm_base_url: str = ""           # empty -> provider preset
    llm_model: str = ""              # empty -> provider preset
    llm_enabled: bool = True         # RATSNEST_LLM=off disables
    llm_required: bool = False       # RATSNEST_LLM=require forbids fallback

    @property
    def kicad_scripts(self) -> Path:
        return self.kicad_happy_root / "skills" / "kicad" / "scripts"

    @property
    def bom_scripts(self) -> Path:
        return self.kicad_happy_root / "skills" / "bom" / "scripts"

    @classmethod
    def load(cls) -> "Config":
        _apply_dotenv()
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
            llm_api_key=(os.environ.get("RATSNEST_LLM_API_KEY")
                         or os.environ.get("ANTHROPIC_API_KEY")
                         or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                         or os.environ.get("OPENAI_API_KEY")),
            llm_provider=os.environ.get(
                "RATSNEST_LLM_PROVIDER", "anthropic").lower(),
            llm_base_url=(os.environ.get("RATSNEST_LLM_BASE_URL")
                          or (os.environ.get("ANTHROPIC_BASE_URL")
                              if os.environ.get("RATSNEST_LLM_PROVIDER",
                                                "anthropic") == "anthropic"
                              else "")
                          or ""),
            llm_model=os.environ.get("RATSNEST_LLM_MODEL", ""),
            llm_enabled=os.environ.get("RATSNEST_LLM", "auto") != "off",
            llm_required=os.environ.get("RATSNEST_LLM", "auto") == "require",
        )


def _first_existing(*candidates) -> Path | None:
    for c in candidates:
        if c and Path(c).exists():
            return Path(c)
    return None
