import { RotateCcw } from "lucide-react";
import { Fragment, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { type ChartLine, SeriesChart } from "../components/backtest/SeriesChart";
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
  Segmented,
  Stat,
  TD,
  TH,
  Timestamp,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import {
  chartNumber,
  formatRatio,
  METRIC_STATUS_LABEL,
  metricMap,
  metricValue,
  STATUS_TONE,
} from "../lib/backtest";
import { humanize, MISSING } from "../lib/format";
import { formatDecimal, formatQuantityExact, signOf } from "../lib/money";
import { ApiError } from "../services/api";
import { backtests } from "../services/backtests";
import type {
  BacktestDetail,
  BenchmarkRow,
  EquityRow,
  MetricRow,
  TradeRow,
} from "../types/backtests";

const tone = (value: string | null | undefined) => {
  const s = signOf(value ?? null);
  return s === 1 ? "positive" : s === -1 ? "negative" : undefined;
};

export function BacktestDetailPage() {
  const { runId = "" } = useParams();
  const detail = useApiResource((signal) => backtests.detail(runId, { signal }), [runId], {
    refreshMs: 2_000,
  });
  const run = detail.data;
  const done = run?.status === "COMPLETED";

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title={run ? run.name : "Backtest run"}
        description={
          <>
            <Link to="/backtesting" className="text-accent hover:underline">
              Backtesting
            </Link>{" "}
            · a simulation over stored historical data, not a forecast
          </>
        }
        actions={run && <ReproduceButton run={run} />}
      />
      {detail.status === "loading" && <Loading />}
      {detail.status === "error" && !run && <ErrorState message={detail.error ?? ""} onRetry={detail.reload} />}
      {run && (
        <div className="flex flex-col gap-3">
          <RunHeader run={run} />
          {run.status === "FAILED" && (
            <ErrorState message={`${run.error_stage ?? "error"}: ${run.error_message ?? "the replay failed"}`} />
          )}
          {done && <Results run={run} />}
          <ConfigPanel run={run} />
        </div>
      )}
    </section>
  );
}

function ReproduceButton({ run }: { run: BacktestDetail }) {
  const navigate = useNavigate();
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  if (run.status !== "COMPLETED") return null;
  return (
    <div className="flex items-center gap-2">
      <input
        type="password"
        autoComplete="off"
        placeholder="operator token"
        aria-label="Operator token"
        value={token}
        onChange={(e) => setToken(e.target.value)}
        className="border-border-strong bg-panel-raised h-7 w-36 rounded-sm border px-2 text-xs"
      />
      <Button
        onClick={() => {
          setError(null);
          backtests
            .reproduce(run.id, token || undefined)
            .then((created) => navigate(`/backtesting/${created.id}`))
            .catch((err: unknown) => setError(err instanceof ApiError ? err.message : String(err)));
        }}
        title="Re-execute this run from its stored configuration and compare the result hash"
      >
        <RotateCcw aria-hidden className="size-3.5" />
        Reproduce
      </Button>
      {error && <span className="text-negative text-2xs">{error}</span>}
    </div>
  );
}

function RunHeader({ run }: { run: BacktestDetail }) {
  return (
    <Panel>
      <div className="flex flex-wrap items-center gap-3 text-xs">
        <Badge tone={STATUS_TONE[run.status]}>{run.status}</Badge>
        <Badge tone="caution">simulated</Badge>
        <span className="num">
          {run.start_date} → {run.end_date}
        </span>
        <span className="num">
          {run.sessions_done}/{run.sessions_total} sessions
        </span>
        {(run.status === "RUNNING" || run.status === "QUEUED") && (
          <div className="bg-panel-raised h-1.5 w-48 overflow-hidden rounded-full" role="progressbar" aria-valuenow={Math.round((run.progress ?? 0) * 100)} aria-label="Replay progress">
            <div className="bg-accent h-full" style={{ width: `${Math.round((run.progress ?? 0) * 100)}%` }} />
          </div>
        )}
        {run.simulated_through && (
          <span className="text-ink-muted">
            simulated through <Timestamp iso={run.simulated_through} seconds={false} />
          </span>
        )}
        {run.reproduction_of && (
          <Badge tone={run.reproduction_matches ? "positive" : run.reproduction_matches === false ? "negative" : "neutral"}>
            {run.reproduction_matches === null
              ? "reproduction"
              : run.reproduction_matches
                ? "identical to the original"
                : "differs from the original"}
          </Badge>
        )}
      </div>
    </Panel>
  );
}

