import { Fragment, useState } from "react";
import { Link } from "react-router";

import { formatPrice, humanize } from "../lib/format";
import type {
  CheckItem,
  ConflictView,
  Direction,
  EvaluationStatus,
  EvaluationView,
  ProposalStatus,
  ProposalView,
  SignalState,
  StrategySummary,
} from "../types/strategies";
import { FAMILY_LABELS, SIGNAL_STATE_LABELS } from "../types/strategies";
import { Badge, DataTable, EmptyState, TD, TH, Timestamp, type Tone } from "./ui";

/** Enabled / disabled, and whether the strategy has evaluation logic at all. */
export function StrategyStatusBadges({ strategy }: { strategy: StrategySummary }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <Badge tone={strategy.enabled ? "positive" : "neutral"} dot>
        {strategy.enabled ? "Enabled" : "Disabled"}
      </Badge>
      {strategy.implemented ? (
        <Badge tone="info">Logic implemented</Badge>
      ) : (
        <Badge tone="caution" title={strategy.implementation_note}>
          Interface only
        </Badge>
      )}
    </span>
  );
}

export function FamilyBadge({ family }: { family: StrategySummary["family"] }) {
  return <Badge tone="accent">{FAMILY_LABELS[family]}</Badge>;
}

export function DirectionBadge({ direction }: { direction: Direction }) {
  return <Badge tone={direction === "LONG" ? "positive" : "negative"}>{direction === "LONG" ? "Long" : "Short"}</Badge>;
}

const PROPOSAL_TONES: Record<ProposalStatus, Tone> = { ACTIVE: "info", EXPIRED: "neutral", SUPERSEDED: "neutral" };

export function ProposalStatusBadge({ status }: { status: ProposalStatus }) {
  return <Badge tone={PROPOSAL_TONES[status]}>{humanize(status)}</Badge>;
}

const EVALUATION_TONES: Record<EvaluationStatus, Tone> = {
  RUNNING: "info",
  PROPOSED: "positive",
  NO_PROPOSAL: "neutral",
  SKIPPED: "caution",
  FAILED: "negative",
};

export function EvaluationStatusBadge({ status }: { status: EvaluationStatus }) {
  return <Badge tone={EVALUATION_TONES[status]}>{humanize(status)}</Badge>;
}

const SIGNAL_TONES: Record<SignalState, Tone> = {
  SIGNAL: "positive",
  NO_SIGNAL: "neutral",
  INSUFFICIENT_DATA: "caution",
  MISSING_DATA: "caution",
  UNAVAILABLE_DATA: "caution",
  INVALID_DATA: "negative",
  INELIGIBLE_ASSET: "caution",
  DUPLICATE: "neutral",
  FAILED: "negative",
};

/** What an evaluation found (Phase 4B): finer than its status. */
export function SignalStateBadge({ state }: { state: SignalState }) {
  return <Badge tone={SIGNAL_TONES[state]}>{SIGNAL_STATE_LABELS[state]}</Badge>;
}

/** Counts of evaluation results per signal state, in a fixed order. */
export function SignalStateCounts({ counts }: { counts: StrategySummary["signal_states"] }) {
  const states = (Object.keys(SIGNAL_STATE_LABELS) as SignalState[]).filter((s) => (counts[s] ?? 0) > 0);
  if (states.length === 0) return <span className="text-ink-faint">none yet</span>;
  return (
    <span className="inline-flex flex-wrap gap-1" aria-label="Evaluation results">
      {states.map((s) => (
        <Badge key={s} tone={SIGNAL_TONES[s]}>
          {SIGNAL_STATE_LABELS[s]} <span className="num">{counts[s]}</span>
        </Badge>
      ))}
    </span>
  );
}

/** LIVE + RECORDED is what a running strategy knew; anything else is labelled. */
export function BasisLabel({ mode, basis }: { mode: string; basis: string }) {
  if (mode === "LIVE" && basis === "RECORDED") return <span className="text-ink-muted">Live</span>;
  return (
    <span className="text-ink-muted" title="Re-evaluated at a past decision time">
      {humanize(mode)} · {humanize(basis)}
    </span>
  );
}

