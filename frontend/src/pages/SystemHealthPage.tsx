import { Link } from "react-router";

import { StatusPill } from "../components/StatusPill";
import { PIPELINE_TONES } from "../components/status";
import { OriginBadge, WORKER_LABELS, WORKER_TONES } from "../components/provenance";
import {
  Badge,
  DataTable,
  EmptyState,
  ErrorState,
  KeyValues,
  Loading,
  Notice,
  PageHeader,
  Panel,
  RefreshButton,
  Stat,
  TD,
  TH,
  Timestamp,
  type Tone,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { useNow } from "../hooks/useNow";
import { useDashboardStatus } from "../layouts/DashboardLayout";
import { formatAge, humanize, MISSING } from "../lib/format";
import { marketData } from "../services/marketData";
import { news } from "../services/news";
import { system } from "../services/system";
import type { WorkerStatus } from "../types/system";

const COMPONENT_LABELS: Record<string, string> = {
  database: "PostgreSQL",
  migrations: "Database migrations",
  redis: "Redis",
};

/** Workers change state on a minute scale; errors and recovery more slowly. */
export const WORKERS_POLL_MS = 30_000;
export const ERRORS_POLL_MS = 60_000;

function formatUptime(seconds: number): string {
  const s = Math.floor(seconds);
  const d = Math.floor(s / 86_400);
  const h = Math.floor((s % 86_400) / 3_600);
  const m = Math.floor((s % 3_600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s % 60}s`;
  return `${s} s`;
}

function enabledText(api: boolean | null, reported: boolean | null): string {
  const show = (v: boolean | null) => (v === null ? "?" : v ? "on" : "off");
  if (api === null && reported === null) return "not switchable";
  if (reported === null) return `API env ${show(api)}`;
  return `API env ${show(api)} · worker ${show(reported)}`;
}

interface ErrorRow {
  at: string;
  source: string;
  severity: "WARNING" | "ERROR" | "CRITICAL";
  message: string;
  /** Occurrences folded into this row (same source, type and severity); newest shown. */
  count: number;
}

const SEVERITY_TONES: Record<ErrorRow["severity"], Tone> = { WARNING: "caution", ERROR: "negative", CRITICAL: "negative" };

function WorkersTable({ workers, now }: { workers: WorkerStatus[]; now: number }) {
  return (
    <DataTable label="Background workers">
      <thead>
        <tr>
          <th scope="col" className={TH}>Worker</th>
          <th scope="col" className={TH}>Health</th>
          <th scope="col" className={TH}>Recorded state</th>
          <th scope="col" className={TH}>Last heartbeat</th>
          <th scope="col" className={TH}>Last activity</th>
          <th scope="col" className={TH}>Switch</th>
        </tr>
      </thead>
      <tbody>
        {workers.map((w) => (
          <tr key={w.worker} title={w.purpose}>
            <td className={TD}>
              <span className="font-medium">{w.title}</span>
              <span className="text-ink-faint num block text-2xs">{w.worker}</span>
            </td>
            <td className={TD}>
              <span title={w.detail}>
                <Badge tone={WORKER_TONES[w.health]} dot>
                  {WORKER_LABELS[w.health]}
                </Badge>
              </span>
            </td>
            <td className={`${TD} num`}>{w.state ?? MISSING}</td>
            <td className={TD}>
              {w.observation === "HEARTBEAT" ? (
                w.last_heartbeat_at ? (
                  <span className="num" title={`${w.last_heartbeat_at} (UTC); stale after ${w.stale_after_seconds ?? "?"} s`}>
                    {formatAge(w.last_heartbeat_at, now)}
                  </span>
                ) : (
                  <span className="text-ink-faint">{MISSING}</span>
                )
              ) : (
                <span className="text-ink-faint" title="This worker records no heartbeat">none recorded</span>
              )}
            </td>
            <td className={`${TD} whitespace-normal!`}>
              {w.last_activity_at ? (
                <span>
                  <Timestamp iso={w.last_activity_at} />
                  {w.last_activity && <span className="text-ink-faint block text-2xs">{w.last_activity}</span>}
                </span>
              ) : (
                <span className="text-ink-faint">{MISSING}</span>
              )}
            </td>
            <td className={`${TD} text-ink-muted`}>{enabledText(w.enabled_in_api_env, w.enabled_reported)}</td>
          </tr>
        ))}
      </tbody>
    </DataTable>
  );
}

export function SystemHealthPage() {
  const { health, readiness, error, loading, refresh } = useDashboardStatus();
  const now = useNow(5_000);
  const reachable = error === null;
  const workers = useApiResource((signal) => system.workers({ signal }), [], {
    refreshMs: WORKERS_POLL_MS,
    enabled: reachable,
  });
  const pipeline = useApiResource((signal) => marketData.pipelineHealth({ signal }), [], {
    refreshMs: ERRORS_POLL_MS,
    enabled: reachable,
  });
  const providers = useApiResource((signal) => marketData.providers({ signal }), [], { enabled: reachable });
  const newsProviders = useApiResource((signal) => news.providers({ signal }), [], {
    refreshMs: ERRORS_POLL_MS,
    enabled: reachable,
  });
  const quality = useApiResource((signal) => marketData.qualityEvents({ limit: 50 }, { signal }), [], {
    refreshMs: ERRORS_POLL_MS,
    enabled: reachable,
  });
  const recovery = useApiResource((signal) => marketData.recoveryJobs({ limit: 15 }, { signal }), [], {
    refreshMs: ERRORS_POLL_MS,
    enabled: reachable,
  });
  const failedIngestion = useApiResource(
    (signal) => marketData.ingestionJobs({ status: ["FAILED"], limit: 10 }, { signal }),
    [],
    { refreshMs: ERRORS_POLL_MS, enabled: reachable },
  );

  const reloadAll = () => {
    refresh();
    workers.reload();
    pipeline.reload();
    providers.reload();
    newsProviders.reload();
    quality.reload();
    recovery.reload();
    failedIngestion.reload();
  };

  const errors: ErrorRow[] = [];
  for (const w of workers.data?.workers ?? []) {
    if (w.last_error) errors.push({ at: w.last_error_at ?? workers.data?.checked_at ?? "", source: w.title, severity: "ERROR", message: w.last_error, count: 1 });
  }
  // Repeated data-quality events (e.g. one per skipped trade id) fold into one row each.
  const folded = new Map<string, ErrorRow>();
  for (const q of quality.data ?? []) {
    if (q.severity === "INFO") continue;
    const source = `Data quality · ${q.provider_symbol ?? q.provider}`;
    const key = `${source}|${q.event_type}|${q.severity}`;
    const seen = folded.get(key);
    if (seen) {
      seen.count += 1;
      if (q.detected_at > seen.at) Object.assign(seen, { at: q.detected_at, message: `${humanize(q.event_type)}: ${q.message}` });
    } else {
      folded.set(key, { at: q.detected_at, source, severity: q.severity, message: `${humanize(q.event_type)}: ${q.message}`, count: 1 });
    }
  }
  errors.push(...folded.values());
  for (const j of recovery.data ?? []) {
    if (j.status === "FAILED") errors.push({ at: j.completed_at ?? j.created_at, source: `Recovery · ${j.provider_symbol}`, severity: "ERROR", message: j.error ?? "recovery job failed", count: 1 });
  }
  for (const j of failedIngestion.data ?? []) {
    errors.push({ at: j.created_at, source: `Ingestion · ${j.provider_symbols.join(", ")}`, severity: "ERROR", message: j.error ?? "ingestion job failed", count: 1 });
  }
  for (const p of newsProviders.data ?? []) {
    const job = p.last_failed_job;
    if (job) errors.push({ at: job.completed_at ?? job.created_at, source: `News · ${p.display_name}`, severity: "ERROR", message: job.error ?? "news job failed", count: 1 });
  }
  // Errors before warnings, newest first within each.
  const rank = (e: ErrorRow) => (e.severity === "WARNING" ? 1 : 0);
  errors.sort((a, b) => rank(a) - rank(b) || b.at.localeCompare(a.at));
  const errorSourcesLoading = workers.loading || quality.loading || recovery.loading || failedIngestion.loading;
  const errorSourceFailures = [workers.error, quality.error, recovery.error, failedIngestion.error, newsProviders.error].filter(
    (e): e is string => e !== null,
  );

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="System Health"
        description="API, dependencies, background workers, providers, recent errors and recovery activity, as the backend reports them. Read-only: nothing here starts, stops or reconfigures anything."
        actions={<RefreshButton onClick={reloadAll} loading={loading || workers.loading} />}
      />

      {error && (
        <div role="alert" className="border-negative/40 bg-negative/10 text-negative mb-3 rounded-sm border p-3 text-sm">
          {error}. Check that the backend is running. Worker, provider and error panels are hidden until it answers.
        </div>
      )}

      {loading && !readiness && !error && <Loading label="Checking…" />}

      {health && (
        <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
          <Stat label="API" value={health.status === "ok" ? "Live" : health.status} tone="positive" />
          <Stat label="Trading mode" value={health.trading_mode} tone="caution" hint="simulated paper only" />
          <Stat label="Environment" value={health.environment} />
          <Stat label="Version" value={health.version} />
          <Stat label="Uptime" value={formatUptime(health.uptime_seconds)} hint={<Timestamp iso={health.time} />} />
          <Stat
            label="Readiness"
            value={readiness ? (readiness.status === "ready" ? "Ready" : "Not ready") : MISSING}
            tone={readiness?.status === "ready" ? "positive" : readiness ? "negative" : undefined}
          />
        </div>
      )}

      {readiness && (
        <Panel title="Dependencies" subtitle={<>checked <Timestamp iso={readiness.checked_at} /></>} bodyClassName="">
          <DataTable label="Dependencies">
            <thead>
              <tr>
                <th scope="col" className={TH}>Component</th>
                <th scope="col" className={TH}>Status</th>
                <th scope="col" className={`${TH} text-right`}>Latency</th>
                <th scope="col" className={TH}>Detail</th>
              </tr>
            </thead>
            <tbody>
              {readiness.components.map((component) => (
                <tr key={component.name}>
                  <td className={TD}>{COMPONENT_LABELS[component.name] ?? component.name}</td>
                  <td className={TD}>
                    <StatusPill tone={component.status === "ok" ? "positive" : "negative"} label={component.status === "ok" ? "OK" : "Error"} />
                  </td>
                  <td className={`${TD} num text-right`}>{component.latency_ms === null ? "—" : `${component.latency_ms.toFixed(1)} ms`}</td>
                  <td className={`${TD} text-ink-muted num whitespace-normal!`}>{component.detail ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        </Panel>
      )}

      {reachable && (
        <>
          <Panel
            className="mt-3"
            title="Background workers"
            subtitle={<span className="flex items-center gap-1.5"><OriginBadge origin="RECORDED" />{workers.data && <>checked <Timestamp iso={workers.data.checked_at} /></>}</span>}
            bodyClassName=""
          >
            {workers.error && !workers.data ? (
              <div className="p-3"><ErrorState message={workers.error} onRetry={workers.reload} /></div>
            ) : !workers.data ? (
              <Loading label="Loading workers…" />
            ) : (
              <>
                <WorkersTable workers={workers.data.workers} now={now} />
                <ul className="text-ink-faint space-y-0.5 px-3 py-2 text-2xs">
                  {workers.data.notes.map((n) => (
                    <li key={n}>{n}</li>
                  ))}
                </ul>
              </>
            )}
          </Panel>

          <div className="mt-3 grid gap-3 lg:grid-cols-2">
            <Panel title="Market-data provider" actions={<Link to="/data-health" className="text-accent text-xs hover:underline">Data Health</Link>}>
              {(pipeline.error && !pipeline.data) || (providers.error && !providers.data) ? (
                <ErrorState message={pipeline.error ?? providers.error ?? ""} onRetry={() => { pipeline.reload(); providers.reload(); }} compact />
              ) : !pipeline.data || !providers.data ? (
                <Loading />
              ) : (
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={PIPELINE_TONES[pipeline.data.pipeline_health]} dot>Pipeline {pipeline.data.pipeline_health}</Badge>
                    <Badge>Stream {humanize(pipeline.data.stream_health)}</Badge>
                  </div>
                  {providers.data.length === 0 ? (
                    <EmptyState title="No market-data provider registered" />
                  ) : (
                    providers.data.map((p) => (
                      <KeyValues
                        key={`${p.provider}-${p.feed}`}
                        items={[
                          { label: "Provider", value: <span>{p.capabilities.display_name} · <span className="num">{p.feed.toUpperCase()}</span></span> },
                          { label: "Credentials", value: <span>{humanize(p.credentials)} <span className="text-ink-faint">(status only)</span></span> },
                          {
                            label: "Configuration",
                            value: p.configuration.issues.length === 0 ? (
                              <span className="text-positive">no issues</span>
                            ) : (
                              <span className="text-caution">{p.configuration.issues.map((i) => `${i.severity}: ${i.message}`).join("; ")}</span>
                            ),
                          },
                          { label: "Last message", value: <Timestamp iso={pipeline.data?.last_message_at} age now={now} /> },
                          { label: "Last backfill", value: <Timestamp iso={pipeline.data?.last_successful_backfill_at} age now={now} /> },
                          {
                            label: "Recovery queue",
                            value: <span className="num">{pipeline.data?.pending_recovery_jobs ?? 0} pending · {pipeline.data?.failed_recovery_jobs ?? 0} failed</span>,
                          },
                        ]}
                      />
                    ))
                  )}
                </div>
              )}
            </Panel>

            <Panel title="News providers" actions={<Link to="/news" className="text-accent text-xs hover:underline">News & Events</Link>}>
              {newsProviders.error && !newsProviders.data ? (
                <ErrorState message={newsProviders.error} onRetry={newsProviders.reload} compact />
              ) : !newsProviders.data ? (
                <Loading />
              ) : newsProviders.data.length === 0 ? (
                <EmptyState title="No news provider registered" />
              ) : (
                <div className="space-y-3">
                  {newsProviders.data.map((p) => (
                    <KeyValues
                      key={p.provider}
                      items={[
                        { label: "Provider", value: <span className="font-medium">{p.display_name}</span> },
                        { label: "Credentials", value: <span>{humanize(p.credentials)} <span className="text-ink-faint">(status only)</span></span> },
                        { label: "Stream", value: humanize(p.stream_health) },
                        { label: "Articles stored", value: <span className="num">{p.articles_stored.toLocaleString("en-US")}</span> },
                        { label: "Newest received", value: <Timestamp iso={p.newest_article_received_at} age now={now} /> },
                        { label: "Last job", value: p.last_job ? `${humanize(p.last_job.kind)} · ${humanize(p.last_job.status)}` : MISSING },
                      ]}
                    />
                  ))}
                </div>
              )}
            </Panel>
          </div>

          <Panel className="mt-3" title="Recent errors and warnings" subtitle="worker errors, data-quality events, failed recovery, ingestion and news jobs; repeats folded" bodyClassName="">
            {errorSourceFailures.length > 0 && (
              <div className="p-2">
                <Notice tone="caution">Some error sources could not be read: {errorSourceFailures.join("; ")}</Notice>
              </div>
            )}
            {errors.length === 0 ? (
              errorSourcesLoading ? (
                <Loading />
              ) : (
                <div className="px-3">
                  <EmptyState title="No recent errors recorded">
                    Checked the workers' last errors, the 50 newest data-quality events, the 15 newest recovery jobs, failed
                    ingestion jobs and each news provider's last failed job.
                  </EmptyState>
                </div>
              )
            ) : (
              <DataTable label="Recent errors">
                <thead>
                  <tr>
                    <th scope="col" className={TH}>When</th>
                    <th scope="col" className={TH}>Severity</th>
                    <th scope="col" className={TH}>Source</th>
                    <th scope="col" className={TH}>Message</th>
                    <th scope="col" className={`${TH} text-right`}>Count</th>
                  </tr>
                </thead>
                <tbody>
                  {errors.slice(0, 25).map((e, i) => (
                    <tr key={`${e.source}-${e.at}-${i}`}>
                      <td className={TD}><Timestamp iso={e.at} /></td>
                      <td className={TD}><Badge tone={SEVERITY_TONES[e.severity]}>{e.severity}</Badge></td>
                      <td className={TD}>{e.source}</td>
                      <td className={`${TD} text-ink-muted whitespace-normal!`}>
                        {e.message}
                        {e.count > 1 && <span className="text-ink-faint"> (newest of {e.count})</span>}
                      </td>
                      <td className={`${TD} num text-right`}>{e.count}</td>
                    </tr>
                  ))}
                </tbody>
              </DataTable>
            )}
          </Panel>

          <Panel
            className="mt-3"
            title="Recovery activity"
            subtitle="newest market-data recovery jobs"
            actions={<Link to="/ingestion" className="text-accent text-xs hover:underline">Ingestion</Link>}
            bodyClassName=""
          >
            {recovery.error && !recovery.data ? (
              <div className="p-3"><ErrorState message={recovery.error} onRetry={recovery.reload} /></div>
            ) : !recovery.data ? (
              <Loading />
            ) : recovery.data.length === 0 ? (
              <div className="px-3">
                <EmptyState title="No recovery jobs recorded">Recovery jobs appear when the recovery worker finds and backfills gaps.</EmptyState>
              </div>
            ) : (
              <DataTable label="Recovery jobs">
                <thead>
                  <tr>
                    <th scope="col" className={TH}>Created</th>
                    <th scope="col" className={TH}>Symbol</th>
                    <th scope="col" className={TH}>Data</th>
                    <th scope="col" className={TH}>Missing interval</th>
                    <th scope="col" className={TH}>Status</th>
                    <th scope="col" className={`${TH} text-right`}>Recovered</th>
                    <th scope="col" className={`${TH} text-right`}>Attempts</th>
                  </tr>
                </thead>
                <tbody>
                  {recovery.data.map((j) => (
                    <tr key={j.id}>
                      <td className={TD}><Timestamp iso={j.created_at} /></td>
                      <td className={`${TD} num`}>{j.provider_symbol}</td>
                      <td className={`${TD} num`}>{j.data_type}{j.timeframe ? ` ${j.timeframe}` : ""}</td>
                      <td className={TD}><Timestamp iso={j.missing_start} /> – <Timestamp iso={j.missing_end} /></td>
                      <td className={TD}>
                        <Badge tone={j.status === "FAILED" ? "negative" : j.status === "COMPLETED" ? "positive" : j.status === "PARTIALLY_COMPLETED" ? "caution" : "neutral"}>
                          {humanize(j.status)}
                        </Badge>
                      </td>
                      <td className={`${TD} num text-right`}>{j.records_recovered.toLocaleString("en-US")}</td>
                      <td className={`${TD} num text-right`}>{j.attempts}/{j.max_attempts}</td>
                    </tr>
                  ))}
                </tbody>
              </DataTable>
            )}
          </Panel>
        </>
      )}
    </section>
  );
}
