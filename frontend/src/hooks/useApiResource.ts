import { type DependencyList, useCallback, useEffect, useRef, useState } from "react";

export type ResourceStatus = "loading" | "success" | "empty" | "error";

export interface Resource<T> {
  data: T | null;
  error: string | null;
  status: ResourceStatus;
  /** A request is in flight (initial load or refresh). */
  loading: boolean;
  /** When the current data was received (ms since epoch). */
  updatedAt: number | null;
  reload: () => void;
}

interface Options<T> {
  /** Re-request every N ms while the tab is visible (0 = no polling). */
  refreshMs?: number;
  /** Decides whether a successful response is "empty". */
  isEmpty?: (data: T) => boolean;
  /** Skip loading (e.g. missing input). */
  enabled?: boolean;
}

/**
 * Load a backend resource with loading / success / empty / error states.
 *
 * Every request gets an AbortController and a sequence number: when inputs change or
 * a refresh starts, the older request is aborted and its late result ignored, so stale
 * responses never overwrite newer ones. Polling pauses while the tab is hidden. A
 * failed refresh keeps the last good data visible together with the error.
 */
export function useApiResource<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  deps: DependencyList,
  { refreshMs = 0, isEmpty, enabled = true }: Options<T> = {},
): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [tick, setTick] = useState(0);
  const sequence = useRef(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const isEmptyRef = useRef(isEmpty);
  isEmptyRef.current = isEmpty;

  const reload = useCallback(() => setTick((t) => t + 1), []);

  // Inputs changed: forget data that belongs to the previous inputs.
  useEffect(() => {
    setData(null);
    setError(null);
    setUpdatedAt(null);
  }, deps);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    const id = ++sequence.current;
    const controller = new AbortController();
    setLoading(true);
    loaderRef
      .current(controller.signal)
      .then((result) => {
        if (id !== sequence.current) return;
        setData(result);
        setError(null);
        setUpdatedAt(Date.now());
      })
      .catch((reason: unknown) => {
        if (id !== sequence.current) return;
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(reason instanceof Error ? reason.message : "Unknown error");
      })
      .finally(() => {
        if (id === sequence.current) setLoading(false);
      });
    return () => controller.abort();
  }, [...deps, tick, enabled]);

  useEffect(() => {
    if (!enabled || refreshMs <= 0) return;
    const timer = window.setInterval(() => {
      if (typeof document === "undefined" || !document.hidden) reload();
    }, refreshMs);
    return () => window.clearInterval(timer);
  }, [enabled, refreshMs, reload]);

  let status: ResourceStatus;
  if (data !== null) {
    status = isEmptyRef.current?.(data) ? "empty" : "success";
  } else if (error !== null) {
    status = "error";
  } else {
    status = "loading";
  }
  return { data, error, status, loading, updatedAt, reload };
}
