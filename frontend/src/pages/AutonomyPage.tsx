import { OctagonX, Pause, PowerOff } from "lucide-react";
import { type FormEvent, useState } from "react";

import {
  Badge,
  Button,
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
import { MISSING, humanize } from "../lib/format";
import { ApiError } from "../services/api";
import { orchestration } from "../services/orchestration";
import type {
  ControlState,
  IntentStatus,
  JobStatus,
  OrchestrationStatus,
  TradingMode,
} from "../types/orchestration";

const REFRESH_MS = 15_000;

const MODE_TONE: Record<TradingMode, Tone> = {
  DISABLED: "neutral",
  ENABLED: "positive",
  PAUSED: "caution",
  EMERGENCY_STOP: "negative",
};
const MODE_LABEL: Record<TradingMode, string> = {
  DISABLED: "Disabled",
  ENABLED: "Enabled",
  PAUSED: "Paused",
  EMERGENCY_STOP: "Emergency stop",
};
const INTENT_TONE: Record<IntentStatus, Tone> = {
  RECEIVED: "info",
  ORDER_CREATED: "positive",
  RISK_REJECTED: "neutral",
  EXECUTION_REFUSED: "caution",
  NOT_TRADED: "neutral",
  INVALID: "caution",
  DEAD: "negative",
};
const JOB_TONE: Record<JobStatus, Tone> = {
  RUNNING: "info",
  SUCCEEDED: "positive",
  FAILED: "caution",
  DEAD: "negative",
};
const HEALTH_TONE: Record<string, Tone> = {
  RUNNING: "positive",
  DEGRADED: "caution",
  STALE: "negative",
  FAILED: "negative",
  STOPPED: "neutral",
  DISABLED: "neutral",
  NOT_REPORTED: "neutral",
};

function errorText(reason: unknown): string {
  return reason instanceof ApiError ? `${reason.message}${reason.status ? ` (HTTP ${reason.status})` : ""}` : String(reason);
}

export function AutonomyPage() {
  const [refresh, setRefresh] = useState(0);
  const status = useApiResource((signal) => orchestration.status({ signal }), [], { refreshMs: REFRESH_MS });
  // Reload in place (the control panel keeps its confirmation) and refresh the lists.
  const reload = () => {
    status.reload();
    setRefresh((n) => n + 1);
  };
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Autonomous Trading"
        description="The trading orchestrator: strategies propose, the AI coordinator prioritizes, the risk engine decides and reserves, the execution engine places simulated paper orders. New entries need an operator to enable them; exits and monitoring always run."
        actions={<RefreshButton onClick={reload} loading={status.loading} />}
      />
      {status.status === "loading" && !status.data && <Loading label="Loading the trading control…" />}
      {status.status === "error" && <ErrorState message={status.error ?? ""} onRetry={status.reload} />}
      {status.data && <Autonomy data={status.data} refreshKey={refresh} onChanged={reload} />}
    </section>
  );
}

function Autonomy({ data, refreshKey, onChanged }: { data: OrchestrationStatus; refreshKey: number; onChanged: () => void }) {
  const mode = data.control.mode;
  return (
    <div className="flex flex-col gap-3">
      <Notice tone="info">
        Paper trading only. Every order is a simulated paper order filled by the isolated paper broker from stored market
        data; no brokerage connection exists.
      </Notice>
      {mode === "EMERGENCY_STOP" && (
        <Notice tone="negative">
          <strong>Emergency stop is engaged.</strong> No new entries; working entry orders were cancelled and the risk
          kill switch is engaged. Open positions stay protected by the position monitor.
        </Notice>
      )}
      {mode === "DISABLED" && data.control.default && (
        <Notice tone="neutral">
          Autonomous trading has never been enabled for this account. It stays disabled until an operator enables it from
          the command line.
        </Notice>
      )}
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4" aria-label="Trade intents summary">
        <Stat label="Trading mode" value={MODE_LABEL[mode]} tone={MODE_TONE[mode]} hint={data.account_name} />
        <Stat label="Orders created" value={data.intents.ORDER_CREATED} hint="simulated paper entries" />
        <Stat label="Risk rejected" value={data.intents.RISK_REJECTED} hint="no-trade decisions" />
        <Stat
          label="Pending / dead"
          value={`${data.intents.RECEIVED} / ${data.intents.DEAD}`}
          tone={data.intents.DEAD > 0 ? "negative" : undefined}
          hint="intents awaiting delivery / failed"
        />
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        <ControlPanel control={data.control} onChanged={onChanged} />
        <OrchestratorPanel data={data} />
      </div>
      <IntentsPanel refreshKey={refreshKey} />
      <JobsPanel refreshKey={refreshKey} />
      <HistoryPanel refreshKey={refreshKey} />
    </div>
  );
}

