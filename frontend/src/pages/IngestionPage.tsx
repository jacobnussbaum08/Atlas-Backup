import { ChevronDown, ChevronRight, Send } from "lucide-react";
import { type FormEvent, Fragment, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router";

import { JobStatusBadge } from "../components/status";
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
  TD,
  TH,
  Timestamp,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { CHART_TIMEFRAMES } from "../lib/candles";
import { formatCount, formatDateTime, humanize, MISSING } from "../lib/format";
import { dateInZone, useDisplayZone, wallTimeToUtc, zoneLabel } from "../lib/time";
import { ApiError } from "../services/api";
import { marketData } from "../services/marketData";
import type { IngestionJob, IngestionJobStatus, RecoveryJobStatus } from "../types/marketData";

const PAGE = 25;
const TERMINAL: ReadonlySet<string> = new Set(["COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "CANCELLED"]);
const JOB_STATUSES: IngestionJobStatus[] = ["PENDING", "RUNNING", "COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "CANCELLED"];

export function IngestionPage() {
  const [submitted, setSubmitted] = useState<IngestionJob | null>(null);
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Ingestion"
        description="Historical retrieval and automatic recovery jobs. Requests go through the backend, which validates them and enforces the provider's limits."
      />
      <div className="grid gap-3 xl:grid-cols-[22rem_minmax(0,1fr)]">
        <FetchForm onSubmitted={setSubmitted} />
        <SubmittedJob job={submitted} />
      </div>
      <JobsPanel refreshKey={submitted?.job_id ?? ""} />
      <RecoveryJobsPanel />
    </section>
  );
}

export interface FetchFormValues {
  symbol: string;
  timeframe: string;
  startDate: string;
  startTime: string;
  endDate: string;
  endTime: string;
}

/** Client-side checks; the backend validates again and has the final word. */
export function validateFetch(
  values: FetchFormValues,
  zone: Parameters<typeof wallTimeToUtc>[2],
  now: Date,
): { error: string } | { start: Date; end: Date } {
  if (!values.symbol) return { error: "Choose an asset." };
  if (!values.timeframe) return { error: "Choose a timeframe." };
  const start = wallTimeToUtc(values.startDate, values.startTime || "00:00", zone);
  const end = wallTimeToUtc(values.endDate, values.endTime || "23:59", zone);
  if (!start || !end) return { error: "Enter valid start and end dates." };
  if (end <= start) return { error: "The end must be after the start." };
  if (start > now) return { error: "The start is in the future: there is no history to retrieve." };
  return { start, end: end > now ? now : end };
}

function FetchForm({ onSubmitted }: { onSubmitted: (job: IngestionJob) => void }) {
  const { zone } = useDisplayZone();
  const [params] = useSearchParams();
  const assets = useApiResource((signal) => marketData.assets({ signal }), []);
  const config = useApiResource((signal) => marketData.configuration({ signal }), []);
  const today = dateInZone(new Date(), zone);
  const prefillStart = params.get("start");
  const prefillEnd = params.get("end");
  const [values, setValues] = useState<FetchFormValues>({
    symbol: params.get("symbol") ?? "",
    timeframe: params.get("timeframe") ?? "1Min",
    startDate: prefillStart ? dateInZone(new Date(prefillStart), zone) : today,
    startTime: "00:00",
    endDate: prefillEnd ? dateInZone(new Date(prefillEnd), zone) : today,
    endTime: "23:59",
  });
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!values.symbol && assets.data?.[0]) setValues((v) => ({ ...v, symbol: assets.data?.[0]?.symbol ?? "" }));
  }, [assets.data, values.symbol]);

  const checked = useMemo(() => validateFetch(values, zone, new Date()), [values, zone]);
  const set = (key: keyof FetchFormValues) => (e: { target: { value: string } }) =>
    setValues((v) => ({ ...v, [key]: e.target.value }));

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if ("error" in checked) {
      setError(checked.error);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const job = await marketData.submitHistoricalFetch(
        { symbols: [values.symbol], timeframe: values.timeframe, start: checked.start.toISOString(), end: checked.end.toISOString() },
        token || undefined,
      );
      onSubmitted(job);
    } catch (reason) {
      setError(reason instanceof ApiError ? `${reason.message}${reason.status ? ` (HTTP ${reason.status})` : ""}` : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const input = "border-border-strong bg-surface focus:border-accent h-7 w-full rounded-sm border px-2 text-xs outline-none";
  return (
    <Panel title="Request historical bars" subtitle={`times in ${zoneLabel(zone)}`}>
      <form onSubmit={(e) => void submit(e)} className="space-y-2.5" aria-label="Historical retrieval request">
        <div className="grid grid-cols-2 gap-2">
          <label className="text-ink-muted text-2xs font-semibold uppercase">
            Asset
            <select value={values.symbol} onChange={set("symbol")} className={`${input} mt-1 normal-case`} aria-label="Asset">
              {!assets.data && <option value="">Loading…</option>}
              {assets.data?.map((a) => (
                <option key={a.asset_id} value={a.symbol}>
                  {a.symbol}
                </option>
              ))}
            </select>
          </label>
          <label className="text-ink-muted text-2xs font-semibold uppercase">
            Timeframe
            <select value={values.timeframe} onChange={set("timeframe")} className={`${input} mt-1 normal-case`} aria-label="Timeframe">
              {CHART_TIMEFRAMES.map((tf) => (
                <option key={tf} value={tf}>{tf}</option>
              ))}
            </select>
          </label>
          <label className="text-ink-muted text-2xs font-semibold uppercase">
            Start date
            <input type="date" value={values.startDate} onChange={set("startDate")} className={`${input} num mt-1`} aria-label="Start date" />
          </label>
          <label className="text-ink-muted text-2xs font-semibold uppercase">
            Start time
            <input type="time" value={values.startTime} onChange={set("startTime")} className={`${input} num mt-1`} aria-label="Start time" />
          </label>
          <label className="text-ink-muted text-2xs font-semibold uppercase">
            End date
            <input type="date" value={values.endDate} onChange={set("endDate")} className={`${input} num mt-1`} aria-label="End date" />
          </label>
          <label className="text-ink-muted text-2xs font-semibold uppercase">
            End time
            <input type="time" value={values.endTime} onChange={set("endTime")} className={`${input} num mt-1`} aria-label="End time" />
          </label>
        </div>
        {config.data?.operator_token_configured && (
          <label className="text-ink-muted block text-2xs font-semibold uppercase">
            Operator token
            <input
              type="password"
              autoComplete="off"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              className={`${input} mt-1`}
              aria-label="Operator token"
            />
            <span className="text-ink-faint mt-0.5 block font-normal normal-case">
              Required by this server for operational actions. Kept in memory only.
            </span>
          </label>
        )}
        <div className="text-ink-muted text-2xs">
          {"error" in checked ? (
            <span className="text-caution">{checked.error}</span>
          ) : (
            <span className="num">
              UTC {checked.start.toISOString().replace(".000Z", "Z")} → {checked.end.toISOString().replace(".000Z", "Z")}
            </span>
          )}
        </div>
        {error && <ErrorState message={error} compact />}
        <Button type="submit" variant="primary" disabled={busy || "error" in checked}>
          <Send aria-hidden className="size-3.5" />
          {busy ? "Submitting…" : "Submit request"}
        </Button>
        <p className="text-ink-faint text-2xs">
          Feed: {config.data ? config.data.feed.toUpperCase() : MISSING} (fixed by configuration). Bars already stored are
          recognised as duplicates, not stored twice.
        </p>
      </form>
    </Panel>
  );
}

function SubmittedJob({ job }: { job: IngestionJob | null }) {
  const [current, setCurrent] = useState<IngestionJob | null>(job);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setCurrent(job);
    setError(null);
    if (!job) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const next = await marketData.ingestionJob(job.job_id);
        if (stopped) return;
        setCurrent(next);
        setError(null);
        if (!TERMINAL.has(next.status)) timer = setTimeout(() => void poll(), 2_000);
      } catch (reason) {
        if (stopped) return;
        setError(reason instanceof Error ? reason.message : String(reason));
        timer = setTimeout(() => void poll(), 5_000);
      }
    };
    timer = setTimeout(() => void poll(), 1_000);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [job]);

  if (!current) {
    return (
      <Panel title="Request status">
        <EmptyState title="No request submitted in this session">
          Submitted requests appear here and in the job list below.
        </EmptyState>
      </Panel>
    );
  }
  const message: Record<string, { tone: Parameters<typeof Notice>[0]["tone"]; text: string }> = {
    PENDING: { tone: "info", text: "Accepted: the backend queued the retrieval." },
    RUNNING: { tone: "info", text: "Running: pages are being retrieved from the provider." },
    COMPLETED: { tone: "positive", text: "Completed: every requested page was retrieved and stored." },
    PARTIALLY_COMPLETED: { tone: "caution", text: "Partially completed: some data could not be retrieved or was rejected." },
    FAILED: { tone: "negative", text: "Failed: nothing more will be retrieved for this job." },
    CANCELLED: { tone: "neutral", text: "Cancelled." },
  };
  const info = message[current.status] ?? { tone: "neutral" as const, text: current.status };
  return (
    <Panel title="Request status" subtitle={<span className="num">{current.job_id}</span>}>
      <div className="space-y-2" aria-live="polite">
        <div className="flex items-center gap-2">
          <JobStatusBadge status={current.status} />
          <span className="text-xs">{current.provider_symbols.join(", ")} · {current.timeframe} · {current.feed.toUpperCase()}</span>
        </div>
        <Notice tone={info.tone}>{info.text}</Notice>
        {current.error && <Notice tone="negative">{current.error}</Notice>}
        {error && <ErrorState message={`Status refresh failed: ${error}`} compact />}
        <Counters job={current} />
      </div>
    </Panel>
  );
}

