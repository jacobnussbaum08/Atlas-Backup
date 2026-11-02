import { useState } from "react";

import {
  DimensionBadge,
  GapStatusBadge,
  PIPELINE_TONES,
  SeverityBadge,
  streamTone,
} from "../components/status";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  Loading,
  Notice,
  PageHeader,
  Panel,
  RefreshButton,
  Segmented,
  Stat,
  TD,
  TH,
  Timestamp,
} from "../components/ui";
import { type Resource, useApiResource } from "../hooks/useApiResource";
import { useNow } from "../hooks/useNow";
import { useDashboardStatus } from "../layouts/DashboardLayout";
import { formatAge, formatCount, formatDuration, humanize, MISSING } from "../lib/format";
import { marketData } from "../services/marketData";
import type { DataGap, GapStatus, StreamMetrics } from "../types/marketData";

export const DIMENSION_LABELS: Record<string, string> = {
  provider_connection: "Provider connectivity",
  authentication: "Authentication",
  subscription: "Subscription",
  message_reception: "Message reception",
  message_processing: "Processing",
  data_validation: "Validation",
  database_persistence: "Database persistence",
  historical_completeness: "Historical completeness",
  backfill_activity: "Backfill activity",
};

const HEALTH_POLL_MS = 15_000;
const PAGE = 50;

export function DataHealthPage() {
  const now = useNow(5_000);
  const { stream } = useDashboardStatus();
  const health = useApiResource((signal) => marketData.pipelineHealth({ signal }), [], { refreshMs: HEALTH_POLL_MS });
  const metrics = useApiResource((signal) => marketData.metrics({ signal }), [], { refreshMs: 30_000 });
  const h = health.data;
  const s = stream.data;

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Data Health"
        description="Each pipeline stage is judged separately: a connected WebSocket does not make the pipeline healthy."
        actions={
          <RefreshButton
            loading={health.loading}
            onClick={() => {
              health.reload();
              metrics.reload();
              stream.reload();
            }}
          />
        }
      />

      {health.error && !h && <ErrorState message={health.error} onRetry={health.reload} />}
      {!h && !health.error && <Loading />}
      {h && (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
            <Badge tone={PIPELINE_TONES[h.pipeline_health]} dot>Pipeline {h.pipeline_health}</Badge>
            <Badge tone={streamTone(h.stream_health)} dot>Stream {humanize(h.stream_health)}</Badge>
            <span className="text-ink-muted">
              {humanize(h.provider)} · {h.feed.toUpperCase()} · checked {formatAge(h.checked_at, now)}
            </span>
            {health.error && <span className="text-negative">refresh failed: {health.error}</span>}
          </div>
          {s && s.reasons.length > 0 && (
            <div className="mb-3">
              <Notice tone={streamTone(s.health)}>{s.reasons.join("; ")}</Notice>
            </div>
          )}

          <Panel title="Pipeline stages" bodyClassName="">
            <DataTable label="Pipeline stages">
              <thead>
                <tr>
                  <th className={TH}>Stage</th>
                  <th className={TH}>Status</th>
                  <th className={TH}>Detail</th>
                  <th className={TH}>Last success</th>
                  <th className={TH}>Last update</th>
                  <th className={TH}>Role</th>
                </tr>
              </thead>
              <tbody>
                {h.dimensions.map((d) => (
                  <tr key={d.name}>
                    <td className={`${TD} font-medium`}>{DIMENSION_LABELS[d.name] ?? humanize(d.name)}</td>
                    <td className={TD}><DimensionBadge state={d.state} /></td>
                    <td className={`${TD} text-ink-muted max-w-md truncate whitespace-normal`} title={d.detail}>{d.detail}</td>
                    <td className={TD}>{d.last_success_at ? <span title={d.last_success_at}>{formatAge(d.last_success_at, now)}</span> : <span className="text-ink-faint">{MISSING}</span>}</td>
                    <td className={TD}>{d.updated_at ? <span title={d.updated_at}>{formatAge(d.updated_at, now)}</span> : <span className="text-ink-faint">{MISSING}</span>}</td>
                    <td className={`${TD} text-ink-faint`}>{d.essential ? "essential" : "background"}</td>
                  </tr>
                ))}
              </tbody>
            </DataTable>
          </Panel>

          <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8">
            <Stat label="Outstanding gaps" value={formatCount(Object.values(h.outstanding_gaps).reduce((a, b) => a + b, 0))} hint={Object.entries(h.outstanding_gaps).map(([k, v]) => `${k.toLowerCase()} ${v}`).join(" · ") || "none"} tone={Object.values(h.outstanding_gaps).some((v) => v > 0) ? "caution" : undefined} />
            <Stat label="Pending backfill" value={formatCount(h.pending_recovery_jobs)} />
            <Stat label="Failed backfill" value={formatCount(h.failed_recovery_jobs)} tone={h.failed_recovery_jobs > 0 ? "negative" : undefined} />
            <Stat label="Last backfill" value={h.last_successful_backfill_at ? formatAge(h.last_successful_backfill_at, now) : MISSING} />
            <MetricStats metrics={metrics} />
          </div>
        </>
      )}

      <div className="mt-3 grid gap-3 2xl:grid-cols-2">
        <GapsPanel now={now} />
        <InterruptionsPanel now={now} />
      </div>
      <div className="mt-3 grid gap-3 2xl:grid-cols-2">
        <QualityEventsPanel />
        <AuditPanel />
      </div>
      {s && s.recent_errors.length > 0 && (
        <Panel className="mt-3" title="Recent stream errors" subtitle="as reported by the stream worker">
          <ul className="space-y-1 text-xs">
            {s.recent_errors.slice(-10).reverse().map((e, i) => (
              <li key={i} className="num text-ink-muted break-words">
                {String(e.at ?? "")} · <span className="text-caution">{String(e.kind ?? "")}</span> · {String(e.message ?? "")}
              </li>
            ))}
          </ul>
        </Panel>
      )}
    </section>
  );
}

