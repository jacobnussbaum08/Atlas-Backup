import { useMemo, useState } from "react";
import { Link, useParams } from "react-router";

import { type ChartLine, SeriesChart } from "../components/backtest/SeriesChart";
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
  Stat,
  TD,
  TH,
  Timestamp,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { STATUS_TONE } from "../lib/backtest";
import { MISSING } from "../lib/format";
import { drawdownSeries, num, pct, SAMPLE_LABEL, stitchedSeries, VERDICT_LABEL, VERDICT_TONE } from "../lib/validation";
import { ApiError } from "../services/api";
import { validation } from "../services/validation";
import type { Fold, Json, RobustnessRow, SegmentRow, Stitched, TradeStatistics, Trial, WindowMetrics } from "../types/validation";

/** One validation run: walk-forward folds, in-sample vs out-of-sample, robustness and the
 * holdout. Every number is read from the stored run; the charts only draw them. */
export function ValidationRunPage() {
  const { runId = "" } = useParams();
  const detail = useApiResource((signal) => validation.detail(runId, { signal }), [runId], { refreshMs: 5_000 });
  const done = detail.data?.status === "COMPLETED";
  const folds = useApiResource((signal) => validation.folds(runId, { signal }), [runId, done], { enabled: done });
  const trials = useApiResource((signal) => validation.trials(runId, { signal }), [runId, done], { enabled: done });
  const robustness = useApiResource((signal) => validation.robustness(runId, { signal }), [runId, done], { enabled: done });
  const segments = useApiResource((signal) => validation.segments(runId, { signal }), [runId, done], { enabled: done });

  if (detail.status === "loading") return <Loading />;
  if (detail.status === "error" || !detail.data)
    return <ErrorState message={detail.error ?? "not found"} onRetry={detail.reload} />;
  const d = detail.data;
  const oos = d.summary?.out_of_sample;
  const champion = d.summary?.champion_out_of_sample;
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title={d.name}
        description={`${d.kind === "WALK_FORWARD" ? "Walk-forward validation" : "Fixed-configuration evaluation"} of ${d.strategies.join(", ")} · ${d.start_date} → ${d.end_date}`}
        actions={
          <>
            <Link to="/research" className="text-accent text-xs hover:underline">
              All runs
            </Link>
            <RefreshButton onClick={detail.reload} loading={detail.loading} />
          </>
        }
      />
      <div className="mb-3 flex flex-col gap-2">
        <Notice tone="caution">
          Historical backtest validation, not live paper trading. {d.summary?.statement ?? ""}
        </Notice>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Badge tone={STATUS_TONE[d.status]}>{d.status}</Badge>
          {d.verdict && <Badge tone={VERDICT_TONE[d.verdict]}>{VERDICT_LABEL[d.verdict]}</Badge>}
          {d.findings.filter((f) => f !== d.verdict).map((f) => (
            <Badge key={f} tone="neutral">
              {f.toLowerCase().replaceAll("_", " ")}
            </Badge>
          ))}
          {d.status === "RUNNING" && (
            <span className="num text-ink-muted">
              {d.progress.stage} {d.progress.done}/{d.progress.total}
            </span>
          )}
        </div>
        {d.error_message && <Notice tone="negative">{d.error_message}</Notice>}
        {d.summary?.reasons && d.summary.reasons.length > 0 && (
          <ul className="text-ink-muted list-disc pl-5 text-xs">
            {d.summary.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        )}
      </div>
      {oos && (
        <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-6">
          <Stat label="OOS net return" value={pct(oos.net_return)} hint={`gross ${pct(oos.gross_return)}`} />
          <Stat label="OOS Sharpe" value={num(oos.sharpe_annualized)} hint={SAMPLE_LABEL[oos.returns_status] || `${oos.sessions} sessions`} />
          <Stat label="OOS max drawdown" value={pct(oos.max_drawdown, { signed: false })} />
          <Stat label="OOS trades" value={oos.trade_statistics.trades} hint={SAMPLE_LABEL[oos.trade_statistics.status]} />
          <Stat label="vs benchmark" value={pct(oos.excess_return)} hint={`${d.benchmark ?? "none"} ${pct(oos.benchmark_return)}`} />
          <Stat label="Champion OOS" value={champion && champion.windows > 0 ? pct(champion.net_return) : MISSING} hint="stored parameters, same windows" />
        </div>
      )}
      <div className="grid gap-3 xl:grid-cols-2">
        {folds.data && folds.data.length > 0 && <EquityPanel folds={folds.data} />}
        {folds.data && <FoldsPanel folds={folds.data} />}
      </div>
      {robustness.data && <RobustnessPanels rows={robustness.data} />}
      {trials.data && trials.data.length > 1 && <TrialsPanel trials={trials.data} selected={d.summary?.selected_trials ?? []} />}
      {segments.data && <SegmentsPanel rows={segments.data} />}
      <div className="mt-3 grid gap-3 xl:grid-cols-2">
        <HoldoutPanel runId={d.id} status={d.holdout_status} completed={done} evaluations={d.holdout_evaluations} start={d.holdout_start} end={d.holdout_end} onDone={detail.reload} />
        <Panel title="Configuration and reproducibility">
          <KeyValues
            items={[
              { label: "Strategy versions", value: d.strategy_versions.map((s) => `${s.strategy_id} v${s.version}`).join(", ") },
              { label: "Search", value: `${d.search_method} · ${d.trials ?? MISSING} trials (+${d.prior_trials ?? 0} earlier on overlapping data)` },
              { label: "Universe", value: `${d.assets.length} assets` },
              { label: "Data known at", value: <Timestamp iso={d.data_known_at} /> },
              { label: "Risk", value: d.risk_source ?? MISSING },
              { label: "Execution model", value: <span className="num">{d.execution_model_sha256?.slice(0, 12)}</span> },
              { label: "Engine / code", value: `${d.engine_version} · ${d.code_version.slice(0, 12)}` },
              { label: "Seed", value: d.seed },
              { label: "Result hash", value: <span className="num">{d.result_sha256?.slice(0, 16) ?? MISSING}</span> },
            ]}
          />
        </Panel>
      </div>
    </section>
  );
}

