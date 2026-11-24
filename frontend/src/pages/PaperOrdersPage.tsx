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
import { type Resource, useApiResource } from "../hooks/useApiResource";
import { MISSING, humanize } from "../lib/format";
import { formatDecimal, formatQuantityExact } from "../lib/money";
import { orders } from "../services/orders";
import { portfolio } from "../services/portfolio";
import type { ExecutionAttempt, OrderGroup, OrderStatus, PaperBrokerStatus, PaperOrder } from "../types/orders";
import { fillBasis } from "./PaperPositionsPage";

/**
 * Phases 7A-7B: simulated paper orders handled by the isolated Atlas paper broker, with the
 * simulator's fills, costs and evaluations. Read-only: this page cannot submit, cancel, fill
 * or approve anything, and there is no brokerage connection or live-trading control
 * anywhere in Atlas.
 */
const REFRESH_MS = 15_000;
// The expanded detail wraps (the table's own cells do not).
const DETAIL_CELL = "border-border/60 bg-panel-raised/30 border-b px-2 align-top whitespace-normal";

export const STATUS_TONE: Record<OrderStatus, Tone> = {
  PROPOSED: "neutral",
  APPROVED: "info",
  OPEN: "info",
  PARTIALLY_FILLED: "accent",
  FILLED: "positive",
  CANCEL_PENDING: "caution",
  CANCELLED: "neutral",
  EXPIRED: "neutral",
  REJECTED: "negative",
  FAILED: "negative",
};

const ATTEMPT_TONE: Record<ExecutionAttempt["outcome"], Tone> = {
  FILLED: "positive",
  PARTIALLY_FILLED: "accent",
  TRIGGERED: "caution",
  NO_FILL: "neutral",
  DEFERRED: "caution",
};

const GROUPS: { value: OrderGroup | "all"; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "all", label: "All" },
  { value: "partially_filled", label: "Partially filled" },
  { value: "filled", label: "Filled" },
  { value: "cancelled", label: "Cancelled" },
  { value: "rejected", label: "Rejected" },
  { value: "expired", label: "Expired" },
  { value: "failed", label: "Failed" },
];

function price(value: string | null): string {
  return value === null ? MISSING : formatDecimal(value, { places: 2 });
}

function levels(o: PaperOrder): string {
  const parts: string[] = [];
  if (o.limit_price) parts.push(`limit ${price(o.limit_price)}`);
  if (o.stop_price) parts.push(`stop ${price(o.stop_price)}`);
  if (o.trail_amount) parts.push(`trail ${price(o.trail_amount)}`);
  if (o.trail_percent) parts.push(`trail ${formatQuantityExact(o.trail_percent)}%`);
  return parts.length ? parts.join(", ") : "—";
}