function MetricStats({ metrics }: { metrics: Resource<StreamMetrics> }) {
  const m = metrics.data;
  if (!m) return null;
  const dup = m.ingestion.duplicates;
  const duplicates = (dup.bars ?? 0) + (dup.quotes ?? 0) + (dup.trades ?? 0);
  const interruptions = (m.reliability.disconnections ?? 0) + (m.reliability.worker_not_running_intervals ?? 0);
  return (
    <>
      <Stat label="Invalid / quarantined" value={`${formatCount(m.ingestion.rejected)} / ${formatCount(m.ingestion.quarantined)}`} hint="live, since worker start" tone={m.ingestion.quarantined > 0 ? "caution" : undefined} />
      <Stat label="Duplicates suppressed" value={formatCount(duplicates)} hint={`${dup.replayed_frames_suppressed ?? 0} replayed frames`} />
      <Stat label="Provider interruptions" value={formatCount(interruptions)} hint={`${m.reliability.disconnections ?? 0} disconnects · ${m.reliability.worker_not_running_intervals ?? 0} worker down`} tone={interruptions > 0 ? "caution" : undefined} />
      <Stat label="DB interruptions" value={formatCount(m.reliability.database_interruptions ?? 0)} hint={`${m.reliability.data_not_persisted_intervals ?? 0} unpersisted intervals`} tone={(m.reliability.database_interruptions ?? 0) > 0 ? "caution" : undefined} />
    </>
  );
}

const GAP_FILTERS: { value: string; label: string; statuses: GapStatus[] }[] = [
  { value: "outstanding", label: "Outstanding", statuses: ["OPEN", "RECOVERING"] },
  { value: "recovered", label: "Recovered", statuses: ["RECOVERED"] },
  { value: "partial", label: "Partial", statuses: ["PARTIALLY_RECOVERED"] },
  { value: "unavailable", label: "Unavailable", statuses: ["UNAVAILABLE"] },
  { value: "all", label: "All", statuses: [] },
];

