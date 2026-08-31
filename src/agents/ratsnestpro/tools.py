"""Typed tools that expose RatsNestPro workflows to LangGraph agents."""

from __future__ import annotations

import difflib
import importlib.util
import json
import os
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError
from ratsnestpro.agents import Architect, LlmError, LlmMode, parse_mode
from ratsnestpro.eda import grounding, symbols
from ratsnestpro.eda.adapter import kicad_cli_available, run_erc
from ratsnestpro.families import Atmega328Params, expectations_for
from ratsnestpro.orchestration import generate_design, review_project, run_repair
from ratsnestpro.orchestration.generate import build_design_plan
from ratsnestpro.orchestration.pipeline import (
    PIPELINE_TOTAL_STEPS,
    Pipeline,
    PipelineContext,
    PipelineState,
    PipelineStep,
    _mcu_models,
    restore_pipeline_state,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    ComponentPrepareResult,
    RouteResult,
    SelectionPlan,
)
from ratsnestpro.orchestration.review_project import ReviewProjectError
from ratsnestpro.parts import PartConstraint, PartSelector, ProcurementContext

from agents.ratsnestpro.capability import (
    normalize_order_code,
    order_code_family,
    order_code_matches,
    package_pin_count,
)

LlmModeName = Literal["offline", "auto", "required"]

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_.-]+")


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _pipeline_steps(state: PipelineState) -> list[dict[str, Any]]:
    return [
        {
            "name": result.step.value,
            "blocked": result.blocked,
            "used_llm": result.used_llm,
            "summary": result.summary,
            # Deterministic corrections the step made on its own. Carried so
            # attribution can tell them apart from a repair attempt's effect.
            "auto_fixes": list(result.auto_fixes),
            "failed_checks": [
                {
                    "name": check.name,
                    "message": check.message,
                    # Declared by the engine. ``failure_class`` comes from the
                    # check-class table and ``targets`` from the check itself, so
                    # the diagnosis layer does not have to re-derive either from
                    # the message prose. Both may be absent or empty, and the
                    # inference fallback still applies then.
                    "failure_class": check.failure_class or "",
                    "targets": list(check.targets),
                }
                for check in result.error_checks
            ],
            "warnings": [
                {"name": check.name, "message": check.message}
                for check in result.checks
                if not check.ok and check.severity.value == "warning"
            ],
        }
        for result in state.results
    ]


def _write_pipeline_state(
    path: Path,
    requirement: str,
    state: PipelineState,
) -> None:
    payload = {
        "requirement": requirement,
        "project_name": state.project_name,
        "completed_steps": len(state.results),
        "steps": _pipeline_steps(state),
        "intermediate_artifacts": {
            step.value: artifact.model_dump(mode="json")
            for step, artifact in state.artifacts.items()
        },
    }
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(_json(payload), encoding="utf-8")
    temporary.replace(path)


def _announce_pipeline_step(state: PipelineState, step: PipelineStep) -> None:
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except RuntimeError:
        return
    writer(
        {
            "kind": "workflow_event",
            "phase": f"pipeline:{getattr(step, 'value', step)}",
            "status": "started",
            "completed_steps": len(state.results),
            "total_steps": PIPELINE_TOTAL_STEPS,
        }
    )


def _checkpoint_pipeline_step(
    path: Path,
    requirement: str,
    state: PipelineState,
    result: Any,
) -> None:
    _write_pipeline_state(path, requirement, state)
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except RuntimeError:
        return
    writer(
        {
            "kind": "workflow_event",
            "phase": f"pipeline:{result.step.value}",
            "status": "blocked" if result.blocked else "completed",
            "detail": result.summary,
            "completed_steps": len(state.results),
            "total_steps": PIPELINE_TOTAL_STEPS,
        }
    )


def _apply_selection_actions(
    payload: dict[str, Any],
    remove_refs: list[str],
) -> dict[str, Any]:
    """Apply typed repair removals to the checkpointed SelectionPlan.

    G3: a repair must change the design state, not only re-prompt. Reuses the
    pipeline's own ``_apply_selection_patch`` so its uniqueness and contract
    validation still decide what is acceptable. Parts are never fabricated here:
    only refs the diagnosis named are removed, and the selection step then
    re-selects grounded replacements.
    """
    if not remove_refs:
        return payload
    artifacts = payload.get("intermediate_artifacts")
    if not isinstance(artifacts, dict) or "selection" not in artifacts:
        return payload
    try:
        from ratsnestpro.orchestration.pipeline import _apply_selection_patch
        from ratsnestpro.orchestration.pipeline_contracts import SelectionPatch

        plan = SelectionPlan.model_validate(artifacts["selection"])
        patched = _apply_selection_patch(
            plan,
            SelectionPatch(
                remove_refs=list(dict.fromkeys(remove_refs)),
                rationale="AHE repair: drop parts named by the failure diagnosis",
            ),
        )
    except (ImportError, ValidationError, ValueError, TypeError):
        return payload
    updated = dict(artifacts)
    updated["selection"] = patched.model_dump(mode="json")
    return {**payload, "intermediate_artifacts": updated}


