import { useState } from "react";

import {
  Badge,
  DataTable,
  EmptyState,
  ErrorState,
  Notice,
  Panel,
  Segmented,
  Stat,
  TD,
  TH,
  Timestamp,
  type Tone,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { MISSING, humanize } from "../lib/format";
import { formatDecimal, formatMoney, formatQuantityExact } from "../lib/money";
import { risk } from "../services/risk";
import type { ReservationCapacity, ReservationStatus } from "../types/risk";

/**
 * Phase 6C: reserved capacity for approved trade intents. Read-only: the dashboard shows
 * what is held and why; it cannot reserve, release, consume or execute anything.
 */
const RESERVATION_TONE: Record<ReservationStatus, Tone> = {
  PENDING: "neutral",
  APPROVED: "info",
  PARTIALLY_CONSUMED: "info",
  CONSUMED: "positive",
  RELEASED: "neutral",
  EXPIRED: "neutral",
  REJECTED: "negative",
  FAILED: "caution",
};
const EVENT_FILTERS = ["ALL", "APPROVED", "REJECTED", "CONSUMED", "RELEASED", "EXPIRED", "FAILED"] as const;
type EventFilter = (typeof EVENT_FILTERS)[number];

export function ReservationsPanel({
  capacity,
  cur,
  refreshKey,
}: {
  capacity: ReservationCapacity;
  cur: string;
  refreshKey: number;
}) {
  const [filter, setFilter] = useState<EventFilter>("ALL");
  const active = useApiResource((signal) => risk.reservations(true, { signal }), [refreshKey]);
  const events = useApiResource(
    (signal) => risk.reservationEvents(filter === "ALL" ? null : filter, { signal }),
    [refreshKey, filter],
  );
  const r = capacity.reserved;
  const a = capacity.available;
  const recon = capacity.reconciliation;
  return (
    <Panel title="Risk reservations" subtitle="capacity held for approved intents; not orders, fills or ledger entries">
      <div className="flex flex-col gap-3">
        {capacity.approvals_blocked && (
          <Notice tone="negative">
            <strong>New reservation approvals are blocked</strong>: the latest reservation reconciliation found
            unexplained inconsistencies. Nothing held was released.
          </Notice>
        )}
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          <Stat label="Total reserved risk" value={formatMoney(r.risk, cur)} hint="Potential loss at the stops" />
          <Stat
            label="Available risk budget"
            value={a.computed ? formatMoney(a.risk_budget, cur) : MISSING}
            hint="Loss allowance after reservations"
          />
          <Stat
            label="Reserved gross exposure"
            value={formatMoney(r.gross_exposure, cur)}
            hint={`long ${formatDecimal(r.long_exposure, { places: 0 })} / short ${formatDecimal(r.short_exposure, { places: 0 })}`}
          />
          <Stat
            label="Available exposure"
            value={a.computed ? formatMoney(a.gross_exposure, cur) : MISSING}
            hint="Gross limit − positions − reservations"
          />
          <Stat label="Active reservations" value={String(r.active_count)} hint={`capital ${formatMoney(r.capital, cur)}`} />
          <Stat
            label="Reservation reconciliation"
            value={recon ? humanize(recon.status) : "Not run"}
            {...(recon ? { tone: recon.status === "PASSED" ? ("positive" as Tone) : ("negative" as Tone) } : {})}
            hint={
              recon
                ? `${recon.unexplained} unexplained, ${recon.warnings} warning(s)`
                : "python -m app.risk.cli reservations reconcile"
            }
          />
        </div>
        {active.status === "error" && <ErrorState message={active.error ?? ""} onRetry={active.reload} />}
        {active.data && active.data.length === 0 && <EmptyState title="No active reservation" />}
        {active.data && active.data.length > 0 && (
          <DataTable label="Active reservations">
            <thead>
              <tr>
                <th className={TH}>Asset</th>
                <th className={TH}>Status</th>
                <th className={`${TH} text-right`}>Proposed / approved</th>
                <th className={`${TH} text-right`}>Consumed / remaining</th>
                <th className={`${TH} text-right`}>Risk held</th>
                <th className={`${TH} text-right`}>Exposure held</th>
                <th className={TH}>Execution</th>
                <th className={TH}>Expires</th>
              </tr>
            </thead>
            <tbody>
              {active.data.map((x) => (
                <tr key={x.id}>
                  <td className={TD}>
                    {x.symbol} <span className="text-ink-faint">{humanize(x.direction)}</span>
                  </td>
                  <td className={TD}>
                    <Badge tone={RESERVATION_TONE[x.status]}>{humanize(x.status)}</Badge>
                  </td>
                  <td className={`${TD} num text-right`}>
                    {x.proposed_quantity === null ? MISSING : formatQuantityExact(x.proposed_quantity)} /{" "}
                    {formatQuantityExact(x.approved_quantity)}
                  </td>
                  <td className={`${TD} num text-right`}>
                    {formatQuantityExact(x.consumed_quantity)} / {formatQuantityExact(x.remaining_quantity)}
                  </td>
                  <td className={`${TD} num text-right`}>{formatMoney(x.remaining.risk, x.functional_currency)}</td>
                  <td className={`${TD} num text-right`}>{formatMoney(x.remaining.exposure, x.functional_currency)}</td>
                  <td className={TD}>
                    {x.execution_link === "NONE" ? "No order attached" : humanize(x.execution_link)}
                    {x.expiry_deferred_at && <span className="text-ink-faint"> (held past expiry)</span>}
                  </td>
                  <td className={TD}>
                    <Timestamp iso={x.expires_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        )}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-sm font-medium">Reservation lifecycle history</h3>
          <Segmented
            label="Event"
            value={filter}
            onChange={setFilter}
            options={EVENT_FILTERS.map((f) => ({ value: f, label: f === "ALL" ? "All" : humanize(f) }))}
          />
        </div>
        {events.status === "error" && <ErrorState message={events.error ?? ""} onRetry={events.reload} />}
        {events.data && events.data.length === 0 && <EmptyState title="No reservation events" />}
        {events.data && events.data.length > 0 && (
          <DataTable label="Reservation events">
            <thead>
              <tr>
                <th className={TH}>When</th>
                <th className={TH}>Asset</th>
                <th className={TH}>Event</th>
                <th className={TH}>Status</th>
                <th className={`${TH} text-right`}>Quantity</th>
                <th className={`${TH} text-right`}>Risk</th>
                <th className={TH}>Actor</th>
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
                  <td className={TD}>
                    {humanize(e.event_type)} <span className="text-ink-faint">#{e.account_sequence}</span>
                  </td>
                  <td className={TD}>
                    <Badge tone={RESERVATION_TONE[e.to_status]}>{humanize(e.to_status)}</Badge>
                  </td>
                  <td className={`${TD} num text-right`}>{formatQuantityExact(e.quantity)}</td>
                  <td className={`${TD} num text-right`}>{formatDecimal(e.risk, { places: 2 })}</td>
                  <td className={TD}>{e.actor}</td>
                  <td className={`${TD} max-w-[28rem] truncate`} title={e.reason}>
                    {e.resolution ? `${humanize(e.resolution)}: ` : ""}
                    {e.reason}
                  </td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        )}
        <p className="text-xs text-ink-faint">{capacity.note}</p>
      </div>
    </Panel>
  );
}