function outcome(e: EvaluationView): string {
  if (e.status === "SKIPPED" && e.skip_reasons.length > 0) {
    return e.skip_reasons.map((r) => `${r.feature}: ${humanize(r.status).toLowerCase()}`).join("; ");
  }
  return e.signal_reason ?? e.notes.join("; ");
}

function checkMark(passed: boolean | null): string {
  if (passed === null) return "·";
  return passed ? "✓" : "✗";
}

function checkState(passed: boolean | null): string {
  if (passed === null) return "informational";
  return passed ? "passed" : "failed";
}

/** The conditions a strategy evaluated, for each direction. */
export function ChecksList({ checks }: { checks: CheckItem[] }) {
  if (checks.length === 0) return <p className="text-ink-faint text-xs">No conditions were evaluated.</p>;
  return (
    <ul aria-label="Evaluated conditions" className="flex flex-col gap-0.5 text-xs whitespace-normal">
      {checks.map((c, i) => (
        <li
          key={`${c.direction ?? "-"}-${c.name}-${i}`}
          className={c.passed === false ? "text-negative" : c.passed ? "text-ink" : "text-ink-muted"}
          title={c.features.join(", ")}
        >
          <span aria-hidden className="num inline-block w-3">
            {checkMark(c.passed)}
          </span>{" "}
          {c.direction && <span className="text-ink-faint">{c.direction} · </span>}
          <span className="font-medium">{humanize(c.name)}</span>
          <span className="sr-only"> ({checkState(c.passed)})</span>: {c.detail}
        </li>
      ))}
    </ul>
  );
}

/** Reasoning, level methods and supporting evidence of one proposal. */
export function ProposalDetails({ proposal: p }: { proposal: ProposalView }) {
  const levels = p.levels;
  return (
    <div className="flex flex-col gap-2 py-1 text-xs whitespace-normal">
      <p>{p.reasoning}</p>
      {levels.entry_method && (
        <dl aria-label="Level methods" className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
          <dt className="text-ink-muted">Entry</dt>
          <dd>{levels.entry_method}</dd>
          <dt className="text-ink-muted">Invalidation</dt>
          <dd>{levels.invalidation_method}</dd>
          <dt className="text-ink-muted">Target</dt>
          <dd>{levels.target_method ?? "—"}</dd>
          <dt className="text-ink-muted">Setup</dt>
          <dd>{levels.setup ? humanize(levels.setup) : "—"}</dd>
          <dt className="text-ink-muted">Reward / risk</dt>
          <dd className="num" title="Ratio of the distances between the reference levels; not an expected outcome">
            {levels.reward_risk ?? "—"}
          </dd>
          <dt className="text-ink-muted">Horizon</dt>
          <dd className="num">{p.holding_horizon_periods} periods</dd>
        </dl>
      )}
      <div>
        <p className="text-ink-muted mb-0.5 text-2xs font-semibold tracking-wider uppercase">Supporting evidence</p>
        <ul aria-label="Supporting evidence" className="flex flex-col gap-0.5">
          {p.evidence.map((e) => (
            <li key={`${e.feature_key}-${e.role}`} title={e.available_at ? `available ${e.available_at}` : undefined}>
              <Badge tone={e.role === "trigger" ? "accent" : e.role === "context" ? "neutral" : "info"}>{e.role}</Badge>{" "}
              <span className="num text-ink-muted">{e.feature_key}</span> = <span className="num">{e.value ?? "—"}</span>
              <span className="text-ink-faint"> · {e.note}</span>
            </li>
          ))}
        </ul>
      </div>
      <p className="text-ink-faint text-2xs">
        Event {p.evaluation_event || "—"} · strength is an uncalibrated score, not a probability
        {p.conflict_count > 0 && ` · ${p.conflict_count} recorded conflict(s) with opposite proposals`}
      </p>
    </div>
  );
}