def _truncate_checkpoint(
    payload: dict[str, Any],
    resume_from: str,
) -> dict[str, Any]:
    """Drop checkpointed artifacts from ``resume_from`` onward.

    AHE repairs re-enter the pipeline at the failing step: everything before it
    stays valid, so only that step and its downstream results are discarded. An
    unknown step name leaves the checkpoint untouched rather than silently
    restarting the whole flow.
    """
    order = [step.value for step in PipelineStep]
    if resume_from not in order:
        return payload
    cutoff = order.index(resume_from)
    keep = set(order[:cutoff])
    steps = [
        step
        for step in payload.get("steps", [])
        if isinstance(step, dict) and str(step.get("name", "")) in keep
    ]
    artifacts = {
        name: artifact
        for name, artifact in (payload.get("intermediate_artifacts") or {}).items()
        if name in keep
    }
    return {**payload, "steps": steps, "intermediate_artifacts": artifacts}


def _load_pipeline_state(
    path: Path,
    requirement: str,
    project_name: str,
    resume_from: str = "",
    drop_refs: list[str] | None = None,
) -> PipelineState:
    payload = json.loads(path.read_text(encoding="utf-8"))
    saved_requirement = str(payload.get("requirement", ""))
    saved_project = str(payload.get("project_name", ""))
    # An explicit resume is a deliberate repair of the same board, and its
    # requirement text carries the appended review/diagnosis feedback, so only
    # the project identity is enforced there. Without resume_from a differing
    # requirement still means the run name was reused for another design.
    if saved_project != project_name or (not resume_from and saved_requirement != requirement):
        raise ValueError(
            "run_name already has a checkpoint for a different requirement or "
            "project_name; choose a new run_name"
        )
    if resume_from:
        payload = _truncate_checkpoint(payload, resume_from)
    payload = _apply_selection_actions(payload, list(drop_refs or []))
    artifacts = payload.get("intermediate_artifacts")
    steps = payload.get("steps")
    if not isinstance(artifacts, dict) or not isinstance(steps, list):
        raise TypeError("pipeline checkpoint is missing artifacts or step history")
    return restore_pipeline_state(
        requirement_text=requirement,
        project_name=project_name,
        intermediate_artifacts=artifacts,
        steps=steps,
    )


def _workspace_root() -> Path:
    root = Path(os.getenv("RATSNESTPRO_WORKSPACE_ROOT", "data/ratsnestpro"))
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _name(value: str, fallback: str) -> str:
    cleaned = _SAFE_NAME.sub("-", value.strip()).strip(".-")
    return cleaned[:80] or fallback


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class _ToolkitLlmClient:
    """Adapt the toolkit's configured chat model to RatsNestPro's text client."""

    def __init__(self) -> None:
        from core import get_model, settings

        self._model = get_model(settings.DEFAULT_MODEL)

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        response = self._model.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        if isinstance(response.content, str):
            return response.content
        return "".join(
            str(block.get("text", "")) for block in response.content if isinstance(block, dict)
        )


def _positive_mcu_models(requirement: str) -> set[str]:
    return _mcu_models(requirement)


def _is_atmega328_only(requirement: str) -> bool:
    models = _positive_mcu_models(requirement)
    return bool(models) and all(model.startswith("atmega328") for model in models)


def _pipeline_mode(requirement: str, requested: LlmMode) -> LlmMode:
    """Use the deterministic template only for the family it actually models."""
    if _is_atmega328_only(requirement):
        return requested
    return LlmMode.REQUIRED


def _pipeline_client(mode: LlmMode) -> object | None:
    """Pick the chat client for a pipeline run.

    A full pipeline run outlives a single short-lived credential. RatsNestPro's
    own EricAI client renews its SSO token per call, whereas the toolkit's
    LangChain model is constructed with a fixed API key, so a long run against
    the EricAI gateway fails midway with ``Unauthorized``. Prefer the native
    client when EricAI is the configured backend; every other provider keeps
    using the toolkit adapter.
    """
    if mode == LlmMode.OFFLINE:
        return None
    if _use_native_ericai_client():
        from ratsnestpro.agents.llm import EricAIClient

        return EricAIClient()
    return _ToolkitLlmClient()


