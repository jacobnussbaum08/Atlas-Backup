import type { HealthResponse, ReadinessResponse } from "../types/health";

export class ApiError extends Error {
  readonly status: number | null;

  constructor(message: string, status: number | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function getJson<T>(path: string, acceptedStatuses: readonly number[] = [200]): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { headers: { Accept: "application/json" } });
  } catch {
    throw new ApiError("Atlas API is unreachable", null);
  }
  if (!acceptedStatuses.includes(response.status)) {
    throw new ApiError(`Atlas API returned HTTP ${response.status}`, response.status);
  }
  return (await response.json()) as T;
}

export function fetchHealth(): Promise<HealthResponse> {
  return getJson<HealthResponse>("/health");
}

/** Readiness answers 503 with a full body when a dependency is down; both are valid reports. */
export function fetchReadiness(): Promise<ReadinessResponse> {
  return getJson<ReadinessResponse>("/ready", [200, 503]);
}