type Action = "pause" | "disable" | "emergency";

function ControlPanel({ control, onChanged }: { control: ControlState; onChanged: () => void }) {
  const [reason, setReason] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState<Action | null>(null);
  const [message, setMessage] = useState<{ tone: Tone; text: string } | null>(null);
  const run = async (action: Action, event?: FormEvent) => {
    event?.preventDefault();
    setBusy(action);
    setMessage(null);
    try {
      const t = token || undefined;
      if (action === "emergency") {
        const result = await orchestration.emergencyStop(reason, t);
        setMessage({
          tone: "negative",
          text: `Emergency stop ${result.changed ? "engaged" : "was already engaged"}: ${result.cancel_requested.length} entry order(s) cancelled${result.cancel_failed.length ? `, ${result.cancel_failed.length} could not be` : ""}.`,
        });
      } else {
        const result = await (action === "pause" ? orchestration.pause(reason, t) : orchestration.disable(reason, t));
        setMessage({
          tone: "caution",
          text: result.changed ? `Trading is now ${MODE_LABEL[result.control.mode].toLowerCase()}.` : "Nothing changed.",
        });
      }
      onChanged();
    } catch (reason_) {
      setMessage({ tone: "negative", text: errorText(reason_) });
    } finally {
      setBusy(null);
    }
  };
  const input = "border-border-strong bg-surface focus:border-accent h-7 w-full rounded-sm border px-2 text-xs outline-none";
  const noReason = reason.trim().length < 3;
  const mode = control.mode;
  return (
    <Panel
      title="Trading control"
      actions={
        <Badge tone={MODE_TONE[mode]} dot>
          {MODE_LABEL[mode]}
        </Badge>
      }
    >
      <KeyValues
        items={[
          { label: "New entries", value: control.entries_allowed ? "Allowed" : "Not allowed" },
          { label: "Since", value: control.since ? <Timestamp iso={control.since} /> : "never changed (default)" },
          { label: "Changed by", value: control.actor ?? MISSING },
          { label: "Reason", value: control.reason ?? MISSING },
          { label: "Event", value: control.sequence ? `#${control.sequence}` : MISSING },
        ]}
      />
      <form
        onSubmit={(e) => void run("emergency", e)}
        className="border-border mt-3 space-y-2.5 border-t pt-3"
        aria-label="Trading controls"
      >
        <label className="text-ink-muted block text-2xs font-semibold uppercase">
          Reason
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className={`${input} mt-1 normal-case`}
            aria-label="Reason"
            placeholder="why trading must pause or stop"
          />
        </label>
        <label className="text-ink-muted block text-2xs font-semibold uppercase">
          Operator token
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            autoComplete="off"
            className={`${input} mt-1 normal-case`}
            aria-label="Operator token"
            placeholder="only if ATLAS_OPERATOR_TOKEN is set"
          />
        </label>
        <div className="flex flex-wrap gap-2">
          <Button disabled={busy !== null || noReason || mode !== "ENABLED"} onClick={() => void run("pause")}>
            <Pause aria-hidden className="size-3.5" />
            {busy === "pause" ? "Pausing…" : "Pause"}
          </Button>
          <Button
            disabled={busy !== null || noReason || (mode !== "ENABLED" && mode !== "PAUSED")}
            onClick={() => void run("disable")}
          >
            <PowerOff aria-hidden className="size-3.5" />
            {busy === "disable" ? "Disabling…" : "Disable"}
          </Button>
          <Button type="submit" variant="primary" disabled={busy !== null || noReason}>
            <OctagonX aria-hidden className="size-3.5" />
            {busy === "emergency" ? "Stopping…" : "Emergency stop"}
          </Button>
        </div>
        {message && <Notice tone={message.tone}>{message.text}</Notice>}
        <p className="text-ink-faint text-2xs">
          The dashboard can only stop trading (audited, with a reason). Enabling, resuming and clearing an emergency are
          operator actions from the command line, e.g.{" "}
          <code>python -m app.orchestration.cli enable --actor operator:&lt;name&gt; --reason …</code>. An emergency stop
          clears to <em>disabled</em>, and the risk kill switch must be released separately.
        </p>
      </form>
    </Panel>
  );
}

