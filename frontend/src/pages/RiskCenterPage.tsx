import { ChevronRight, OctagonX } from "lucide-react";
import { type FormEvent, Fragment, useState } from "react";

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
import { ApiError } from "../services/api";
import { risk } from "../services/risk";
import { ReservationsPanel } from "./RiskReservationsPanel";
import type {
  AssessmentResult,
  AssessmentSummary,
  ExposureGroup,
  LiveState,
  RiskOverview,
} from "../types/risk";

const REFRESH_MS = 30_000;

const RESULT_TONE: Record<AssessmentResult, Tone> = {
  PASS_FOR_RESERVATION: "positive",
  REJECTED: "negative",
  INSUFFICIENT_DATA: "caution",
  RISK_HALTED: "negative",
};
const OUTCOME_TONE: Record<string, Tone> = {
  PASS: "positive",
  FAIL: "negative",
  UNKNOWN: "caution",
  HALT: "negative",
  RESTRICT: "info",
  INFO: "neutral",
};
const MARK_TONE: Record<string, Tone> = { COMPLETE: "positive", STALE: "caution", INCOMPLETE: "caution" };

/** A fraction from the API ("0.0125") as a percentage for display only (not money). */
function pct(fraction: string | null | undefined, digits = 2): string {
  if (fraction === null || fraction === undefined) return MISSING;
  const value = Number(fraction);
  return Number.isFinite(value) ? `${(value * 100).toFixed(digits)}%` : MISSING;
}

function ratio(part: string | null, whole: string | null): number | null {
  if (part === null || whole === null) return null;
  const p = Number(part);
  const w = Number(whole);
  return Number.isFinite(p) && Number.isFinite(w) && w > 0 ? p / w : null;
}

function resultLabel(result: string): string {
  return result === "PASS_FOR_RESERVATION" ? "Pass for reservation" : humanize(result);
}

export function RiskCenterPage() {
  const [refresh, setRefresh] = useState(0);
  const state = useApiResource((signal) => risk.state({ signal }), [refresh], { refreshMs: REFRESH_MS });
  const notInitialized = state.status === "error" && state.data === null && /cli init/.test(state.error ?? "");
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Risk Center"
        description="The deterministic risk engine: hard limits, live exposure from the ledger, reserved capacity, daily and weekly loss, drawdown, circuit breakers, the kill switch and every recorded risk decision. AI models and strategies cannot change any of it."
        actions={<RefreshButton onClick={() => setRefresh((n) => n + 1)} loading={state.loading} />}
      />
      {state.status === "loading" && <Loading label="Loading the risk state…" />}
      {notInitialized && (
        <EmptyState title="The paper account has not been created yet">{state.error}</EmptyState>
      )}
      {state.status === "error" && !notInitialized && <ErrorState message={state.error ?? ""} onRetry={state.reload} />}
      {state.data && <RiskState data={state.data} refreshKey={refresh} onChanged={state.reload} />}
    </section>
  );
}

function RiskState({ data, refreshKey, onChanged }: { data: RiskOverview; refreshKey: number; onChanged: () => void }) {
  const live = data.live;
  return (
    <div className="flex flex-col gap-3">
      <Notice tone="info">{data.disclaimer}</Notice>
      {data.trading_halted && (
        <Notice tone="negative">
          <strong>New entries are halted</strong> by {data.active_breakers.map(humanize).join(", ")}. Existing positions
          stay monitored; nothing is liquidated. Recovery needs the breaker&apos;s configured process (see Circuit
          breakers).
        </Notice>
      )}
      {!data.limits.configured && (
        <Notice tone="caution">
          {data.limits.message} Until then every assessment is recorded as insufficient data.
        </Notice>
      )}
      {live && live.mark_status !== "COMPLETE" && (
        <Notice tone="caution">
          The portfolio cannot be marked to fresh prices ({live.mark_status.toLowerCase()}): equity, exposure and losses
          below are not reliable and the data-quality breaker blocks new entries. Nothing is substituted.
        </Notice>
      )}
      {live && <Headline live={live} reserved={data.reserved_exposure} />}
      <div className="grid gap-3 lg:grid-cols-2">
        <BreakersPanel data={data} />
        <KillSwitchPanel data={data} onChanged={onChanged} />
      </div>
      {live && <LossPanel live={live} data={data} />}
      <ReservationsPanel capacity={data.reserved_exposure} cur={data.account.base_currency} refreshKey={refreshKey} />
      {live && <ExposurePanel live={live} />}
      <LimitsPanel data={data} />
      <AssessmentsPanel counts={data.assessment_counts} refreshKey={refreshKey} />
      <BreakerHistoryPanel refreshKey={refreshKey} />
      <ConfigHistoryPanel refreshKey={refreshKey} />
    </div>
  );
}

