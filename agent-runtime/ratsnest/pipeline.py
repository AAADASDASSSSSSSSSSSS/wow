"""Shared design pipeline — one code path for the CLI and the Kafka worker.

generate_for_backend()  requirement -> DesignSpec -> KiCad project via the
                        selected backend (template | crew | mcp)
finalize_outputs()      after the repair loop: headless SVG previews, the
                        markdown report, and the release zip

Keeping this in one place means `python -m ratsnest design ... --backend X`
and a queued cluster job produce byte-identical deliverables.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ratsnest.config import Config
from ratsnest.data_proxy import Recorder
from ratsnest.design_gen import generate_project, parse_requirement
from ratsnest.preview import generate_previews
from ratsnest.reporting import write_report
from ratsnest.schemas import DesignSpec, EvaluationResult, RunRecord, StrategyBundle

VALID_BACKENDS = ("template", "crew", "mcp")


def generate_for_backend(requirement: str, out_dir: Path, backend: str,
                         strategy: StrategyBundle, config: Config,
                         recorder: Recorder | None = None) -> DesignSpec:
    """Parse the requirement and build the project with the chosen backend."""
    backend = (backend or "template").lower()
    if backend not in VALID_BACKENDS:
        raise ValueError(f"backend must be one of {VALID_BACKENDS}, got {backend!r}")
    spec = parse_requirement(requirement)
    out_dir = Path(out_dir)

    if backend == "crew":
        from ratsnest.crews import CreatorCrew
        CreatorCrew(config, recorder).generate(spec, out_dir, strategy)
    elif backend == "mcp":
        from ratsnest.mcp_exec import KiCadMcpBackend
        KiCadMcpBackend(config, recorder).generate(spec, out_dir, strategy)
    else:
        generate_project(spec, out_dir, strategy, config)
    return spec


def finalize_outputs(project_dir: Path, evaluation: EvaluationResult,
                     record: RunRecord | None, spec: DesignSpec | None,
                     config: Config) -> dict[str, Path]:
    """Produce the end-of-process deliverables. Returns paths that exist."""
    project_dir = Path(project_dir)
    out: dict[str, Path] = {}

    previews = generate_previews(project_dir, config)   # feature-gated
    out.update({f"preview_{k}": v for k, v in previews.items()})

    report = write_report(project_dir / "ratsnest_report.md",
                          evaluation, record, spec)
    out["report"] = report

    stem = (spec.project_name if spec else project_dir.name)
    release = Path(shutil.make_archive(
        str(project_dir.parent / f"{stem}_release"), "zip", project_dir))
    out["release"] = release
    return out