function GapsPanel({ now }: { now: number }) {
  const [filter, setFilter] = useState("outstanding");
  const [pages, setPages] = useState<DataGap[][]>([]);
  const statuses = GAP_FILTERS.find((f) => f.value === filter)?.statuses ?? [];
  const first = useApiResource(
    (signal) => marketData.gaps({ status: statuses, limit: PAGE }, { signal }),
    [filter],
    { refreshMs: 30_000 },
  );
  const rows = [...(first.data ?? []), ...pages.flat()];
  const loadMore = async () => {
    const last = rows[rows.length - 1];
    if (!last) return;
    const next = await marketData.gaps({ status: statuses, limit: PAGE, before_id: last.id });
    setPages((p) => [...p, next]);
  };
  return (
    <Panel
      title="Missing intervals"
      subtitle="per series · newest first"
      bodyClassName=""
      actions={
        <Segmented
          label="Gap status"
          value={filter}
          onChange={(v) => {
            setFilter(v);
            setPages([]);
          }}
          options={GAP_FILTERS.map((f) => ({ value: f.value, label: f.label }))}
        />
      }
    >
      {first.status === "loading" && <Loading />}
      {first.status === "error" && <div className="p-2"><ErrorState message={first.error ?? ""} onRetry={first.reload} compact /></div>}
      {first.data && rows.length === 0 && (
        <div className="px-3"><EmptyState title="No intervals with this status" /></div>
      )}
      {rows.length > 0 && (
        <DataTable label="Missing intervals">
          <thead>
            <tr>
              <th className={TH}>Series</th>
              <th className={TH}>From</th>
              <th className={TH}>Length</th>
              <th className={TH}>Cause</th>
              <th className={TH}>Status</th>
              <th className={`${TH} text-right`}>Recovered</th>
              <th className={TH}>Resolution</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((g) => (
              <tr key={g.id}>
                <td className={`${TD} num`}>
                  {g.provider_symbol} <span className="text-ink-muted">{g.data_type.toLowerCase()}{g.timeframe ? ` ${g.timeframe}` : ""}</span>
                </td>
                <td className={TD}><Timestamp iso={g.gap_start} seconds={false} /></td>
                <td className={`${TD} num`} title={`until ${g.gap_end}`}>{formatDuration(g.gap_start, g.gap_end)}</td>
                <td className={`${TD} text-ink-muted`}>{humanize(g.cause)}</td>
                <td className={TD}><GapStatusBadge status={g.status} /></td>
                <td className={`${TD} num text-right`}>{formatCount(g.records_recovered)}</td>
                <td className={`${TD} text-ink-muted max-w-xs truncate`} title={g.resolution ?? (g.details.reason as string | undefined) ?? ""}>
                  {g.resolution ?? (typeof g.details.reason === "string" ? g.details.reason : MISSING)}
                </td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
      {first.data && (first.data.length === PAGE || (pages.at(-1)?.length ?? 0) === PAGE) && (
        <div className="p-2"><Button onClick={() => void loadMore()}>Load more</Button></div>
      )}
      <p className="text-ink-faint px-3 py-2 text-2xs">
        Quote gaps stay outstanding unless quote recovery is enabled: Atlas never reports them as recovered.
        Last checked {first.updatedAt ? formatAge(new Date(first.updatedAt).toISOString(), now) : MISSING}.
      </p>
    </Panel>
  );
}

function InterruptionsPanel({ now }: { now: number }) {
  const resource = useApiResource((signal) => marketData.streamGaps({ limit: 50 }, { signal }), [], {
    refreshMs: 30_000,
    isEmpty: (d) => d.length === 0,
  });
  return (
    <Panel title="Provider interruptions" subtitle="live-collection gaps recorded by the stream worker" bodyClassName="">
      {resource.status === "loading" && <Loading />}
      {resource.status === "error" && <div className="p-2"><ErrorState message={resource.error ?? ""} onRetry={resource.reload} compact /></div>}
      {resource.status === "empty" && <div className="px-3"><EmptyState title="No interruptions recorded" /></div>}
      {resource.data && resource.data.length > 0 && (
        <DataTable label="Provider interruptions">
          <thead>
            <tr>
              <th className={TH}>Kind</th>
              <th className={TH}>Started</th>
              <th className={TH}>Length</th>
              <th className={`${TH} text-right`}>Dropped</th>
              <th className={TH}>Recovery</th>
              <th className={TH}>Reason</th>
            </tr>
          </thead>
          <tbody>
            {resource.data.map((g) => (
              <tr key={g.id}>
                <td className={`${TD} font-medium`}>{humanize(g.kind)}</td>
                <td className={TD} title={g.gap_start}>{formatAge(g.gap_start, now)}</td>
                <td className={`${TD} num`}>{formatDuration(g.gap_start, g.gap_end)}</td>
                <td className={`${TD} num text-right`}>{formatCount(g.frames_dropped)}</td>
                <td className={TD}><Badge tone={g.recovery_status === "RECOVERED" ? "positive" : g.recovery_status === "PARTIALLY_RECOVERED" ? "caution" : "neutral"}>{humanize(g.recovery_status)}</Badge></td>
                <td className={`${TD} text-ink-muted max-w-xs truncate`} title={g.reason}>{g.reason}</td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}

function QualityEventsPanel() {
  const [types, setTypes] = useState<string>("all");
  const groups: Record<string, string[] | undefined> = {
    all: undefined,
    invalid: [
      "INVALID_OHLC",
      "INVALID_PRICE",
      "INVALID_QUOTE",
      "INVALID_VOLUME",
      "MISSING_FIELD",
      "REJECTED_MESSAGE",
      "TIMEFRAME_MISALIGNED",
      "CURRENCY_MISMATCH",
    ],
    duplicates: ["DUPLICATE_EVENT", "OUT_OF_ORDER_EVENT", "LATE_ARRIVAL", "SEQUENCE_GAP"],
    revisions: ["CORRECTION", "TRADE_CANCEL", "UNEXPECTED_PRICE_CHANGE"],
    missing: [
      "MISSING_INTERVAL",
      "DATA_DROPPED",
      "DATA_NOT_PERSISTED",
      "STREAM_MISSED_DATA",
      "UNRECOVERABLE_INTERVAL",
      "PROVIDER_OUTAGE",
      "RECOVERY",
    ],
  };
  const resource = useApiResource(
    (signal) => marketData.qualityEvents({ limit: 50, ...(groups[types] ? { event_type: groups[types] } : {}) }, { signal }),
    [types],
    { refreshMs: 60_000, isEmpty: (d) => d.length === 0 },
  );
  return (
    <Panel
      title="Data-quality events"
      bodyClassName=""
      actions={
        <Segmented
          label="Event types"
          value={types}
          onChange={setTypes}
          options={[
            { value: "all", label: "All" },
            { value: "invalid", label: "Invalid" },
            { value: "duplicates", label: "Duplicates" },
            { value: "revisions", label: "Corrections" },
            { value: "missing", label: "Missing / recovery" },
          ]}
        />
      }
    >
      {resource.status === "loading" && <Loading />}
      {resource.status === "error" && <div className="p-2"><ErrorState message={resource.error ?? ""} onRetry={resource.reload} compact /></div>}
      {resource.status === "empty" && <div className="px-3"><EmptyState title="No events of this kind" /></div>}
      {resource.data && resource.data.length > 0 && (
        <DataTable label="Data-quality events">
          <thead>
            <tr>
              <th className={TH}>Detected</th>
              <th className={TH}>Sev.</th>
              <th className={TH}>Type</th>
              <th className={TH}>Series</th>
              <th className={TH}>Result</th>
              <th className={TH}>Message</th>
            </tr>
          </thead>
          <tbody>
            {resource.data.map((e) => (
              <tr key={e.id}>
                <td className={TD}><Timestamp iso={e.detected_at} seconds={false} /></td>
                <td className={TD}><SeverityBadge severity={e.severity} /></td>
                <td className={`${TD} font-medium`}>{humanize(e.event_type)}</td>
                <td className={`${TD} num text-ink-muted`}>{e.provider_symbol ?? MISSING}{e.timeframe ? ` ${e.timeframe}` : ""}</td>
                <td className={`${TD} text-ink-muted`}>{e.resulting_status ?? MISSING}</td>
                <td className={`${TD} text-ink-muted max-w-sm truncate`} title={e.message}>{e.message}</td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}

function AuditPanel() {
  const [priority, setPriority] = useState<"all" | "HIGH">("all");
  const resource = useApiResource(
    (signal) => marketData.auditEvents({ limit: 50, ...(priority === "HIGH" ? { priority: "HIGH" as const } : {}) }, { signal }),
    [priority],
    { refreshMs: 60_000, isEmpty: (d) => d.length === 0 },
  );
  return (
    <Panel
      title="Reliability audit trail"
      subtitle="outages, recoveries, unavailable data"
      bodyClassName=""
      actions={
        <Segmented
          label="Priority"
          value={priority}
          onChange={setPriority}
          options={[
            { value: "all", label: "All" },
            { value: "HIGH", label: "High priority" },
          ]}
        />
      }
    >
      {resource.status === "loading" && <Loading />}
      {resource.status === "error" && <div className="p-2"><ErrorState message={resource.error ?? ""} onRetry={resource.reload} compact /></div>}
      {resource.status === "empty" && <div className="px-3"><EmptyState title="No audit events" /></div>}
      {resource.data && resource.data.length > 0 && (
        <DataTable label="Audit events">
          <thead>
            <tr>
              <th className={TH}>Time</th>
              <th className={TH}>Action</th>
              <th className={TH}>Priority</th>
              <th className={TH}>Reason</th>
            </tr>
          </thead>
          <tbody>
            {resource.data.map((e) => (
              <tr key={e.id}>
                <td className={TD}><Timestamp iso={e.occurred_at} seconds={false} /></td>
                <td className={`${TD} font-medium`}>{humanize(e.action.replace(/^market_data\./, ""))}</td>
                <td className={TD}>{e.priority === "HIGH" ? <Badge tone="negative">High</Badge> : <span className="text-ink-faint">normal</span>}</td>
                <td className={`${TD} text-ink-muted max-w-md truncate`} title={e.reason ?? ""}>{e.reason ?? MISSING}</td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}
