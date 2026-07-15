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

Detailed Agent/Tool/Blackboard design: [`docs/autonomous-agent-architecture.md`](docs/autonomous-agent-architecture.md).
Enterprise delivery roadmap: [`docs/enterprise-product-roadmap.md`](docs/enterprise-product-roadmap.md).
Stage 2 workflow contract: [`docs/stage-2-controlled-agent-workflow.md`](docs/stage-2-controlled-agent-workflow.md).

## Quick start (Windows)

```powershell
# venv was created from KiCad 10's bundled Python 3.11 (run from agent-runtime/)
$py = "..\.venv\Scripts\python.exe"

# 0. Local developer shortcut: explicitly approve and execute immediately
& $py -m ratsnest design "a 12V to 3.3V power board with a green LED" --backend crew --auto-approve

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
Set-Location C:\path\to\workspace  # directory containing both repos
docker compose -f RatsNest/infra/docker-compose.yml build
docker compose -f RatsNest/infra/docker-compose.yml run --rm tests     # 82 tests
docker compose -f RatsNest/infra/docker-compose.yml run --rm fix-demo  # closed loop
docker compose -f RatsNest/infra/docker-compose.yml run --rm evolve    # AHE experiment
docker compose -f RatsNest/infra/docker-compose.yml up backend         # REST API :8080
docker run --rm ratsnest-runtime design "9V to 5V board with a blue LED" --out /tmp/gen
```

K8s starting manifests: `infra/k8s/ratsnest.yaml` (syntax-only validated — no cluster here).

## REST API (Spring Boot control plane, :8080)

| Endpoint | Purpose |
|---|---|
| `POST /api/designs {requirement, backend}` | natural language -> immutable reviewable design plan |
| `POST /api/runs {projectDir}` | auto-fix loop on an existing project |
| `GET /api/runs/{id}` | status, initial/final score, strategy version, full RunRecord |
| `GET /api/runs/{id}/events` | ATDP trajectory events (streamed in live during the run) |
| `POST /api/atdp/events` | ATDP ingest (used by the Python data proxy) |
| `GET /api/tenant/context` | organizations, workspaces, projects, and membership roles for the current user |
| `GET /api/runs/{id}/artifacts` | immutable artifact metadata (size and SHA-256; storage keys are not exposed) |
| `GET /api/runs/{id}/plan` | immutable PlannedDesign and its control-plane SHA-256 |
| `GET /api/runs/{id}/approvals` | BoardPlan and release decision history |
| `POST /api/runs/{id}/approvals/{board_plan\|design_release}` | make an immutable typed decision |
| `GET /api/runs/{id}/download` | download the persisted project ZIP after release approval |

## Enterprise alpha foundation

The control plane now has an initial enterprise ownership hierarchy:

```text
Organization -> Membership -> Workspace -> HardwareProject -> DesignRun
```

Registration provisions a personal organization, default workspace, and
sandbox project. Authenticated runs are scoped to the organization, while
legacy owner-only rows remain readable by their original owner. Run details,
ATDP events, EDA access, previews, reports, approvals, artifacts, and downloads
all use the same access policy. Platform admins and the runtime service
identity remain explicit privileged paths.

Generated project ZIPs are stored behind `ArtifactStore`; the default
implementation writes to a durable control-plane filesystem/volume and records
the filename, byte length, and SHA-256 in `run_artifacts`. Kafka workers upload
the ZIP before posting their final RunRecord, so downloads and browser previews
no longer depend on a worker's local directory. The interface is the extension
point for the planned S3/MinIO implementation.

Kafka dispatch uses a database Outbox. A run row and its pending dispatch event
commit together, publishing retries with bounded backoff, worker offsets commit
only after the control plane acknowledges success/failure, and duplicate final
callbacks are accepted only when their SHA-256 is identical.

Design runs follow `Plan -> Approve -> Execute -> Verify/Repair -> Release`.
Planning stores exact `PlannedDesign` bytes and a control-plane SHA-256 without
creating a KiCad project. Only an approved matching BoardPlan can enqueue KiCad
execution. Successful runs then enter a second, immutable engineering release
review; download returns HTTP 409 until that release is approved.

Existing H2 databases are baselined by Flyway before Hibernate updates the
schema. The first migration backfills optimistic-lock and attempt counters for
legacy `design_runs` rows.

## Frontend dashboard

The dashboard at `http://localhost:8080/` is built from `frontend/` with
React, Vite, TypeScript, Tailwind CSS, framer-motion, and lucide-react.

For local frontend development:

```powershell
cd RatsNest/frontend
npm install
npm run dev
```

The Vite dev server proxies `/api` to `http://localhost:8080`.

To refresh the Spring Boot static assets:

```powershell
cd RatsNest/frontend
npm run build
```

`npm run build` writes the compiled app to
`backend/src/main/resources/static`, so the Spring Boot jar and Docker image
serve the latest dashboard without changing the REST API.

## Configuration (env vars)

