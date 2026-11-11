import { Link } from "react-router";

import { useApiResource } from "../hooks/useApiResource";
import { MISSING } from "../lib/format";
import { num, pct, VERDICT_LABEL, VERDICT_TONE } from "../lib/validation";
import { validation } from "../services/validation";
import type { ValidationRun } from "../types/validation";
import { OriginBadge } from "./provenance";
import { Badge, DataTable, EmptyState, ErrorState, Loading, Panel, TD, TH, Timestamp } from "./ui";

/** Newest completed single-strategy walk-forward run per strategy (runs arrive newest first). */
export function latestByStrategy(runs: readonly ValidationRun[]): Map<string, ValidationRun> {
  const latest = new Map<string, ValidationRun>();
  for (const r of runs) {
    if (r.kind !== "WALK_FORWARD" || r.status !== "COMPLETED" || r.strategies.length !== 1) continue;
    const id = r.strategies[0] ?? "";
    const seen = latest.get(id);
    if (!seen || (r.finished_at ?? r.created_at) > (seen.finished_at ?? seen.created_at)) latest.set(id, r);
  }
  return latest;
}

/**
 * Strategy performance as the research record shows it: out-of-sample walk-forward results of
 * replays of stored history (Phase 8B). They are not paper-trading results.
 */
export function StrategyEvidencePanel({ strategyIds }: { strategyIds: readonly string[] }) {
  const runs = useApiResource((signal) => validation.runs({ signal }), [], { refreshMs: 300_000 });
  const latest = runs.data ? latestByStrategy(runs.data) : null;
  return (
    <Panel
      title="Research evidence"
      subtitle={
        <span className="flex items-center gap-1.5">
          <OriginBadge origin="REPLAY" />
          newest completed walk-forward run per strategy, out-of-sample
        </span>
      }
      actions={<Link to="/research" className="text-accent text-xs hover:underline">Research & Validation</Link>}
      bodyClassName=""
    >
      {runs.error && !runs.data ? (
        <div className="p-3">
          <ErrorState message={runs.error} onRetry={runs.reload} />
        </div>
      ) : !latest ? (
        <Loading />
      ) : latest.size === 0 ? (
        <div className="px-3">
          <EmptyState title="No completed walk-forward validation yet">
            Performance figures appear here once a strategy has an out-of-sample validation run. No strategy has
            paper-trading results.
          </EmptyState>
        </div>
      ) : (
        <>
          <DataTable label="Strategy research evidence">
            <thead>
              <tr>
                <th scope="col" className={TH}>Strategy</th>
                <th scope="col" className={TH}>Verdict</th>
                <th scope="col" className={`${TH} text-right`}>OOS net return</th>
                <th scope="col" className={`${TH} text-right`}>OOS Sharpe</th>
                <th scope="col" className={`${TH} text-right`}>OOS max drawdown</th>
                <th scope="col" className={`${TH} text-right`}>Trades</th>
                <th scope="col" className={`${TH} text-right`}>Sessions</th>
                <th scope="col" className={TH}>Period</th>
                <th scope="col" className={TH}>Finished</th>
              </tr>
            </thead>
            <tbody>
              {strategyIds.map((id) => {
                const r = latest.get(id);
                if (!r) {
                  return (
                    <tr key={id}>
                      <td className={`${TD} num`}>{id}</td>
                      <td className={`${TD} text-ink-faint`} colSpan={8}>
                        no completed walk-forward run
                      </td>
                    </tr>
                  );
                }
                return (
                  <tr key={id}>
                    <td className={`${TD} num`}>
                      <Link to={`/research/runs/${r.id}`} className="hover:text-accent">
                        {id}
                      </Link>
                    </td>
                    <td className={TD}>
                      {r.verdict ? <Badge tone={VERDICT_TONE[r.verdict]}>{VERDICT_LABEL[r.verdict]}</Badge> : MISSING}
                    </td>
                    <td className={`${TD} num text-right`}>{pct(r.oos_net_return)}</td>
                    <td className={`${TD} num text-right`}>{num(r.oos_sharpe)}</td>
                    <td className={`${TD} num text-right`}>{pct(r.oos_max_drawdown, { signed: false })}</td>
                    <td className={`${TD} num text-right`}>{r.oos_trades ?? MISSING}</td>
                    <td className={`${TD} num text-right`}>{r.oos_sessions ?? MISSING}</td>
                    <td className={`${TD} num`}>
                      {r.start_date} – {r.end_date}
                    </td>
                    <td className={TD}>
                      <Timestamp iso={r.finished_at} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </DataTable>
          <p className="text-ink-faint px-3 py-2 text-2xs">
            Replays of stored history with costs, not paper or live trading. Optimized historical performance is not
            predictive ability; see each run for sample sizes, overfitting checks and the holdout.
          </p>
        </>
      )}
    </Panel>
  );
}