function Counters({ job }: { job: IngestionJob }) {
  const c = job.counters;
  const items: [string, number][] = [
    ["Pages", c.pages_retrieved],
    ["Retrieved", c.records_retrieved],
    ["Stored", c.records_stored],
    ["Duplicate", c.records_duplicate],
    ["Revised", c.records_revised],
    ["Rejected", c.records_rejected],
    ["Quarantined", c.records_quarantined],
  ];
  return (
    <dl className="grid grid-cols-4 gap-2 text-xs sm:grid-cols-7">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt className="text-ink-faint text-2xs uppercase">{label}</dt>
          <dd className={`num ${label === "Rejected" && value > 0 ? "text-negative" : ""}`}>{formatCount(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function JobsPanel({ refreshKey }: { refreshKey: string }) {
  const { zone } = useDisplayZone();
  const [status, setStatus] = useState<string>("");
  const [symbol, setSymbol] = useState("");
  const [timeframe, setTimeframe] = useState("");
  const [date, setDate] = useState("");
  const [offset, setOffset] = useState(0);
  const [expanded, setExpanded] = useState<string | null>(null);
  const assets = useApiResource((signal) => marketData.assets({ signal }), []);

  const day = date ? wallTimeToUtc(date, "00:00", zone) : null;
  const nextDay = date && day ? wallTimeToUtc(dateInZone(new Date(day.getTime() + 36 * 3_600_000), zone), "00:00", zone) : null;
  const jobs = useApiResource(
    (signal) =>
      marketData.ingestionJobs(
        {
          ...(status ? { status: [status as IngestionJobStatus] } : {}),
          ...(symbol ? { symbol } : {}),
          ...(timeframe ? { timeframe } : {}),
          ...(day ? { created_from: day.toISOString() } : {}),
          ...(nextDay ? { created_to: nextDay.toISOString() } : {}),
          limit: PAGE,
          offset,
        },
        { signal },
      ),
    [status, symbol, timeframe, date, offset, refreshKey, zone],
    { refreshMs: 10_000, isEmpty: (d) => d.length === 0 },
  );
  const select = "border-border-strong bg-surface h-7 rounded-sm border px-1.5 text-xs";
  const reset = () => setOffset(0);

  return (
    <Panel
      className="mt-3"
      title="Historical retrieval jobs"
      subtitle="newest first"
      bodyClassName=""
      actions={
        <div className="flex flex-wrap items-center gap-1.5">
          <select aria-label="Filter by status" className={select} value={status} onChange={(e) => { setStatus(e.target.value); reset(); }}>
            <option value="">All statuses</option>
            {JOB_STATUSES.map((s) => <option key={s} value={s}>{humanize(s)}</option>)}
          </select>
          <select aria-label="Filter by asset" className={select} value={symbol} onChange={(e) => { setSymbol(e.target.value); reset(); }}>
            <option value="">All assets</option>
            {assets.data?.map((a) => <option key={a.asset_id} value={a.symbol}>{a.symbol}</option>)}
          </select>
          <select aria-label="Filter by timeframe" className={select} value={timeframe} onChange={(e) => { setTimeframe(e.target.value); reset(); }}>
            <option value="">All timeframes</option>
            {CHART_TIMEFRAMES.map((tf) => <option key={tf} value={tf}>{tf}</option>)}
          </select>
          <input aria-label="Filter by creation date" type="date" className={`${select} num`} value={date} onChange={(e) => { setDate(e.target.value); reset(); }} />
          <RefreshButton onClick={jobs.reload} loading={jobs.loading} />
        </div>
      }
    >
      {jobs.status === "loading" && <Loading />}
      {jobs.status === "error" && <div className="p-2"><ErrorState message={jobs.error ?? ""} onRetry={jobs.reload} /></div>}
      {jobs.status === "empty" && <div className="px-3"><EmptyState title="No jobs match these filters" /></div>}
      {jobs.data && jobs.data.length > 0 && (
        <DataTable label="Historical retrieval jobs">
          <thead>
            <tr>
              <th className={TH} />
              <th className={TH}>Job</th>
              <th className={TH}>Assets</th>
              <th className={TH}>TF</th>
              <th className={TH}>Requested range</th>
              <th className={TH}>Feed</th>
              <th className={TH}>Status</th>
              <th className={`${TH} text-right`}>Pages</th>
              <th className={`${TH} text-right`}>Retrieved</th>
              <th className={`${TH} text-right`}>Stored</th>
              <th className={`${TH} text-right`}>Rejected</th>
              <th className={`${TH} text-right`}>Dup.</th>
              <th className={TH}>Started</th>
              <th className={TH}>Completed</th>
            </tr>
          </thead>
          <tbody>
            {jobs.data.map((job) => {
              const open = expanded === job.job_id;
              return (
                <Fragment key={job.job_id}>
                  <tr className="hover:bg-panel-raised cursor-pointer" onClick={() => setExpanded(open ? null : job.job_id)}>
                    <td className={TD}>{open ? <ChevronDown aria-hidden className="size-3.5" /> : <ChevronRight aria-hidden className="size-3.5" />}</td>
                    <td className={`${TD} num text-ink-muted`} title={job.job_id}>{job.job_id.slice(0, 8)}</td>
                    <td className={`${TD} font-medium`}>{job.provider_symbols.join(", ")}</td>
                    <td className={`${TD} num`}>{job.timeframe ?? job.data_type.toLowerCase()}</td>
                    <td className={`${TD} num text-ink-muted`}>
                      {formatDateTime(job.range_start, zone, { seconds: false, label: false })} → {formatDateTime(job.range_end, zone, { seconds: false })}
                    </td>
                    <td className={TD}><Badge>{job.feed.toUpperCase()}</Badge></td>
                    <td className={TD}><JobStatusBadge status={job.status} /></td>
                    <td className={`${TD} num text-right`}>{formatCount(job.counters.pages_retrieved)}</td>
                    <td className={`${TD} num text-right`}>{formatCount(job.counters.records_retrieved)}</td>
                    <td className={`${TD} num text-right`}>{formatCount(job.counters.records_stored)}</td>
                    <td className={`${TD} num text-right ${job.counters.records_rejected > 0 ? "text-negative" : ""}`}>{formatCount(job.counters.records_rejected)}</td>
                    <td className={`${TD} num text-right`}>{formatCount(job.counters.records_duplicate)}</td>
                    <td className={TD}><Timestamp iso={job.started_at} seconds={false} /></td>
                    <td className={TD}><Timestamp iso={job.completed_at} seconds={false} /></td>
                  </tr>
                  {open && (
                    <tr>
                      <td colSpan={14} className="bg-surface/60 border-border border-b px-4 py-2">
                        <div className="space-y-2 text-xs">
                          <p className="text-ink-muted">
                            Requested by {job.requested_by ?? MISSING} · created <Timestamp iso={job.created_at} /> · adjustment {job.adjustment}
                          </p>
                          {job.error && <Notice tone="negative">{job.error}</Notice>}
                          <Counters job={job} />
                          {job.symbols.map((s) => (
                            <p key={s.provider_symbol} className="num text-ink-muted">
                              {s.provider_symbol}: {humanize(s.status)} · stored {s.counters.records_stored} · duplicate {s.counters.records_duplicate} · rejected {s.counters.records_rejected}
                              {s.error ? ` · ${s.error}` : ""}
                            </p>
                          ))}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </DataTable>
      )}
      <div className="flex items-center gap-2 p-2 text-xs">
        <Button disabled={offset === 0} onClick={() => setOffset((o) => Math.max(0, o - PAGE))}>Previous</Button>
        <Button disabled={(jobs.data?.length ?? 0) < PAGE} onClick={() => setOffset((o) => o + PAGE)}>Next</Button>
        <span className="text-ink-faint num">rows {offset + 1}–{offset + (jobs.data?.length ?? 0)}</span>
      </div>
    </Panel>
  );
}

const RECOVERY_FILTERS: { value: string; label: string; statuses: RecoveryJobStatus[] }[] = [
  { value: "active", label: "Pending / running", statuses: ["PENDING", "RUNNING"] },
  { value: "failed", label: "Failed / partial", statuses: ["FAILED", "PARTIALLY_COMPLETED"] },
  { value: "completed", label: "Completed", statuses: ["COMPLETED"] },
  { value: "all", label: "All", statuses: [] },
];

function RecoveryJobsPanel() {
  const [filter, setFilter] = useState("all");
  const statuses = RECOVERY_FILTERS.find((f) => f.value === filter)?.statuses ?? [];
  const jobs = useApiResource(
    (signal) => marketData.recoveryJobs({ status: statuses, limit: 100 }, { signal }),
    [filter],
    { refreshMs: 15_000, isEmpty: (d) => d.length === 0 },
  );
  return (
    <Panel
      className="mt-3"
      title="Backfill and reconciliation jobs"
      subtitle="created by the recovery worker for missing intervals"
      bodyClassName=""
      actions={<Segmented label="Recovery job status" value={filter} onChange={setFilter} options={RECOVERY_FILTERS.map((f) => ({ value: f.value, label: f.label }))} />}
    >
      {jobs.status === "loading" && <Loading />}
      {jobs.status === "error" && <div className="p-2"><ErrorState message={jobs.error ?? ""} onRetry={jobs.reload} /></div>}
      {jobs.status === "empty" && <div className="px-3"><EmptyState title="No recovery jobs with this status" /></div>}
      {jobs.data && jobs.data.length > 0 && (
        <DataTable label="Recovery jobs">
          <thead>
            <tr>
              <th className={TH}>Kind</th>
              <th className={TH}>Series</th>
              <th className={TH}>Missing interval</th>
              <th className={TH}>Status</th>
              <th className={`${TH} text-right`}>Attempts</th>
              <th className={`${TH} text-right`}>Retrieved</th>
              <th className={`${TH} text-right`}>Recovered</th>
              <th className={`${TH} text-right`}>Dup.</th>
              <th className={`${TH} text-right`}>Rejected</th>
              <th className={`${TH} text-right`}>Outstanding</th>
              <th className={TH}>Completed</th>
              <th className={TH}>Error</th>
            </tr>
          </thead>
          <tbody>
            {jobs.data.map((j) => (
              <tr key={j.id}>
                <td className={TD}>{humanize(j.kind)}</td>
                <td className={`${TD} num`}>{j.provider_symbol} <span className="text-ink-muted">{j.data_type.toLowerCase()}{j.timeframe ? ` ${j.timeframe}` : ""}</span></td>
                <td className={TD}><Timestamp iso={j.missing_start} seconds={false} /> <span className="text-ink-faint">→</span> <Timestamp iso={j.missing_end} seconds={false} /></td>
                <td className={TD}><JobStatusBadge status={j.status} /></td>
                <td className={`${TD} num text-right`}>{j.attempts}/{j.max_attempts}</td>
                <td className={`${TD} num text-right`}>{formatCount(j.records_retrieved)}</td>
                <td className={`${TD} num text-right`}>{formatCount(j.records_recovered)}</td>
                <td className={`${TD} num text-right`}>{formatCount(j.records_duplicate)}</td>
                <td className={`${TD} num text-right ${j.records_rejected > 0 ? "text-negative" : ""}`}>{formatCount(j.records_rejected)}</td>
                <td className={`${TD} num text-right ${j.remaining.length > 0 ? "text-caution" : ""}`} title="Intervals still missing after this job">{j.remaining.length}</td>
                <td className={TD}><Timestamp iso={j.completed_at} seconds={false} /></td>
                <td className={`${TD} text-ink-muted max-w-xs truncate`} title={j.error ?? ""}>{j.error ?? MISSING}</td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}