function Results({ run }: { run: BacktestDetail }) {
  const id = run.id;
  const equity = useApiResource((signal) => backtests.equity(id, { signal }), [id]);
  const metrics = useApiResource((signal) => backtests.metrics(id, { signal }), [id]);
  const benchmarks = useApiResource((signal) => backtests.benchmarks(id, { signal }), [id]);
  const portfolio = useMemo(() => metricMap(metrics.data ?? []), [metrics.data]);
  const m = (name: string) => portfolio.get(name);

  return (
    <>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8">
        <Stat label="Final equity" value={metricValue(m("final_equity"))} hint={`from ${formatDecimal(run.initial_capital)}`} />
        <Stat label="Return (net)" value={metricValue(m("total_return"))} tone={tone(m("total_return")?.value)} />
        <Stat label="Return (gross)" value={metricValue(m("gross_total_return"))} hint="before costs" />
        <Stat label="Max drawdown" value={metricValue(m("max_drawdown_pct"))} />
        <Stat label="Sharpe" value={metricValue(m("sharpe_ratio"))} hint={statusHint(m("sharpe_ratio"))} />
        <Stat label="Closed trades" value={metricValue(m("trades_closed"))} />
        <Stat label="Win rate" value={metricValue(m("win_rate"))} hint={statusHint(m("win_rate"))} />
        <Stat label="Costs" value={metricValue(m("total_costs"))} hint="commissions, fees, borrow" />
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Equity" subtitle="net, gross of costs, and the benchmark scaled to the same capital">
          {equity.data && benchmarks.data ? (
            <EquityChart rows={equity.data} benchmarks={benchmarks.data} capital={run.initial_capital} />
          ) : (
            <Loading />
          )}
        </Panel>
        <Panel title="Drawdown" subtitle="fall from the running peak of equity">
          {equity.data ? <DrawdownChart rows={equity.data} /> : <Loading />}
        </Panel>
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Benchmark comparison">
          {benchmarks.data ? <BenchmarkTable rows={benchmarks.data} portfolio={m("total_return")} /> : <Loading />}
        </Panel>
        <Panel title="Costs and activity">
          <KeyValues
            items={[
              "commissions",
              "fees",
              "borrow_costs",
              "total_costs",
              "costs_pct_of_capital",
              "gross_pnl",
              "net_pnl",
              "realized_pnl",
              "unrealized_pnl",
              "turnover",
              "average_gross_exposure",
              "proposals",
              "risk_approved",
              "entries_filled",
              "annualized_return",
              "sortino_ratio",
              "profit_factor",
              "expectancy",
              "average_holding_sessions",
            ].map((name) => ({
              label: humanize(name),
              value: (
                <span className="num">
                  {metricValue(m(name))}
                  {m(name) && m(name)?.status !== "OK" && (
                    <span className="text-ink-faint ml-1" title={m(name)?.detail}>
                      ({METRIC_STATUS_LABEL[m(name)!.status]})
                    </span>
                  )}
                </span>
              ),
            }))}
          />
        </Panel>
      </div>
      {metrics.data && <Breakdowns rows={metrics.data} />}
      <TradesPanel runId={id} currency={run.currency} />
      <DecisionsPanel runId={id} />
    </>
  );
}

function statusHint(row: MetricRow | undefined): string | undefined {
  if (!row || row.status === "OK") return undefined;
  return row.detail || METRIC_STATUS_LABEL[row.status];
}

