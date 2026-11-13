import { useState } from "react";

import {
  Badge,
  DataTable,
  EmptyState,
  ErrorState,
  Loading,
  Notice,
  PageHeader,
  RefreshButton,
  Segmented,
  TD,
  TH,
  Timestamp,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { humanize } from "../lib/format";
import { performance } from "../services/performance";
import { portfolio } from "../services/portfolio";
import type { NotificationItem } from "../types/performance";

/**
 * Spec 23: dashboard notifications. Each is a view of a stored record (position events,
 * rejected orders and trade intents, circuit-breaker events, high and critical system
 * alerts); minor market updates are never notifications.
 */
const REFRESH_MS = 20_000;
const FILTERS = ["ALL", "POSITION_OPENED", "POSITION_CLOSED", "LARGE_PNL", "ORDER_REJECTED", "TRADE_REJECTED", "CIRCUIT_BREAKER", "SYSTEM_ALERT"] as const;
type Filter = (typeof FILTERS)[number];

function severityTone(severity: NotificationItem["severity"]) {
  if (severity === "CRITICAL") return "negative" as const;
  if (severity === "HIGH" || severity === "WARNING") return "caution" as const;
  return "neutral" as const;
}

export function NotificationsPage() {
  const [refresh, setRefresh] = useState(0);
  const [filter, setFilter] = useState<Filter>("ALL");
  const accounts = useApiResource((signal) => portfolio.accounts({ signal }), [refresh]);
  const list = accounts.data ?? [];
  const selected = list.find((a) => a.purpose === "PRIMARY")?.id ?? list[0]?.id ?? null;
  const feed = useApiResource(
    (signal) => performance.notifications(selected, filter === "ALL" ? null : filter, { signal }),
    [refresh, selected, filter],
    { refreshMs: REFRESH_MS, enabled: selected !== null },
  );
  const items = feed.data?.items ?? [];
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Notifications"
        description="Opened and closed paper positions, large P/L, rejected orders and trades, circuit breakers and system alerts, newest first."
        actions={<RefreshButton onClick={() => setRefresh((n) => n + 1)} loading={feed.loading} />}
      />
      <Notice tone="info">
        Simulated paper trading. Notifications are shown here only; Telegram and email delivery are not built.
      </Notice>
      <div className="my-3">
        <Segmented
          label="Kind"
          value={filter}
          onChange={setFilter}
          options={FILTERS.map((f) => ({ value: f, label: f === "ALL" ? "All" : humanize(f) }))}
        />
      </div>
      {accounts.error ? (
        <ErrorState message={accounts.error} onRetry={() => setRefresh((n) => n + 1)} />
      ) : feed.error ? (
        <ErrorState message={feed.error} onRetry={() => setRefresh((n) => n + 1)} />
      ) : !feed.data && selected !== null ? (
        <Loading />
      ) : items.length === 0 ? (
        <EmptyState title="No notifications">Nothing has happened that needs attention.</EmptyState>
      ) : (
        <DataTable label="notifications">
          <thead>
            <tr>
              <th className={TH}>When</th>
              <th className={TH}>Severity</th>
              <th className={TH}>Kind</th>
              <th className={TH}>Notification</th>
            </tr>
          </thead>
          <tbody>
            {items.map((n) => (
              <tr key={n.id}>
                <td className={TD}>
                  <Timestamp iso={n.occurred_at} />
                </td>
                <td className={TD}>
                  <Badge tone={severityTone(n.severity)}>{n.severity}</Badge>
                </td>
                <td className={TD}>{humanize(n.kind)}</td>
                <td className={`${TD} whitespace-normal`}>
                  <p className="text-ink">{n.title}</p>
                  <p className="text-ink-faint text-2xs">{n.message}</p>
                </td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </section>
  );
}
