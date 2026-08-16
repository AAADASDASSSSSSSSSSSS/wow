import {
  controlPlaneFetch,
  forwardJson,
  isSafeId,
  isUuid,
  jsonError,
  problemResponse,
} from "@/lib/backend";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

interface RouteContext {
  params: Promise<{ projectId: string; threadId: string }>;
}

export async function GET(request: Request, context: RouteContext): Promise<Response> {
  const { projectId, threadId } = await context.params;
  const organizationId = new URL(request.url).searchParams.get("organization_id");
  if (!isUuid(projectId) || !isUuid(organizationId)) {
    return jsonError(request, "projectId and organization_id must be UUIDs.");
  }
  if (!isSafeId(threadId)) return jsonError(request, "threadId is invalid.");

  try {
    const upstream = await controlPlaneFetch(
      request,
      `/api/v1/projects/${encodeURIComponent(projectId)}/threads/${encodeURIComponent(threadId)}/latest-run`,
      { signal: request.signal },
      organizationId,
    );
    return forwardJson(upstream);
  } catch {
    return problemResponse(
      request,
      "CONTROL_PLANE_UNAVAILABLE",
      502,
      "The latest run is unavailable.",
    );
  }
}
