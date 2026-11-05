import { ChevronRight } from "lucide-react";
import { Fragment, useState } from "react";

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
  Segmented,
  Stat,
  TD,
  TH,
  Timestamp,
  type Tone,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { MISSING, humanize } from "../lib/format";
import { formatDecimal, formatMoney, formatQuantityExact, signOf } from "../lib/money";
import { portfolio } from "../services/portfolio";
import type { CheckResult, EntrySummary, PortfolioFigures, PortfolioOverview } from "../types/portfolio";

const REFRESH_MS = 30_000;

/** humanize() with accounting spelling: "Realized P/L", "FX clearing". */
function label(code: string): string {
  return humanize(code).replace(/\bpnl\b/gi, "P/L").replace(/\bfx\b/gi, "FX");
}

function pnlTone(value: string | null | undefined): Tone | undefined {
  const sign = signOf(value);
  return sign === 1 ? "positive" : sign === -1 ? "negative" : undefined;
}

const VALUATION_TONE: Record<string, Tone> = { COMPLETE: "positive", STALE: "caution", INCOMPLETE: "caution" };
const PRICE_TONE: Record<string, Tone> = { FRESH: "positive", STALE: "caution", MISSING: "negative" };

/** Return as a percentage of the simulated funding, computed from decimal strings. */
function returnPercent(ret: string, funding: string): string {
  const base = Number(funding);
  if (!Number.isFinite(base) || base === 0) return MISSING;
  const pct = (Number(ret) / base) * 100;
  return `${pct > 0 ? "+" : pct < 0 ? "−" : ""}${Math.abs(pct).toFixed(2)}%`;
}

export function PortfolioPage() {
  const [refresh, setRefresh] = useState(0);
  const [accountId, setAccountId] = useState<string | null>(null);
  const accounts = useApiResource((signal) => portfolio.accounts({ signal }), [refresh]);
  const overview = useApiResource((signal) => portfolio.overview(accountId, { signal }), [refresh, accountId], {
    refreshMs: REFRESH_MS,
  });
  const notInitialized =
    overview.status === "error" && overview.data === null && /cli init/.test(overview.error ?? "");
  const list = accounts.data ?? [];
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Portfolio & Accounting"
        description="The simulated paper account's double-entry books: cash by currency, holdings, exposure, realized and unrealized P/L, costs, the journal, account balances and reconciliation. Every figure is derived from posted journal lines."
        actions={
          <>
            {list.length > 1 && (
              <Segmented
                label="Account"
                value={accountId ?? list.find((a) => a.purpose === "PRIMARY")?.id ?? list[0]?.id ?? ""}
                onChange={setAccountId}
                options={list.map((a) => ({
                  value: a.id,
                  label: a.purpose === "PRIMARY" ? a.name : `${a.name} (test)`,
                }))}
              />
            )}
            <RefreshButton onClick={() => setRefresh((n) => n + 1)} loading={overview.loading} />
          </>
        }
      />
      {overview.status === "loading" && <Loading label="Loading the books…" />}
      {notInitialized && (
        <EmptyState title="The paper account has not been created yet">
          {overview.error} Until then there are no balances to show.
        </EmptyState>
      )}
      {overview.status === "error" && !notInitialized && (
        <ErrorState message={overview.error ?? ""} onRetry={overview.reload} />
      )}
      {overview.data && <Books data={overview.data} refreshKey={refresh} />}
    </section>
  );
}