function OrchestratorPanel({ data }: { data: OrchestrationStatus }) {
  const o = data.orchestrator;
  const counters = Object.entries(o.counters).sort(([a], [b]) => a.localeCompare(b));
  return (
    <Panel
      title="Orchestrator worker"
      actions={
        <Badge tone={HEALTH_TONE[o.health] ?? "neutral"} dot>
          {humanize(o.health)}
        </Badge>
      }
    >
      <p className="text-ink-muted mb-2 text-xs">{o.detail}</p>
      <KeyValues
        items={[
          { label: "Recorded state", value: o.state ? humanize(o.state) : MISSING },
          { label: "Heartbeat", value: <Timestamp iso={o.heartbeat_at} /> },
          { label: "Last tick", value: o.last_tick_at ? <><Timestamp iso={o.last_tick_at} /> · {o.last_tick_ms} ms</> : MISSING },
          { label: "Worker", value: o.worker_id ?? MISSING },
          {
            label: "Enabled (API env)",
            value: o.worker_enabled_in_api_env === null ? "unknown" : o.worker_enabled_in_api_env ? "yes" : "no",
          },
          { label: "Outbox", value: `${data.outbox.counts.PENDING ?? 0} pending · ${data.outbox.counts.DEAD ?? 0} dead` },
          { label: "Jobs", value: `${data.jobs.SUCCEEDED} succeeded · ${data.jobs.FAILED} failed · ${data.jobs.DEAD} dead` },
        ]}
      />
      {counters.length > 0 && (
        <p className="text-ink-faint num mt-2 text-2xs">
          {counters.map(([k, v]) => `${humanize(k)} ${v}`).join(" · ")}
        </p>
      )}
      {o.last_error && (
        <div className="mt-2">
          <Notice tone="caution">
            Last error <Timestamp iso={o.last_error_at} />: {o.last_error}
          </Notice>
        </div>
      )}
    </Panel>
  );
}