function Headline({ live, reserved }: { live: LiveState; reserved: RiskOverview["reserved_exposure"] }) {
  const cur = live.functional_currency;
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
      <Stat label="Equity (marked)" value={formatMoney(live.equity, cur)} hint={<Timestamp iso={live.as_of} />} />
      <Stat
        label="Gross exposure"
        value={formatMoney(live.gross_exposure, cur)}
        hint={`${pct(live.of_equity.gross_exposure)} of equity`}
      />
      <Stat label="Net exposure" value={formatMoney(live.net_exposure, cur, { signed: true })} hint={`${pct(live.of_equity.net_exposure)} of equity`} />
      <Stat label="Long / short" value={`${formatDecimal(live.long_exposure, { places: 0 })} / ${formatDecimal(live.short_exposure, { places: 0 })}`} hint={`${live.open_positions} open position(s)`} />
      <Stat label="Margin used" value={formatMoney(live.margin_used, cur)} hint="Short collateral beyond proceeds" />
      <Stat label="Available capital" value={formatMoney(live.available_capital, cur)} hint="Cash − accrued costs − collateral" />
      <Stat
        label="Available risk budget"
        value={formatMoney(live.available_risk_budget, cur)}
        hint="Loss allowance before the nearest limit"
      />
      <Stat
        label="Reserved exposure"
        value={formatMoney(reserved.reserved.gross_exposure, cur)}
        hint={`${reserved.reserved.active_count} active reservation(s)`}
      />
    </div>
  );
}

function Meter({ label, used, limit, cur }: { label: string; used: string | null; limit: string | null; cur: string }) {
  const r = ratio(used, limit);
  const width = r === null ? 0 : Math.min(Math.max(r, 0), 1) * 100;
  const tone = r === null ? "bg-ink-faint" : r >= 1 ? "bg-negative" : r >= 0.75 ? "bg-caution" : "bg-positive";
  return (
    <div className="flex flex-col gap-1">
      <div className="flex justify-between text-xs">
        <span className="text-ink-muted">{label}</span>
        <span className="num">
          {formatMoney(used, cur)} of {formatMoney(limit, cur)}
        </span>
      </div>
      <div
        className="bg-panel-raised h-1.5 rounded-sm"
        role="meter"
        aria-label={`${label} used`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(width)}
      >
        <div className={`h-1.5 rounded-sm ${tone}`} style={{ width: `${width}%` }} />
      </div>
    </div>
  );
}

