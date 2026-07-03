import type {
  AtdpEvent,
  CreateRunResponse,
  DesignRun,
  HealthResponse
} from "./runData";

async function requestJson<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const response = await fetch(path, options);

  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `${response.status} ${response.statusText}`);
  }

  return (await response.json()) as T;
}

export function getHealth(): Promise<HealthResponse> {
  return requestJson<HealthResponse>("/api/health");
}

export function listRuns(): Promise<DesignRun[]> {
  return requestJson<DesignRun[]>("/api/runs");
}

export function getRun(id: string): Promise<DesignRun> {
  return requestJson<DesignRun>(`/api/runs/${encodeURIComponent(id)}`);
}

export function getRunEvents(id: string): Promise<AtdpEvent[]> {
  return requestJson<AtdpEvent[]>(
    `/api/runs/${encodeURIComponent(id)}/events`
  );
}

export function createDesignRun(
  requirement: string
): Promise<CreateRunResponse> {
  return requestJson<CreateRunResponse>("/api/designs", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ requirement })
  });
}

export function createRepairRun(projectDir: string): Promise<CreateRunResponse> {
  return requestJson<CreateRunResponse>("/api/runs", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ projectDir })
  });
}
