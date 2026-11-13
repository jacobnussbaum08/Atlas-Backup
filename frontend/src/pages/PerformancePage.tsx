import { useState } from "react";

import { type ChartLine, SeriesChart } from "../components/backtest/SeriesChart";
import {
  DataTable,
  EmptyState,
  ErrorState,
  Loading,
  Notice,
  PageHeader,
  Panel,
  RefreshButton,
  Stat,
  TD,
  TH,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { MISSING, humanize } from "../lib/format";
import { formatDecimal, formatMoney, signOf } from "../lib/money";
import { performance } from "../services/performance";
import { portfolio } from "../services/portfolio";
import type { Performance, TradeStats } from "../types/performance";

/**
 * Spec 22.6: performance of the simulated paper account, from recorded valuation checkpoints
 * and closed paper positions only. Every figure shows its sample size; ratios below their
 * minimum sample are withheld with the reason. Simulated fills, not real executions.
 */
const REFRESH_MS = 60_000;

function pct(value: string | null | undefined): string {
  if (value === null || value === undefined) return MISSING;
  return `${formatDecimal(String(Number(value) * 100), { places: 2, signed: true })}%`;
}

function tone(value: string | null | undefined) {
  const s = signOf(value);
  return s === 1 ? ("positive" as const) : s === -1 ? ("negative" as const) : undefined;
}

function StatsTable<K extends string>({
  label,
  rows,
  keyName,
  currency,
}: {
  label: string;
  rows: (TradeStats & Record<K, string>)[];
  keyName: K;
  currency: string;
}) {
  if (rows.length === 0) return <p className="text-ink-faint text-xs">No closed paper trades yet.</p>;
  return (
    <DataTable label={label}>
      <thead>
        <tr>
          <th className={TH}>{humanize(keyName.toUpperCase())}</th>
          <th className={`${TH} text-right`}>Trades</th>
          <th className={`${TH} text-right`}>Win rate</th>
          <th className={`${TH} text-right`}>Net P/L</th>
          <th className={`${TH} text-right`}>Costs</th>
          <th className={`${TH} text-right`}>Profit factor</th>
          <th className={`${TH} text-right`}>Expectancy</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r[keyName]}>
            <td className={TD}>{humanize(r[keyName])}</td>
            <td className={`${TD} num text-right`}>{r.trades}</td>
            <td className={`${TD} num text-right`}>{pct(r.win_rate)}</td>
            <td className={`${TD} num text-right`}>{formatMoney(r.net_pnl, currency, { signed: true })}</td>
            <td className={`${TD} num text-right`}>{formatMoney(r.costs, currency)}</td>
            <td className={`${TD} num text-right`}>{formatDecimal(r.profit_factor, { places: 2 })}</td>
            <td className={`${TD} num text-right`}>{formatMoney(r.expectancy, currency, { signed: true })}</td>
          </tr>
        ))}
      </tbody>
    </DataTable>
  );
}

