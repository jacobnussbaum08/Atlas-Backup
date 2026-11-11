import { lazy, Suspense, useMemo } from "react";
import { Link } from "react-router";

import type { Resource } from "../hooks/useApiResource";
import { formatPercent, humanize, MISSING } from "../lib/format";
import { formatMoney, negateDecimal, percentOf, signOf, subtractDecimal } from "../lib/money";
import type { Maybe } from "../services/api";
import type { ReadinessResponse } from "../types/health";
import type { PortfolioOverview, PortfolioSnapshot } from "../types/portfolio";
import type { RiskOverview } from "../types/risk";
import type { WorkerHealth, WorkersReport } from "../types/system";
import { OriginBadge, WORKER_LABELS, WORKER_TONES } from "./provenance";
import { Badge, EmptyState, ErrorState, KeyValues, Loading, Notice, Panel, Stat, StatusDot, Timestamp, type Tone } from "./ui";

// The chart library loads only when there is a curve to draw.
const SeriesChart = lazy(() => import("./backtest/SeriesChart").then((m) => ({ default: m.SeriesChart })));

function tone(value: string | null | undefined): Tone | undefined {
  const s = signOf(value);
  return s === 1 ? "positive" : s === -1 ? "negative" : undefined;
}

/** Unsigned share for display ("12.50%"); null shows as missing. */
function share(percent: number | null): string {
  return percent === null || !Number.isFinite(percent)
    ? MISSING
    : `${percent.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}%`;
}

function NotInitialized({ message }: { message: string }) {
  return (
    <EmptyState title="No paper account yet">
      <p>{message}</p>
      <p className="mt-1">
        Equity, P/L and exposure appear once the simulated paper account exists. Nothing is estimated in the
        meantime.
      </p>
    </EmptyState>
  );
}

/**
 * Equity, P/L and exposure of the simulated paper account. Live figures come from the risk
 * engine's current evaluation (marked now, not recorded); realized/unrealized P/L and costs
 * come from the double-entry ledger.
 */
export function PaperAccountPanel({
  risk,
  ledger,
}: {
  risk: Resource<Maybe<RiskOverview>>;
  ledger: Resource<Maybe<PortfolioOverview>>;
}) {
  const r = risk.data;
  const l = ledger.data;
  const live = r?.found ? r.data.live : null;
  const figures = l?.found ? l.data.figures : null;
  const ccy = live?.functional_currency ?? figures?.functional_currency ?? "";
  const notFound = (r && !r.found ? r.message : null) ?? (l && !l.found ? l.message : null);

  const header = (
    <span className="flex items-center gap-1.5">
      <OriginBadge origin="SIMULATED" />
      {live ? <OriginBadge origin="LIVE" title="Risk engine's current evaluation: marked now, not recorded." /> : null}
    </span>
  );

  let body;
  if (risk.error && !r && ledger.error && !l) {
    body = <ErrorState message={risk.error} onRetry={() => { risk.reload(); ledger.reload(); }} compact />;
  } else if (!r && !l) {
    body = <Loading label="Loading paper account…" />;
  } else if (notFound && !live && !figures) {
    body = <NotInitialized message={notFound} />;
  } else {
    const equity = live?.equity ?? figures?.equity ?? null;
    const contributed = live?.contributed_capital ?? figures?.contributed_capital ?? null;
    const gain = subtractDecimal(equity, contributed);
    const cumulative = percentOf(gain, contributed);
    const daily = negateDecimal(live?.daily_loss);
    const weekly = negateDecimal(live?.weekly_loss);
    const drawdownPct = percentOf(live?.drawdown, live?.peak_equity);
    const unmeasured = live ? "not measurable yet (no baseline)" : "needs the live risk state";
    body = (
      <div className="space-y-2">
        {live && live.mark_status !== "COMPLETE" && (
          <Notice tone="caution">
            Marks are {live.mark_status}: some positions are valued at the last available price.
            {live.mark_issues.length > 0 && ` ${live.mark_issues.length} issue(s); see the Risk Center.`}
          </Notice>
        )}
        {!live && r?.found && (
          <Notice tone="caution">
            The risk engine returned no live state; figures below are the ledger's last recorded valuation.
          </Notice>
        )}
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6">
          <Stat label="Total equity" value={formatMoney(equity, ccy)} hint={live ? <Timestamp iso={live.as_of} /> : "ledger"} />
          <Stat label="Daily P/L" value={daily ? formatMoney(daily, ccy, { signed: true }) : MISSING} tone={tone(daily)} hint={daily ? "since today's baseline" : unmeasured} />
          <Stat label="Weekly P/L" value={weekly ? formatMoney(weekly, ccy, { signed: true }) : MISSING} tone={tone(weekly)} hint={weekly ? "since this week's baseline" : unmeasured} />
          <Stat
            label="Cumulative return"
            value={cumulative === null ? MISSING : formatPercent(cumulative)}
            tone={tone(gain)}
            hint={gain ? formatMoney(gain, ccy, { signed: true }) : "vs contributed capital"}
          />
          <Stat label="Cash" value={formatMoney(live?.cash ?? figures?.cash, ccy)} hint={figures ? `restricted ${formatMoney(figures.restricted_cash, ccy)}` : undefined} />
          <Stat label="Open positions" value={live ? live.open_positions : l?.found ? l.data.positions.long + l.data.positions.short : MISSING} hint={l?.found ? `${l.data.positions.long} long · ${l.data.positions.short} short` : undefined} />
          <Stat label="Available capital" value={formatMoney(live?.available_capital ?? figures?.available_capital, ccy)} />
          <Stat label="Gross exposure" value={formatMoney(live?.gross_exposure ?? figures?.gross_exposure, ccy)} hint={live?.of_equity.gross_exposure ? `${share(Number(live.of_equity.gross_exposure) * 100)} of equity` : undefined} />
          <Stat label="Net exposure" value={formatMoney(live?.net_exposure ?? figures?.net_exposure, ccy, { signed: true })} />
          <Stat
            label="Drawdown"
            value={live?.drawdown ? formatMoney(live.drawdown, ccy) : MISSING}
            tone={signOf(live?.drawdown) === 1 ? "negative" : undefined}
            hint={drawdownPct === null ? (live ? "no peak recorded" : unmeasured) : `${share(drawdownPct)} from peak`}
          />
          <Stat label="Realized P/L" value={formatMoney(figures?.realized_pnl, ccy, { signed: true })} tone={tone(figures?.realized_pnl)} hint="ledger" />
          <Stat label="Unrealized P/L" value={formatMoney(figures?.unrealized_pnl, ccy, { signed: true })} tone={tone(figures?.unrealized_pnl)} hint="ledger, last valuation" />
        </div>
        {(risk.error || ledger.error) && (
          <ErrorState message={`Partial data: ${risk.error ?? ledger.error ?? ""}`} onRetry={() => { risk.reload(); ledger.reload(); }} compact />
        )}
      </div>
    );
  }

  return (
    <Panel
      title="Paper account"
      subtitle={header}
      actions={<Link to="/portfolio" className="text-accent text-xs hover:underline">Portfolio & accounting</Link>}
    >
      {body}
    </Panel>
  );
}

