import type { Protocol, Scorecard } from "../types/evidence";
import { getJson } from "./api";

/**
 * Read-only client for the Phase 11A evidence program: pre-registered protocols and
 * readiness scorecards. Building a scorecard or registering a protocol is an operator action
 * (CLI or API); the dashboard only displays the stored records. Nothing here promotes,
 * orders or executes anything.
 */
type Opts = { signal?: AbortSignal };
const base = "/api/v1/evidence";

export const evidence = {
  latest: (o: Opts = {}) => getJson<Scorecard>(`${base}/scorecards/latest`, o),
  protocols: (o: Opts = {}) => getJson<Protocol[]>(`${base}/protocols`, o),
};
