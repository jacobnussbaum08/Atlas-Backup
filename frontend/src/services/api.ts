import type { HealthResponse, ReadinessResponse } from "../types/health";

/**
 * HTTP access to the Atlas API (same origin; the dev server proxies /api, /health and
 * /ready). The browser never talks to a market-data provider and holds no provider
 * credentials. The optional operator token is an Atlas token for operational actions,
 * kept in memory only and sent as a header.
 */
export class ApiError extends Error {
  readonly status: number | null;
  readonly code: string | null;

  constructor(message: string, status: number | null, code: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export type QueryValue = string | number | boolean | null | undefined | readonly string[];

export function buildUrl(path: string, query: Record<string, QueryValue> = {}): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined || value === "") continue;
    if (Array.isArray(value)) {
      for (const item of value) params.append(key, item);
    } else {
      params.append(key, String(value));
    }
  }
  const search = params.toString();
  return search ? `${path}?${search}` : path;
}

/** Message from an Atlas error body ({error: {code, message}}) or a FastAPI 422 body. */
async function errorFrom(response: Response): Promise<ApiError> {
  let message = `Atlas API returned HTTP ${response.status}`;
  let code: string | null = null;
  try {
    const body = (await response.json()) as {
      error?: { code?: string; message?: string };
      detail?: { loc?: (string | number)[]; msg?: string }[] | string;
    };
    if (body.error?.message) {
      message = body.error.message;
      code = body.error.code ?? null;
    } else if (Array.isArray(body.detail)) {
      message = body.detail
        .map((d) => `${(d.loc ?? []).filter((p) => p !== "body" && p !== "query").join(".")}: ${d.msg ?? ""}`)
        .join("; ");
      code = "validation_error";
    } else if (typeof body.detail === "string") {
      message = body.detail;
    }
  } catch {
    // Not JSON: keep the generic message.
  }
  return new ApiError(message, response.status, code);
}

interface RequestOptions {
  signal?: AbortSignal;
  acceptedStatuses?: readonly number[];
}

export async function getJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const accepted = options.acceptedStatuses ?? [200];
  let response: Response;
  try {
    response = await fetch(path, {
      headers: { Accept: "application/json" },
      ...(options.signal ? { signal: options.signal } : {}),
    });
  } catch (reason) {
    if (reason instanceof DOMException && reason.name === "AbortError") throw reason;
    throw new ApiError("Atlas API is unreachable", null);
  }
  if (!accepted.includes(response.status)) throw await errorFrom(response);
  return (await response.json()) as T;
}

export async function postJson<T>(
  path: string,
  body: unknown,
  { operatorToken }: { operatorToken?: string } = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        ...(operatorToken ? { "X-Atlas-Operator-Token": operatorToken } : {}),
      },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError("Atlas API is unreachable", null);
  }
  if (!response.ok) throw await errorFrom(response);
  return (await response.json()) as T;
}

export function fetchHealth(): Promise<HealthResponse> {
  return getJson<HealthResponse>("/health");
}

/** Readiness answers 503 with a full body when a dependency is down; both are valid reports. */
export function fetchReadiness(): Promise<ReadinessResponse> {
  return getJson<ReadinessResponse>("/ready", { acceptedStatuses: [200, 503] });
}