function EquityChart({ rows, benchmarks, capital }: { rows: EquityRow[]; benchmarks: BenchmarkRow[]; capital: string }) {
  const lines = useMemo<ChartLine[]>(() => {
    const out: ChartLine[] = [
      { label: "Equity (net)", tone: "accent", points: rows.map((r) => [r.session, chartNumber(r.equity)] as const) },
      { label: "Equity before costs", tone: "neutral", points: rows.map((r) => [r.session, chartNumber(r.gross_equity)] as const) },
    ];
    const bench = benchmarks.find((b) => b.kind === "BENCHMARK" && b.available);
    if (bench?.details.series) {
      out.push({
        label: `${bench.label} (buy at the first open)`,
        tone: "caution",
        points: bench.details.series.map(([d, v]) => [d, chartNumber(v) * chartNumber(capital)] as const),
      });
    }
    return out;
  }, [rows, benchmarks, capital]);
  return <SeriesChart lines={lines} label="Equity curve" />;
}

function DrawdownChart({ rows }: { rows: EquityRow[] }) {
  const lines = useMemo<ChartLine[]>(
    () => [
      {
        label: "Drawdown (%)",
        tone: "negative",
        area: true,
        points: rows.map((r) => [r.session, -chartNumber(r.drawdown_pct) * 100] as const),
      },
    ],
    [rows],
  );
  return <SeriesChart lines={lines} label="Drawdown chart" height={200} />;
}

function BenchmarkTable({ rows, portfolio }: { rows: BenchmarkRow[]; portfolio: MetricRow | undefined }) {
  if (rows.length === 0) return <EmptyState title="No benchmark was configured" />;
  return (
    <>
      <DataTable label="Benchmarks">
        <thead>
          <tr>
            <th className={TH}>Reference</th>
            <th className={`${TH} text-right`}>Return</th>
            <th className={`${TH} text-right`}>Max drawdown</th>
            <th className={`${TH} text-right`}>Atlas minus reference</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td className={TD}>Atlas portfolio (net)</td>
            <td className={`${TD} num text-right`}>{metricValue(portfolio)}</td>
            <td className={`${TD} num text-right`}>{MISSING}</td>
            <td className={`${TD} num text-right`}>{MISSING}</td>
          </tr>
          {rows.map((b) => (
            <tr key={b.kind}>
              <td className={TD}>
                {b.kind === "BENCHMARK" ? b.label : "Equal-weight buy-and-hold of the universe"}
                {!b.available && <Badge tone="caution">unavailable</Badge>}
              </td>
              <td className={`${TD} num text-right`}>{formatRatio(b.total_return)}</td>
              <td className={`${TD} num text-right`}>{formatRatio(b.max_drawdown_pct, { signed: false })}</td>
              <td className={`${TD} num text-right`}>{formatRatio(b.excess_return)}</td>
            </tr>
          ))}
        </tbody>
      </DataTable>
      <p className="text-ink-faint mt-2 text-2xs">
        {rows[0]?.details.notes?.join(" · ") ?? "Price returns over exactly the run's sessions."}
      </p>
    </>
  );
}

const SCOPES = [
  { value: "STRATEGY", label: "By strategy" },
  { value: "DIRECTION", label: "Long vs short" },
  { value: "ASSET", label: "By asset" },
] as const;
const BREAKDOWN = ["trades_closed", "trades_open_at_end", "net_pnl_trades", "win_rate", "average_winner", "average_loser", "profit_factor", "expectancy", "trade_costs"];

