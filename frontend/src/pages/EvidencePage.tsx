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
  Stat,
  TD,
  TH,
  Timestamp,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { MISSING, humanize } from "../lib/format";
import { num, pct } from "../lib/validation";
import { ApiError } from "../services/api";
import { evidence } from "../services/evidence";
import type {
  Classification,
  Criterion,
  CriterionStatus,
  Scorecard,
  StrategyScore,
} from "../types/evidence";

type Tone = "positive" | "negative" | "caution" | "info" | "neutral";

export const CLASSIFICATION_TONE: Record<Classification, Tone> = {
  REJECTED: "negative",
  INSUFFICIENT_EVIDENCE: "caution",
  NOT_DEMONSTRATED: "negative",
  CONTINUE_PAPER: "info",
  CANDIDATE_FOR_REVIEW: "positive",
};

const STATUS_TONE: Record<CriterionStatus, Tone> = {
  PASS: "positive",
  FAIL: "negative",
  INSUFFICIENT: "caution",
  NOT_AVAILABLE: "neutral",
};

const money = (v: number | null | undefined) => (v == null ? MISSING : num(v, 2, { signed: true }));

async function latestOrNone(signal: AbortSignal): Promise<Scorecard | null> {
  try {
    return await evidence.latest({ signal });
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

function CriteriaTable({ criteria, label }: { criteria: Criterion[]; label: string }) {
  return (
    <DataTable label={label}>
      <thead>
        <tr>
          <th className={TH}>Criterion</th>
          <th className={TH}>Section</th>
          <th className={TH}>Status</th>
          <th className={TH}>Detail</th>
        </tr>
      </thead>
      <tbody>
        {criteria.map((c) => (
          <tr key={`${c.section}-${c.code}`}>
            <td className={TD}>{humanize(c.code)}</td>
            <td className={TD}>{humanize(c.section)}</td>
            <td className={TD}>
              <Badge tone={STATUS_TONE[c.status]}>{humanize(c.status)}</Badge>
              {!c.blocking && <span className="text-ink-faint ml-1 text-2xs">informational</span>}
            </td>
            <td className={`${TD} whitespace-normal`}>{c.message}</td>
          </tr>
        ))}
      </tbody>
    </DataTable>
  );
}

function StrategyPanel({ id, s }: { id: string; s: StrategyScore }) {
  const oos = s.out_of_sample;
  const bt = s.backtest;
  const paper = s.paper;
  return (
    <Panel
      title={humanize(id)}
      actions={<Badge tone={CLASSIFICATION_TONE[s.classification]}>{humanize(s.classification)}</Badge>}
    >
      <ul className="text-ink-muted mb-2 list-disc pl-4 text-xs">
        {s.reasons.slice(0, 4).map((r) => (
          <li key={r}>{r}</li>
        ))}
      </ul>
      <div className="grid gap-2 md:grid-cols-3">
        <Panel title="Backtest (in sample)" subtitle="chosen on this period: passes nothing">
          {bt ? (
            <KeyValues
              items={[
                { label: "Period", value: `${bt.first ?? MISSING} → ${bt.last ?? MISSING}` },
                { label: "Net return", value: pct(bt.net_return) },
                { label: "Sharpe", value: num(bt.sharpe_annualized) },
                { label: "Closed trades", value: bt.trade_statistics?.trades ?? MISSING },
              ]}
            />
          ) : (
            <span className="text-ink-faint text-xs">No search run (stored parameters only).</span>
          )}
        </Panel>
        <Panel title="Out of sample" subtitle="walk-forward test windows + first holdout read">
          {oos ? (
            <KeyValues
              items={[
                { label: "Sessions / closed trades", value: `${oos.sessions} / ${oos.trades}` },
                { label: "Net return", value: pct(oos.net_return) },
                { label: "Net P/L", value: money(oos.net_pnl) },
                { label: "Profit factor", value: num(oos.profit_factor) },
                { label: "Sharpe vs benchmark", value: `${num(oos.sharpe_annualized)} vs ${num(oos.benchmark_sharpe_annualized)}` },
                { label: "Deflated Sharpe / PBO", value: `${num(oos.deflated_sharpe)} / ${num(oos.pbo)}` },
                { label: "Max drawdown", value: pct(oos.max_drawdown, { signed: false }) },
                {
                  label: "Holdout (first read)",
                  value: oos.holdout
                    ? `#${oos.holdout.access_number}: ${oos.holdout.trades} trades, ${money(oos.holdout.net_pnl)}`
                    : "not read",
                },
                {
                  label: "Baselines",
                  value: oos.baselines
                    ? `${oos.baselines.benchmark ?? "benchmark"} ${pct(oos.baselines.benchmark_return)}, equal-weight ${pct(oos.baselines.equal_weight_universe_return)}, cash 0%`
                    : MISSING,
                },
              ]}
            />
          ) : (
            <span className="text-ink-faint text-xs">No validation run.</span>
          )}
        </Panel>
        <Panel title="Paper trading" subtitle="closed simulated positions, never a replay">
          {paper ? (
            <KeyValues
              items={[
                { label: "Observed sessions / closed trades", value: `${paper.sessions} / ${paper.trades}` },
                { label: "Net P/L", value: money(paper.net_pnl) },
                { label: "Profit factor", value: num(paper.profit_factor) },
                {
                  label: "Entry shortfall vs assumption",
                  value:
                    paper.entry_shortfall_vs_decision_bps == null
                      ? MISSING
                      : `${num(paper.entry_shortfall_vs_decision_bps, 1)} bps vs ${num(paper.assumed_cost_bps, 1)} bps`,
                },
              ]}
            />
          ) : (
            <span className="text-ink-faint text-xs">No paper account in this scorecard.</span>
          )}
        </Panel>
      </div>
      <details className="mt-2">
        <summary className="text-ink-muted cursor-pointer text-xs">Criteria ({s.criteria.length})</summary>
        <CriteriaTable criteria={s.criteria} label={`${id} criteria`} />
      </details>
    </Panel>
  );
}

/** Evidence & Readiness (Phase 11A): the latest readiness scorecard under a pre-registered
 * protocol, with backtest, out-of-sample and paper-trading evidence kept apart. */
export function EvidencePage() {
  const card = useApiResource((signal) => latestOrNone(signal), [], { refreshMs: 60_000 });
  const protocols = useApiResource((signal) => evidence.protocols({ signal }), []);
  const result = card.data?.result;
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Evidence & Readiness"
        description="Readiness scorecards judged under pre-registered criteria. Backtest, out-of-sample and paper-trading evidence are never pooled."
        actions={<RefreshButton onClick={card.reload} loading={card.loading} />}
      />
      <div className="mb-3 flex flex-col gap-2">
        <Notice tone="caution">
          <strong>Evidence, not authority.</strong> A scorecard promotes nothing and changes nothing that trades.
          “Candidate for review” asks a human to review; Atlas is never declared ready for live trading.
        </Notice>
      </div>
      {card.loading && !card.data && <Loading />}
      {card.error && <ErrorState message={card.error} onRetry={card.reload} />}
      {!card.loading && !card.error && card.data === null && (
        <EmptyState title="No readiness scorecard yet">
          Register a protocol and build a scorecard with <code>python -m app.evidence.cli</code>.
        </EmptyState>
      )}
      {card.data && result && (
        <>
          <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-5">
            <Stat
              label="Verdict"
              value={humanize(card.data.verdict)}
              tone={card.data.verdict === "READY_FOR_REVIEW" ? "positive" : "caution"}
            />
            <Stat label="Portfolio" value={humanize(result.portfolio.classification)} />
            <Stat label="Out-of-sample trades" value={result.portfolio.oos_trades} hint={result.portfolio.oos_source} />
            <Stat
              label="Paper sessions observed"
              value={result.paper_observation?.observed_sessions ?? 0}
              hint="sessions the orchestrator actually ran"
            />
            <Stat label="Paper closed trades" value={result.paper_observation?.closed_trades ?? 0} />
          </div>
          <Panel title="Scorecard" className="mb-3">
            <KeyValues
              items={[
                { label: "As of", value: <Timestamp iso={card.data.as_of} /> },
                {
                  label: "Protocol",
                  value: `${result.protocol.name} v${result.protocol.version} (${result.protocol.criteria_sha256.slice(0, 12)}…)`,
                },
                { label: "Protocol registered", value: <Timestamp iso={result.protocol.registered_at} /> },
                {
                  label: "Data audit",
                  value: result.portfolio.data_audit
                    ? `${result.portfolio.data_audit.status}: ${result.portfolio.data_audit.warnings.length} warnings, ${result.portfolio.data_audit.failures.length} failures`
                    : "none attached",
                },
                { label: "Result hash", value: card.data.result_sha256.slice(0, 16) },
              ]}
            />
          </Panel>
          <div className="mb-3 flex flex-col gap-3">
            {Object.entries(result.strategies).map(([id, s]) => (
              <StrategyPanel key={id} id={id} s={s} />
            ))}
          </div>
          <Panel title="Portfolio gates" className="mb-3">
            <CriteriaTable criteria={result.portfolio.criteria} label="portfolio criteria" />
          </Panel>
        </>
      )}
      <Panel title="Pre-registered protocols">
        {protocols.data && protocols.data.length > 0 ? (
          <DataTable label="protocols">
            <thead>
              <tr>
                <th className={TH}>Protocol</th>
                <th className={TH}>Registered</th>
                <th className={TH}>By</th>
                <th className={TH}>Criteria hash</th>
              </tr>
            </thead>
            <tbody>
              {protocols.data.map((p) => (
                <tr key={p.id}>
                  <td className={TD}>
                    {p.name} v{p.version}
                  </td>
                  <td className={TD}>
                    <Timestamp iso={p.registered_at} />
                  </td>
                  <td className={TD}>{p.registered_by}</td>
                  <td className={TD}>{p.criteria_sha256.slice(0, 16)}</td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        ) : (
          <span className="text-ink-faint text-xs">No protocol registered.</span>
        )}
      </Panel>
    </section>
  );
}