export function PaperOrdersPage() {
  const [refresh, setRefresh] = useState(0);
  const [accountId, setAccountId] = useState<string | null>(null);
  const [group, setGroup] = useState<OrderGroup | "all">("open");
  const accounts = useApiResource((signal) => portfolio.accounts({ signal }), [refresh]);
  const list = accounts.data ?? [];
  const selected = accountId ?? list.find((a) => a.purpose === "PRIMARY")?.id ?? list[0]?.id ?? null;
  const enabled = selected !== null;
  const summary = useApiResource((signal) => orders.summary(selected, { signal }), [refresh, selected], {
    refreshMs: REFRESH_MS,
    enabled,
  });
  const rows = useApiResource(
    (signal) => orders.list(selected, group === "all" ? null : group, { signal }),
    [refresh, selected, group],
    { refreshMs: REFRESH_MS, enabled },
  );
  const broker = useApiResource((signal) => orders.broker({ signal }), [refresh], { refreshMs: REFRESH_MS });

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Paper Orders"
        description="Simulated paper orders: each executes an approved risk reservation through the isolated Atlas paper broker. Order history, state changes, simulated fills with their price and cost breakdown, reservation linkage and the simulator's status."
        actions={
          <>
            {list.length > 1 && selected && (
              <Segmented
                label="Account"
                value={selected}
                onChange={setAccountId}
                options={list.map((a) => ({
                  value: a.id,
                  label: a.purpose === "PRIMARY" ? a.name : `${a.name} (test)`,
                }))}
              />
            )}
            <RefreshButton onClick={() => setRefresh((n) => n + 1)} loading={rows.loading} />
          </>
        }
      />
      <div className="mb-3">
        <Notice tone="caution">
          <strong>Simulated orders only.</strong> No brokerage is connected and no real order exists. Orders are
          handled by the isolated Atlas paper broker, which simulates fills from stored market data under
          recorded, conservative execution assumptions; simulated fills are not guaranteed to match real-world
          execution. Controlled test fills are labelled as such. This page has no trading controls.
        </Notice>
      </div>
      {accounts.status === "loading" && <Loading label="Loading accounts…" />}
      {accounts.status === "error" && <ErrorState message={accounts.error ?? ""} onRetry={accounts.reload} />}
      {accounts.data && accounts.data.length === 0 && (
        <EmptyState title="The paper account has not been created yet">
          Run <code>python -m app.accounting.cli init</code> to create it. Until then there are no orders.
        </EmptyState>
      )}
      <div className="flex flex-col gap-3">
        <BrokerPanel broker={broker} />
        {summary.data && (
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
            <Stat label="Open" value={String(summary.data.open)} hint="approved, working or cancel pending" />
            <Stat label="Filled" value={String(summary.data.counts.FILLED)} tone="positive" />
            <Stat label="Partially filled" value={String(summary.data.counts.PARTIALLY_FILLED)} />
            <Stat label="Cancel pending" value={String(summary.data.counts.CANCEL_PENDING)} />
            <Stat label="Cancelled" value={String(summary.data.counts.CANCELLED)} />
            <Stat label="Expired" value={String(summary.data.counts.EXPIRED)} />
            <Stat
              label="Rejected / failed"
              value={`${summary.data.counts.REJECTED} / ${summary.data.counts.FAILED}`}
              {...(summary.data.counts.REJECTED + summary.data.counts.FAILED > 0
                ? { tone: "negative" as Tone }
                : {})}
            />
            <Stat
              label="Refused requests"
              value={String(summary.data.refused_requests)}
              hint="unauthorized or invalid; no order"
            />
          </div>
        )}
        <Panel title="Orders" subtitle="newest first; select a row for its events, fills and reservation">
          <div className="mb-2 flex justify-end">
            <Segmented label="Order group" value={group} onChange={setGroup} options={GROUPS} />
          </div>
          {rows.status === "loading" && <Loading label="Loading orders…" />}
          {rows.status === "error" && <ErrorState message={rows.error ?? ""} onRetry={rows.reload} />}
          {rows.data && rows.data.length === 0 && <EmptyState title="No paper order in this group" />}
          {rows.data && rows.data.length > 0 && <OrdersTable rows={rows.data} refreshKey={refresh} />}
        </Panel>
        {enabled && <EventsPanel accountId={selected} refreshKey={refresh} />}
        {enabled && <RefusedPanel accountId={selected} refreshKey={refresh} />}
      </div>
    </section>
  );
}

function BrokerPanel({ broker }: { broker: Resource<PaperBrokerStatus> }) {
  if (broker.status === "error") return <ErrorState message={broker.error ?? ""} onRetry={broker.reload} />;
  const b = broker.data;
  if (!b) return null;
  const tone: Tone = b.status === "RUNNING" ? "positive" : b.status === "PAUSED" ? "caution" : "neutral";
  return (
    <Panel title="Paper broker" subtitle="simulated; not connected to any brokerage">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Stat label="Simulator" value={<Badge tone={tone}>{humanize(b.status)}</Badge>} hint={b.broker_id} />
        <Stat label="Accepting orders" value={b.accepting_orders ? "Yes" : "No"} />
        <Stat label="Working orders" value={String(b.working_orders)} />
        <Stat label="Fill model" value="Simulated" hint={b.fill_model} />
        <Stat
          label="Outbox"
          value={`${b.outbox.counts.PENDING ?? 0} pending`}
          hint={`${b.outbox.counts.DELIVERED ?? 0} delivered, ${b.outbox.counts.DEAD ?? 0} dead`}
          {...((b.outbox.counts.DEAD ?? 0) > 0 ? { tone: "negative" as Tone } : {})}
        />
        <Stat label="Last heartbeat" value={<Timestamp iso={b.last_heartbeat_at} />} hint="recovery: see detail" />
      </div>
    </Panel>
  );
}