function LossPanel({ live, data }: { live: LiveState; data: RiskOverview }) {
  const cur = live.functional_currency;
  const limits = (data.limits.active?.hard_limits ?? {}) as Record<string, string>;
  const limitOf = (base: string | null, pctKey: string) =>
    base === null || limits[pctKey] === undefined ? null : String(Number(base) * Number(limits[pctKey]));
  return (
    <Panel title="Loss limits and drawdown" subtitle={data.limits.timezone_policy}>
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="flex flex-col gap-3">
          <Meter label="Daily loss" used={live.daily_loss} limit={limitOf(live.daily_baseline, "daily_loss_limit_pct")} cur={cur} />
          <Meter label="Weekly loss" used={live.weekly_loss} limit={limitOf(live.weekly_baseline, "weekly_loss_limit_pct")} cur={cur} />
          <Meter label="Drawdown from peak" used={live.drawdown} limit={limitOf(live.peak_equity, "max_drawdown_pct")} cur={cur} />
          <p className="text-ink-faint text-2xs">
            Loss = fall in investment return (equity − contributed capital) since the start-of-period equity: realized and
            unrealized P/L, FX and every cost; funding is never a gain. Peak equity {formatMoney(live.peak_equity, cur)}.
          </p>
        </div>
        <DataTable label="Period baselines">
          <thead>
            <tr>
              <th className={TH}>Period</th>
              <th className={TH}>Starts</th>
              <th className={`${TH} text-right`}>Start equity</th>
              <th className={TH}>Basis</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(live.baselines).map(([kind, b]) => (
              <tr key={kind}>
                <td className={TD}>
                  {humanize(kind)} {b ? <span className="text-ink-faint">{b.period}</span> : null}
                </td>
                <td className={TD}>{b ? <Timestamp iso={b.period_start} /> : MISSING}</td>
                <td className={`${TD} num text-right`}>{b ? formatMoney(b.equity, cur) : MISSING}</td>
                <td className={TD} title={b?.source}>
                  {b ? (
                    <Badge tone={b.basis === "FIRST_OBSERVATION_IN_PERIOD" ? "caution" : "neutral"}>{humanize(b.basis)}</Badge>
                  ) : (
                    "not measurable"
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      </div>
    </Panel>
  );
}

function BreakersPanel({ data }: { data: RiskOverview }) {
  return (
    <Panel title="Circuit breakers" subtitle="block new entries; never liquidate">
      <DataTable label="Circuit breakers">
        <thead>
          <tr>
            <th className={TH}>Breaker</th>
            <th className={TH}>Status</th>
            <th className={TH}>Recovery</th>
            <th className={TH}>Since</th>
            <th className={TH}>Last</th>
          </tr>
        </thead>
        <tbody>
          {data.breakers.map((b) => (
            <tr key={b.breaker}>
              <td className={TD}>{humanize(b.breaker)}</td>
              <td className={TD}>
                <Badge tone={b.status === "TRIPPED" ? "negative" : "positive"} dot>
                  {b.status === "TRIPPED" ? "Tripped" : "Clear"}
                </Badge>
              </td>
              <td className={`${TD} text-ink-muted`}>{b.recovery === "AUTO_WHEN_CLEAR" ? "automatic, recorded" : "operator reset"}</td>
              <td className={TD}>{b.since ? <Timestamp iso={b.since} /> : MISSING}</td>
              <td className={`${TD} text-ink-muted max-w-64 truncate`} title={b.reason ?? undefined}>
                {b.actor ? `${b.actor}: ${b.reason ?? ""}` : MISSING}
              </td>
            </tr>
          ))}
        </tbody>
      </DataTable>
    </Panel>
  );
}

function KillSwitchPanel({ data, onChanged }: { data: RiskOverview; onChanged: () => void }) {
  const [reason, setReason] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: Tone; text: string } | null>(null);
  const engaged = data.kill_switch === "TRIPPED";
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      const result = await risk.engageKillSwitch(reason, token || undefined);
      setMessage({
        tone: "negative",
        text: result.already_engaged ? "The kill switch was already engaged." : "Kill switch engaged: new entries are halted.",
      });
      onChanged();
    } catch (reason_) {
      setMessage({
        tone: "negative",
        text: reason_ instanceof ApiError ? `${reason_.message}${reason_.status ? ` (HTTP ${reason_.status})` : ""}` : String(reason_),
      });
    } finally {
      setBusy(false);
    }
  };
  const input = "border-border-strong bg-surface focus:border-accent h-7 w-full rounded-sm border px-2 text-xs outline-none";
  return (
    <Panel
      title="Kill switch"
      actions={
        <Badge tone={engaged ? "negative" : "positive"} dot>
          {engaged ? "Engaged" : "Not engaged"}
        </Badge>
      }
    >
      <form onSubmit={(e) => void submit(e)} className="space-y-2.5" aria-label="Engage the kill switch">
        <label className="text-ink-muted block text-2xs font-semibold uppercase">
          Reason
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className={`${input} mt-1 normal-case`}
            aria-label="Reason"
            placeholder="why new entries must stop"
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
        <Button type="submit" variant="primary" disabled={busy || engaged || reason.trim().length < 3}>
          <OctagonX aria-hidden className="size-3.5" />
          {busy ? "Engaging…" : "Engage kill switch"}
        </Button>
        {message && <Notice tone={message.tone}>{message.text}</Notice>}
        <p className="text-ink-faint text-2xs">
          Engaging only halts new entries (audited). Releasing it is an operator action with a reason, from the command
          line: <code>python -m app.risk.cli kill-switch release --actor operator:&lt;name&gt; --reason …</code>. The
          dashboard cannot loosen risk.
        </p>
      </form>
    </Panel>
  );
}

function GroupTable({ title, rows, keyName, cur }: { title: string; rows: ExposureGroup[]; keyName: "asset_class" | "sector" | "cluster"; cur: string }) {
  return (
    <DataTable label={title}>
      <thead>
        <tr>
          <th className={TH}>{title}</th>
          <th className={`${TH} text-right`}>Gross</th>
          <th className={`${TH} text-right`}>Net</th>
          <th className={`${TH} text-right`}>Of equity</th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <tr>
            <td className={`${TD} text-ink-faint`} colSpan={4}>
              No exposure
            </td>
          </tr>
        ) : (
          rows.map((r) => (
            <tr key={r[keyName]}>
              <td className={TD}>{humanize(r[keyName] ?? "")}</td>
              <td className={`${TD} num text-right`}>{formatMoney(r.gross, cur)}</td>
              <td className={`${TD} num text-right`}>{formatMoney(r.net, cur, { signed: true })}</td>
              <td className={`${TD} num text-right`}>{pct(r.of_equity)}</td>
            </tr>
          ))
        )}
      </tbody>
    </DataTable>
  );
}

