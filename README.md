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
# venv was created from KiCad 10's bundled Python 3.11 (run from agent-runtime/)
$py = "..\.venv\Scripts\python.exe"

# 0. GENERATE a board from a natural-language requirement, review it, report
& $py -m ratsnest design "a 12V to 3.3V power board with a green LED"

# 1. Evaluate a KiCad project -> findings + scorecard
& $py -m ratsnest evaluate ..\benchmarks\seeded\demo_board_defective

# 2. Run the full auto-fix loop -> iterations until converged
& $py -m ratsnest fix ..\benchmarks\seeded\demo_board_defective

# 3. Run an AHE experiment -> candidate strategy vs incumbent, gate report
& $py -m ratsnest evolve            # add --promote to promote through the gates

# 4. Trigger statistics from captured ATDP trajectories
& $py -m ratsnest stats
```

Run artifacts land in `runs/<run_id>/` (iterations, patch plans, ATDP trajectory JSONL, scorecards).

## Quick start (Docker — avoids all host environment problems)

```powershell
cd E:\agent-service-toolkit-main\kicad-happy-main    # context = dir with both repos
docker compose -f RatsNest/infra/docker-compose.yml build
docker compose -f RatsNest/infra/docker-compose.yml run --rm tests     # 35 tests
docker compose -f RatsNest/infra/docker-compose.yml run --rm fix-demo  # closed loop
docker compose -f RatsNest/infra/docker-compose.yml run --rm evolve    # AHE experiment
docker compose -f RatsNest/infra/docker-compose.yml up backend         # REST API :8080
docker run --rm ratsnest-runtime design "9V to 5V board with a blue LED" --out /tmp/gen
```

K8s starting manifests: `infra/k8s/ratsnest.yaml` (syntax-only validated — no cluster here).

## REST API (Spring Boot control plane, :8080)

| Endpoint | Purpose |
|---|---|
| `POST /api/designs {requirement}` | natural language -> generated + verified KiCad project |
| `POST /api/runs {projectDir}` | auto-fix loop on an existing project |
| `GET /api/runs/{id}` | status, initial/final score, strategy version, full RunRecord |
| `GET /api/runs/{id}/events` | ATDP trajectory events (streamed in live during the run) |
| `POST /api/atdp/events` | ATDP ingest (used by the Python data proxy) |

## Configuration (env vars)

| Var | Default | Meaning |
|---|---|---|
| `RATSNEST_KICAD_HAPPY_ROOT` | `../kicad-happy-main` (sibling) | kicad-happy checkout (skills/kicad/scripts etc.) |
| `RATSNEST_KICAD_CLI` | `E:\KiCad\10.0\bin\kicad-cli.exe` if present | ERC/DRC gate (feature-gated, optional) |
| `RATSNEST_RUNS_DIR` | `./runs` | run records + trajectories |
| `RATSNEST_CONTROL_PLANE_URL` | *(unset)* | if set, ATDP events also POST to Spring backend |

## Status (2026-07-03)

**Working end-to-end, 35 tests green (host and in Docker):**
- design generation: requirement → DesignSpec → KiCad project (regulator family), ERC-clean
- review: kicad-happy analyzers (unforked) + synthesizer augmentation + suppressions → scorecard
- auto-fix loop: findings → repair mappings → patch plans → verify (score-monotonic + new-critical veto) → converge/escalate
- ATDP: every node emits trajectory events (JSONL + HTTP sink to the Java store)
- AHE v1: benchmark w/ seeded-defect ground truth, candidate-vs-incumbent experiments,
  promotion gates (verified to promote a good candidate and reject a sabotaged one), rollback
- Spring control plane: runs/designs REST API, ATDP trajectory store, async dispatch (H2)
- Docker: runtime + backend images, compose services; K8s manifests (untested, no cluster)

Deferred: frontend UI; RabbitMQ/Postgres/MinIO infra (compose profiles later); PCB layout
generation; LLM agent modes (hooks exist — deterministic mode needs zero API keys).