function OrdersTable({ rows, refreshKey }: { rows: PaperOrder[]; refreshKey: number }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <DataTable label="Paper orders">
      <thead>
        <tr>
          <th className={TH} />
          <th className={TH}>Asset</th>
          <th className={TH}>Side</th>
          <th className={TH}>Type</th>
          <th className={TH}>Status</th>
          <th className={`${TH} text-right`}>Quantity</th>
          <th className={`${TH} text-right`}>Filled / remaining</th>
          <th className={`${TH} text-right`}>Avg fill</th>
          <th className={TH}>Levels</th>
          <th className={TH}>Submitted</th>
          <th className={TH}>Expires</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((o) => (
          <Fragment key={o.id}>
            <tr
              className="hover:bg-panel-raised cursor-pointer"
              onClick={() => setOpen(open === o.id ? null : o.id)}
              aria-expanded={open === o.id}
            >
              <td className={TD}>
                <ChevronRight aria-hidden className={`size-3.5 transition-transform ${open === o.id ? "rotate-90" : ""}`} />
              </td>
              <td className={TD}>
                {o.symbol} <span className="text-ink-faint">{humanize(o.purpose)}</span>
              </td>
              <td className={TD}>{humanize(o.side)}</td>
              <td className={TD}>{humanize(o.order_type)}</td>
              <td className={TD}>
                <Badge tone={STATUS_TONE[o.status]}>{humanize(o.status)}</Badge>
              </td>
              <td className={`${TD} num text-right`}>{formatQuantityExact(o.quantity)}</td>
              <td className={`${TD} num text-right`}>
                {formatQuantityExact(o.filled_quantity)} / {formatQuantityExact(o.remaining_quantity)}
              </td>
              <td className={`${TD} num text-right`}>{price(o.average_fill_price)}</td>
              <td className={TD}>{levels(o)}</td>
              <td className={TD}>
                <Timestamp iso={o.submitted_at} />
              </td>
              <td className={TD}>
                <Timestamp iso={o.expires_at} />
              </td>
            </tr>
            {open === o.id && (
              <tr>
                <td className={DETAIL_CELL} colSpan={11}>
                  <OrderDetail id={o.id} refreshKey={refreshKey} />
                </td>
              </tr>
            )}
          </Fragment>
        ))}
      </tbody>
    </DataTable>
  );
}