def _use_native_ericai_client() -> bool:
    override = os.getenv("RATSNESTPRO_LLM_CLIENT", "").strip().lower()
    if override:
        return override == "ericai"
    try:
        from core import settings

        base_url = str(settings.COMPATIBLE_BASE_URL or "")
    except Exception:
        return False
    if "ray.sero.gic.ericsson.se" not in base_url:
        return False
    return importlib.util.find_spec("ericai") is not None


def _run_dir(run_name: str) -> Path:
    return _workspace_root() / "runs" / _name(run_name, "design")


@contextmanager
def _serialize_pipeline_run(run_dir: Path) -> Iterator[None]:
    """Lock one run directory across threads and service processes."""
    lock_dir = run_dir.parent / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{run_dir.name}.lock"
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _workspace_path(value: str) -> Path:
    root = _workspace_root()
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path must stay inside the RatsNestPro workspace: {root}") from exc
    return candidate


def _overrides(
    crystal_mhz: int | None,
    ldo_output_v: float | None,
    decoupling_count: int | None,
    power_led: bool | None,
    breakout_rows: int | None,
    breakout_pins_per_row: int | None,
    mounting_holes: int | None,
) -> dict[str, object]:
    values = {
        "crystal_mhz": crystal_mhz,
        "ldo_output_v": ldo_output_v,
        "decoupling_count": decoupling_count,
        "power_led": power_led,
        "breakout_rows": breakout_rows,
        "breakout_pins_per_row": breakout_pins_per_row,
        "mounting_holes": mounting_holes,
    }
    return {key: value for key, value in values.items() if value is not None}


def _resolve_params(
    requirement: str,
    llm_mode: LlmModeName,
    overrides: dict[str, object],
) -> tuple[Atmega328Params | None, dict[str, Any]]:
    result = Architect().plan(requirement, mode=parse_mode(llm_mode))
    if not result.decision.qualified:
        return None, {
            "status": "needs_clarification",
            "reason": result.decision.rationale,
            "questions": result.decision.clarifying_questions,
        }
    if result.params is None and not overrides:
        return None, {
            "status": "needs_clarification",
            "reason": result.decision.rationale,
            "questions": result.decision.clarifying_questions,
        }
    values = result.params.model_dump() if result.params else {}
    try:
        params = Atmega328Params(**{**values, **overrides})  # type: ignore[arg-type]
    except ValidationError as exc:
        return None, {
            "status": "needs_clarification",
            "reason": "The selected parameters violate the board-family contract.",
            "questions": [error["msg"] for error in exc.errors()],
        }
    return params, {
        "architect_source": result.source,
        "architect_rationale": result.decision.rationale,
    }


def _files(path: Path) -> list[str]:
    root = _workspace_root()
    return [
        str(file.resolve().relative_to(root)) for file in sorted(path.rglob("*")) if file.is_file()
    ]


