import type { WorkersReport } from "../types/system";
import { getJson } from "./api";

/** Typed client for the Phase 9 system-status endpoint (GET only; it controls nothing). */
type Opts = { signal?: AbortSignal };

export const system = {
  workers: (opts: Opts = {}) => getJson<WorkersReport>("/api/v1/system/workers", opts),
};