function OrderDetail({ id, refreshKey }: { id: string; refreshKey: number }) {
  const detail = useApiResource((signal) => orders.detail(id, { signal }), [id, refreshKey]);
  if (detail.status === "loading") return <Loading label="Loading order…" />;
  if (detail.status === "error") return <ErrorState message={detail.error ?? ""} onRetry={detail.reload} />;
  const d = detail.data;
  if (!d) return null;
  const r = d.reservation;
  return (
    <div className="flex flex-col gap-3 py-2">
      <p className="text-ink-faint text-2xs">{d.notice}</p>
      <div className="grid gap-4 md:grid-cols-2">
        <KeyValues
          items={[
            { label: "Order", value: <code>{d.order.id}</code> },
            { label: "Status reason", value: d.order.status_reason ?? MISSING },
            { label: "Requested by", value: d.order.requested_by },
            { label: "Entry protection", value: price(d.order.protection_price) },
            {
              label: "Origin",
              value: d.order.strategy_proposal_id
                ? `strategy proposal ${d.order.strategy_proposal_id}`
                : d.order.ai_decision_id
                  ? `AI decision ${d.order.ai_decision_id}`
                  : "operator intent",
            },
            { label: "Parent order", value: d.order.parent_order_id ?? MISSING },
          ]}
        />
        <KeyValues
          items={[
            { label: "Reservation", value: <code>{r.reservation_id}</code> },
            {
              label: "Role",
              value: r.role === "CONSUMED_BY_FILLS" ? "consumed by this order's fills" : "parent's (an exit consumes none)",
            },
            { label: "Reservation status", value: `${humanize(r.status)} · ${humanize(r.execution_link)}` },
            {
              label: "Approved / consumed / released",
              value: `${formatQuantityExact(r.approved_quantity)} / ${formatQuantityExact(r.consumed_quantity)} / ${formatQuantityExact(r.released_quantity)}`,
            },
            { label: "Still held", value: formatQuantityExact(r.remaining_quantity) },
            {
              label: "Paper broker book",
              value: d.paper_broker.book
                ? `${humanize(d.paper_broker.book.status)} (${formatQuantityExact(d.paper_broker.book.filled_quantity)} filled)`
                : "not received",
            },
          ]}
        />
      </div>
      <DataTable label="Order events">
        <thead>
          <tr>
            <th className={TH}>#</th>
            <th className={TH}>Event</th>
            <th className={TH}>Transition</th>
            <th className={TH}>Source</th>
            <th className={TH}>Actor</th>
            <th className={TH}>Reason</th>
            <th className={TH}>At</th>
          </tr>
        </thead>
        <tbody>
          {d.events.map((e) => (
            <tr key={e.id}>
              <td className={`${TD} num`}>{e.sequence}</td>
              <td className={TD}>{humanize(e.event_type)}</td>
              <td className={TD}>
                {e.from_status ? humanize(e.from_status) : "new"} → {humanize(e.to_status)}
              </td>
              <td className={TD}>{humanize(e.source)}</td>
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
        <DataTable label="Fills">
          <thead>
            <tr>
              <th className={TH}>#</th>
              <th className={`${TH} text-right`}>Quantity</th>
              <th className={`${TH} text-right`}>Price</th>
              <th className={`${TH} text-right`}>Commission / fees</th>
              <th className={TH}>Source</th>
              <th className={TH}>Journal entry</th>
              <th className={TH}>At</th>
            </tr>
          </thead>
          <tbody>
            {d.fills.map((f) => (
              <tr key={f.id}>
                <td className={`${TD} num`}>{f.sequence}</td>
                <td className={`${TD} num text-right`}>{formatQuantityExact(f.quantity)}</td>
                <td className={`${TD} num text-right`}>{price(f.price)}</td>
                <td className={`${TD} num text-right`}>
                  {formatDecimal(f.commission)} / {formatDecimal(f.fees)} {f.currency}
                </td>
                <td className={TD}>{fillBasis(d.execution.fill_details.find((x) => x.price === f.price && x.quantity === f.quantity) ?? null, f.source)}</td>
                <td className={TD}>
                  <code>{f.journal_entry_id.slice(0, 8)}</code>
                </td>
                <td className={TD}>
                  <Timestamp iso={f.filled_at} />
                </td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
      {d.execution.evaluations > 0 && (
        <KeyValues
          items={[
            {
              label: "Simulator",
              value: `${d.execution.evaluations} evaluation(s); last ${d.execution.last_outcome ? humanize(d.execution.last_outcome) : MISSING} (${d.execution.last_code ? humanize(d.execution.last_code) : MISSING})`,
            },
            { label: "Execution model", value: d.execution.execution_model_id === null ? MISSING : `version #${d.execution.execution_model_id}` },
            { label: "Arrival price (mid)", value: price(d.execution.arrival_price) },
            {
              label: "Stop trigger",
              value: d.execution.triggered_at ? (
                <>
                  {price(d.execution.trigger_price)} at <Timestamp iso={d.execution.triggered_at} />
                </>
              ) : d.execution.trailing_stop_price ? (
                `trailing at ${price(d.execution.trailing_stop_price)} (reference ${price(d.execution.trail_reference_price)})`
              ) : (
                "not triggered / not a stop"
              ),
            },
          ]}
        />
      )}
      {d.execution.fill_details.length > 0 && (
        <DataTable label="Simulated fill breakdown">
          <thead>
            <tr>
              <th className={TH}>Basis</th>
              <th className={`${TH} text-right`}>Quantity</th>
              <th className={`${TH} text-right`}>Price</th>
              <th className={`${TH} text-right`}>Touch / reference</th>
              <th className={`${TH} text-right`}>Slippage / impact (bps)</th>
              <th className={`${TH} text-right`}>Participation</th>
              <th className={`${TH} text-right`}>Commission / reg. fees</th>
            </tr>
          </thead>
          <tbody>
            {d.execution.fill_details.map((x) => (
              <tr key={x.broker_event_id}>
                <td className={TD}>{fillBasis(x, "SIMULATION")}</td>
                <td className={`${TD} num text-right`}>{formatQuantityExact(x.quantity)}</td>
                <td className={`${TD} num text-right`}>{price(x.price)}</td>
                <td className={`${TD} num text-right`}>
                  {price(x.touch_price)} / {price(x.reference_price)}
                </td>
                <td className={`${TD} num text-right`}>
                  {x.slippage_bps ? formatDecimal(x.slippage_bps, { signed: true }) : MISSING} /{" "}
                  {x.impact_bps ? formatDecimal(x.impact_bps) : MISSING}
                </td>
                <td className={`${TD} num text-right`}>
                  {x.participation ? `${formatDecimal(String(Number(x.participation) * 100), { places: 1 })}%` : MISSING}
                </td>
                <td className={`${TD} num text-right`}>
                  {formatDecimal(x.commission)} / {formatDecimal(x.regulatory_fees)}
                </td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
      {d.execution.attempts.length > 0 && (
        <DataTable label="Simulator evaluations">
          <thead>
            <tr>
              <th className={TH}>At</th>
              <th className={TH}>Outcome</th>
              <th className={TH}>Reason</th>
            </tr>
          </thead>
          <tbody>
            {d.execution.attempts.map((x) => (
              <tr key={x.id}>
                <td className={TD}>
                  <Timestamp iso={x.evaluated_at} />
                </td>
                <td className={TD}>
                  <Badge tone={ATTEMPT_TONE[x.outcome] ?? "neutral"}>{humanize(x.outcome)}</Badge>
                </td>
                <td className={TD}>{x.message}</td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </div>
  );
}

function EventsPanel({ accountId, refreshKey }: { accountId: string | null; refreshKey: number }) {
  const events = useApiResource((signal) => orders.events(accountId, { signal }), [accountId, refreshKey]);
  return (
    <Panel title="Recent order events" subtitle="every state change, with its source and reason">
      {events.status === "error" && <ErrorState message={events.error ?? ""} onRetry={events.reload} />}
      {events.data && events.data.length === 0 && <EmptyState title="No order event yet" />}
      {events.data && events.data.length > 0 && (
        <DataTable label="Recent order events">
          <thead>
            <tr>
              <th className={TH}>At</th>
              <th className={TH}>Asset</th>
              <th className={TH}>Event</th>
              <th className={TH}>Transition</th>
              <th className={TH}>Source</th>
              <th className={TH}>Reason</th>
            </tr>
          </thead>
          <tbody>
            {events.data.map((e) => (
              <tr key={e.id}>
                <td className={TD}>
                  <Timestamp iso={e.occurred_at} />
                </td>
                <td className={TD}>{e.symbol ?? MISSING}</td>
                <td className={TD}>{humanize(e.event_type)}</td>
                <td className={TD}>
                  {e.from_status ? humanize(e.from_status) : "new"} → {humanize(e.to_status)}
                </td>
                <td className={TD}>{humanize(e.source)}</td>
                <td className={TD}>{e.reason}</td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}

function RefusedPanel({ accountId, refreshKey }: { accountId: string | null; refreshKey: number }) {
  const refused = useApiResource((signal) => orders.refused(accountId, { signal }), [accountId, refreshKey]);
  return (
    <Panel title="Refused submissions" subtitle="unauthorized or invalid requests: no order was created">
      {refused.status === "error" && <ErrorState message={refused.error ?? ""} onRetry={refused.reload} />}
      {refused.data && refused.data.length === 0 && <EmptyState title="No refused submission" />}
      {refused.data && refused.data.length > 0 && (
        <DataTable label="Refused submissions">
          <thead>
            <tr>
              <th className={TH}>At</th>
              <th className={TH}>Requested by</th>
              <th className={TH}>Reasons</th>
            </tr>
          </thead>
          <tbody>
            {refused.data.map((x) => (
              <tr key={x.id}>
                <td className={TD}>
                  <Timestamp iso={x.requested_at} />
                </td>
                <td className={TD}>{x.requested_by}</td>
                <td className={TD}>
                  {x.reasons.map((r) => (
                    <div key={`${r.code}-${r.message}`}>
                      <Badge tone="negative">{humanize(r.code)}</Badge> <span className="text-ink-muted">{r.message}</span>
                    </div>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}