function Body({ p }: { p: Performance }) {
  const c = p.currency;
  const s = p.summary;
  const equity: ChartLine[] = [
    { label: "Equity", tone: "accent", area: true, points: p.curve.points.map((x) => [x.day, Number(x.equity)] as const) },
  ];
  const drawdown: ChartLine[] = [
    {
      label: "Drawdown %",
      tone: "negative",
      area: true,
      points: p.curve.drawdown.map((x) => [x.day, Number(x.drawdown ?? 0) * 100] as const),
    },
  ];
  return (
    <>
      <Notice tone="info">{p.disclaimer}</Notice>
      <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8">
        <Stat label="Total return" value={pct(s.total_return)} tone={tone(s.total_return)} hint={`${p.curve.days} recorded days`} />
        <Stat label="Max drawdown" value={pct(s.max_drawdown)} tone={tone(s.max_drawdown)} />
        <Stat label="Current drawdown" value={pct(s.current_drawdown)} />
        <Stat label="Sharpe" value={formatDecimal(s.sharpe, { places: 2 })} hint={`${s.daily_returns} daily returns (min ${s.minimum})`} />
        <Stat label="Sortino" value={formatDecimal(s.sortino, { places: 2 })} />
        <Stat label="Closed trades" value={p.trades.trades} hint={`${p.trades.wins} won · ${p.trades.losses} lost`} />
        <Stat label="Net P/L" value={formatMoney(p.trades.net_pnl, c, { signed: true })} tone={tone(p.trades.net_pnl)} hint="closed trades, after costs" />
        <Stat label="Trading costs" value={formatMoney(p.trades.costs, c)} hint="commissions, fees, borrow" />
      </div>
      {s.reason && <p className="text-ink-faint mt-1 text-2xs">Risk-adjusted ratios withheld: {s.reason}.</p>}

      <Panel title="Equity curve" subtitle="last COMPLETE valuation checkpoint of each New York day; nothing interpolated">
        {p.curve.points.length < 2 ? (
          <EmptyState title="Not enough recorded valuations">
            {p.curve.points.length} day(s) recorded{p.curve.checkpoints_excluded ? `; ${p.curve.checkpoints_excluded} incomplete checkpoint(s) left out` : ""}.
          </EmptyState>
        ) : (
          <>
            <SeriesChart lines={equity} label="Equity curve" />
            <div className="mt-2">
              <SeriesChart lines={drawdown} label="Drawdown chart" height={160} />
            </div>
          </>
        )}
      </Panel>

      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Monthly returns" subtitle="from the previous month's last recorded equity">
          {p.monthly_returns.length === 0 ? (
            <p className="text-ink-faint text-xs">No recorded month yet.</p>
          ) : (
            <DataTable label="monthly returns">
              <thead>
                <tr>
                  <th className={TH}>Month</th>
                  <th className={`${TH} text-right`}>Return</th>
                  <th className={`${TH} text-right`}>End equity</th>
                </tr>
              </thead>
              <tbody>
                {p.monthly_returns.map((m) => (
                  <tr key={m.month}>
                    <td className={TD}>{m.month}</td>
                    <td className={`${TD} num text-right`}>{pct(m.return)}</td>
                    <td className={`${TD} num text-right`}>{formatMoney(m.end_equity, c)}</td>
                  </tr>
                ))}
              </tbody>
            </DataTable>
          )}
        </Panel>
        <Panel title="Benchmark comparison" subtitle={`${p.benchmark.symbol}, price return without dividends, same recorded days`}>
          {p.benchmark.return === null ? (
            <p className="text-ink-faint text-xs">Not available: {p.benchmark.reason}.</p>
          ) : (
            <div className="grid grid-cols-3 gap-2">
              <Stat label="Account" value={pct(p.benchmark.account_return_same_period)} />
              <Stat label={p.benchmark.symbol} value={pct(p.benchmark.return)} />
              <Stat label="Excess" value={pct(p.benchmark.excess_return)} tone={tone(p.benchmark.excess_return)} hint={`${p.benchmark.start} → ${p.benchmark.end}`} />
            </div>
          )}
        </Panel>
      </div>

      <Panel title="By strategy" subtitle="closed simulated paper positions, net of costs">
        <StatsTable label="performance by strategy" rows={p.by_strategy} keyName="strategy_id" currency={c} />
      </Panel>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Long versus short">
          <StatsTable label="performance by direction" rows={p.by_direction} keyName="direction" currency={c} />
        </Panel>
        <Panel title="By exit reason">
          <StatsTable label="performance by exit reason" rows={p.by_exit_reason} keyName="exit_reason" currency={c} />
        </Panel>
      </div>
    </>
  );
}

export function PerformancePage() {
  const [refresh, setRefresh] = useState(0);
  const accounts = useApiResource((signal) => portfolio.accounts({ signal }), [refresh]);
  const list = accounts.data ?? [];
  const selected = list.find((a) => a.purpose === "PRIMARY")?.id ?? list[0]?.id ?? null;
  const data = useApiResource((signal) => performance.report(selected, { signal }), [refresh, selected], {
    refreshMs: REFRESH_MS,
    enabled: selected !== null,
  });
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Performance"
        description="The simulated paper account's recorded results: equity curve, drawdown, monthly and risk-adjusted returns, costs, strategy and benchmark comparisons, each with its sample size."
        actions={<RefreshButton onClick={() => setRefresh((n) => n + 1)} loading={data.loading} />}
      />
      {accounts.error ? (
        <ErrorState message={accounts.error} onRetry={() => setRefresh((n) => n + 1)} />
      ) : !accounts.data ? (
        <Loading />
      ) : selected === null ? (
        <EmptyState title="No paper account">Create the paper account first (python -m app.accounting.cli init).</EmptyState>
      ) : data.error ? (
        <ErrorState message={data.error} onRetry={() => setRefresh((n) => n + 1)} />
      ) : !data.data ? (
        <Loading />
      ) : (
        <Body p={data.data} />
      )}
    </section>
  );
}
