import { ChevronRight } from "lucide-react";
import { Fragment, type ReactNode, useState } from "react";

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
import { positions } from "../services/orders";
import { portfolio } from "../services/portfolio";
import type { FillDetail, PaperPosition } from "../types/orders";

/**
 * Phase 7B: simulated paper positions. Read-only: this page cannot open, close or change a
 * position (closing and tightening protection are operator CLI actions), and no brokerage
 * is connected anywhere in Atlas.
 */
const REFRESH_MS = 15_000;
const DETAIL_CELL = "border-border/60 bg-panel-raised/30 border-b px-2 align-top whitespace-normal";

export const SIMULATED_POSITIONS_NOTICE = (
  <>
    <strong>Simulated positions only.</strong> Fills are simulated by the isolated Atlas paper broker from stored
    market data under recorded, deliberately conservative execution assumptions. They are not real executions
    and are not guaranteed to match what a real market would have filled. No brokerage is connected.
  </>
);

export function price(value: string | null | undefined): string {
  return value === null || value === undefined ? MISSING : formatDecimal(value, { places: 2 });
}

function pnlTone(value: string | null | undefined): Tone | undefined {
  const sign = signOf(value);
  return sign === 1 ? "positive" : sign === -1 ? "negative" : undefined;
}

function Pnl({ value, currency }: { value: string | null | undefined; currency: string }) {
  const tone = pnlTone(value);
  const color = tone === "positive" ? "text-positive" : tone === "negative" ? "text-negative" : "";
  return <span className={color}>{formatMoney(value, currency, { signed: true })}</span>;
}

export function holding(seconds: number | null): string {
  if (seconds === null) return MISSING;
  if (seconds < 120) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 120) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

/** The protective level currently binding: the tighter of the stop and the trailing stop. */
export function effectiveStop(p: PaperPosition): { value: string; trailing: boolean } {
  if (!p.trailing_stop_price) return { value: p.stop_price, trailing: false };
  const long = p.direction === "LONG";
  const trailing = Number(p.trailing_stop_price);
  const stop = Number(p.stop_price);
  const useTrailing = long ? trailing > stop : trailing < stop;
  return useTrailing ? { value: p.trailing_stop_price, trailing: true } : { value: p.stop_price, trailing: false };
}

function usePaperAccount(refresh: number) {
  const [accountId, setAccountId] = useState<string | null>(null);
  const accounts = useApiResource((signal) => portfolio.accounts({ signal }), [refresh]);
  const list = accounts.data ?? [];
  const selected = accountId ?? list.find((a) => a.purpose === "PRIMARY")?.id ?? list[0]?.id ?? null;
  const picker =
    list.length > 1 && selected ? (
      <Segmented
        label="Account"
        value={selected}
        onChange={setAccountId}
        options={list.map((a) => ({ value: a.id, label: a.purpose === "PRIMARY" ? a.name : `${a.name} (test)` }))}
      />
    ) : null;
  return { accounts, selected, picker };
}

