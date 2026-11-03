import { Link } from "react-router";

import { formatPrice, humanize } from "../lib/format";
import type {
  Direction,
  EvaluationStatus,
  EvaluationView,
  ProposalStatus,
  ProposalView,
  StrategySummary,
} from "../types/strategies";
import { FAMILY_LABELS } from "../types/strategies";
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
  return e.notes.join("; ");
}

export function ProposalsTable({ proposals, showStrategy = true }: { proposals: ProposalView[]; showStrategy?: boolean }) {
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
        </tr>
      </thead>
      <tbody>
        {proposals.map((p) => (
          <tr key={p.id} title={p.reasoning}>
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
          </tr>
        ))}
      </tbody>
    </DataTable>
  );
}

export function EvaluationsTable({ evaluations, showStrategy = true }: { evaluations: EvaluationView[]; showStrategy?: boolean }) {
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
          <th className={`${TH} text-right`}>Proposals</th>
          <th className={`${TH} text-right`}>Duration</th>
          <th className={TH}>Basis</th>
          <th className={TH}>Outcome</th>
        </tr>
      </thead>
      <tbody>
        {evaluations.map((e) => (
          <tr key={e.id}>
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
            <td className={`${TD} num text-right`}>{e.proposals_count}</td>
            <td className={`${TD} num text-right`}>{e.duration_ms === null ? "—" : `${e.duration_ms} ms`}</td>
            <td className={TD}>
              <BasisLabel mode={e.mode} basis={e.basis} />
            </td>
            <td className={`${TD} text-ink-muted max-w-80 truncate`} title={outcome(e)}>
              {outcome(e) || "—"}
            </td>
          </tr>
        ))}
      </tbody>
    </DataTable>
  );
}
