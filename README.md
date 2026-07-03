# RatsNest — Auto-Evolving Multi-Agent System for KiCad Design Review & Repair

RatsNest closes the loop **evaluate → repair → re-evaluate → converge** on KiCad projects,
and evolves its own repair strategies offline (AHE — Automated Heuristic Evolution),
following the three pillars of the AREAL2.0 paper (ATDP, Agentic Data Proxy,
Agent Evolution Control Plane). Design doc: `../kicad_auto_evolving_multiagent_plan.md`.

## Architecture

| Layer | Tech | Location |
|---|---|---|
| Control plane (governance) | Java 21/17 + Spring Boot 3 | `backend/` |
| Agent runtime (intelligence) | Python 3.11 | `agent-runtime/` |
| Contracts | JSON Schema (generated from Pydantic) | `schemas/` |
| Evaluation engine | kicad-happy (vendored by reference, unforked) | `RATSNEST_KICAD_HAPPY_ROOT` |
| Benchmark corpus | golden + seeded-defect KiCad projects | `benchmarks/` |

**Discipline rule:** Java is governance, Python is intelligence. The Spring backend never
parses a KiCad file or reasons about a finding.

## Quick start (Windows, this machine)

```powershell
# venv was created from KiCad 10's bundled Python 3.11
$py = ".\.venv\Scripts\python.exe"

# 1. Evaluate a KiCad project -> findings + scorecard
& $py -m ratsnest evaluate benchmarks\seeded\demo_board_defective

# 2. Run the full auto-fix loop -> iterations until converged
& $py -m ratsnest fix benchmarks\seeded\demo_board_defective

# 3. Run an AHE experiment -> candidate strategy vs incumbent, gate report
& $py -m ratsnest evolve

# 4. Trigger statistics from captured ATDP trajectories
& $py -m ratsnest stats
```

Run artifacts land in `runs/<run_id>/` (iterations, patch plans, ATDP trajectory JSONL, scorecards).

## Configuration (env vars)

| Var | Default | Meaning |
|---|---|---|
| `RATSNEST_KICAD_HAPPY_ROOT` | `../kicad-happy-main` (sibling) | kicad-happy checkout (skills/kicad/scripts etc.) |
| `RATSNEST_KICAD_CLI` | `E:\KiCad\10.0\bin\kicad-cli.exe` if present | ERC/DRC gate (feature-gated, optional) |
| `RATSNEST_RUNS_DIR` | `./runs` | run records + trajectories |
| `RATSNEST_CONTROL_PLANE_URL` | *(unset)* | if set, ATDP events also POST to Spring backend |

## Status

Phases 0–3 core implemented in Python (walking skeleton → review → auto-fix loop → AHE v1).
Spring control plane: minimal (runs CRUD, ATDP ingest, dev-profile dispatch). Frontend: deferred.
LLM agent hooks exist but default to deterministic mode — the loop runs with zero API keys.