function Frame({
  title,
  description,
  status,
}: {
  title: string;
  description: string;
  status: "open" | "closed";
}) {
  const [refresh, setRefresh] = useState(0);
  const { accounts, selected, picker } = usePaperAccount(refresh);
  const enabled = selected !== null;
  const rows = useApiResource((signal) => positions.list(selected, status, { signal }), [refresh, selected], {
    refreshMs: REFRESH_MS,
    enabled,
  });
  const summary = useApiResource((signal) => positions.summary(selected, { signal }), [refresh, selected], {
    refreshMs: REFRESH_MS,
    enabled,
  });
  const s = summary.data;
  const currency = rows.data?.[0]?.currency ?? "USD";
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title={title}
        description={description}
        actions={
          <>
            {picker}
            <RefreshButton onClick={() => setRefresh((n) => n + 1)} loading={rows.loading} />
          </>
        }
      />
      <div className="mb-3">
        <Notice tone="caution">{SIMULATED_POSITIONS_NOTICE}</Notice>
      </div>
      {accounts.status === "loading" && <Loading label="Loading accounts…" />}
      {accounts.status === "error" && <ErrorState message={accounts.error ?? ""} onRetry={accounts.reload} />}
      {accounts.data && accounts.data.length === 0 && (
        <EmptyState title="The paper account has not been created yet">
          Run <code>python -m app.accounting.cli init</code> to create it. Until then there are no positions.
        </EmptyState>
      )}
      <div className="flex flex-col gap-3">
        {s && status === "open" && (
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            <Stat label="Open positions" value={String(s.open)} hint={`${s.long} long · ${s.short} short`} />
            <Stat
              label="Unrealized P/L"
              value={formatMoney(s.unrealized_pnl, currency, { signed: true })}
              tone={pnlTone(s.unrealized_pnl)}
              hint="from the last fresh stored price"
            />
            <Stat
              label="Stale marks"
              value={String(s.stale)}
              hint="no fresh price: protection waits"
              {...(s.stale > 0 ? { tone: "caution" as Tone } : {})}
            />
            <Stat label="Closed" value={String(s.closed)} />
            <Stat
              label="Net P/L (closed)"
              value={formatMoney(s.net_pnl_closed, currency, { signed: true })}
              tone={pnlTone(s.net_pnl_closed)}
            />
          </div>
        )}
        {s && status === "closed" && (
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            <Stat label="Closed positions" value={String(s.closed)} />
            <Stat
              label="Realized P/L"
              value={formatMoney(s.realized_pnl, currency, { signed: true })}
              tone={pnlTone(s.realized_pnl)}
              hint="before costs"
            />
            <Stat
              label="Net P/L"
              value={formatMoney(s.net_pnl_closed, currency, { signed: true })}
              tone={pnlTone(s.net_pnl_closed)}
              hint="after commissions, fees, borrow"
            />
            <Stat label="Winners / losers" value={`${s.winners} / ${s.losers}`} />
            <Stat
              label="Exit reasons"
              value={Object.keys(s.exit_reasons).length ? Object.entries(s.exit_reasons).map(([k, n]) => `${humanize(k)} ${n}`).join(" · ") : MISSING}
            />
          </div>
        )}
        <Panel
          title={status === "open" ? "Open positions" : "Closed positions"}
          subtitle="select a row for its events, orders and fills"
        >
          {rows.status === "loading" && <Loading label="Loading positions…" />}
          {rows.status === "error" && <ErrorState message={rows.error ?? ""} onRetry={rows.reload} />}
          {rows.data && rows.data.length === 0 && (
            <EmptyState title={status === "open" ? "No open position" : "No closed position yet"}>
              Positions appear when a simulated fill executes an approved order.
            </EmptyState>
          )}
          {rows.data && rows.data.length > 0 &&
            (status === "open" ? (
              <OpenTable rows={rows.data} refreshKey={refresh} />
            ) : (
              <ClosedTable rows={rows.data} refreshKey={refresh} />
            ))}
        </Panel>
      </div>
    </section>
  );
}

export function OpenPositionsPage() {
  return (
    <Frame
      title="Open Positions"
      description="Simulated positions the paper broker has open: entry, current mark, unrealized P/L and the protection the position monitor enforces (approved stop, trailing stop, target, expiry)."
      status="open"
    />
  );
}

export function ClosedPositionsPage() {
  return (
    <Frame
      title="Closed Positions"
      description="Simulated positions that are closed: entry and exit, realized and net P/L after costs, the exit reason and the strategy or operator that originated the trade."
      status="closed"
    />
  );
}

function Expandable({
  id,
  columns,
  row,
  refreshKey,
}: {
  id: string;
  columns: number;
  row: ReactNode;
  refreshKey: number;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Fragment>
      <tr className="hover:bg-panel-raised cursor-pointer" onClick={() => setOpen(!open)} aria-expanded={open}>
        <td className={TD}>
          <ChevronRight aria-hidden className={`size-3.5 transition-transform ${open ? "rotate-90" : ""}`} />
        </td>
        {row}
      </tr>
      {open && (
        <tr>
          <td className={DETAIL_CELL} colSpan={columns}>
            <PositionDetail id={id} refreshKey={refreshKey} />
          </td>
        </tr>
      )}
    </Fragment>
  );
}