export function RiskStatusPanel({ risk }: { risk: Resource<Maybe<RiskOverview>> }) {
  const r = risk.data;
  return (
    <Panel title="Risk status" actions={<Link to="/risk" className="text-accent text-xs hover:underline">Risk Center</Link>}>
      {risk.error && !r ? (
        <ErrorState message={risk.error} onRetry={risk.reload} compact />
      ) : !r ? (
        <Loading />
      ) : !r.found ? (
        <p className="text-ink-muted text-xs">No paper account: there is nothing to assess.</p>
      ) : (
        <div className="space-y-2.5">
          <div className="flex flex-wrap items-center gap-2">
            {r.data.trading_halted ? (
              <Badge tone="negative" dot>New entries halted</Badge>
            ) : (
              <Badge tone="positive" dot>Not halted</Badge>
            )}
            <Badge tone={r.data.kill_switch === "TRIPPED" ? "negative" : "neutral"}>
              Kill switch {r.data.kill_switch === "TRIPPED" ? "engaged" : "clear"}
            </Badge>
          </div>
          <KeyValues
            items={[
              {
                label: "Circuit breakers",
                value: r.data.active_breakers.length ? (
                  <span className="text-negative">{r.data.active_breakers.map(humanize).join(", ")}</span>
                ) : (
                  <span className="text-ink-muted">none tripped ({r.data.breakers.length} monitored)</span>
                ),
              },
              {
                label: "Limits",
                value: r.data.limits.configured ? (
                  <span className="num">version {r.data.limits.active?.version ?? MISSING}</span>
                ) : (
                  <span className="text-caution">engine defaults (no stored configuration)</span>
                ),
              },
              {
                label: "Reserved",
                value: (
                  <span className="num">
                    {r.data.reserved_exposure.reserved.active_count} active ·{" "}
                    {formatMoney(r.data.reserved_exposure.reserved.gross_exposure, r.data.account.base_currency)} gross
                  </span>
                ),
              },
              {
                label: "Assessments",
                value: (
                  <span className="num">
                    {r.data.assessment_counts.PASS_FOR_RESERVATION ?? 0} passed · {r.data.assessment_counts.REJECTED ?? 0} rejected ·{" "}
                    {r.data.assessment_counts.INSUFFICIENT_DATA ?? 0} insufficient
                  </span>
                ),
              },
            ]}
          />
        </div>
      )}
    </Panel>
  );
}

