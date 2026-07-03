export interface HealthResponse {
  service: string;
  status: string;
}

export interface CreateRunResponse {
  runId: string;
  status: string;
  projectDir?: string;
}

export interface DesignRun {
  id: string;
  kind?: string | null;
  status: string;
  projectDir?: string | null;
  requirement?: string | null;
  maxIterations?: number | null;
  pythonRunId?: string | null;
  strategyVersionId?: string | null;
  initialScore?: number | null;
  finalScore?: number | null;
  resultJson?: string | null;
  createdAt?: string | null;
  finishedAt?: string | null;
}

export interface PatchPlan {
  ops?: unknown[];
  rationale?: Record<string, string>;
}

export interface RunIteration {
  iteration: number;
  score_delta?: number | null;
  scorecard: {
    score?: number | null;
    [key: string]: unknown;
  };
  patch_plan?: PatchPlan | null;
  resolved_findings?: string[];
}

export interface RunRecord {
  run_id?: string;
  status?: string;
  strategy_version_id?: string | null;
  iterations?: RunIteration[];
  escalation?: unknown;
}

export interface AtdpEvent {
  id?: number;
  eventId?: string | null;
  runId?: string | null;
  iteration: number;
  step: number;
  node?: string | null;
  reward?: number | null;
  receivedAt?: string | null;
  payload?: string | null;
}

interface EventPayload {
  action?: {
    tool?: string;
    arguments?: unknown;
  };
  outcome?: unknown;
  [key: string]: unknown;
}

export function parseRunRecord(resultJson?: string | null): RunRecord | null {
  if (!resultJson) {
    return null;
  }

  try {
    return JSON.parse(resultJson) as RunRecord;
  } catch {
    return null;
  }
}

export function parseEventPayload(payload?: string | null): EventPayload | null {
  if (!payload) {
    return null;
  }

  try {
    return JSON.parse(payload) as EventPayload;
  } catch {
    return null;
  }
}

export function formatScoreDelta(delta?: number | null): string {
  if (delta === null || delta === undefined) {
    return "-";
  }

  return delta > 0 ? `+${delta}` : String(delta);
}

export function summarizeEvent(event: AtdpEvent): string {
  const payload = parseEventPayload(event.payload);

  if (event.node === "mcp_tool") {
    const tool = payload?.action?.tool ?? "mcp_tool";
    const args = JSON.stringify(payload?.action?.arguments ?? {});
    return `${tool} ${args}`.slice(0, 140);
  }

  return JSON.stringify(payload?.outcome ?? payload ?? {}).slice(0, 140);
}

export function statusClassName(status?: string | null): string {
  switch (status) {
    case "converged":
    case "suggested":
      return "border-emerald-300/25 bg-emerald-300/10 text-emerald-100";
    case "running":
    case "dispatched":
      return "border-primary/25 bg-primary/10 text-primary";
    case "failed":
    case "escalated":
      return "border-red-300/25 bg-red-300/10 text-red-100";
    default:
      return "border-white/15 bg-white/5 text-gray-300";
  }
}

export function shortId(id?: string | null): string {
  return id ? id.slice(0, 8) : "-";
}

export function formatDate(value?: string | null): string {
  if (!value) {
    return "-";
  }

  return value.replace("T", " ").slice(0, 16);
}