function OpenTable({ rows, refreshKey }: { rows: PaperPosition[]; refreshKey: number }) {
  return (
    <DataTable label="Open positions">
      <thead>
        <tr>
          <th className={TH} />
          <th className={TH}>Asset</th>
          <th className={TH}>Direction</th>
          <th className={`${TH} text-right`}>Quantity</th>
          <th className={`${TH} text-right`}>Avg entry</th>
          <th className={`${TH} text-right`}>Current price</th>
          <th className={`${TH} text-right`}>Unrealized P/L</th>
          <th className={`${TH} text-right`}>Stop</th>
          <th className={`${TH} text-right`}>Target</th>
          <th className={TH}>Held</th>
          <th className={TH}>Monitor</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((p) => {
          const stop = effectiveStop(p);
          return (
            <Expandable
              key={p.id}
              id={p.id}
              columns={11}
              refreshKey={refreshKey}
              row={
                <>
                  <td className={TD}>
                    {p.symbol} <span className="text-ink-faint">{humanize(p.asset_class)}</span>
                  </td>
                  <td className={TD}>
                    <Badge tone={p.direction === "LONG" ? "info" : "accent"}>{humanize(p.direction)}</Badge>
                  </td>
                  <td className={`${TD} num text-right`}>{formatQuantityExact(p.open_quantity)}</td>
                  <td className={`${TD} num text-right`}>{price(p.average_entry_price)}</td>
                  <td className={`${TD} num text-right`}>
                    {price(p.current_price)}
                    {p.current_price_source && <span className="text-ink-faint"> {humanize(p.current_price_source)}</span>}
                  </td>
                  <td className={`${TD} num text-right`}>
                    <Pnl value={p.unrealized_pnl} currency={p.currency} />
                  </td>
                  <td className={`${TD} num text-right`}>
                    {price(stop.value)}
                    {stop.trailing && <span className="text-ink-faint"> trailing</span>}
                  </td>
                  <td className={`${TD} num text-right`}>{price(p.target_price)}</td>
                  <td className={TD}>{holding(p.holding_seconds)}</td>
                  <td className={TD}>
                    {p.trigger_reason ? (
                      <Badge tone="caution">exiting: {humanize(p.trigger_reason)}</Badge>
                    ) : (
                      <Badge tone={p.monitor_status === "ACTIVE" ? "positive" : "neutral"}>
                        {humanize(p.monitor_status)}
                      </Badge>
                    )}
                  </td>
                </>
              }
            />
          );
        })}
      </tbody>
    </DataTable>
  );
}

function ClosedTable({ rows, refreshKey }: { rows: PaperPosition[]; refreshKey: number }) {
  return (
    <DataTable label="Closed positions">
      <thead>
        <tr>
          <th className={TH} />
          <th className={TH}>Asset</th>
          <th className={TH}>Direction</th>
          <th className={`${TH} text-right`}>Quantity</th>
          <th className={`${TH} text-right`}>Entry</th>
          <th className={`${TH} text-right`}>Exit</th>
          <th className={`${TH} text-right`}>Realized P/L</th>
          <th className={`${TH} text-right`}>Fees</th>
          <th className={`${TH} text-right`}>Net P/L</th>
          <th className={`${TH} text-right`}>Net return</th>
          <th className={TH}>Exit reason</th>
          <th className={TH}>Origin</th>
          <th className={TH}>Closed</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((p) => {
          const costs = (Number(p.commissions) + Number(p.fees) + Number(p.borrow_accrued)).toFixed(2);
          return (
            <Expandable
              key={p.id}
              id={p.id}
              columns={13}
              refreshKey={refreshKey}
              row={
                <>
                  <td className={TD}>{p.symbol}</td>
                  <td className={TD}>{humanize(p.direction)}</td>
                  <td className={`${TD} num text-right`}>{formatQuantityExact(p.quantity_opened)}</td>
                  <td className={`${TD} num text-right`}>{price(p.average_entry_price)}</td>
                  <td className={`${TD} num text-right`}>{price(p.average_exit_price)}</td>
                  <td className={`${TD} num text-right`}>
                    <Pnl value={p.realized_pnl} currency={p.currency} />
                  </td>
                  <td className={`${TD} num text-right`}>{formatMoney(costs, p.currency)}</td>
                  <td className={`${TD} num text-right`}>
                    <Pnl value={p.net_pnl} currency={p.currency} />
                  </td>
                  <td className={`${TD} num text-right`}>
                    {p.net_return === null ? MISSING : `${formatDecimal(String(Number(p.net_return) * 100), { places: 2, signed: true })}%`}
                  </td>
                  <td className={TD}>{p.exit_reason ? humanize(p.exit_reason) : MISSING}</td>
                  <td className={TD}>{p.strategy_id ?? humanize(p.origin)}</td>
                  <td className={TD}>
                    <Timestamp iso={p.closed_at} />
                  </td>
                </>
              }
            />
          );
        })}
      </tbody>
    </DataTable>
  );
}

export function fillBasis(detail: FillDetail | null, source: string): ReactNode {
  if (source === "CONTROLLED_TEST") return <Badge tone="caution">Controlled test</Badge>;
  if (!detail) return <Badge tone="neutral">{humanize(source)}</Badge>;
  const tone: Tone = detail.basis === "BAR_FALLBACK" ? "caution" : "info";
  return (
    <Badge tone={tone}>
      Simulated · {humanize(detail.basis)}
      {detail.gap ? " · gap" : ""}
    </Badge>
  );
}