| Var | Default | Meaning |
|---|---|---|
| `RATSNEST_KICAD_HAPPY_ROOT` | `../kicad-happy-main` (sibling) | kicad-happy checkout (skills/kicad/scripts etc.) |
| `RATSNEST_KICAD_CLI` | auto-detected from PATH or a standard KiCad install | ERC/DRC gate (feature-gated, optional) |
| `RATSNEST_RUNS_DIR` | `./runs` | run records + trajectories |
| `RATSNEST_CONTROL_PLANE_URL` | *(unset)* | if set, ATDP events also POST to Spring backend |
| `RATSNEST_LLM` | `auto` | `off`, `auto` (fallback allowed), or `require` |
| `RATSNEST_LLM_PROVIDER` | `anthropic` | `anthropic`, `openai`, `deepseek`, `qwen`, `moonshot`, `zhipu`, or `ollama` |
| `RATSNEST_LLM_API_KEY` | *(unset)* | provider API key; never committed |
| `RATSNEST_LLM_MODEL` | provider default | model used by every LLM brain seam |
| `RATSNEST_LLM_BASE_URL` | provider default | optional compatible endpoint |
| `RATSNEST_ARTIFACT_ROOT` | `./data/artifacts` | durable control-plane artifact storage root |
| `RATSNEST_ARTIFACT_MAX_BYTES` | `268435456` | maximum accepted artifact size |
| `RATSNEST_SECURITY_MODE` | `open` | `open` for local development or `jwt` for authenticated deployments |
| `RATSNEST_COOKIE_SECURE` | `false` | set `true` behind production HTTPS |
| `RATSNEST_SERVICE_TOKEN` | *(unset)* | service identity used by workers for callbacks and artifact upload |

## Autonomous design crew

The Crew backend separates decision-makers from deterministic executors:

| Agent / crew | Owns |
|---|---|
| `RequirementAgent` | natural language -> validated `DesignSpec` |
| `CircuitArchitect` | topology intent, constraints, and authoritative `BoardPlan` |
| `SchematicDesigner` | observes file truth and plans schematic `ToolCall[]` batches |
| `PcbDesigner` | plans synchronization, outline, placement, and routing calls |
| `VerificationCrew` | runs kicad-happy/ERC/DRC checkers and publishes typed findings |
| `RepairAgent` | assigns each finding to a designer, repair loop, or human |
| `EvolutionAgent` | proposes bounded prompt, budget, solver, and mapping changes |

`ProjectTools`, `SymbolTools`, `WiringTools`, `LayoutTools`, and `RoutingTools`
are capability-scoped **Tool Services**, not agents. They expose deterministic
KiCAD-MCP-Server Python commands through one in-process KiCad Host.

Each design agent runs a bounded loop:

```text
Observe file-derived DesignState -> Reason/Plan AgentPlan + ToolCall[]
-> validate capability, argument, topology, and electrical contracts
-> execute trusted KiCad tool -> observe files again -> verify or re-plan
```

Agents collaborate through a shared `DesignBlackboard`. `BoardPlan`, tasks,
messages, findings, plans, and tool outcomes are Pydantic models; free-form
agent chat is never an execution interface. The LLM cannot emit or edit KiCad
S-expressions. `RATSNEST_LLM=auto` uses the LLM when configured and a bounded
deterministic recovery policy otherwise; `require` rejects fallback.

Generation, verification, and repair share one run ID and one ordered ATDP
trajectory. Agent prompt policies and action budgets live in the versioned
strategy bundle, so AHE may propose bounded changes, but benchmark gates still
control promotion.

## Design creation backends

| Backend | What it does |
|---|---|
| `template` | deterministic S-expression writer, self-contained symbols |
| `crew` | autonomous LLM-capable agents plan validated calls to in-process KiCad Tool Services; produces a schematic and routed PCB |
| `mcp` | deterministic generation through the external [KiCAD-MCP-Server](../KiCAD-MCP-Server-main) stdio transport; every call is captured as an ATDP event |

```powershell
& $py -m ratsnest design "a 12V to 5V board with a red LED" --backend crew --auto-approve
```

All backends solve values from the SAME strategy assets (Vref table, E-series,
MPN patterns) and are judged by the same kicad-happy loop — one evolvable
knowledge base governs every path a design can be created through.

The external `mcp` backend additionally requires Node >= 18 and a built server.
The `crew` backend hosts the server's Python commands directly and does not
start Node. Both real-KiCad backends require KiCad 10 with `pcbnew` available.

## Status (2026-07-15)

**Working end-to-end: 82 Python, 7 frontend, and 17 backend tests green:**
- autonomous Crew generation: typed Blackboard, CircuitArchitect, bounded Schematic/PCB Agent loops, capability validation, real KiCad Tool Services, and hard BoardPlan completion gates
- design generation: requirement -> DesignSpec -> KiCad project through template, autonomous crew, or external MCP
- review: kicad-happy analyzers (unforked) + synthesizer augmentation + suppressions → scorecard
- auto-fix loop: findings → repair mappings → patch plans → verify (score-monotonic + new-critical veto) → converge/escalate
- ATDP: generation, Agent plans, KiCad tool calls, checker results, repairs, and final reward share one ordered trajectory
- AHE v1: benchmark w/ seeded-defect ground truth, candidate-vs-incumbent experiments,
  bounded Agent-policy mutations, promotion gates, and rollback
- Spring control plane: runs/designs REST API, ATDP trajectory store, dual dispatch (local subprocess / Kafka queue)
- enterprise alpha foundation: tenant-scoped projects/runs, durable artifacts,
  transactional Kafka Outbox, idempotent callbacks, release approvals, Flyway
  legacy-database migration, and cookie mutation protection
- **Dashboard** at `http://localhost:8080/` — React/Vite hybrid landing page plus live control console for design form, runs, scorecards, repair rationale, and ATDP timeline
- **Cluster mode**: `docker compose -f RatsNest/infra/docker-compose.yml --profile cluster up` → Postgres 16 + Kafka (KRaft) + backend (cluster profile) + Python Kafka worker. Code-complete and compose-validated; full cluster e2e not yet exercised on this machine.

Current production scope is intentionally constrained to qualified TLV1117
adjustable-LDO and LM2596 asynchronous-Buck board families through the Crew
backend. The Agent architecture is LLM-driven when a key is configured, but
arbitrary topology synthesis, regulatory qualification, and full cluster
load/security validation remain future product phases. K8s manifests are still
untested on a live cluster.