function IntentsPanel({ refreshKey }: { refreshKey: number }) {
  const intents = useApiResource((signal) => orchestration.intents(null, { signal }), [refreshKey], {
    refreshMs: REFRESH_MS,
    isEmpty: (rows) => rows.length === 0,
  });
  return (
    <Panel title="Trade intents" subtitle="prioritized AI decisions and what the risk and execution engines did with them" bodyClassName="p-0">
      {intents.status === "loading" && <Loading />}
      {intents.status === "error" && <ErrorState compact message={intents.error ?? ""} onRetry={intents.reload} />}
      {intents.status === "empty" && (
        <EmptyState title="No trade intents yet">Intents are taken in only while trading is enabled.</EmptyState>
      )}
      {intents.data && intents.data.length > 0 && (
        <DataTable label="Trade intents">
          <thead>
            <tr>
              <th className={TH}>Received</th>
              <th className={TH}>Symbol</th>
              <th className={TH}>Direction</th>
              <th className={TH}>Outcome</th>
              <th className={TH}>Reasons</th>
              <th className={TH}>Order</th>
            </tr>
          </thead>
          <tbody>
            {intents.data.map((i) => (
              <tr key={i.id}>
                <td className={TD}>
                  <Timestamp iso={i.received_at} />
                </td>
                <td className={`${TD} font-medium`}>{i.symbol ?? MISSING}</td>
                <td className={TD}>{i.direction ?? MISSING}</td>
                <td className={TD}>
                  <Badge tone={INTENT_TONE[i.status]}>{humanize(i.status)}</Badge>
                </td>
                <td className={`${TD} max-w-80 truncate`} title={i.reasons.map((r) => r.message ?? r.code).join("; ")}>
                  {i.error ?? (i.reasons.map((r) => r.code).filter(Boolean).join(", ") || MISSING)}
                </td>
                <td className={`${TD} num text-ink-muted`}>{i.order_id ? i.order_id.slice(0, 8) : MISSING}</td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}

function JobsPanel({ refreshKey }: { refreshKey: number }) {
  const jobs = useApiResource((signal) => orchestration.jobs({ signal }), [refreshKey], {
    refreshMs: REFRESH_MS,
    isEmpty: (rows) => rows.length === 0,
  });
  return (
    <Panel title="Scheduled jobs" subtitle="risk monitoring, reconciliations, worker health; one run per slot" bodyClassName="p-0">
      {jobs.status === "loading" && <Loading />}
      {jobs.status === "error" && <ErrorState compact message={jobs.error ?? ""} onRetry={jobs.reload} />}
      {jobs.status === "empty" && <EmptyState title="No scheduled job has run yet" />}
      {jobs.data && jobs.data.length > 0 && (
        <DataTable label="Scheduled jobs">
          <thead>
            <tr>
              <th className={TH}>Job</th>
              <th className={TH}>Slot</th>
              <th className={TH}>Status</th>
              <th className={TH}>Attempts</th>
              <th className={TH}>Finished</th>
              <th className={TH}>Result</th>
            </tr>
          </thead>
          <tbody>
            {jobs.data.map((j) => {
              const passed = j.result["passed"];
              return (
                <tr key={j.id}>
                  <td className={`${TD} font-medium`}>{humanize(j.kind)}</td>
                  <td className={`${TD} num text-ink-muted`}>{j.slot}</td>
                  <td className={TD}>
                    <Badge tone={JOB_TONE[j.status]}>{humanize(j.status)}</Badge>
                    {j.reclaimed > 0 && <span className="text-caution ml-1 text-2xs">reclaimed</span>}
                  </td>
                  <td className={`${TD} num`}>{j.attempts}</td>
                  <td className={TD}>
                    <Timestamp iso={j.finished_at} />
                  </td>
                  <td className={`${TD} max-w-80 truncate`} title={j.error ?? undefined}>
                    {j.error ?? (passed === true ? "passed" : passed === false ? "FAILED: see system health" : MISSING)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}

function HistoryPanel({ refreshKey }: { refreshKey: number }) {
  const history = useApiResource((signal) => orchestration.history({ signal }), [refreshKey], {
    isEmpty: (rows) => rows.length === 0,
  });
  return (
    <Panel title="Control history" subtitle="append-only, audited operator actions" bodyClassName="p-0">
      {history.status === "loading" && <Loading />}
      {history.status === "error" && <ErrorState compact message={history.error ?? ""} onRetry={history.reload} />}
      {history.status === "empty" && <EmptyState title="No operator has changed the trading control" />}
      {history.data && history.data.length > 0 && (
        <DataTable label="Control history">
          <thead>
            <tr>
              <th className={TH}>#</th>
              <th className={TH}>When</th>
              <th className={TH}>Action</th>
              <th className={TH}>Mode</th>
              <th className={TH}>Operator</th>
              <th className={TH}>Reason</th>
            </tr>
          </thead>
          <tbody>
            {history.data.map((h) => (
              <tr key={h.sequence}>
                <td className={`${TD} num`}>{h.sequence}</td>
                <td className={TD}>
                  <Timestamp iso={h.occurred_at} />
                </td>
                <td className={`${TD} font-medium`}>{humanize(h.action)}</td>
                <td className={TD}>
                  {MODE_LABEL[h.from_mode]} → <Badge tone={MODE_TONE[h.to_mode]}>{MODE_LABEL[h.to_mode]}</Badge>
                </td>
                <td className={TD}>{h.actor}</td>
                <td className={`${TD} max-w-80 truncate`} title={h.reason}>
                  {h.reason}
                </td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}
