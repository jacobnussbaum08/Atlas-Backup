import { useState } from "react";

import { withLiveQuote, withLiveTrade } from "../lib/snapshots";
import type { LiveConnectionState } from "../services/liveUpdates";
import { marketData } from "../services/marketData";
import type { SnapshotsResponse } from "../types/marketData";
import { type Resource, useApiResource } from "./useApiResource";
import { useLiveUpdates } from "./useLiveUpdates";

export const SNAPSHOT_POLL_MS = 15_000;

/**
 * Snapshots from the backend (polled), with live trades and quotes merged in between
 * polls. A poll always replaces merged values with the backend's view.
 */
export function useSnapshots(
  symbols?: string[],
  { poll = true }: { poll?: boolean } = {},
): { resource: Resource<SnapshotsResponse>; data: SnapshotsResponse | null; live: LiveConnectionState } {
  const key = symbols?.join(",") ?? "";
  const resource = useApiResource(
    (signal) => marketData.snapshots(symbols, { signal }),
    [key],
    { refreshMs: poll ? SNAPSHOT_POLL_MS : 0, isEmpty: (d) => d.snapshots.length === 0 },
  );
  // Live merges apply to one backend response; a new response replaces them, and until
  // the first merge the backend data is shown as is (no render without data).
  const [merged, setMerged] = useState<{ base: SnapshotsResponse; value: SnapshotsResponse } | null>(null);
  const base = resource.data;
  const data = merged !== null && merged.base === base ? merged.value : base;
  const update = (fn: (s: SnapshotsResponse["snapshots"][number]) => SnapshotsResponse["snapshots"][number]) =>
    setMerged((current) => {
      if (base === null) return current;
      const from = current !== null && current.base === base ? current.value : base;
      return { base, value: { ...from, snapshots: from.snapshots.map(fn) } };
    });

  const live = useLiveUpdates(
    {
      trade: (trade) => update((s) => withLiveTrade(s, trade)),
      quote: (quote) => update((s) => withLiveQuote(s, quote)),
      resync: () => resource.reload(),
    },
    resource.data !== null,
  );

  return { resource, data, live };
}