function Books({ data, refreshKey }: { data: PortfolioOverview; refreshKey: number }) {
  const f = data.figures;
  const cur = f.functional_currency;
  const funding = data.account.opening_funding ?? f.contributed_capital;
  const accountId = data.account.id;
  const snapshot = data.latest_snapshot;
  return (
    <div className="flex flex-col gap-3">
      <Notice tone="info">{data.disclaimer}</Notice>
      {data.account.purpose === "CONTROLLED_TEST" && (
        <Notice tone="caution">
          This is a CONTROLLED_TEST account: its transactions are controlled test postings, not the paper
          account&apos;s activity.
        </Notice>
      )}
      {data.accounting_errors.length > 0 && (
        <Notice tone="negative">
          <strong>{data.accounting_errors.length} high-priority accounting error(s)</strong> in the latest
          reconciliation. Nothing has been corrected automatically; see Reconciliation below.
        </Notice>
      )}
      {snapshot && snapshot.valuation_status !== "COMPLETE" && (
        <Notice tone="caution">
          Valuation as of <Timestamp iso={snapshot.as_of} /> is {snapshot.valuation_status.toLowerCase()}: positions
          without a fresh price or FX rate keep their last posted valuation (no price is substituted).
        </Notice>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Stat label="Opening simulated funding" value={formatMoney(funding, cur)} hint="Contributed capital, not a return" />
        <Stat label="Equity" value={formatMoney(f.equity, cur)} hint="Assets − liabilities" />
        <Stat
          label="Investment return"
          value={formatMoney(f.investment_return, cur, { signed: true })}
          tone={pnlTone(f.investment_return)}
          hint={`${returnPercent(f.investment_return, funding)} of funding · excludes funding`}
        />
        <Stat label="Available cash" value={formatMoney(f.cash, cur)} hint={`Unrestricted; all currencies at carrying value in ${cur}`} />
        <Stat
          label="Available capital"
          value={formatMoney(f.available_capital, cur)}
          hint="Cash − accrued unpaid costs"
        />
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Stat label="Long exposure" value={formatMoney(f.long_exposure, cur)} hint={`${data.positions.long} long position(s)`} />
        <Stat label="Short exposure" value={formatMoney(f.short_exposure, cur)} hint={`${data.positions.short} short position(s) · a liability`} />
        <Stat label="Realized P/L" value={formatMoney(f.realized_pnl, cur, { signed: true })} tone={pnlTone(f.realized_pnl)} />
        <Stat
          label="Unrealized P/L"
          value={formatMoney(f.unrealized_pnl, cur, { signed: true })}
          tone={pnlTone(f.unrealized_pnl)}
          hint={snapshot ? "As of the last valuation checkpoint" : "Nothing marked yet"}
        />
        <Stat label="Fees & costs" value={formatMoney(f.total_costs, cur)} hint="Commissions, fees, borrow, funding" />
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <EquationPanel f={f} />
        <ValuationPanel data={data} />
      </div>
      <CashPanel data={data} />
      <HoldingsPanel accountId={accountId} refreshKey={refreshKey} />
      <PnlPanel accountId={accountId} refreshKey={refreshKey} />
      <JournalPanel accountId={accountId} refreshKey={refreshKey} />
      <BalancesPanel accountId={accountId} refreshKey={refreshKey} />
      <ReconciliationPanel data={data} refreshKey={refreshKey} />
    </div>
  );
}

function EquationPanel({ f }: { f: PortfolioFigures }) {
  const cur = f.functional_currency;
  const money = (v: string, signed = false) => <span className="num">{formatMoney(v, cur, { signed })}</span>;
  const balanced = formatDecimal(f.equity, { places: 8 }) === formatDecimal(f.equity_by_components, { places: 8 });
  return (
    <Panel
      title="Accounting equation"
      subtitle="assets − liabilities = equity"
      actions={
        <Badge tone={balanced ? "positive" : "negative"} dot>
          {balanced ? "Balanced" : "Out of balance"}
        </Badge>
      }
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <KeyValues
          items={[
            { label: "Cash", value: money(f.cash) },
            { label: "Restricted short proceeds", value: money(f.restricted_cash) },
            { label: "Long securities", value: money(f.long_value) },
            { label: "FX translation", value: money(f.fx_translation, true) },
            { label: <strong>Total assets</strong>, value: money(f.total_assets) },
            { label: "Short liability", value: money(f.short_liability) },
            { label: "Accrued costs", value: money(f.accrued_costs) },
            { label: <strong>Total liabilities</strong>, value: money(f.total_liabilities) },
            { label: <strong>Equity</strong>, value: money(f.equity) },
          ]}
        />
        <KeyValues
          items={[
            { label: "Contributed capital", value: money(f.contributed_capital) },
            { label: "+ Realized P/L", value: money(f.realized_pnl, true) },
            { label: "+ Unrealized P/L", value: money(f.unrealized_pnl, true) },
            { label: "+ Realized FX", value: money(f.realized_fx, true) },
            { label: "+ Unrealized FX", value: money(f.unrealized_fx, true) },
            { label: "− Commissions", value: money(f.commissions) },
            { label: "− Fees", value: money(f.fees) },
            { label: "− Borrow costs", value: money(f.borrow_costs) },
            { label: "− Funding costs", value: money(f.funding_costs) },
            { label: <strong>= Equity</strong>, value: money(f.equity_by_components) },
          ]}
        />
      </div>
    </Panel>
  );
}

function ValuationPanel({ data }: { data: PortfolioOverview }) {
  const snap = data.latest_snapshot;
  return (
    <Panel title="Valuation" subtitle="latest checkpoint">
      {!snap ? (
        <EmptyState title="No valuation checkpoint yet">
          Run <code>python -m app.accounting.cli checkpoint</code>. Until then unrealized P/L is zero by construction:
          nothing has been marked.
        </EmptyState>
      ) : (
        <div className="flex flex-col gap-2">
          <KeyValues
            items={[
              { label: "As of", value: <Timestamp iso={snap.as_of} /> },
              {
                label: "Status",
                value: (
                  <Badge tone={VALUATION_TONE[snap.valuation_status] ?? "neutral"} dot>
                    {humanize(snap.valuation_status)}
                  </Badge>
                ),
              },
              { label: "Equity then", value: <span className="num">{formatMoney(snap.equity, data.figures.functional_currency)}</span> },
              {
                label: "Ledger entries since",
                value: snap.entries_since === 0 ? "none (current)" : `${snap.entries_since} (re-run a checkpoint to re-mark)`,
              },
            ]}
          />
          {snap.issues.length > 0 && (
            <ul className="text-caution list-disc pl-5 text-xs" aria-label="Valuation issues">
              {snap.issues.map((issue, i) => (
                <li key={i}>
                  {label(issue.kind)}: {issue.symbol ?? issue.currency ?? ""} {issue.detail ? `(${issue.detail})` : ""}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Panel>
  );
}

function CashPanel({ data }: { data: PortfolioOverview }) {
  const cur = data.figures.functional_currency;
  return (
    <Panel title="Cash by currency" subtitle="restricted short proceeds are collateral, never available cash">
      {data.cash_by_currency.length === 0 ? (
        <EmptyState title="No cash balances" />
      ) : (
        <DataTable label="Cash by currency">
          <thead>
            <tr>
              <th className={TH}>Currency</th>
              <th className={`${TH} text-right`}>Available</th>
              <th className={`${TH} text-right`}>Restricted</th>
              <th className={`${TH} text-right`}>Accrued payables</th>
              <th className={`${TH} text-right`}>Carrying value ({cur})</th>
              <th className={`${TH} text-right`}>FX translation ({cur})</th>
            </tr>
          </thead>
          <tbody>
            {data.cash_by_currency.map((row) => (
              <tr key={row.currency}>
                <td className={TD}>{row.currency}</td>
                <td className={`${TD} num text-right`}>{formatDecimal(row.cash)}</td>
                <td className={`${TD} num text-right`}>{formatDecimal(row.restricted)}</td>
                <td className={`${TD} num text-right`}>{formatDecimal(row.accrued_payables)}</td>
                <td className={`${TD} num text-right`}>{formatDecimal(row.cash_functional)}</td>
                <td className={`${TD} num text-right`}>
                  {row.currency === cur ? MISSING : formatDecimal(row.translation_functional, { signed: true })}
                </td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}

function HoldingsPanel({ accountId, refreshKey }: { accountId: string; refreshKey: number }) {
  const holdings = useApiResource((signal) => portfolio.holdings(accountId, { signal }), [accountId, refreshKey], {
    isEmpty: (rows) => rows.length === 0,
  });
  return (
    <Panel title="Holdings" subtitle="quantities from the asset quantity ledger; value as last posted">
      {holdings.status === "loading" && <Loading />}
      {holdings.status === "error" && <ErrorState message={holdings.error ?? ""} onRetry={holdings.reload} compact />}
      {holdings.status === "empty" && (
        <EmptyState title="No open positions">
          The account holds only cash. No order has been executed: paper execution arrives in a later phase.
        </EmptyState>
      )}
      {holdings.status === "success" && holdings.data && (
        <DataTable label="Holdings">
          <thead>
            <tr>
              <th className={TH}>Asset</th>
              <th className={TH}>Side</th>
              <th className={`${TH} text-right`}>Quantity</th>
              <th className={`${TH} text-right`}>Avg entry</th>
              <th className={`${TH} text-right`}>Cost / proceeds</th>
              <th className={`${TH} text-right`}>Last price</th>
              <th className={`${TH} text-right`}>Carried value</th>
              <th className={`${TH} text-right`}>Unrealized</th>
              <th className={`${TH} text-right`}>Realized</th>
            </tr>
          </thead>
          <tbody>
            {holdings.data.map((h) => (
              <tr key={h.asset_id}>
                <td className={TD}>
                  <span className="font-medium">{h.symbol}</span>{" "}
                  <span className="text-ink-faint">{h.currency}</span>
                </td>
                <td className={TD}>
                  <Badge tone={h.side === "LONG" ? "info" : "accent"}>{h.side}</Badge>
                </td>
                <td className={`${TD} num text-right`}>{formatQuantityExact(h.quantity)}</td>
                <td className={`${TD} num text-right`}>{formatDecimal(h.average_price, { places: 4 })}</td>
                <td className={`${TD} num text-right`}>{formatDecimal(h.cost_basis)}</td>
                <td
                  className={`${TD} num text-right`}
                  title={h.last_price_provenance ? JSON.stringify(h.last_price_provenance) : "no price observed"}
                >
                  {h.last_price ? formatDecimal(h.last_price, { places: 4 }) : MISSING}{" "}
                  {h.last_price_status && (
                    <Badge tone={PRICE_TONE[h.last_price_status] ?? "neutral"}>{h.last_price_status}</Badge>
                  )}
                </td>
                <td className={`${TD} num text-right`}>{formatDecimal(h.carried_value)}</td>
                <PnlCell value={h.side === "LONG" ? h.valuation_adjustment : negate(h.valuation_adjustment)} />
                <td className={`${TD} num text-right`}>{formatDecimal(h.realized_pnl, { signed: true })}</td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}

/** Unrealized P/L of a holding: a short's positive liability adjustment is a loss. */
function PnlCell({ value }: { value: string }) {
  const tone = pnlTone(value);
  const color = tone === "positive" ? "text-positive" : tone === "negative" ? "text-negative" : "";
  return <td className={`${TD} num text-right ${color}`}>{formatDecimal(value, { signed: true })}</td>;
}

/** Negates a decimal string (a short's liability adjustment is a loss when positive). */
function negate(value: string): string {
  const v = value.trim();
  return v.startsWith("-") ? v.slice(1) : `-${v.replace(/^\+/, "")}`;
}

function PnlPanel({ accountId, refreshKey }: { accountId: string; refreshKey: number }) {
  const pnl = useApiResource((signal) => portfolio.pnl(accountId, { signal }), [accountId, refreshKey]);
  if (pnl.status === "loading") return <Panel title="P/L and costs"><Loading /></Panel>;
  if (pnl.status === "error" || !pnl.data)
    return (
      <Panel title="P/L and costs">
        <ErrorState message={pnl.error ?? ""} onRetry={pnl.reload} compact />
      </Panel>
    );
  const d = pnl.data;
  const cur = d.functional_currency;
  return (
    <Panel title="P/L and costs" subtitle={`in ${cur} (functional values)`}>
      <p className="text-ink-muted mb-2 text-xs">{d.note}</p>
      {d.by_asset.length === 0 ? (
        <EmptyState title="No realized or unrealized P/L yet">
          Only realized gains and losses on closed quantity and the posted valuation of open quantity would appear here.
        </EmptyState>
      ) : (
        <DataTable label="P/L by asset">
          <thead>
            <tr>
              <th className={TH}>Asset</th>
              <th className={`${TH} text-right`}>Realized</th>
              <th className={`${TH} text-right`}>Unrealized</th>
              <th className={`${TH} text-right`}>Realized FX</th>
              <th className={`${TH} text-right`}>Commissions</th>
              <th className={`${TH} text-right`}>Fees</th>
              <th className={`${TH} text-right`}>Borrow</th>
              <th className={`${TH} text-right`}>Funding</th>
            </tr>
          </thead>
          <tbody>
            {d.by_asset.map((row) => (
              <tr key={row.asset_id}>
                <td className={TD}>{row.symbol}</td>
                {["realized", "unrealized", "realized_fx", "commissions", "fees", "borrow_costs", "funding_costs"].map((k) => (
                  <td key={k} className={`${TD} num text-right`}>
                    {formatDecimal(row[`${k}_functional`] ?? "0", { signed: ["realized", "unrealized", "realized_fx"].includes(k) })}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
      <div className="mt-3 grid grid-cols-2 gap-x-6 sm:grid-cols-4">
        {["realized_pnl", "unrealized_pnl", "realized_fx", "unrealized_fx", "commissions", "fees", "borrow_costs", "funding_costs"].map((k) => (
          <div key={k} className="flex justify-between border-b border-border/60 py-1 text-xs">
            <span className="text-ink-muted">{label(k)}</span>
            <span className="num">{formatDecimal(d.totals[k], { signed: k.includes("pnl") || k.includes("fx") })}</span>
          </div>
        ))}
      </div>
    </Panel>
  );
}

const ENTRY_TONE: Record<string, Tone> = {
  OPENING_FUNDING: "accent",
  VALUATION: "info",
  REVERSAL: "caution",
};

function JournalPanel({ accountId, refreshKey }: { accountId: string; refreshKey: number }) {
  const journal = useApiResource((signal) => portfolio.journal(accountId, { signal }), [accountId, refreshKey], {
    isEmpty: (rows) => rows.length === 0,
  });
  const [open, setOpen] = useState<string | null>(null);
  return (
    <Panel title="Ledger activity" subtitle="immutable journal entries, newest first; corrections are reversals">
      {journal.status === "loading" && <Loading />}
      {journal.status === "error" && <ErrorState message={journal.error ?? ""} onRetry={journal.reload} compact />}
      {journal.status === "empty" && <EmptyState title="No journal entries" />}
      {journal.status === "success" && journal.data && (
        <DataTable label="Journal entries">
          <thead>
            <tr>
              <th className={TH} aria-label="Expand" />
              <th className={`${TH} text-right`}>#</th>
              <th className={TH}>Posted</th>
              <th className={TH}>Type</th>
              <th className={TH}>Description</th>
              <th className={TH}>Source</th>
              <th className={`${TH} text-right`}>Debits</th>
            </tr>
          </thead>
          <tbody>
            {journal.data.map((e) => (
              <Fragment key={e.id}>
                <tr className="hover:bg-panel-raised/60 cursor-pointer" onClick={() => setOpen(open === e.id ? null : e.id)}>
                  <td className={TD}>
                    <ChevronRight aria-hidden className={`size-3.5 transition-transform ${open === e.id ? "rotate-90" : ""}`} />
                  </td>
                  <td className={`${TD} num text-right`}>{e.sequence}</td>
                  <td className={TD}>
                    <Timestamp iso={e.posted_at} seconds={false} />
                  </td>
                  <td className={TD}>
                    <Badge tone={ENTRY_TONE[e.entry_type] ?? "neutral"}>{humanize(e.entry_type)}</Badge>
                    {e.reversed_by_entry_id && <span className="text-caution ml-1 text-2xs">reversed</span>}
                  </td>
                  <td className={`${TD} max-w-80 truncate`} title={e.description}>
                    {e.description}
                  </td>
                  <td className={TD}>{humanize(e.source)}</td>
                  <td className={`${TD} num text-right`}>{formatMoney(e.functional_total, e.functional_currency)}</td>
                </tr>
                {open === e.id && (
                  <tr>
                    <td colSpan={7} className="bg-panel-raised/40 border-border/60 border-b p-2">
                      <EntryLines entry={e} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}

function EntryLines({ entry }: { entry: EntrySummary }) {
  const detail = useApiResource((signal) => portfolio.entry(entry.id, { signal }), [entry.id]);
  if (detail.status === "loading") return <Loading label="Loading lines…" />;
  if (!detail.data) return <ErrorState message={detail.error ?? ""} compact />;
  return (
    <div className="flex flex-col gap-2">
      <p className="text-ink-muted text-2xs">
        Event <code>{entry.event_id}</code> · template {entry.template} · effective <Timestamp iso={entry.effective_at} />
      </p>
      <DataTable label={`Lines of entry ${entry.sequence}`}>
        <thead>
          <tr>
            <th className={TH}>Account</th>
            <th className={TH}>Currency</th>
            <th className={`${TH} text-right`}>Debit</th>
            <th className={`${TH} text-right`}>Credit</th>
            <th className={`${TH} text-right`}>Functional Dr</th>
            <th className={`${TH} text-right`}>Functional Cr</th>
            <th className={TH}>Rate</th>
            <th className={TH}>Asset</th>
          </tr>
        </thead>
        <tbody>
          {detail.data.lines.map((line) => (
            <tr key={line.line_number}>
              <td className={TD}>
                <span className="num">{line.account_code}</span> {line.account_name}
              </td>
              <td className={TD}>{line.currency}</td>
              <td className={`${TD} num text-right`}>{signOf(line.debit) ? formatDecimal(line.debit) : ""}</td>
              <td className={`${TD} num text-right`}>{signOf(line.credit) ? formatDecimal(line.credit) : ""}</td>
              <td className={`${TD} num text-right`}>{signOf(line.functional_debit) ? formatDecimal(line.functional_debit) : ""}</td>
              <td className={`${TD} num text-right`}>{signOf(line.functional_credit) ? formatDecimal(line.functional_credit) : ""}</td>
              <td className={TD} title={line.fx_rate_id ? `fx_rates #${line.fx_rate_id}` : undefined}>
                {humanize(line.rate_basis)}
                {line.fx_rate && line.rate_basis !== "FUNCTIONAL" ? ` ${formatDecimal(line.fx_rate, { places: 6 })}` : ""}
              </td>
              <td className={TD}>{line.symbol ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </DataTable>
    </div>
  );
}

function BalancesPanel({ accountId, refreshKey }: { accountId: string; refreshKey: number }) {
  const balances = useApiResource((signal) => portfolio.balances(accountId, { signal }), [accountId, refreshKey]);
  return (
    <Panel
      title="Account balances"
      subtitle="trial balance per ledger account and currency"
      actions={
        balances.data && (
          <Badge tone={balances.data.balanced ? "positive" : "negative"} dot>
            {balances.data.balanced ? "Debits = credits" : "Unbalanced"}
          </Badge>
        )
      }
    >
      {balances.status === "loading" && <Loading />}
      {balances.status === "error" && <ErrorState message={balances.error ?? ""} onRetry={balances.reload} compact />}
      {balances.data && (
        <DataTable label="Trial balance">
          <thead>
            <tr>
              <th className={TH}>Account</th>
              <th className={TH}>Type</th>
              <th className={TH}>Currency</th>
              <th className={`${TH} text-right`}>Debits</th>
              <th className={`${TH} text-right`}>Credits</th>
              <th className={`${TH} text-right`}>Balance</th>
              <th className={`${TH} text-right`}>Functional</th>
            </tr>
          </thead>
          <tbody>
            {balances.data.rows.map((r) => (
              <tr key={`${r.account_code}-${r.currency}`}>
                <td className={TD}>
                  <span className="num">{r.account_code}</span> {r.account_name}
                </td>
                <td className={`${TD} text-ink-muted`}>{humanize(r.account_type)}</td>
                <td className={TD}>{r.currency}</td>
                <td className={`${TD} num text-right`}>{formatDecimal(r.debit_total)}</td>
                <td className={`${TD} num text-right`}>{formatDecimal(r.credit_total)}</td>
                <td className={`${TD} num text-right`}>{formatDecimal(r.balance)}</td>
                <td className={`${TD} num text-right`}>{formatDecimal(r.functional_balance)}</td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}

function ReconciliationPanel({ data, refreshKey }: { data: PortfolioOverview; refreshKey: number }) {
  const run = data.reconciliation;
  const detail = useApiResource(
    (signal) => portfolio.reconciliationRun(run?.id ?? "", { signal }),
    [run?.id, refreshKey],
    { enabled: run !== null },
  );
  const byCode = new Map<string, CheckResult[]>();
  for (const result of detail.data?.results ?? []) {
    byCode.set(result.check_code, [...(byCode.get(result.check_code) ?? []), result]);
  }
  return (
    <Panel
      title="Reconciliation"
      subtitle="automatic accounting checks; discrepancies are reported, never corrected"
      actions={
        run && (
          <Badge tone={run.status === "PASSED" ? "positive" : "negative"} dot>
            {run.status === "PASSED" ? "Passed" : `Failed · ${run.checks_failed}`}
          </Badge>
        )
      }
    >
      {!run ? (
        <EmptyState title="Not reconciled yet">
          Run <code>python -m app.accounting.cli reconcile</code>.
        </EmptyState>
      ) : (
        <div className="flex flex-col gap-2">
          <p className="text-ink-muted text-xs">
            {run.checks_total} checks at entry #{run.entry_sequence} · finished <Timestamp iso={run.finished_at} /> ·{" "}
            {run.current ? "up to date with the ledger" : "entries were posted since; re-run to cover them"}
          </p>
          {data.accounting_errors.length > 0 && (
            <ul className="flex flex-col gap-1" aria-label="Accounting errors">
              {data.accounting_errors.map((e, i) => (
                <li key={i} className="text-negative text-xs">
                  <Badge tone="negative">{e.severity}</Badge> {e.check_code} · {e.scope}: {e.message}
                </li>
              ))}
            </ul>
          )}
          {detail.data && (
            <DataTable label="Reconciliation checks">
              <thead>
                <tr>
                  <th className={TH}>Check</th>
                  <th className={`${TH} text-right`}>Scopes</th>
                  <th className={TH}>Result</th>
                </tr>
              </thead>
              <tbody>
                {[...byCode.entries()].map(([code, results]) => {
                  const failed = results.filter((r) => r.status === "FAIL").length;
                  return (
                    <tr key={code}>
                      <td className={TD}>{label(code)}</td>
                      <td className={`${TD} num text-right`}>{results.length}</td>
                      <td className={TD}>
                        <Badge tone={failed ? "negative" : "positive"}>{failed ? `${failed} failed` : "pass"}</Badge>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </DataTable>
          )}
        </div>
      )}
    </Panel>
  );
}