function Breakdowns({ rows }: { rows: MetricRow[] }) {
  const [scope, setScope] = useState<(typeof SCOPES)[number]["value"]>("STRATEGY");
  const keys = Array.from(new Set(rows.filter((r) => r.scope === scope).map((r) => r.scope_key))).sort();
  return (
    <Panel title="Breakdown" actions={<Segmented label="Breakdown scope" options={SCOPES} value={scope} onChange={setScope} />}>
      {keys.length === 0 ? (
        <EmptyState title="No trades in this scope" />
      ) : (
        <DataTable label="Results breakdown">
          <thead>
            <tr>
              <th className={TH}>{scope.toLowerCase()}</th>
              {BREAKDOWN.map((m) => (
                <th key={m} className={`${TH} text-right`}>
                  {humanize(m)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {keys.map((key) => {
              const map = metricMap(rows, scope, key);
              return (
                <tr key={key}>
                  <td className={TD}>{key}</td>
                  {BREAKDOWN.map((m) => {
                    const row = map.get(m);
                    return (
                      <td key={m} className={`${TD} num text-right`} title={row?.detail}>
                        {metricValue(row)}
                        {row && row.status === "INSUFFICIENT_SAMPLE" && <span className="text-ink-faint">*</span>}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </DataTable>
      )}
      <p className="text-ink-faint mt-2 text-2xs">* fewer than 30 closed trades: shown, not reliable.</p>
    </Panel>
  );
}

function TradesPanel({ runId, currency }: { runId: string; currency: string }) {
  const trades = useApiResource((signal) => backtests.trades(runId, { signal }), [runId], {
    isEmpty: (r) => r.length === 0,
  });
  const [open, setOpen] = useState<number | null>(null);
  return (
    <Panel title="Trades" subtitle={`${trades.data?.length ?? 0} simulated trades (${currency}); select one for entry and exit details`}>
      {trades.status === "loading" && <Loading />}
      {trades.status === "error" && <ErrorState message={trades.error ?? ""} />}
      {trades.status === "empty" && <EmptyState title="No trade was opened" />}
      {trades.data && trades.data.length > 0 && (
        <div className="max-h-[32rem] overflow-y-auto">
          <DataTable label="Trades">
            <thead>
              <tr>
                {["#", "Strategy", "Symbol", "Side", "Opened", "Entry", "Qty", "Exit", "Reason", "Sessions", "Costs", "Net P/L"].map((h) => (
                  <th key={h} className={TH}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {trades.data.map((t) => (
                <Fragment key={t.seq}>
                  <TradeLine trade={t} onToggle={() => setOpen(open === t.seq ? null : t.seq)} />
                  {open === t.seq && (
                    <tr>
                      <td colSpan={12} className="bg-panel-raised/40 p-2">
                        <TradeDetailView runId={runId} seq={t.seq} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </DataTable>
        </div>
      )}
    </Panel>
  );
}

function TradeLine({ trade: t, onToggle }: { trade: TradeRow; onToggle: () => void }) {
  return (
    <tr className="hover:bg-panel-raised/40 cursor-pointer" onClick={onToggle}>
      <td className={`${TD} num`}>{t.seq}</td>
      <td className={TD}>
        {t.strategy_id} <span className="text-ink-faint">{t.timeframe}</span>
      </td>
      <td className={TD}>{t.symbol}</td>
      <td className={TD}>
        <Badge tone={t.direction === "LONG" ? "info" : "accent"}>{t.direction}</Badge>
      </td>
      <td className={`${TD} num`}>{(t.details.first_session as string) ?? MISSING}</td>
      <td className={`${TD} num`}>{formatDecimal(t.entry_price, { places: 4 })}</td>
      <td className={`${TD} num`}>{formatQuantityExact(t.entry_quantity)}</td>
      <td className={`${TD} num`}>{formatDecimal(t.exit_price, { places: 4 })}</td>
      <td className={TD}>{t.status === "OPEN_AT_END" ? <Badge tone="caution">open at end</Badge> : humanize(t.exit_reason ?? "")}</td>
      <td className={`${TD} num`}>{t.holding_sessions ?? MISSING}</td>
      <td className={`${TD} num`}>{formatDecimal(t.costs, { places: 2 })}</td>
      <td className={`${TD} num ${signOf(t.net_pnl) === -1 ? "text-negative" : signOf(t.net_pnl) === 1 ? "text-positive" : ""}`}>
        {formatDecimal(t.net_pnl, { places: 2, signed: true })}
      </td>
    </tr>
  );
}

function TradeDetailView({ runId, seq }: { runId: string; seq: number }) {
  const detail = useApiResource((signal) => backtests.trade(runId, seq, { signal }), [runId, seq]);
  if (!detail.data) return detail.status === "error" ? <ErrorState message={detail.error ?? ""} compact /> : <Loading />;
  const { proposal, orders, fills, trade } = detail.data;
  return (
    <div className="grid gap-3 lg:grid-cols-[22rem_minmax(0,1fr)]">
      <KeyValues
        items={[
          { label: "Signal", value: proposal ? `${proposal.session} · ${proposal.strategy_id} ${proposal.direction}` : MISSING },
          { label: "Reference entry", value: formatDecimal(proposal?.reference_entry ?? null, { places: 4 }) },
          { label: "Stop / target", value: `${formatDecimal(trade.details.stop_level as string, { places: 4 })} / ${formatDecimal((trade.details.target_level as string | null) ?? null, { places: 4 })}` },
          { label: "Risk sized", value: `${formatQuantityExact(proposal?.permitted_quantity ?? null)} (binding: ${proposal?.binding_constraint ?? MISSING})` },
          { label: "Reasoning", value: (proposal?.details.reasoning as string) ?? MISSING },
          { label: "Notes", value: ((trade.details.notes as string[]) ?? []).join(" · ") || MISSING },
        ]}
      />
      <DataTable label="Orders and fills">
        <thead>
          <tr>
            {["Order", "Role", "Type", "Status", "Fill session", "Qty", "Price", "Basis", "Commission + fees"].map((h) => (
              <th key={h} className={TH}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {orders.map((o) => {
            const mine = fills.filter((f) => f.order_seq === o.seq);
            return mine.length === 0 ? (
              <tr key={o.seq}>
                <td className={`${TD} num`}>{o.seq}</td>
                <td className={TD}>{o.role}</td>
                <td className={TD}>{o.order_type}</td>
                <td className={TD}>{o.status}</td>
                <td className={TD} colSpan={5}>
                  <span className="text-ink-faint">{(o.details.final_reason as string) ?? "no fill"}</span>
                </td>
              </tr>
            ) : (
              mine.map((f) => (
                <tr key={`${o.seq}-${f.seq}`}>
                  <td className={`${TD} num`}>{o.seq}</td>
                  <td className={TD}>{o.role}</td>
                  <td className={TD}>{o.order_type}</td>
                  <td className={TD}>{o.status}</td>
                  <td className={`${TD} num`}>{f.session}</td>
                  <td className={`${TD} num`}>{formatQuantityExact(f.quantity)}</td>
                  <td className={`${TD} num`}>{formatDecimal(f.price, { places: 4 })}</td>
                  <td className={TD} title={(f.details.assumptions ?? []).join("; ")}>
                    {humanize(f.basis)} {f.details.gap && <Badge tone="caution">gap</Badge>}
                  </td>
                  <td className={`${TD} num`}>
                    {formatDecimal(f.commission, { places: 2 })} + {formatDecimal(f.fees, { places: 2 })}
                  </td>
                </tr>
              ))
            );
          })}
        </tbody>
      </DataTable>
    </div>
  );
}

function DecisionsPanel({ runId }: { runId: string }) {
  const evaluations = useApiResource((signal) => backtests.evaluations(runId, { signal }), [runId]);
  const rejections = useApiResource((signal) => backtests.rejections(runId, { signal }), [runId]);
  const events = useApiResource((signal) => backtests.events(runId, { signal }), [runId]);
  const errors = useApiResource((signal) => backtests.errors(runId, { signal }), [runId]);
  const reasons = useMemo(() => {
    const counts = new Map<string, number>();
    for (const r of rejections.data ?? []) counts.set(`${r.stage} · ${r.code}`, (counts.get(`${r.stage} · ${r.code}`) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [rejections.data]);
  return (
    <div className="grid gap-3 xl:grid-cols-3">
      <Panel title="Strategy evaluations">
        <DataTable label="Evaluations by state">
          <tbody>
            {(evaluations.data ?? []).map((e) => (
              <tr key={`${e.strategy_id}-${e.timeframe}-${e.state}`}>
                <td className={TD}>{e.strategy_id}</td>
                <td className={TD}>{e.timeframe}</td>
                <td className={TD}>{humanize(e.state)}</td>
                <td className={`${TD} num text-right`}>{e.count}</td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      </Panel>
      <Panel title="Why proposals did not become trades">
        {reasons.length === 0 ? (
          <EmptyState title="No rejection" />
        ) : (
          <DataTable label="Rejection reasons">
            <tbody>
              {reasons.map(([reason, count]) => (
                <tr key={reason}>
                  <td className={TD}>{reason}</td>
                  <td className={`${TD} num text-right`}>{count}</td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        )}
      </Panel>
      <Panel title="Run events and errors">
        {(errors.data ?? []).map((e) => (
          <Notice key={`e${e.seq}`} tone={e.fatal ? "negative" : "caution"}>
            {e.stage}: {e.message}
          </Notice>
        ))}
        {(events.data ?? []).length === 0 && (errors.data ?? []).length === 0 && <EmptyState title="Nothing notable" />}
        <ul className="flex flex-col gap-1 text-xs">
          {(events.data ?? []).slice(0, 50).map((e) => (
            <li key={e.seq}>
              <Badge tone="neutral">{humanize(e.kind)}</Badge> <Timestamp iso={e.at} seconds={false} /> {e.message}
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}

function ConfigPanel({ run }: { run: BacktestDetail }) {
  const config = run.config;
  return (
    <Panel title="Configuration and reproducibility" subtitle="everything the replay depended on is stored with the run">
      <div className="grid gap-4 lg:grid-cols-2">
        <KeyValues
          items={[
            { label: "Universe", value: `${run.universe_mode}: ${run.symbols.join(", ")}${run.asset_count > run.symbols.length ? " …" : ""}` },
            { label: "Benchmark", value: run.benchmark_symbol ?? "none" },
            { label: "Capital", value: `${formatDecimal(run.initial_capital)} ${run.currency}` },
            { label: "Data", value: `${run.data_provider} ${run.data_feed.toUpperCase()} daily bars, known at ${run.data_known_at}` },
            { label: "Dataset", value: <code className="num text-2xs">{run.dataset_sha256 ?? MISSING} ({run.dataset_bars ?? 0} bars)</code> },
            { label: "Risk", value: `${run.risk_source} · ${run.risk_sha256.slice(0, 12)}` },
            { label: "Execution model", value: `#${run.execution_model_id} · ${run.execution_model_sha256.slice(0, 12)}` },
            { label: "Code / engine", value: `${run.code_version} · ${run.engine_version}` },
            { label: "Seed", value: String(run.seed) },
            { label: "Result hash", value: <code className="num text-2xs">{run.result_sha256 ?? MISSING}</code> },
            { label: "Models", value: config.model_usage ?? MISSING },
          ]}
        />
        <div className="flex flex-col gap-2">
          <DataTable label="Strategy versions">
            <thead>
              <tr>
                {["Strategy", "Version", "Logic", "Timeframes", "Parameters"].map((h) => (
                  <th key={h} className={TH}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {run.strategy_versions.map((v) => (
                <tr key={v.strategy_id}>
                  <td className={TD}>{v.strategy_id}</td>
                  <td className={`${TD} num`}>v{v.version}</td>
                  <td className={TD}>{v.logic_version}</td>
                  <td className={TD}>{v.timeframes.join(", ")}</td>
                  <td className={TD}>{humanize(v.parameters_source)}</td>
                </tr>
              ))}
            </tbody>
          </DataTable>
          <ul className="text-ink-muted list-disc pl-4 text-2xs">
            {Object.entries(config.policies ?? {}).map(([k, v]) => (
              <li key={k}>
                <span className="text-ink">{humanize(k)}:</span> {v}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </Panel>
  );
}