function ExposurePanel({ live }: { live: LiveState }) {
  const cur = live.functional_currency;
  const b = live.breakdown;
  return (
    <Panel
      title="Exposure"
      subtitle={`from the ledger, marked to fresh prices · pending ${formatMoney(live.pending_exposure, cur)} (${live.pending_source})`}
      actions={
        <Badge tone={MARK_TONE[live.mark_status] ?? "neutral"} dot>
          {humanize(live.mark_status)}
        </Badge>
      }
    >
      <div className="flex flex-col gap-3">
        {live.mark_issues.length > 0 && (
          <ul className="text-caution list-disc pl-5 text-xs" aria-label="Marking issues">
            {live.mark_issues.map((issue, i) => (
              <li key={i}>
                {humanize(issue.kind)}: {issue.symbol ?? issue.currency ?? ""} {issue.detail ? `(${issue.detail})` : ""}
              </li>
            ))}
          </ul>
        )}
        <DataTable label="Positions">
          <thead>
            <tr>
              <th className={TH}>Asset</th>
              <th className={TH}>Side</th>
              <th className={`${TH} text-right`}>Quantity</th>
              <th className={`${TH} text-right`}>Exposure</th>
              <th className={`${TH} text-right`}>Of equity</th>
              <th className={TH}>Sector / cluster</th>
              <th className={TH}>Price</th>
            </tr>
          </thead>
          <tbody>
            {b.assets.length === 0 ? (
              <tr>
                <td className={`${TD} text-ink-faint`} colSpan={7}>
                  No open positions
                </td>
              </tr>
            ) : (
              b.assets.map((a) => (
                <tr key={a.asset_id}>
                  <td className={TD}>
                    {a.symbol} <span className="text-ink-faint">{a.asset_class}</span>
                  </td>
                  <td className={TD}>
                    <Badge tone={a.direction === "LONG" ? "info" : "accent"}>{a.direction}</Badge>
                  </td>
                  <td className={`${TD} num text-right`}>{formatQuantityExact(a.quantity)}</td>
                  <td className={`${TD} num text-right`}>{formatMoney(a.exposure, cur)}</td>
                  <td className={`${TD} num text-right`}>{pct(a.of_equity)}</td>
                  <td className={TD}>
                    {a.sector ?? "unclassified"} {a.cluster ? <span className="text-ink-faint">/ {a.cluster}</span> : null}
                  </td>
                  <td className={TD} title={a.price_source ?? undefined}>
                    <Badge tone={a.price_status === "FRESH" ? "positive" : "caution"}>{humanize(a.price_status)}</Badge>{" "}
                    <span className="num">{a.price ? formatDecimal(a.price, { places: 4 }) : MISSING}</span>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </DataTable>
        <div className="grid gap-3 lg:grid-cols-3">
          <GroupTable title="Asset class" rows={b.asset_classes} keyName="asset_class" cur={cur} />
          <GroupTable title="Sector" rows={b.sectors} keyName="sector" cur={cur} />
          <GroupTable title="Correlation cluster" rows={b.clusters} keyName="cluster" cur={cur} />
        </div>
        <p className="text-ink-faint text-2xs">
          Gross adds long and short; offsetting positions are never assumed to cancel correlated or directional risk.
        </p>
      </div>
    </Panel>
  );
}

function LimitsPanel({ data }: { data: RiskOverview }) {
  const active = data.limits.active;
  const [section, setSection] = useState<"hard" | "prefs">("hard");
  const values = active ? (section === "hard" ? active.hard_limits : active.preferences) : {};
  return (
    <Panel
      title="Active limits"
      subtitle={active ? `configuration version ${active.version}` : "not configured"}
      actions={
        <Segmented
          label="Section"
          value={section}
          onChange={setSection}
          options={[
            { value: "hard", label: "Hard limits" },
            { value: "prefs", label: "Preferences" },
          ]}
        />
      }
    >
      <div className="flex flex-col gap-2">
        <Notice tone="caution">{data.limits.note}</Notice>
        {active ? (
          <KeyValues
            items={Object.entries(values)
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([key, value]) => ({
              label: humanize(key),
              value: <code className="text-xs">{typeof value === "object" ? JSON.stringify(value) : String(value)}</code>,
            }))}
          />
        ) : (
          <EmptyState title="No risk configuration">{data.limits.message}</EmptyState>
        )}
        <p className="text-ink-faint text-2xs">{data.limits.governance}</p>
      </div>
    </Panel>
  );
}

function AssessmentsPanel({ counts, refreshKey }: { counts: RiskOverview["assessment_counts"]; refreshKey: number }) {
  const [filter, setFilter] = useState<AssessmentResult | "ALL">("ALL");
  const [open, setOpen] = useState<string | null>(null);
  const list = useApiResource((signal) => risk.assessments(filter === "ALL" ? null : filter, { signal }), [refreshKey, filter]);
  return (
    <Panel
      title="Risk assessments"
      subtitle="a pass is not an execution approval: reservations and order authorization are Phase 6C"
      actions={
        <Segmented
          label="Result"
          value={filter}
          onChange={setFilter}
          options={[
            { value: "ALL", label: "All" },
            { value: "PASS_FOR_RESERVATION", label: `Pass (${counts.PASS_FOR_RESERVATION ?? 0})` },
            { value: "REJECTED", label: `Rejected (${counts.REJECTED ?? 0})` },
            { value: "INSUFFICIENT_DATA", label: `Insufficient (${counts.INSUFFICIENT_DATA ?? 0})` },
            { value: "RISK_HALTED", label: `Halted (${counts.RISK_HALTED ?? 0})` },
          ]}
        />
      }
    >
      {list.status === "loading" && <Loading />}
      {list.status === "error" && <ErrorState message={list.error ?? ""} onRetry={list.reload} />}
      {list.data && list.data.length === 0 && (
        <EmptyState title="No assessments recorded">
          Assess a proposal with <code>python -m app.risk.cli assess</code>.
        </EmptyState>
      )}
      {list.data && list.data.length > 0 && (
        <DataTable label="Risk assessments">
          <thead>
            <tr>
              <th className={TH} aria-label="Details" />
              <th className={TH}>Assessed</th>
              <th className={TH}>Asset</th>
              <th className={TH}>Result</th>
              <th className={`${TH} text-right`}>Entry / stop</th>
              <th className={`${TH} text-right`}>Permitted</th>
              <th className={`${TH} text-right`}>Potential loss</th>
              <th className={TH}>Binding / reasons</th>
            </tr>
          </thead>
          <tbody>
            {list.data.map((a) => (
              <Fragment key={a.id}>
                <AssessmentRow a={a} open={open === a.id} onToggle={() => setOpen(open === a.id ? null : a.id)} />
                {open === a.id && (
                  <tr>
                    <td colSpan={8} className="border-border/60 border-b p-2">
                      <AssessmentChecks id={a.id} />
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

function AssessmentRow({ a, open, onToggle }: { a: AssessmentSummary; open: boolean; onToggle: () => void }) {
  return (
    <tr>
      <td className={TD}>
        <button type="button" onClick={onToggle} aria-label={`${open ? "Hide" : "Show"} checks for ${a.symbol}`} aria-expanded={open}>
          <ChevronRight aria-hidden className={`size-3.5 transition-transform ${open ? "rotate-90" : ""}`} />
        </button>
      </td>
      <td className={TD}>
        <Timestamp iso={a.assessed_at} />
      </td>
      <td className={TD}>
        {a.symbol} <Badge tone={a.direction === "LONG" ? "info" : "accent"}>{a.direction}</Badge>{" "}
        <span className="text-ink-faint">{humanize(a.source)}</span>
      </td>
      <td className={TD}>
        <Badge tone={RESULT_TONE[a.result]} dot>
          {resultLabel(a.result)}
        </Badge>
      </td>
      <td className={`${TD} num text-right`}>
        {formatDecimal(a.entry, { places: 2 })} / {a.stop ? formatDecimal(a.stop, { places: 2 }) : MISSING}
      </td>
      <td className={`${TD} num text-right`}>{a.permitted_quantity ? formatQuantityExact(a.permitted_quantity) : MISSING}</td>
      <td className={`${TD} num text-right`}>
        {a.potential_loss ? `${formatMoney(a.potential_loss, a.functional_currency)} / ${formatMoney(a.risk_budget, a.functional_currency)}` : MISSING}
      </td>
      <td className={`${TD} max-w-80 truncate`}>
        {a.reasons.length > 0 ? a.reasons.map(humanize).join(", ") : a.binding_constraint ? `binding: ${humanize(a.binding_constraint)}` : MISSING}
      </td>
    </tr>
  );
}

function AssessmentChecks({ id }: { id: string }) {
  const detail = useApiResource((signal) => risk.assessment(id, { signal }), [id]);
  if (detail.status === "loading") return <Loading label="Loading checks…" />;
  if (detail.status === "error" || !detail.data) return <ErrorState message={detail.error ?? ""} onRetry={detail.reload} />;
  const d = detail.data;
  return (
    <div className="flex flex-col gap-2">
      <Notice tone="info">{d.note}</Notice>
      <DataTable label="Assessment checks">
        <thead>
          <tr>
            <th className={TH}>#</th>
            <th className={TH}>Check</th>
            <th className={TH}>Outcome</th>
            <th className={TH}>Message</th>
            <th className={`${TH} text-right`}>Measured</th>
            <th className={`${TH} text-right`}>Limit</th>
          </tr>
        </thead>
        <tbody>
          {d.checks.map((c) => (
            <tr key={c.ordinal}>
              <td className={`${TD} num`}>{c.ordinal}</td>
              <td className={TD}>{humanize(c.check)}</td>
              <td className={TD}>
                <Badge tone={OUTCOME_TONE[c.outcome] ?? "neutral"}>{c.outcome}</Badge>
              </td>
              <td className={`${TD} max-w-[28rem] truncate whitespace-normal`} title={c.reason_code ?? undefined}>
                {c.message}
              </td>
              <td className={`${TD} num text-right`}>{c.measured ? formatDecimal(c.measured, { places: 4 }) : MISSING}</td>
              <td className={`${TD} num text-right`}>
                {c.limit ? formatDecimal(c.limit, { places: 4 }) : MISSING} {c.unit && c.unit !== "fraction" ? c.unit : ""}
              </td>
            </tr>
          ))}
        </tbody>
      </DataTable>
      <p className="text-ink-faint text-2xs">
        Inputs sha256 <code>{d.inputs_sha256.slice(0, 16)}…</code> · engine {d.engine_version} · replayable from the stored inputs.
      </p>
    </div>
  );
}

function BreakerHistoryPanel({ refreshKey }: { refreshKey: number }) {
  const events = useApiResource((signal) => risk.breakerEvents({ signal }), [refreshKey]);
  return (
    <Panel title="Circuit-breaker history" subtitle="append-only; nothing resets silently">
      {events.status === "loading" && <Loading />}
      {events.status === "error" && <ErrorState message={events.error ?? ""} onRetry={events.reload} />}
      {events.data && events.data.length === 0 && <EmptyState title="No breaker has ever tripped" />}
      {events.data && events.data.length > 0 && (
        <DataTable label="Circuit-breaker events">
          <thead>
            <tr>
              <th className={TH}>When</th>
              <th className={TH}>Breaker</th>
              <th className={TH}>Event</th>
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
                <td className={TD}>
                  {humanize(e.breaker)} <span className="text-ink-faint">#{e.sequence}</span>
                </td>
                <td className={TD}>
                  <Badge tone={e.event === "TRIPPED" ? "negative" : "positive"}>{humanize(e.event)}</Badge>
                </td>
                <td className={TD}>{e.actor}</td>
                <td className={`${TD} max-w-[32rem] truncate`} title={e.reason}>
                  {e.reason}
                </td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}

function ConfigHistoryPanel({ refreshKey }: { refreshKey: number }) {
  const versions = useApiResource((signal) => risk.configurations({ signal }), [refreshKey]);
  return (
    <Panel title="Configuration history" subtitle="immutable versions; operator-only">
      {versions.status === "loading" && <Loading />}
      {versions.status === "error" && <ErrorState message={versions.error ?? ""} onRetry={versions.reload} />}
      {versions.data && versions.data.length === 0 && <EmptyState title="No configuration versions" />}
      {versions.data && versions.data.length > 0 && (
        <DataTable label="Configuration versions">
          <thead>
            <tr>
              <th className={TH}>Version</th>
              <th className={TH}>When</th>
              <th className={TH}>Actor</th>
              <th className={TH}>Reason</th>
              <th className={TH}>Changes</th>
            </tr>
          </thead>
          <tbody>
            {versions.data.map((v) => (
              <tr key={v.id}>
                <td className={`${TD} num`}>
                  v{v.version} <span className="text-ink-faint">{humanize(v.change_kind)}</span>
                </td>
                <td className={TD}>
                  <Timestamp iso={v.created_at} />
                </td>
                <td className={TD}>{v.actor}</td>
                <td className={`${TD} max-w-64 truncate`} title={v.reason}>
                  {v.reason}
                </td>
                <td className={`${TD} max-w-[28rem] truncate whitespace-normal`}>
                  {v.change_kind === "INITIAL"
                    ? `${v.changes.length} starting values`
                    : v.changes.map((c) => `${c.field}: ${String(c.previous)} → ${String(c.new)}`).join("; ")}
                </td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}
