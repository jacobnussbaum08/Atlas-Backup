import { useState } from "react";

import {
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
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { MISSING, humanize } from "../lib/format";
import { formatDecimal, formatMoney } from "../lib/money";
import { execution } from "../services/orders";
import { portfolio } from "../services/portfolio";
import type { Stats } from "../types/orders";

/**
 * Phase 7B: what the simulated paper broker's execution model did with stored market data:
 * simulated slippage, fill rates, partial fills, costs and the versioned assumptions.
 * Read-only. These are statistics of a simulation, not of real executions.
 */
const REFRESH_MS = 30_000;

function bps(value: string | null): string {
  return value === null ? MISSING : formatDecimal(value, { places: 2, signed: true });
}

function StatsRow({ label, stats }: { label: string; stats: Stats }) {
  return (
    <tr>
      <td className={TD}>{label}</td>
      <td className={`${TD} num text-right`}>{stats.count}</td>
      <td className={`${TD} num text-right`}>{bps(stats.mean)}</td>
      <td className={`${TD} num text-right`}>{bps(stats.median)}</td>
      <td className={`${TD} num text-right`}>{bps(stats.min)}</td>
      <td className={`${TD} num text-right`}>{bps(stats.max)}</td>
    </tr>
  );
}

export function ExecutionAnalyticsPage() {
  const [refresh, setRefresh] = useState(0);
  const [accountId, setAccountId] = useState<string | null>(null);
  const accounts = useApiResource((signal) => portfolio.accounts({ signal }), [refresh]);
  const list = accounts.data ?? [];
  const selected = accountId ?? list.find((a) => a.purpose === "PRIMARY")?.id ?? list[0]?.id ?? null;
  const data = useApiResource((signal) => execution.analytics(selected, { signal }), [refresh, selected], {
    refreshMs: REFRESH_MS,
    enabled: selected !== null,
  });
  const a = data.data;
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Execution Analytics"
        description="How the simulated paper broker executed orders: slippage against the quote, fill rates by order type, partial fills, estimated costs and the execution-model assumptions behind every simulated fill."
        actions={
          <>
            {list.length > 1 && selected && (
              <Segmented
                label="Account"
                value={selected}
                onChange={setAccountId}
                options={list.map((x) => ({ value: x.id, label: x.purpose === "PRIMARY" ? x.name : `${x.name} (test)` }))}
              />
            )}
            <RefreshButton onClick={() => setRefresh((n) => n + 1)} loading={data.loading} />
          </>
        }
      />
      <div className="mb-3">
        <Notice tone="caution">
          <strong>Simulated execution analytics.</strong> Every figure describes the simulator&apos;s assumptions applied to
          stored market data, not real execution quality. The assumptions are deliberately conservative, not unbiased,
          and simulated fills are not guaranteed to match real-world execution.
        </Notice>
      </div>
      {accounts.status === "error" && <ErrorState message={accounts.error ?? ""} onRetry={accounts.reload} />}
      {data.status === "loading" && <Loading label="Loading execution analytics…" />}
      {data.status === "error" && <ErrorState message={data.error ?? ""} onRetry={data.reload} />}
      {a && (
        <div className="flex flex-col gap-3">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
            <Stat label="Simulated fills" value={String(a.fills)} hint={`${a.orders_with_simulated_fills} orders`} />
            <Stat label="Mean slippage" value={`${bps(a.slippage_bps.mean)} bps`} hint="vs the quote mid; + is adverse" />
            <Stat label="Median slippage" value={`${bps(a.slippage_bps.median)} bps`} />
            <Stat label="Partial fills" value={String(a.multi_fill_orders)} hint="orders filled in several fills" />
            <Stat label="Gaps through stops" value={String(a.gaps)} />
            <Stat
              label="Estimated costs"
              value={a.costs.cost_bps_of_notional === null ? MISSING : `${formatDecimal(a.costs.cost_bps_of_notional)} bps`}
              hint="commissions and fees / notional"
            />
          </div>
          {a.fills === 0 && (
            <EmptyState title="No simulated fill yet">
              Fills appear once the paper execution worker evaluates working orders against stored quotes, trades and bars.
            </EmptyState>
          )}
          <div className="grid gap-3 xl:grid-cols-2">
            <Panel title="Slippage and impact" subtitle="basis points; positive is adverse to the order">
              <DataTable label="Slippage statistics">
                <thead>
                  <tr>
                    <th className={TH}>Measure</th>
                    <th className={`${TH} text-right`}>Fills</th>
                    <th className={`${TH} text-right`}>Mean</th>
                    <th className={`${TH} text-right`}>Median</th>
                    <th className={`${TH} text-right`}>Min</th>
                    <th className={`${TH} text-right`}>Max</th>
                  </tr>
                </thead>
                <tbody>
                  <StatsRow label="Slippage vs mid" stats={a.slippage_bps} />
                  {Object.entries(a.slippage_bps_by_side).map(([side, stats]) => (
                    <StatsRow key={side} label={`· ${humanize(side)}`} stats={stats} />
                  ))}
                  <StatsRow label="Market impact" stats={a.impact_bps} />
                  <StatsRow label="Shortfall vs arrival" stats={a.shortfall_bps} />
                </tbody>
              </DataTable>
            </Panel>
            <Panel title="Fill rates" subtitle="final orders by type (rejected orders excluded)">
              {Object.keys(a.fill_rates).length === 0 ? (
                <EmptyState title="No final order yet" />
              ) : (
                <DataTable label="Fill rates">
                  <thead>
                    <tr>
                      <th className={TH}>Order type</th>
                      <th className={`${TH} text-right`}>Final</th>
                      <th className={`${TH} text-right`}>Filled</th>
                      <th className={`${TH} text-right`}>Partly</th>
                      <th className={`${TH} text-right`}>Unfilled</th>
                      <th className={`${TH} text-right`}>Fill rate</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(a.fill_rates).map(([type, r]) => (
                      <tr key={type}>
                        <td className={TD}>{humanize(type)}</td>
                        <td className={`${TD} num text-right`}>{r.final}</td>
                        <td className={`${TD} num text-right`}>{r.filled}</td>
                        <td className={`${TD} num text-right`}>{r.partial}</td>
                        <td className={`${TD} num text-right`}>{r.unfilled}</td>
                        <td className={`${TD} num text-right`}>
                          {r.fill_rate === null ? MISSING : `${formatDecimal(String(Number(r.fill_rate) * 100), { places: 0 })}%`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </DataTable>
              )}
            </Panel>
            <Panel title="Estimated costs" subtitle="as posted to the 6A ledger">
              <DataTable label="Costs">
                <tbody>
                  {(
                    [
                      ["Notional filled", a.costs.notional],
                      ["Commissions", a.costs.commission],
                      ["Regulatory fees", a.costs.regulatory_fees],
                      ["Exchange fees", a.costs.exchange_fees],
                      ["Borrow accrued", a.costs.borrow_accrued],
                      ["Total costs", a.costs.total],
                    ] as const
                  ).map(([label, value]) => (
                    <tr key={label}>
                      <td className={TD}>{label}</td>
                      <td className={`${TD} num text-right`}>{formatMoney(value, "USD")}</td>
                    </tr>
                  ))}
                </tbody>
              </DataTable>
            </Panel>
            <Panel title="Fill basis and evaluations" subtitle="what data each fill used; why orders did not fill">
              <DataTable label="Fill basis">
                <tbody>
                  {Object.entries(a.by_basis).map(([basis, n]) => (
                    <tr key={basis}>
                      <td className={TD}>{humanize(basis)}</td>
                      <td className={`${TD} num text-right`}>{n}</td>
                    </tr>
                  ))}
                  {a.evaluations.map((e) => (
                    <tr key={`${e.outcome}-${e.code}`}>
                      <td className={TD}>
                        {humanize(e.outcome)} · {humanize(e.code)}
                      </td>
                      <td className={`${TD} num text-right`}>{e.count}</td>
                    </tr>
                  ))}
                </tbody>
              </DataTable>
            </Panel>
          </div>
          <Panel
            title="Execution-model assumptions"
            subtitle={`${a.current_model.code} v${a.current_model.version} · ${a.current_model.sha256.slice(0, 12)}`}
          >
            <p className="text-ink-muted mb-2 text-xs">{a.current_model.description}</p>
            <ul className="text-ink-muted mb-3 list-disc pl-5 text-xs">
              {a.current_model.rules.map((rule) => (
                <li key={rule}>{rule}</li>
              ))}
            </ul>
            <DataTable label="Assumptions by asset class">
              <thead>
                <tr>
                  <th className={TH}>Assumption</th>
                  {Object.keys(a.current_model.classes).map((c) => (
                    <th key={c} className={`${TH} text-right`}>
                      {c}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {Object.keys(Object.values(a.current_model.classes)[0] ?? {}).map((key) => (
                  <tr key={key}>
                    <td className={TD}>{humanize(key)}</td>
                    {Object.entries(a.current_model.classes).map(([c, values]) => (
                      <td key={c} className={`${TD} num text-right`}>
                        {String(values[key] ?? MISSING)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </DataTable>
          </Panel>
          <Panel title="Known limitations" subtitle="where the data cannot support a reliable fill">
            <ul className="text-ink-muted list-disc pl-5 text-xs">
              {a.limitations.map((l) => (
                <li key={l}>{l}</li>
              ))}
            </ul>
          </Panel>
        </div>
      )}
    </section>
  );
}