function EquityPanel({ folds }: { folds: Fold[] }) {
  const lines = useMemo<ChartLine[]>(() => {
    const oos = stitchedSeries(folds.map((f) => f.oos_series));
    const champ = folds.every((f) => f.champion_series)
      ? stitchedSeries(folds.map((f) => f.champion_series ?? []))
      : [];
    return [
      { label: "Walk-forward selections (out of sample)", tone: "accent", points: oos },
      ...(champ.length ? [{ label: "Stored parameters (champion)", tone: "neutral" as const, points: champ }] : []),
    ];
  }, [folds]);
  const drawdown = useMemo<ChartLine[]>(
    () => [{ label: "Drawdown % (out of sample)", tone: "negative", area: true, points: drawdownSeries(lines[0]?.points as [string, number][]) }],
    [lines],
  );
  return (
    <Panel title="Out-of-sample equity" subtitle="test windows stitched in order; each starts flat">
      <SeriesChart lines={lines} label="Out-of-sample equity" />
      <SeriesChart lines={drawdown} height={140} label="Out-of-sample drawdown" />
    </Panel>
  );
}

function FoldsPanel({ folds }: { folds: Fold[] }) {
  return (
    <Panel title="Walk-forward folds" subtitle="in-sample (training window) vs out-of-sample (test window)">
      <DataTable label="Walk-forward folds">
        <thead>
          <tr>
            <th className={TH}>Fold</th>
            <th className={TH}>Train → test</th>
            <th className={`${TH} text-right`}>Trial</th>
            <th className={`${TH} text-right`}>IS return</th>
            <th className={`${TH} text-right`}>IS Sharpe</th>
            <th className={`${TH} text-right`}>OOS return</th>
            <th className={`${TH} text-right`}>OOS Sharpe</th>
            <th className={`${TH} text-right`}>OOS trades</th>
            <th className={`${TH} text-right`}>Champion OOS</th>
          </tr>
        </thead>
        <tbody>
          {folds.map((f) => (
            <tr key={f.fold_index}>
              <td className={`${TD} num`}>
                {f.fold_index}
                {!f.refit && <Badge tone="neutral">reused</Badge>}
              </td>
              <td className={`${TD} num`} title={f.selection.note ?? ""}>
                {f.train[0]}–{f.train[1]} → {f.test[0]}–{f.test[1]}
                <span className="text-ink-faint"> · embargo {f.embargo_sessions}</span>
              </td>
              <td className={`${TD} num text-right`}>{f.selected_trial}</td>
              <td className={`${TD} num text-right`}>{pct(f.in_sample?.net_return)}</td>
              <td className={`${TD} num text-right`}>{num(f.in_sample?.sharpe_annualized)}</td>
              <td className={`${TD} num text-right`}>{pct(f.out_of_sample.net_return)}</td>
              <td className={`${TD} num text-right`}>{num(f.out_of_sample.sharpe_annualized)}</td>
              <td className={`${TD} num text-right`}>{f.out_of_sample.trade_statistics.trades}</td>
              <td className={`${TD} num text-right`}>{pct(f.champion_out_of_sample?.net_return)}</td>
            </tr>
          ))}
        </tbody>
      </DataTable>
    </Panel>
  );
}

