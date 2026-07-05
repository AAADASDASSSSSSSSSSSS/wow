import type {
  AtdpEvent,
  AuthResult,
  CreateRunResponse,
  DesignBackend,
  DesignRun,
  HealthResponse
} from "./runData";

const TOKEN_KEY = "ratsnest_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token);
  } else {
    localStorage.removeItem(TOKEN_KEY);
  }
}

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const token = getToken();
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra;
}

async function requestJson<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: authHeaders(options.headers as Record<string, string>)
  });

  if (!response.ok) {
    const body = await response.text();
    let detail = body;
    try {
      const parsed = JSON.parse(body);
      detail = parsed.detail || parsed.error || body;
    } catch {
      // keep raw body
    }
    const error = new Error(detail || `${response.status} ${response.statusText}`);
    (error as Error & { status?: number }).status = response.status;
    throw error;
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
  requirement: string,
  backend: DesignBackend
): Promise<CreateRunResponse> {
  return requestJson<CreateRunResponse>("/api/designs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ requirement, backend })
  });
}

export function createRepairRun(projectDir: string): Promise<CreateRunResponse> {
  return requestJson<CreateRunResponse>("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ projectDir })
  });
}

// -- auth ---------------------------------------------------------------------

export async function register(
  username: string,
  password: string
): Promise<AuthResult> {
  return requestJson<AuthResult>("/api/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password })
  });
}

export async function login(
  username: string,
  password: string
): Promise<AuthResult> {
  const result = await requestJson<AuthResult>("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password })
  });
  if (result.token) {
    setToken(result.token);
  }
  return result;
}

export async function logout(): Promise<void> {
  try {
    await fetch("/api/auth/logout", {
      method: "POST",
      headers: authHeaders()
    });
  } catch {
    // best effort
  }
  setToken(null);
}

export async function getMe(): Promise<AuthResult> {
  return requestJson<AuthResult>("/api/auth/me");
}

// -- artifact URLs (cookie carries auth in jwt mode; direct in open mode) ------

export function downloadUrl(id: string): string {
  return `/api/runs/${encodeURIComponent(id)}/download`;
}

export function previewUrl(id: string, which: string): string {
  return `/api/runs/${encodeURIComponent(id)}/preview/${encodeURIComponent(which)}`;
}

export function listSteps(id: string): Promise<string[]> {
  return requestJson<string[]>(`/api/runs/${encodeURIComponent(id)}/steps`);
}

export async function getReport(id: string): Promise<string | null> {
  const response = await fetch(`/api/runs/${encodeURIComponent(id)}/report`, {
    headers: authHeaders()
  });
  return response.ok ? response.text() : null;
}