export function ProposalsTable({ proposals, showStrategy = true }: { proposals: ProposalView[]; showStrategy?: boolean }) {
  const [open, setOpen] = useState<string | null>(null);
  if (proposals.length === 0) {
    return (
      <EmptyState title="No proposals">
        Strategies record proposals here once one with evaluation logic is enabled and evaluated.
      </EmptyState>
    );
  }
  return (
    <DataTable label="Strategy proposals">
      <thead>
        <tr>
          <th className={TH}>Signal</th>
          {showStrategy && <th className={TH}>Strategy</th>}
          <th className={TH}>Asset</th>
          <th className={TH}>Direction</th>
          <th className={TH}>TF</th>
          <th className={`${TH} text-right`}>Reference entry</th>
          <th className={`${TH} text-right`}>Invalidation</th>
          <th className={`${TH} text-right`}>Target</th>
          <th className={`${TH} text-right`} title="Uncalibrated score in [0, 1]; not a probability">
            Strength
          </th>
          <th className={TH}>Expires</th>
          <th className={TH}>Status</th>
          <th className={TH}>Basis</th>
          <th className={TH}>Details</th>
        </tr>
      </thead>
      <tbody>
        {proposals.map((p) => (
          <Fragment key={p.id}>
            <tr title={p.reasoning}>
            <td className={TD}>
              <Timestamp iso={p.signal_at} />
            </td>
            {showStrategy && (
              <td className={TD}>
                <Link to={`/strategies/${p.strategy_id}`} className="hover:text-accent">
                  {p.strategy_id}
                </Link>{" "}
                <span className="text-ink-faint num">v{p.version}</span>
              </td>
            )}
            <td className={`${TD} num`}>{p.symbol}</td>
            <td className={TD}>
              <DirectionBadge direction={p.direction} />
            </td>
            <td className={TD}>{p.timeframe}</td>
            <td className={`${TD} num text-right`}>{formatPrice(p.reference_entry)}</td>
            <td className={`${TD} num text-right`}>{p.invalidation_level ? formatPrice(p.invalidation_level) : "—"}</td>
            <td className={`${TD} num text-right`}>{p.target_reference ? formatPrice(p.target_reference) : "—"}</td>
            <td className={`${TD} num text-right`}>{p.signal_strength}</td>
            <td className={TD}>
              <Timestamp iso={p.expires_at} />
            </td>
            <td className={TD}>
              <ProposalStatusBadge status={p.status} />
            </td>
            <td className={TD}>
              <BasisLabel mode={p.mode} basis={p.basis} />
            </td>
            <td className={TD}>
              <span className="inline-flex items-center gap-1">
                <button
                  type="button"
                  className="text-ink-muted hover:text-accent text-xs"
                  aria-expanded={open === p.id}
                  onClick={() => setOpen(open === p.id ? null : p.id)}
                >
                  {open === p.id ? "Hide" : "Evidence"}
                </button>
                {p.conflict_count > 0 && (
                  <Badge tone="caution" title="Overlapping opposite-direction proposal(s) recorded">
                    Conflict
                  </Badge>
                )}
              </span>
            </td>
            </tr>
            {open === p.id && (
              <tr>
                <td className={TD} colSpan={showStrategy ? 13 : 12}>
                  <ProposalDetails proposal={p} />
                </td>
              </tr>
            )}
          </Fragment>
        ))}
      </tbody>
    </DataTable>
  );
}

/** Recorded opposite-direction conflicts. Nothing is resolved here: that is Phase 5. */
export function ConflictsTable({ conflicts }: { conflicts: ConflictView[] }) {
  if (conflicts.length === 0) {
    return (
      <EmptyState title="No conflicts">
        A conflict is recorded when two overlapping proposals for the same asset point in opposite directions.
      </EmptyState>
    );
  }
  return (
    <DataTable label="Proposal conflicts">
      <thead>
        <tr>
          <th className={TH}>Detected</th>
          <th className={TH}>Asset</th>
          <th className={TH}>Proposals</th>
          <th className={TH}>Overlap</th>
          <th className={TH}>Basis</th>
        </tr>
      </thead>
      <tbody>
        {conflicts.map((c) => (
          <tr key={c.id}>
            <td className={TD}>
              <Timestamp iso={c.detected_at} />
            </td>
            <td className={`${TD} num`}>{c.symbol}</td>
            <td className={TD}>
              <span className="inline-flex flex-wrap items-center gap-1">
                {c.proposals.map((p) => (
                  <span key={p.id} className="inline-flex items-center gap-1">
                    <DirectionBadge direction={p.direction} />
                    <span>{p.strategy_id}</span>
                    <span className="text-ink-faint num">{p.timeframe}</span>
                  </span>
                ))}
                {c.same_strategy && <Badge tone="neutral">same strategy</Badge>}
              </span>
            </td>
            <td className={TD}>
              <Timestamp iso={c.overlap_start} /> – <Timestamp iso={c.overlap_end} />
            </td>
            <td className={TD}>
              <BasisLabel mode={c.mode} basis={c.basis} />
            </td>
          </tr>
        ))}
      </tbody>
    </DataTable>
  );
}