function PositionDetail({ id, refreshKey }: { id: string; refreshKey: number }) {
  const detail = useApiResource((signal) => positions.detail(id, { signal }), [id, refreshKey]);
  if (detail.status === "loading") return <Loading label="Loading position…" />;
  if (detail.status === "error") return <ErrorState message={detail.error ?? ""} onRetry={detail.reload} />;
  const d = detail.data;
  if (!d) return null;
  const p = d.position;
  return (
    <div className="flex flex-col gap-3 py-2">
      <p className="text-ink-faint text-2xs">{d.notice}</p>
      <div className="grid gap-4 md:grid-cols-2">
        <KeyValues
          items={[
            { label: "Position", value: <code>{p.id}</code> },
            { label: "Opened / closed", value: `${formatQuantityExact(p.quantity_opened)} / ${formatQuantityExact(p.quantity_closed)}` },
            { label: "Approved stop", value: price(p.initial_stop_price) },
            { label: "Stop / trailing", value: `${price(p.stop_price)} / ${price(p.trailing_stop_price)}` },
            { label: "Invalidation", value: price(p.invalidation_price) },
            { label: "Expires", value: p.expires_at ? <Timestamp iso={p.expires_at} /> : "no holding horizon" },
          ]}
        />
        <KeyValues
          items={[
            { label: "Origin", value: p.strategy_id ? `strategy ${p.strategy_id}` : humanize(p.origin) },
            { label: "Commissions / fees", value: `${formatMoney(p.commissions, p.currency)} / ${formatMoney(p.fees, p.currency)}` },
            { label: "Borrow accrued / paid", value: `${formatMoney(p.borrow_accrued, p.currency)} / ${formatMoney(p.borrow_paid, p.currency)}` },
            { label: "Trigger", value: p.trigger_reason ? `${humanize(p.trigger_reason)}` : "armed" },
            { label: "Last mark", value: p.current_price_at ? <Timestamp iso={p.current_price_at} /> : MISSING },
            { label: "Close requests", value: d.close_requests.length ? d.close_requests.map((r) => humanize(r.status)).join(", ") : "none" },
          ]}
        />
      </div>
      <DataTable label="Position events">
        <thead>
          <tr>
            <th className={TH}>#</th>
            <th className={TH}>Event</th>
            <th className={`${TH} text-right`}>Open after</th>
            <th className={TH}>Actor</th>
            <th className={TH}>Reason</th>
            <th className={TH}>At</th>
          </tr>
        </thead>
        <tbody>
          {d.events.map((e) => (
            <tr key={e.sequence}>
              <td className={`${TD} num`}>{e.sequence}</td>
              <td className={TD}>{humanize(e.event_type)}</td>
              <td className={`${TD} num text-right`}>{formatQuantityExact(e.open_quantity_after)}</td>
              <td className={TD}>{e.actor}</td>
              <td className={TD}>{e.reason}</td>
              <td className={TD}>
                <Timestamp iso={e.occurred_at} />
              </td>
            </tr>
          ))}
        </tbody>
      </DataTable>
      {d.fills.length > 0 && (
        <DataTable label="Position fills">
          <thead>
            <tr>
              <th className={TH}>Side</th>
              <th className={`${TH} text-right`}>Quantity</th>
              <th className={`${TH} text-right`}>Price</th>
              <th className={`${TH} text-right`}>Slippage (bps)</th>
              <th className={`${TH} text-right`}>Commission / fees</th>
              <th className={TH}>Basis</th>
              <th className={TH}>At</th>
            </tr>
          </thead>
          <tbody>
            {d.fills.map((f) => (
              <tr key={f.id}>
                <td className={TD}>{humanize(f.side)}</td>
                <td className={`${TD} num text-right`}>{formatQuantityExact(f.quantity)}</td>
                <td className={`${TD} num text-right`}>{price(f.price)}</td>
                <td className={`${TD} num text-right`}>
                  {f.detail?.slippage_bps ? formatDecimal(f.detail.slippage_bps, { places: 2, signed: true }) : MISSING}
                </td>
                <td className={`${TD} num text-right`}>
                  {formatDecimal(f.commission)} / {formatDecimal(f.fees)}
                </td>
                <td className={TD}>{fillBasis(f.detail, f.source)}</td>
                <td className={TD}>
                  <Timestamp iso={f.filled_at} />
                </td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </div>
  );
}
