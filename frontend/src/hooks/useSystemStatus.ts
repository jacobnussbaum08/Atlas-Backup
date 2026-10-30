import { useCallback, useEffect, useState } from "react";

import { fetchHealth, fetchReadiness } from "../services/api";
import type { HealthResponse, ReadinessResponse } from "../types/health";

export const STATUS_POLL_INTERVAL_MS = 15_000;

export interface SystemStatus {
  health: HealthResponse | null;
  readiness: ReadinessResponse | null;
  /** Set when the API itself could not be reached or answered unexpectedly. */
  error: string | null;
  loading: boolean;
  refresh: () => void;
}

export function useSystemStatus(pollIntervalMs: number = STATUS_POLL_INTERVAL_MS): SystemStatus {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [readiness, setReadiness] = useState<ReadinessResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);

  const refresh = useCallback(() => setTick((value) => value + 1), []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([fetchHealth(), fetchReadiness()])
      .then(([healthResult, readinessResult]) => {
        if (cancelled) return;
        setHealth(healthResult);
        setReadiness(readinessResult);
        setError(null);
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        // Never keep showing stale "healthy" data once the API stops answering.
        setHealth(null);
        setReadiness(null);
        setError(reason instanceof Error ? reason.message : "Unknown error");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tick]);

  useEffect(() => {
    if (pollIntervalMs <= 0) return;
    const id = window.setInterval(refresh, pollIntervalMs);
    return () => window.clearInterval(id);
  }, [pollIntervalMs, refresh]);

  return { health, readiness, error, loading, refresh };
}
