# RatsNestPro Decisions / Local Evidence

This workspace preserves the decisions/local-evidence RatsNestPro Agent kernel
and runs it behind the professional RatsNest product shell:

- Next.js browser workspace and BFF;
- Java 21 Spring Boot control plane;
- OIDC login through OAuth2 Proxy and Keycloak;
- signed Java-to-Python internal REST/gRPC;
- PostgreSQL tenant/project/run data;
- Redis run leases and resumable event streams;
- Kafka audit relay;
- S3-compatible profile/avatar and artifact infrastructure.

The Agent and embedded EDA implementation remain under
`src/agents/ratsnestpro` and `src/RatsNestPro-main`. The product shell does not
silently replace this legacy kernel with the current production Agent.

## Runtime architecture

```text
Browser :8088
  -> OAuth2 Proxy / Keycloak
  -> Next.js BFF
  -> Java control_plane :8081
  -> signed internal REST or gRPC
  -> Python ratsnestpro-multi-agent :8080/:9090
  -> decisions/local-evidence Agent + KiCad pipeline
```

## 原生多轮 HITL

旧内核沿用原生的多轮决策链：Agent 输出 decision menu 后结束本轮
（`END`），前端将其渲染为 `DecisionCard`；用户执行 `PICK` 后，Java
控制面创建新的 Run Revision，并以同一个 `thread_id` 和 checkpoint
继续执行。Agent 可以在后续轮次继续提问；如果用户只回答了部分问题，
它会保留已确认项并重新询问尚未回答的项目。

该机制与产品壳提供的 AG-UI same-run `interrupt/resume` 可以共存，但属于
两套独立协议：同一次交互只能选择其中一种，不能把原生 Revision/PICK
当作 AG-UI resume 混用。页面刷新后，前端先从 `localStorage` 恢复本地
lineage，再通过 `latest-run` 接口校准该项目和 thread 的最新 Revision。

## Shared Docker environment

This checkout intentionally does not contain a `.env` file. It reuses the
infrastructure and model configuration from:

```text
E:\agent-service-toolkit-main\agent-service-toolkit_frame\agent-service-toolkit-main
```

Switch the shared application tier to this kernel with:

```powershell
Set-Location "E:\agent-service-toolkit-main\agent-service-toolkit_frame\RatsNest-ratsnestpro-decisions-local-evidence\RatsNest-ratsnestpro-decisions-local-evidence\agent-service-toolkit-main"
& .\scripts\switch_to_local_evidence.ps1
```

The script keeps the existing PostgreSQL, Redis, Kafka, Temporal, Keycloak and
MinIO containers. It rebuilds only this checkout's Python Runtime, Java control
plane and Next.js frontend. LLM/API credentials remain in the current product
workspace's `.env` and are never copied here.

Restore the current production Agent application tier with:

```powershell
& .\scripts\restore_current_app.ps1
```

Use `-SkipBuild` only when the corresponding application images have already
been built from the desired source tree.

## Compatibility boundary

The UI exposes one honest capability selection:
`local-evidence-open-pcb@1.0`. It is transport and product metadata for this
legacy kernel; it does not claim that the old Agent enforces the current
product's five Capability Profiles.

The old Agent has turn-based clarification rather than the current graph's
same-run `interrupt/resume` interaction. It also predates the current Temporal
Hardware Engineer and immutable artifact-manifest publisher. The professional
shell therefore provides identity, projects, runs, streaming, history and
auditing without misreporting those kernel capabilities as implemented.

## Source boundaries

- `frontend/`: Next.js product UI and BFF
- `backend/`: Java Spring Boot SaaS control plane
- `contracts/`: public and internal versioned contracts
- `src/service/`: Python Runtime transport and run coordination
- `src/agents/ratsnestpro/`: preserved decisions/local-evidence Agent
- `src/RatsNestPro-main/`: preserved embedded EDA engine, plus restored missing
  contract/review/check-taxonomy modules required for import
- `docker/identity/`: development-only Keycloak realm
- `deploy/k8s/`: inherited product-shell deployment manifests; they require
  environment-specific validation before production deployment

## Security

- Do not add `.env`, API keys, Keycloak state, MinIO data or private credentials
  to this checkout.
- The bundled Keycloak realm is for local development only.
- Production OIDC, Kafka TLS/SASL, S3 credentials and internal signing secrets
  must come from an external secret manager.