def _drc_check(pcb_path: Path | None) -> dict[str, Any]:
    if pcb_path is None or not pcb_path.is_file():
        return {
            "applicable": False,
            "available": False,
            "ran": False,
            "errors": None,
            "warnings": None,
            "unconnected": None,
            "report_path": None,
            "by_type": {},
        }
    cli = kicad_cli_available()
    if cli is None:
        return {
            "applicable": True,
            "available": False,
            "ran": False,
            "errors": None,
            "warnings": None,
            "unconnected": None,
            "report_path": None,
            "by_type": {},
        }

    report = pcb_path.with_suffix(".drc.json")
    try:
        subprocess.run(
            [
                cli,
                "pcb",
                "drc",
                "--format",
                "json",
                "--severity-all",
                "--output",
                str(report),
                "--exit-code-violations",
                str(pcb_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, subprocess.SubprocessError, ValueError):
        return {
            "applicable": True,
            "available": True,
            "ran": False,
            "errors": None,
            "warnings": None,
            "unconnected": None,
            "report_path": str(report) if report.is_file() else None,
            "by_type": {},
        }

    violations = data.get("violations", []) if isinstance(data, dict) else []
    parity = data.get("schematic_parity", []) if isinstance(data, dict) else []
    unconnected_items = data.get("unconnected_items", []) if isinstance(data, dict) else []
    findings = [
        item for item in [*violations, *parity, *unconnected_items] if isinstance(item, dict)
    ]
    by_type: dict[str, int] = {}
    for finding in findings:
        finding_type = str(finding.get("type", "unknown"))
        by_type[finding_type] = by_type.get(finding_type, 0) + 1
    return {
        "applicable": True,
        "available": True,
        "ran": True,
        "errors": sum(1 for item in findings if str(item.get("severity", "error")) == "error"),
        "warnings": sum(1 for item in findings if str(item.get("severity", "")) == "warning"),
        "unconnected": len(unconnected_items),
        "report_path": str(report),
        "by_type": by_type,
    }


def _erc_check(sch_path: Path | None) -> dict[str, Any]:
    if sch_path is None or not sch_path.is_file():
        return {
            "applicable": False,
            "available": False,
            "ran": False,
            "errors": None,
            "warnings": None,
            "report_path": None,
            "by_type": {},
        }
    result = run_erc(sch_path)
    by_type: dict[str, int] = {}
    for violation in result.violations:
        by_type[violation.rule_id] = by_type.get(violation.rule_id, 0) + 1
    return {
        "applicable": True,
        "available": result.available,
        "ran": result.ran,
        "errors": result.error_count if result.ran else None,
        "warnings": result.warning_count if result.ran else None,
        "report_path": result.report_path,
        "by_type": by_type,
    }


def _verification(
    sch_path: Path | None,
    pcb_path: Path | None,
) -> dict[str, Any]:
    return {
        "erc": _erc_check(sch_path),
        "drc": _drc_check(pcb_path),
    }


def _paired_project_files(
    project: Path,
    sch_path: Path | None,
    pcb_path: Path | None,
) -> tuple[Path | None, Path | None]:
    schematic = sch_path if sch_path and sch_path.is_file() else None
    board = pcb_path if pcb_path and pcb_path.is_file() else None
    if project.is_dir():
        if schematic is None:
            schematic = next(iter(sorted(project.glob("*.kicad_sch"))), None)
        if board is None:
            board = next(iter(sorted(project.glob("*.kicad_pcb"))), None)
    else:
        if schematic is None:
            candidate = project.with_suffix(".kicad_sch")
            schematic = candidate if candidate.is_file() else None
        if board is None:
            candidate = project.with_suffix(".kicad_pcb")
            board = candidate if candidate.is_file() else None
    return schematic, board


def _verification_blockers(verification: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    for label in ("erc", "drc"):
        result = verification.get(label, {})
        if not result.get("applicable"):
            continue
        display = label.upper()
        if not result.get("available"):
            blockers.append(f"kicad-cli {display} unavailable")
        elif not result.get("ran"):
            blockers.append(f"kicad-cli {display} did not run")
        elif result.get("errors") != 0:
            blockers.append(f"kicad-cli {display} reported {result.get('errors')} error(s)")
    drc = verification.get("drc", {})
    if drc.get("applicable") and drc.get("unconnected") != 0:
        blockers.append(f"kicad-cli DRC reported {drc.get('unconnected')} unconnected item(s)")
    return blockers


def _verification_markdown(verification: dict[str, Any]) -> str:
    erc = verification["erc"]
    drc = verification["drc"]
    return "\n".join(
        [
            "## Independent kicad-cli verification",
            "",
            (
                f"- ERC: ran={erc['ran']}, errors={erc['errors']}, "
                f"warnings={erc['warnings']}, report=`{erc['report_path']}`"
            ),
            (
                f"- DRC: ran={drc['ran']}, errors={drc['errors']}, "
                f"warnings={drc['warnings']}, unconnected={drc['unconnected']}, "
                f"report=`{drc['report_path']}`"
            ),
        ]
    )


def ratsnest_create_design_plan(
    requirement: str,
    run_name: str = "design-plan",
    project_name: str = "atmega328_dev_board",
    llm_mode: LlmModeName = "offline",
    crystal_mhz: int | None = None,
    ldo_output_v: float | None = None,
    decoupling_count: int | None = None,
    power_led: bool | None = None,
    breakout_rows: int | None = None,
    breakout_pins_per_row: int | None = None,
    mounting_holes: int | None = None,
) -> str:
    """Create the immutable ATmega328 DesignPlan without generating KiCad files.

    Use this for design planning or parameter review. Explicit parameters
    override values inferred by the Architect. llm_mode controls only the
    embedded EricAI path; offline is deterministic and needs no EricAI install.
    """
    try:
        if not _is_atmega328_only(requirement):
            return _json(
                {
                    "status": "use_generic_pipeline",
                    "reason": (
                        "This tool is the ATmega328 offline template only; the requested "
                        "family must use the adaptive pipeline."
                    ),
                    "next_tool": "ratsnest_run_pcb_pipeline",
                    "required_llm_mode": "required",
                }
            )
        params, context = _resolve_params(
            requirement,
            llm_mode,
            _overrides(
                crystal_mhz,
                ldo_output_v,
                decoupling_count,
                power_led,
                breakout_rows,
                breakout_pins_per_row,
                mounting_holes,
            ),
        )
        if params is None:
            return _json(context)
        out = _run_dir(run_name)
        out.mkdir(parents=True, exist_ok=True)
        plan = build_design_plan(requirement, params, _name(project_name, "atmega328_dev_board"))
        plan_path = out / "plan.json"
        plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        return _json(
            {
                "status": "ok",
                **context,
                "workspace": str(_workspace_root()),
                "run_directory": str(out),
                "plan_path": str(plan_path),
                "family": plan.circuit.family,
                "components": len(plan.circuit.components),
                "nets": len(plan.circuit.nets),
                "params": params.model_dump(),
            }
        )
    except LlmError as exc:
        return _json({"status": "error", "error": str(exc), "llm_mode": llm_mode})


def ratsnest_generate_schematic(
    requirement: str,
    run_name: str = "schematic",
    project_name: str = "atmega328_dev_board",
    llm_mode: LlmModeName = "offline",
    run_erc: bool = True,
    repair: bool = True,
    max_repair_iterations: int = 5,
    crystal_mhz: int | None = None,
    ldo_output_v: float | None = None,
    decoupling_count: int | None = None,
    power_led: bool | None = None,
    breakout_rows: int | None = None,
    breakout_pins_per_row: int | None = None,
    mounting_holes: int | None = None,
) -> str:
    """Generate and verify an ATmega328 schematic, optionally repairing failures.

    This preserves RatsNestPro pipeline A: Architect planning, typed generation,
    deterministic gates, optional KiCad ERC, and the whitelisted Coder repair
    loop. Generated artifacts stay inside the configured workspace.
    """
    try:
        if not _is_atmega328_only(requirement):
            return _json(
                {
                    "status": "use_generic_pipeline",
                    "reason": (
                        "This tool is the ATmega328 schematic template only; the requested "
                        "family must use the adaptive pipeline."
                    ),
                    "next_tool": "ratsnest_run_pcb_pipeline",
                    "required_llm_mode": "required",
                }
            )
        params, context = _resolve_params(
            requirement,
            llm_mode,
            _overrides(
                crystal_mhz,
                ldo_output_v,
                decoupling_count,
                power_led,
                breakout_rows,
                breakout_pins_per_row,
                mounting_holes,
            ),
        )
        if params is None:
            return _json(context)
        out = _run_dir(run_name)
        project = _name(project_name, "atmega328_dev_board")
        result = generate_design(
            requirement,
            params=params,
            out_dir=out,
            project_name=project,
            run_erc=run_erc,
        )
        repair_summary: dict[str, Any] | None = None
        if result.blocked and repair:
            repaired = run_repair(
                params,
                expectations_for(params),
                max_iter=max(1, min(max_repair_iterations, 10)),
                mode=parse_mode(llm_mode),
            )
            repair_summary = {
                "success": repaired.success,
                "iterations": repaired.iterations,
                "reason": repaired.reason,
            }
            if repaired.success:
                result = generate_design(
                    requirement,
                    params=repaired.params,
                    out_dir=out,
                    project_name=project,
                    run_erc=run_erc,
                )
        return _json(
            {
                "status": "blocked" if result.blocked else "ok",
                **context,
                "summary": result.summary,
                "workspace": str(_workspace_root()),
                "run_directory": str(out),
                "artifacts": _files(out),
                "gates": [
                    {
                        "name": gate.gate,
                        "status": gate.status.value,
                        "required": gate.required,
                    }
                    for gate in result.report.gates
                ],
                "repair": repair_summary,
            }
        )
    except (LlmError, ValidationError, ValueError) as exc:
        return _json({"status": "error", "error": str(exc)})


def _run_pcb_pipeline_unlocked(
    requirement: str,
    run_name: str = "pcb",
    project_name: str = "board",
    llm_mode: LlmModeName = "auto",
    resume_from: str = "",
    drop_refs: list[str] | None = None,
) -> str:
    """Run RatsNestPro pipeline B, the fixed schematic-to-manufacture flow.

    The flow stops at the first blocking bottom-line check. It can generate a
    schematic, PCB, BOM, CPL and Gerbers. In the Docker deployment,
    Freerouting is required: missing or incomplete routing blocks success.
    ``resume_from`` re-enters an existing checkpoint at that step, keeping the
    artifacts produced before it.
    """
    try:
        requested_mode = parse_mode(llm_mode)
        mode = _pipeline_mode(requirement, requested_mode)
        client = _pipeline_client(mode)
        out = _run_dir(run_name)
        out.mkdir(parents=True, exist_ok=True)
        state_path = out / "pipeline_state.json"
        project = _name(project_name, "board")
        state = (
            _load_pipeline_state(state_path, requirement, project, resume_from, drop_refs)
            if state_path.is_file()
            else PipelineState(
                requirement_text=requirement,
                project_name=project,
            )
        )
        resumed_steps = len(state.results)
        require_freerouting = _env_flag(
            "RATSNESTPRO_REQUIRE_FREEROUTING",
            default=True,
        )
        Pipeline().run(
            state,
            PipelineContext(
                mode=mode,
                client=client,
                out_dir=str(out),
                # Complex boards often need several incremental JSON patches.
                # Attempts are used only after deterministic ERROR checks fail.
                repair_attempts=5,
                require_freerouting=require_freerouting,
                continue_on_blocked=_env_flag("RATSNESTPRO_CONTINUE_ON_BLOCKED"),
                capture_step_errors=True,
                on_step_completed=lambda current, result: _checkpoint_pipeline_step(
                    state_path, requirement, current, result
                ),
                on_step_started=lambda current, step: _announce_pipeline_step(current, step),
            ),
        )
        route_artifact = state.artifact(PipelineStep.ROUTE_SIGNALS)
        selection_artifact = state.artifact(PipelineStep.SELECTION)
        preparation_artifact = state.artifact(PipelineStep.COMPONENT_PREPARE)
        routing = (
            route_artifact.model_dump()
            if isinstance(route_artifact, RouteResult)
            else {
                "required": require_freerouting,
                "method": "not_reached",
                "note": "pipeline stopped before signal routing",
            }
        )
        mcu_parts = []
        selected_roles: list[str] = []
        if isinstance(selection_artifact, SelectionPlan):
            mcu_parts = [
                {
                    "ref": part.ref,
                    "value": part.value,
                    "symbol": part.symbol,
                    "footprint": part.footprint,
                }
                for part in selection_artifact.parts
                if part.role.lower() == "mcu" or "mcu_" in part.symbol.lower()
            ]
            selected_roles = sorted(
                {part.role.strip().lower() for part in selection_artifact.parts if part.role}
            )
        schematic_paths = sorted(out.glob("*.kicad_sch"))
        pcb_paths = sorted(out.glob("*.kicad_pcb"))
        verification = _verification(
            schematic_paths[0] if schematic_paths else None,
            pcb_paths[0] if pcb_paths else None,
        )
        verification_blockers = _verification_blockers(verification)
        component_release_ready = (
            preparation_artifact.release_ready
            if isinstance(preparation_artifact, ComponentPrepareResult)
            else False
        )
        component_release_blockers = (
            list(preparation_artifact.release_blockers)
            if isinstance(preparation_artifact, ComponentPrepareResult)
            else ["component preparation report is unavailable"]
        )
        steps = _pipeline_steps(state)
        if not state_path.is_file():
            _write_pipeline_state(state_path, requirement, state)
        result_path = out / "pipeline_result.json"
        payload = {
            "status": "blocked" if state.blocked or verification_blockers else "ok",
            "workspace": str(_workspace_root()),
            "run_directory": str(out),
            "completed_steps": len(state.results),
            "total_steps": PIPELINE_TOTAL_STEPS,
            "resumed_steps": resumed_steps,
            "requested_llm_mode": requested_mode.value,
            "effective_llm_mode": mode.value,
            "design_identity": {"mcu_parts": mcu_parts},
            "selected_roles": selected_roles,
            "routing": routing,
            "verification": verification,
            "verification_blockers": verification_blockers,
            "component_release_ready": component_release_ready,
            "component_release_blockers": component_release_blockers,
            "steps": steps,
            "pipeline_state_path": str(state_path),
            "pipeline_result_path": str(result_path),
            "artifacts": _files(out),
        }
        result_path.write_text(_json(payload), encoding="utf-8")
        return _json(payload)
    except (LlmError, TypeError, ValidationError, ValueError) as exc:
        return _json({"status": "error", "error": str(exc), "llm_mode": llm_mode})


def ratsnest_run_pcb_pipeline(
    requirement: str,
    run_name: str = "pcb",
    project_name: str = "board",
    llm_mode: LlmModeName = "auto",
    resume_from: str = "",
    drop_refs: list[str] | None = None,
) -> str:
    """Run one checkpointed PCB pipeline without run-directory races."""
    out = _run_dir(run_name)
    try:
        with _serialize_pipeline_run(out):
            return _run_pcb_pipeline_unlocked(
                requirement=requirement,
                run_name=run_name,
                project_name=project_name,
                llm_mode=llm_mode,
                resume_from=resume_from,
                drop_refs=drop_refs,
            )
    except OSError as exc:
        return _json(
            {
                "status": "error",
                "error": f"could not lock run directory: {exc}",
                "llm_mode": llm_mode,
            }
        )


def ratsnest_review_kicad_project(
    project_path: str,
    report_name: str = "design-review.md",
    llm_mode: LlmModeName = "offline",
) -> str:
    """Review a KiCad project located inside the RatsNestPro workspace.

    project_path may be absolute or workspace-relative, but cannot escape the
    workspace. The deterministic findings remain authoritative; EricAI can only
    add advisory narrative and triage.
    """
    try:
        project = _workspace_path(project_path)
        mode = parse_mode(llm_mode)
        client = None if mode == LlmMode.OFFLINE else _ToolkitLlmClient()
        reviewed = review_project(project, mode=mode, client=client)
        schematic_path, pcb_path = _paired_project_files(
            project,
            reviewed.schematic_path,
            reviewed.pcb_path,
        )
        verification = _verification(
            schematic_path,
            pcb_path,
        )
        verification_blockers = _verification_blockers(verification)
        blocked = reviewed.blocked or bool(verification_blockers)
        verdict_reasons = verification_blockers or (
            ["deterministic project-review gates failed"] if reviewed.blocked else []
        )
        verdict_markdown = "\n".join(
            [
                "# Authoritative review verdict",
                "",
                f"**Verdict: {'BLOCKED' if blocked else 'PASS'}**",
                "",
                *(
                    [f"- {reason}" for reason in verdict_reasons]
                    if verdict_reasons
                    else ["- All required deterministic and kicad-cli gates passed."]
                ),
            ]
        )
        review_markdown = "\n\n".join(
            [
                verdict_markdown,
                _verification_markdown(verification),
                reviewed.markdown,
            ]
        )
        report_dir = _workspace_root() / "reviews"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / _name(report_name, "design-review.md")
        if report_path.suffix.lower() != ".md":
            report_path = report_path.with_suffix(".md")
        report_path.write_text(review_markdown, encoding="utf-8")
        return _json(
            {
                "status": "blocked" if blocked else "ok",
                "workspace": str(_workspace_root()),
                "project_path": str(project),
                "report_path": str(report_path),
                "schematic_path": str(schematic_path) if schematic_path else None,
                "pcb_path": str(pcb_path) if pcb_path else None,
                "verification": verification,
                "verification_blockers": verification_blockers,
                "review": review_markdown,
            }
        )
    except (LlmError, ReviewProjectError, ValueError) as exc:
        return _json({"status": "error", "error": str(exc)})


def ratsnest_search_parts(query: str, limit: int = 10, package: str = "") -> str:
    """Search configured catalogues without inventing parts or stock data."""
    selector = PartSelector()
    candidates, issues = selector.search_catalog(
        PartConstraint(role="free_text", value=query, package=package),
        ProcurementContext(),
        limit=max(1, min(limit, 50)),
    )
    if not candidates and issues and all(not provider.available() for provider in selector.providers):
        return _json(
            {
                "status": "unavailable",
                "error": "No configured parts catalogue is available.",
                "cache_hint": "Mount jlcpcb.sqlite under KICAD_MCP_HOME.",
                "provider_issues": [issue.__dict__ for issue in issues],
            }
        )
    return _json(
        {
            "status": "ok" if candidates else "partial",
            "query": query,
            "results": [
                {
                    "lcsc": item.lcsc,
                    "mpn": item.mpn,
                    "manufacturer": item.manufacturer,
                    "description": item.description,
                    "package": item.package,
                    "provider": item.provider,
                    "provider_part_id": item.provider_part_id,
                    "basic": item.basic,
                    "stock": item.stock,
                    "price": item.price,
                    "currency": item.currency,
                    "lead_days": item.lead_days,
                    "package_match": item.package_match,
                    "asset_status": item.asset_status,
                    "datasheet": item.datasheet,
                    "source_url": item.source_url,
                    "snapshot_id": item.snapshot_id,
                    "lifecycle": item.lifecycle,
                    "rohs": item.rohs,
                    "constraint_gaps": list(item.constraint_gaps),
                }
                for item in candidates
            ],
            "provider_issues": [issue.__dict__ for issue in issues],
        }
    )


def _symbol_match_score(query: str, lib_id: str) -> float:
    wanted = re.sub(r"[^a-z0-9]", "", query.lower())
    candidate = re.sub(r"[^a-z0-9]", "", lib_id.partition(":")[2].lower())
    if not wanted or not candidate:
        return 0.0
    if wanted == candidate:
        return 2.0
    if wanted in candidate or candidate in wanted:
        return 1.5 + min(len(wanted), len(candidate)) / max(len(wanted), len(candidate))
    return difflib.SequenceMatcher(None, wanted, candidate).ratio()


def _focused_symbol_ids(query: str) -> list[str]:
    """Narrow the symbol index to the query's family, driven by the index itself.

    Replaces a hardcoded brand->library pattern table that missed whole families
    (ATSAME/SAME, LPC, EFM32, GD32, MSP430, Renesas RA, PSoC, PIC32) and made
    those queries score against all ~22k installed symbols. Convergence now
    comes from the installed libraries: symbols sharing the query's order-code
    family prefix, else symbols in the libraries where those matches live.
    """
    index = grounding.symbol_index()
    normalized = normalize_order_code(query)
    if not normalized:
        return list(index)

    family = order_code_family(normalized)
    focused: set[str] = set()
    libraries: set[str] = set()
    for lib_id in index:
        library, _, name = lib_id.partition(":")
        symbol = normalize_order_code(name)
        if not symbol:
            continue
        if family and symbol.startswith(family):
            focused.add(lib_id)
            libraries.add(library)
        elif order_code_matches(normalized, symbol):
            focused.add(lib_id)
            libraries.add(library)
    if not focused:
        return list(index)
    # Include the sibling symbols of the matched libraries so a near-miss part
    # can still be surfaced (and then validated) rather than hidden.
    for lib_id in index:
        if lib_id.partition(":")[0] in libraries:
            focused.add(lib_id)
    return sorted(focused)


def ratsnest_lookup_kicad_symbol(
    query: str,
    limit: int = 3,
    required_package: str = "",
) -> str:
    """Return real KiCad symbol candidates and their library-defined pin maps.

    When ``required_package`` is given (e.g. "TQFP-128"), each candidate is
    validated against the requested order code and the package pin count, and
    ``exact_match`` reports whether any candidate is actually usable. A near
    neighbour with a different pin count is returned as evidence but marked
    unusable, never as a silent substitute.
    """
    required_pins = package_pin_count(required_package)
    ranked = sorted(
        ((_symbol_match_score(query, lib_id), lib_id) for lib_id in _focused_symbol_ids(query)),
        reverse=True,
    )
    matches: list[dict[str, Any]] = []
    for score, lib_id in ranked:
        if score < 0.72:
            break
        info = symbols.symbol_info(lib_id)
        if info is None:
            continue
        pins = sorted(
            info["pins"],
            key=lambda pin: (
                (
                    0,
                    int(pin["number"]),
                )
                if str(pin["number"]).isdigit()
                else (1, str(pin["number"]))
            ),
        )
        symbol_name = lib_id.partition(":")[2]
        identity_match = order_code_matches(query, symbol_name) or (
            normalize_order_code(query) == normalize_order_code(symbol_name)
        )
        pin_count = info["pin_count"]
        pin_count_match = required_pins is None or pin_count == required_pins
        matches.append(
            {
                "lib_id": lib_id,
                "score": round(score, 4),
                "pin_count": pin_count,
                "identity_match": identity_match,
                "pin_count_match": pin_count_match,
                "usable": bool(identity_match and pin_count_match),
                "pins": [
                    {
                        "number": str(pin["number"]),
                        "name": str(pin["name"]),
                        "type": str(pin["type"]),
                    }
                    for pin in pins
                ],
                "properties": info["properties"],
            }
        )
        if len(matches) >= max(1, min(limit, 5)):
            break
    exact = [item for item in matches if item["usable"]]
    return _json(
        {
            "status": "ok" if matches else "no_results",
            "query": query,
            "required_package": required_package,
            "required_pin_count": required_pins,
            "exact_match": bool(exact),
            "source": "installed KiCad symbol libraries",
            "candidates": matches,
        }
    )