const WORKER_ORDER: WorkerHealth[] = ["RUNNING", "DEGRADED", "STALE", "FAILED", "NO_HEARTBEAT", "STOPPED", "DISABLED", "NOT_REPORTED"];

export function SystemStatusPanel({
  readiness,
  apiError,
  workers,
}: {
  readiness: ReadinessResponse | null;
  apiError: string | null;
  workers: Resource<WorkersReport>;
}) {
  const w = workers.data;
  return (
    <Panel title="System status" actions={<Link to="/system-health" className="text-accent text-xs hover:underline">System Health</Link>}>
      <div className="space-y-2.5">
        <div className="flex flex-wrap items-center gap-2">
          {apiError ? (
            <Badge tone="negative" dot>No API connection</Badge>
          ) : !readiness ? (
            <Badge>Checking…</Badge>
          ) : readiness.status === "ready" ? (
            <Badge tone="positive" dot>API ready</Badge>
          ) : (
            <Badge tone="caution" dot>API degraded</Badge>
          )}
          {readiness?.components.map((c) => (
            <StatusDot key={c.name} tone={c.status === "ok" ? "positive" : "negative"} label={humanize(c.name)} />
          ))}
        </div>
        {workers.error && !w ? (
          <ErrorState message={workers.error} onRetry={workers.reload} compact />
        ) : !w ? (
          <Loading label="Loading workers…" />
        ) : (
          <>
            <p className="text-ink-muted text-2xs font-semibold tracking-wider uppercase">Background workers ({w.workers.length})</p>
            <div className="flex flex-wrap gap-1.5">
              {WORKER_ORDER.filter((h) => (w.summary[h] ?? 0) > 0).map((h) => (
                <Badge key={h} tone={WORKER_TONES[h]}>
                  {w.summary[h]} {WORKER_LABELS[h]}
                </Badge>
              ))}
            </div>
            {w.workers.some((x) => x.health === "STALE" || x.health === "FAILED") && (
              <Notice tone="negative">
                {w.workers
                  .filter((x) => x.health === "STALE" || x.health === "FAILED")
                  .map((x) => `${x.title}: ${WORKER_LABELS[x.health].toLowerCase()}`)
                  .join("; ")}
              </Notice>
            )}
          </>
        )}
      </div>
    </Panel>
  );
}

/** Recorded valuation checkpoints only (COMPLETE marks); nothing is interpolated. */
export function equityPoints(snapshots: readonly PortfolioSnapshot[]): [number, number][] {
  const byTime = new Map<number, number>();
  for (const s of snapshots) {
    if (s.valuation_status !== "COMPLETE") continue;
    const t = Math.floor(Date.parse(s.as_of) / 1000);
    const v = Number(s.equity);
    if (!Number.isFinite(t) || !Number.isFinite(v)) continue;
    byTime.set(t, v); // several checkpoints in one second: the later row in the list wins
  }
  return [...byTime.entries()].sort((a, b) => a[0] - b[0]);
}

export function EquityHistoryPanel({ snapshots }: { snapshots: Resource<Maybe<PortfolioSnapshot[]>> }) {
  const s = snapshots.data;
  const rows = s?.found ? s.data : null;
  const points = useMemo(() => (rows ? equityPoints(rows) : []), [rows]);
  const excluded = rows ? rows.filter((x) => x.valuation_status !== "COMPLETE").length : 0;
  const lines = useMemo(() => [{ label: "Equity (recorded checkpoints)", tone: "accent" as const, points, area: true }], [points]);
  return (
    <Panel
      title="Equity history"
      subtitle={<span className="flex items-center gap-1.5"><OriginBadge origin="RECORDED" /><OriginBadge origin="SIMULATED" /></span>}
    >
      {snapshots.error && !s ? (
        <ErrorState message={snapshots.error} onRetry={snapshots.reload} compact />
      ) : !s ? (
        <Loading />
      ) : !s.found ? (
        <p className="text-ink-muted text-xs">No paper account yet.</p>
      ) : points.length < 2 ? (
        <EmptyState title={points.length === 0 ? "No recorded valuation checkpoints" : "Only one recorded checkpoint"}>
          The curve is drawn from stored valuation checkpoints with complete marks (the position monitor and
          accounting jobs record them). Nothing is interpolated.
        </EmptyState>
      ) : (
        <>
          <Suspense fallback={<Loading label="Loading chart…" />}>
            <SeriesChart lines={lines} height={200} label="Paper account equity from recorded valuation checkpoints" />
          </Suspense>
          <p className="text-ink-faint mt-1 text-2xs">
            {points.length} checkpoint(s), newest {rows?.length ?? 0} recorded
            {excluded > 0 ? `; ${excluded} with stale or incomplete marks not drawn` : ""}.
          </p>
        </>
      )}
    </Panel>
  );
}