const METHOD_TITLE: Record<string, string> = {
  PBO: "Probability of backtest overfitting (CSCV)",
  CPCV: "Combinatorial purged cross-validation",
  PURGED_KFOLD: "Purged k-fold",
  DEFLATED_SHARPE: "Deflated Sharpe ratio",
  MULTIPLE_TESTING: "Multiple testing",
  PARAMETER_SENSITIVITY: "Parameter sensitivity",
  OOS_DEGRADATION: "Out-of-sample degradation",
  MONTE_CARLO_TRADES: "Monte Carlo trade sequences",
  BLOCK_BOOTSTRAP: "Stationary block bootstrap",
  COST_STRESS: "Transaction-cost stress",
  SLIPPAGE_STRESS: "Slippage stress",
  REGIME_SEGMENTATION: "Regime segmentation",
};

function headline(row: RobustnessRow): string {
  const r = row.result as Record<string, unknown>;
  const n = (k: string, places = 3) => num(r[k] as number | null, places);
  switch (row.method) {
    case "PBO":
      return `PBO ${n("pbo")} over ${String(r.combinations ?? MISSING)} splits; P(selected loses OOS) ${n("probability_selected_loses_oos")}`;
    case "DEFLATED_SHARPE":
      return `DSR ${n("deflated_sharpe")} · PSR(>0) ${n("probabilistic_sharpe_vs_zero")} · ${n("effective_trials", 1)} effective trials (+${String(r.prior_trials_on_overlapping_data ?? 0)} earlier)`;
    case "CPCV":
    case "PURGED_KFOLD":
      return `${(r.splits as unknown[] | undefined)?.length ?? 0} splits · mean OOS Sharpe ${n("oos_sharpe_mean")} · share positive ${n("share_positive_oos", 2)}`;
    case "MULTIPLE_TESTING":
      return `OOS p ${n("oos_p_value", 4)} → Bonferroni ${n("oos_p_value_bonferroni_all_trials", 4)} · significant trials raw ${String(r.trials_significant_raw_5pct ?? 0)}, Holm ${String(r.trials_significant_holm_5pct ?? 0)}`;
    case "PARAMETER_SENSITIVITY":
      return `${(r.neighbors as unknown[] | undefined)?.length ?? 0} neighbours · ${pct(r.share_positive as number | null, { signed: false, places: 0 })} profitable`;
    case "MONTE_CARLO_TRADES":
      return r.probability_net_loss === undefined ? `${String(r.trades ?? 0)} trades (minimum ${String(r.minimum ?? 10)})` : `P(net loss) ${n("probability_net_loss", 2)}`;
    case "BLOCK_BOOTSTRAP":
      return r.probability_mean_not_positive === undefined ? `${String(r.observations ?? 0)} returns` : `P(mean ≤ 0) ${n("probability_mean_not_positive", 2)}`;
    case "COST_STRESS":
    case "SLIPPAGE_STRESS": {
      const levels = r.levels as Record<string, { net_return: number }> | undefined;
      return levels
        ? Object.entries(levels)
            .map(([m, v]) => `${m}× ${pct(v.net_return)}`)
            .join(" · ")
        : MISSING;
    }
    default:
      return row.scope;
  }
}

