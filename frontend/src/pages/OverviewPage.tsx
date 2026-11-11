import { Link } from "react-router";

import { AssetTable, type ColumnKey } from "../components/market/AssetTable";
import { EquityHistoryPanel, PaperAccountPanel, RiskStatusPanel, SystemStatusPanel } from "../components/overview";
import { DimensionBadge, PIPELINE_TONES, SessionBadge, streamTone } from "../components/status";
import {
  Badge,
  EmptyState,
  ErrorState,
  KeyValues,
  Loading,
  Notice,
  PageHeader,
  Panel,
  RefreshButton,
  Stat,
  StatusDot,
  Timestamp,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { useNow } from "../hooks/useNow";
import { useSnapshots } from "../hooks/useSnapshots";
import { useDashboardStatus } from "../layouts/DashboardLayout";
import { formatAge, formatCount, formatSessionTime, humanize, MISSING } from "../lib/format";
import { useDisplayZone } from "../lib/time";
import { allowNotFound } from "../services/api";
import { marketData } from "../services/marketData";
import { portfolio } from "../services/portfolio";
import { risk as riskApi } from "../services/risk";
import { system } from "../services/system";

const OVERVIEW_COLUMNS: ColumnKey[] = [
  "symbol",
  "name",
  "last",
  "change",
  "change_percent",
  "previous_close",
  "latest_event",
  "source",
  "feed",
  "freshness",
  "quality",
];

export const HEALTH_POLL_MS = 15_000;
/** Account, risk and worker summaries: the backend recomputes them; a minute-scale view is enough. */
export const ACCOUNT_POLL_MS = 30_000;
/** Stream health values in which the recorded auth/subscription states are history. */
const DISCONNECTED = new Set(["NOT_RUNNING", "RECONNECTING", "FAILED", "DISABLED", "STARTING"]);

function LastKnown({ value, connected }: { value: string | null; connected: boolean }) {
  if (!value) return <span className="text-ink-faint">{MISSING}</span>;
  return connected ? (
    <span className="num">{value}</span>
  ) : (
    <span className="num text-ink-faint" title="Recorded during the last connection; the stream is not connected now">
      {value} (last known)
    </span>
  );
}

export function OverviewPage() {
  const now = useNow(1_000);
  const { zone } = useDisplayZone();
  const { stream, market, readiness, error: apiError } = useDashboardStatus();
  const { resource, data, live } = useSnapshots();
  const health = useApiResource((signal) => marketData.pipelineHealth({ signal }), [], {
    refreshMs: HEALTH_POLL_MS,
  });
  const metrics = useApiResource((signal) => marketData.metrics({ signal }), [], { refreshMs: 60_000 });
  const risk = useApiResource((signal) => allowNotFound(riskApi.state({ signal })), [], { refreshMs: ACCOUNT_POLL_MS });
  const ledger = useApiResource((signal) => allowNotFound(portfolio.overview(null, { signal })), [], {
    refreshMs: ACCOUNT_POLL_MS,
  });
  const snapshots = useApiResource((signal) => allowNotFound(portfolio.snapshots(null, { signal })), [], {
    refreshMs: 120_000,
  });
  const workers = useApiResource((signal) => system.workers({ signal }), [], { refreshMs: ACCOUNT_POLL_MS });

  const s = stream.data;
  const m = market.data;
  const connected = s !== null && !DISCONNECTED.has(s.health);
  const h = health.data;
  const latestEvent = data?.snapshots
    .map((x) => x.latest_event_at)
    .filter((t): t is string => t !== null)
    .sort()
    .at(-1);
  const failedIngestion = metrics.data?.completeness.ingestion_jobs.FAILED ?? 0;
  const barGaps = h?.outstanding_gaps.BAR ?? 0;
  const allGaps = h ? Object.values(h.outstanding_gaps).reduce((a, b) => a + b, 0) : null;

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Overview"
        description="Simulated paper account, risk and system status, market session, data pipeline and monitored assets. Stored or backend-computed data only; nothing is estimated."
        actions={
          <RefreshButton
            loading={resource.loading || health.loading || risk.loading}
            onClick={() => {
              resource.reload();
              health.reload();
              risk.reload();
              ledger.reload();
              snapshots.reload();
              workers.reload();
              stream.reload();
              market.reload();
            }}
          />
        }
      />

      <PaperAccountPanel risk={risk} ledger={ledger} />

      <div className="mt-3 grid gap-3 lg:grid-cols-3">
        <RiskStatusPanel risk={risk} />
        <SystemStatusPanel readiness={readiness} apiError={apiError} workers={workers} />
        <EquityHistoryPanel snapshots={snapshots} />
      </div>

      <div className="mt-3 grid gap-3 lg:grid-cols-3">
        <Panel title="Market status" subtitle={m ? "NYSE calendar (backend)" : undefined}>
          {market.error && !m ? (
            <ErrorState message={market.error} onRetry={market.reload} compact />
          ) : !m ? (
            <Loading />
          ) : (
            <div className="space-y-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <SessionBadge session={m.session} />
                <span className="text-sm font-medium">
                  NYSE {m.is_regular_session ? "open" : "closed"}
                </span>
                {m.closure && <Badge>{m.closure}</Badge>}
                {m.early_close && <Badge tone="caution">Early close</Badge>}
              </div>
              <KeyValues
                items={[
                  {
                    label: "Today",
                    value: m.regular_open ? (
                      <span className="num">
                        <Timestamp iso={m.regular_open} timeOnly /> – <Timestamp iso={m.regular_close} timeOnly />
                      </span>
                    ) : (
                      "No session"
                    ),
                  },
                  {
                    label: m.is_regular_session ? "Closes" : "Next open",
                    value: (
                      <span className="num">
                        {formatSessionTime(m.is_regular_session ? m.next_regular_close : m.next_regular_open, zone)}
                      </span>
                    ),
                  },
                  {
                    label: `${m.feed.toUpperCase()} feed`,
                    value: (
                      <span className="flex flex-wrap items-center gap-1.5">
                        <StatusDot tone={m.feed_open ? "positive" : "neutral"} label={m.feed_open ? "Publishing" : "Not publishing"} />
                        {m.feed_window_start && (
                          <span className="text-ink-faint num">
                            <Timestamp iso={m.feed_window_start} timeOnly /> – <Timestamp iso={m.feed_window_end} timeOnly />
                          </span>
                        )}
                      </span>
                    ),
                  },
                  {
                    label: "Calendar",
                    value: m.calendar_verified ? (
                      <span className="text-ink-muted">Verified for this year</span>
                    ) : (
                      <span className="text-caution">Not verified for this year</span>
                    ),
                  },
                ]}
              />
            </div>
          )}
        </Panel>

        <Panel
          title="Market-data pipeline"
          actions={<Link to="/data-health" className="text-accent text-xs hover:underline">Details</Link>}
        >
          {(stream.error && !s) || (health.error && !h) ? (
            <ErrorState message={stream.error ?? health.error ?? ""} onRetry={() => { stream.reload(); health.reload(); }} compact />
          ) : !s || !h ? (
            <Loading />
          ) : (
            <div className="space-y-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={PIPELINE_TONES[h.pipeline_health]} dot>
                  Pipeline {h.pipeline_health}
                </Badge>
                <Badge tone={streamTone(s.health)} dot>
                  Stream {humanize(s.health)}
                </Badge>
              </div>
              <KeyValues
                items={[
                  { label: "Provider", value: <span>{humanize(s.provider)} · <span className="num">{s.feed.toUpperCase()}</span></span> },
                  { label: "WebSocket", value: <span className="num">{s.connection_state ?? MISSING}</span> },
                  { label: "Authentication", value: <LastKnown value={s.auth_state} connected={connected} /> },
                  { label: "Subscription", value: <LastKnown value={s.subscription_state} connected={connected} /> },
                  {
                    label: "Persistence",
                    value: <DimensionBadge state={h.dimensions.find((d) => d.name === "database_persistence")?.state ?? "UNKNOWN"} />,
                  },
                ]}
              />
              {s.reasons.length > 0 && <Notice tone={streamTone(s.health)}>{s.reasons.join("; ")}</Notice>}
            </div>
          )}
        </Panel>

        <div className="grid grid-cols-2 gap-2">
          <Stat label="Monitored assets" value={data ? data.snapshots.length : MISSING} hint={s ? `${s.subscribed_symbols.length} streamed` : undefined} />
          <Stat
            label="Subscriptions"
            value={s ? s.subscribed_symbols.length : MISSING}
            hint={s && s.active_channels.length > 0 ? s.active_channels.join(", ") : "none active"}
          />
          <Stat label="Latest event" value={latestEvent ? formatAge(latestEvent, now) : MISSING} hint={latestEvent ? <Timestamp iso={latestEvent} /> : "nothing stored"} />
          <Stat
            label="Last persisted"
            value={s?.last_persisted_at ? formatAge(s.last_persisted_at, now) : MISSING}
            hint={s?.last_persisted_at ? <Timestamp iso={s.last_persisted_at} /> : "live writes"}
          />
          <Stat
            label="Candle gaps"
            value={h ? formatCount(barGaps) : MISSING}
            tone={barGaps > 0 ? "caution" : undefined}
            hint={allGaps !== null ? `${allGaps} incl. trades/quotes` : undefined}
          />
          <Stat
            label="Backfill pending"
            value={h ? formatCount(h.pending_recovery_jobs) : MISSING}
            hint={h ? `${h.failed_recovery_jobs} failed recovery` : undefined}
            tone={h && h.failed_recovery_jobs > 0 ? "caution" : undefined}
          />
          <Stat
            label="Failed ingestion"
            value={metrics.data ? formatCount(failedIngestion) : MISSING}
            tone={failedIngestion > 0 ? "negative" : undefined}
            hint="historical jobs"
          />
          <Stat
            label="Browser feed"
            value={humanize(live)}
            tone={live === "open" ? "info" : live === "reconnecting" ? "caution" : undefined}
            hint="updates from stored data"
          />
        </div>
      </div>

      <Panel
        className="mt-3"
        title="Monitored assets"
        subtitle={data ? `${data.provider} · ${data.feed.toUpperCase()} · updated ${formatAge(data.checked_at, now)}` : undefined}
        bodyClassName=""
        actions={<Link to="/markets" className="text-accent text-xs hover:underline">All markets</Link>}
      >
        {resource.status === "loading" && <Loading label="Loading assets…" />}
        {resource.status === "error" && (
          <div className="p-3">
            <ErrorState message={resource.error ?? ""} onRetry={resource.reload} />
          </div>
        )}
        {resource.status === "empty" && (
          <div className="px-3">
            <EmptyState title="No monitored assets">
              Register assets with <code className="num">python -m app.data_engine.cli register-assets</code>.
            </EmptyState>
          </div>
        )}
        {data && data.snapshots.length > 0 && <AssetTable rows={data.snapshots} columns={OVERVIEW_COLUMNS} now={now} />}
        {resource.error && data && (
          <div className="p-2">
            <ErrorState message={`Refresh failed: ${resource.error}`} onRetry={resource.reload} compact />
          </div>
        )}
      </Panel>
      {data && data.notes.length > 0 && (
        <ul className="text-ink-faint mt-2 space-y-0.5 text-2xs">
          {data.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
    </section>
  );
}