export function EvaluationsTable({ evaluations, showStrategy = true }: { evaluations: EvaluationView[]; showStrategy?: boolean }) {
  const [open, setOpen] = useState<string | null>(null);
  if (evaluations.length === 0) {
    return (
      <EmptyState title="No evaluations">
        Evaluations appear when the evaluation worker runs a strategy that has logic and is enabled.
      </EmptyState>
    );
  }
  return (
    <DataTable label="Evaluation history">
      <thead>
        <tr>
          <th className={TH}>Started</th>
          {showStrategy && <th className={TH}>Strategy</th>}
          <th className={TH}>Asset</th>
          <th className={TH}>TF</th>
          <th className={TH}>Trigger</th>
          <th className={TH}>Data cutoff</th>
          <th className={TH}>Status</th>
          <th className={TH}>Result</th>
          <th className={`${TH} text-right`}>Proposals</th>
          <th className={`${TH} text-right`}>Duration</th>
          <th className={TH}>Basis</th>
          <th className={TH}>Outcome</th>
        </tr>
      </thead>
      <tbody>
        {evaluations.map((e) => (
          <Fragment key={e.id}>
            <tr>
            <td className={TD}>
              <Timestamp iso={e.started_at} />
            </td>
            {showStrategy && (
              <td className={TD}>
                {e.strategy_id} <span className="text-ink-faint num">v{e.version}</span>
              </td>
            )}
            <td className={`${TD} num`}>{e.symbol}</td>
            <td className={TD}>{e.timeframe}</td>
            <td className={TD} title={e.trigger_ref}>
              {humanize(e.trigger)}
              {e.attempt > 1 && <span className="text-ink-faint"> · attempt {e.attempt}</span>}
            </td>
            <td className={TD}>
              <Timestamp iso={e.data_cutoff} />
            </td>
            <td className={TD}>
              <EvaluationStatusBadge status={e.status} />
            </td>
            <td className={TD}>{e.signal_state ? <SignalStateBadge state={e.signal_state} /> : "—"}</td>
            <td className={`${TD} num text-right`}>{e.proposals_count}</td>
            <td className={`${TD} num text-right`}>{e.duration_ms === null ? "—" : `${e.duration_ms} ms`}</td>
            <td className={TD}>
              <BasisLabel mode={e.mode} basis={e.basis} />
            </td>
            <td className={`${TD} text-ink-muted max-w-80 truncate`} title={outcome(e)}>
              {e.checks.length > 0 && (
                <button
                  type="button"
                  className="hover:text-accent mr-1 text-xs"
                  aria-expanded={open === e.id}
                  onClick={() => setOpen(open === e.id ? null : e.id)}
                >
                  {open === e.id ? "Hide checks" : `${e.checks.length} checks`}
                </button>
              )}
              {outcome(e) || "—"}
            </td>
            </tr>
            {open === e.id && (
              <tr>
                <td className={TD} colSpan={showStrategy ? 12 : 11}>
                  <ChecksList checks={e.checks} />
                  {e.eligibility && (
                    <p className="text-ink-faint mt-1 text-2xs">
                      Eligibility at the cutoff ({e.eligibility.provider} reference v
                      {e.eligibility.reference_version ?? "—"}): long {e.eligibility.long_eligible ? "yes" : "no"},
                      short {e.eligibility.short_eligible ? "yes" : "no"}
                    </p>
                  )}
                </td>
              </tr>
            )}
          </Fragment>
        ))}
      </tbody>
    </DataTable>
  );
}