function RobustnessPanels({ rows }: { rows: RobustnessRow[] }) {
  const [open, setOpen] = useState<string | null>(null);
  const sensitivity = rows.find((r) => r.method === "PARAMETER_SENSITIVITY");
  const regimes = rows.find((r) => r.method === "REGIME_SEGMENTATION" && (r.result as Json).returns_by_trend);
  return (
    <div className="mt-3 grid gap-3 xl:grid-cols-2">
      <Panel title="Robustness and overfitting controls" subtitle="each with its assumptions and limitations">
        <DataTable label="Robustness results">
          <tbody>
            {rows.map((r, i) => (
              <tr key={`${r.method}-${i}`}>
                <td className={`${TD} align-top`}>
                  <button type="button" className="text-accent text-left hover:underline" onClick={() => setOpen(open === `${i}` ? null : `${i}`)}>
                    {METHOD_TITLE[r.method] ?? r.method}
                  </button>
                  {r.status !== "OK" && <Badge tone="caution">{SAMPLE_LABEL[r.status] || r.status}</Badge>}
                  {open === `${i}` && (
                    <div className="text-ink-muted mt-1 max-w-xl whitespace-normal text-2xs">
                      <p>Scope: {r.scope}</p>
                      <p>Assumptions: {r.assumptions.join("; ")}</p>
                      <p>Limitations: {r.limitations.join("; ")}</p>
                    </div>
                  )}
                </td>
                <td className={`${TD} num max-w-xl align-top whitespace-normal`}>{headline(r)}</td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      </Panel>
      <div className="flex flex-col gap-3">
        {sensitivity && <SensitivityPanel row={sensitivity} />}
        {regimes && <RegimePanel row={regimes} />}
      </div>
    </div>
  );
}

function SensitivityPanel({ row }: { row: RobustnessRow }) {
  const r = row.result as { surface?: { trial: number; parameters: Json; net_return: number | null; sharpe_annualized: number | null }[]; selected_trial?: number };
  if (!r.surface?.length) return null;
  const names = Object.keys(r.surface[0]?.parameters ?? {});
  return (
    <Panel title="Parameter sensitivity" subtitle="development period; the selected trial is highlighted">
      <DataTable label="Parameter surface">
        <thead>
          <tr>
            <th className={TH}>Trial</th>
            {names.map((n) => (
              <th key={n} className={TH}>
                {n}
              </th>
            ))}
            <th className={`${TH} text-right`}>Net return</th>
            <th className={`${TH} text-right`}>Sharpe</th>
          </tr>
        </thead>
        <tbody>
          {r.surface.map((s) => (
            <tr key={s.trial} className={s.trial === r.selected_trial ? "bg-accent/10" : ""}>
              <td className={`${TD} num`}>{s.trial}</td>
              {names.map((n) => (
                <td key={n} className={`${TD} num`}>
                  {String(s.parameters[n])}
                </td>
              ))}
              <td className={`${TD} num text-right`}>{pct(s.net_return)}</td>
              <td className={`${TD} num text-right`}>{num(s.sharpe_annualized)}</td>
            </tr>
          ))}
        </tbody>
      </DataTable>
    </Panel>
  );
}

function RegimePanel({ row }: { row: RobustnessRow }) {
  const r = row.result as { returns_by_trend?: Record<string, { sessions: number; compound_return: number; status: string }>; returns_by_volatility?: Record<string, { sessions: number; compound_return: number; status: string }> };
  const rows = [
    ...Object.entries(r.returns_by_trend ?? {}).map(([k, v]) => ({ k: `trend ${k}`, ...v })),
    ...Object.entries(r.returns_by_volatility ?? {}).map(([k, v]) => ({ k: `volatility ${k}`, ...v })),
  ];
  return (
    <Panel title="Regime breakdown" subtitle="out-of-sample daily returns by the regime known the session before">
      <DataTable label="Regime breakdown">
        <tbody>
          {rows.map((x) => (
            <tr key={x.k}>
              <td className={TD}>{x.k}</td>
              <td className={`${TD} num text-right`}>{x.sessions} sessions</td>
              <td className={`${TD} num text-right`}>{pct(x.compound_return)}</td>
              <td className={TD}>{x.status !== "OK" && <Badge tone="caution">small sample</Badge>}</td>
            </tr>
          ))}
        </tbody>
      </DataTable>
    </Panel>
  );
}

function TrialsPanel({ trials, selected }: { trials: Trial[]; selected: number[] }) {
  return (
    <Panel title="Trials" subtitle="every parameter set replayed on the development period; all count against the evidence" >
      <DataTable label="Trials">
        <thead>
          <tr>
            <th className={TH}>#</th>
            <th className={TH}>Source</th>
            <th className={`${TH} text-right`}>Dev return</th>
            <th className={`${TH} text-right`}>Dev Sharpe</th>
            <th className={`${TH} text-right`}>Trades</th>
            <th className={TH}>Selected in folds</th>
          </tr>
        </thead>
        <tbody>
          {trials.map((t) => (
            <tr key={t.trial_number}>
              <td className={`${TD} num`}>{t.trial_number}</td>
              <td className={TD}>{t.source}</td>
              <td className={`${TD} num text-right`}>{pct(t.development?.net_return)}</td>
              <td className={`${TD} num text-right`}>{num(t.development?.sharpe_annualized)}</td>
              <td className={`${TD} num text-right`}>{t.development?.trade_statistics.trades ?? MISSING}</td>
              <td className={`${TD} num`}>
                {selected
                  .map((s, i) => (s === t.trial_number ? i : -1))
                  .filter((i) => i >= 0)
                  .join(", ") || MISSING}
              </td>
            </tr>
          ))}
        </tbody>
      </DataTable>
    </Panel>
  );
}

function statsCells(s: TradeStatistics) {
  return (
    <>
      <td className={`${TD} num text-right`}>{s.trades}</td>
      <td className={`${TD} num text-right`}>{pct(s.win_rate, { signed: false, places: 0 })}</td>
      <td className={`${TD} num text-right`}>{num(s.expectancy, 2, { signed: true })}</td>
      <td className={`${TD} num text-right`}>{num(s.profit_factor)}</td>
      <td className={`${TD} num text-right`}>{num(s.net_pnl, 2, { signed: true })}</td>
      <td className={`${TD} num text-right`}>{num(s.costs)}</td>
    </>
  );
}

function SegmentsPanel({ rows }: { rows: SegmentRow[] }) {
  return (
    <Panel title="Out-of-sample breakdowns" subtitle="closed trades by asset, class, timeframe, direction, regime and period">
      <DataTable label="Out-of-sample breakdowns">
        <thead>
          <tr>
            <th className={TH}>Segment</th>
            <th className={`${TH} text-right`}>Trades</th>
            <th className={`${TH} text-right`}>Win rate</th>
            <th className={`${TH} text-right`}>Expectancy</th>
            <th className={`${TH} text-right`}>Profit factor</th>
            <th className={`${TH} text-right`}>Net P/L</th>
            <th className={`${TH} text-right`}>Costs</th>
            <th className={TH}>Sample</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.segment_kind}:${r.segment_value}`}>
              <td className={TD}>
                <span className="text-ink-faint">{r.segment_kind.toLowerCase().replaceAll("_", " ")}</span> {r.segment_value}
              </td>
              {statsCells(r.metrics)}
              <td className={TD}>{r.status !== "OK" && <Badge tone="caution">small sample</Badge>}</td>
            </tr>
          ))}
        </tbody>
      </DataTable>
    </Panel>
  );
}

function HoldoutPanel({
  runId,
  status,
  completed,
  evaluations,
  start,
  end,
  onDone,
}: {
  runId: string;
  status: string;
  completed: boolean;
  evaluations: { id: string; access_number: number; result: WindowMetrics & { note: string }; created_at: string }[];
  start: string | null;
  end: string | null;
  onDone: () => void;
}) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const read = async () => {
    setBusy(true);
    setError(null);
    try {
      await validation.holdout(runId, token || undefined);
      onDone();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel title="Untouched holdout" subtitle={start ? `${start} → ${end}` : "none reserved"}>
      <p className="text-ink-muted mb-2 text-xs">
        Reserved before the run started and never used for selection. Reading it replays the frozen final parameters once;
        every read is numbered, and after the first it is no longer an untouched test.
      </p>
      <Badge tone={status === "UNTOUCHED" ? "positive" : status === "REUSED" ? "caution" : "info"}>{status}</Badge>
      {evaluations.length === 0 ? (
        <EmptyState title="Not evaluated" />
      ) : (
        <DataTable label="Holdout evaluations">
          <thead>
            <tr>
              <th className={TH}>Access</th>
              <th className={`${TH} text-right`}>Net return</th>
              <th className={`${TH} text-right`}>Trades</th>
              <th className={`${TH} text-right`}>Drawdown</th>
              <th className={TH}>Note</th>
            </tr>
          </thead>
          <tbody>
            {evaluations.map((h) => (
              <tr key={h.id}>
                <td className={`${TD} num`}>#{h.access_number}</td>
                <td className={`${TD} num text-right`}>{pct(h.result.net_return)}</td>
                <td className={`${TD} num text-right`}>{h.result.trade_statistics.trades}</td>
                <td className={`${TD} num text-right`}>{pct(h.result.max_drawdown, { signed: false })}</td>
                <td className={`${TD} max-w-80 whitespace-normal text-2xs`}>{h.result.note}</td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
      {completed && start && (
        <div className="mt-2 flex items-end gap-2">
          <label className="text-ink-muted text-2xs font-semibold uppercase">
            Operator token
            <input
              type="password"
              autoComplete="off"
              className="border-border-strong bg-panel-raised text-ink block h-7 rounded-sm border px-2 text-xs"
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
          </label>
          <Button onClick={() => void read()} disabled={busy}>
            Evaluate holdout
          </Button>
        </div>
      )}
      {error && <p role="alert" className="text-negative mt-1 text-xs">{error}</p>}
    </Panel>
  );
}

export type { Stitched };
