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
  Stat,
  TD,
  TH,
  Timestamp,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { MISSING, formatCount, humanize } from "../lib/format";
import { ml } from "../services/ml";
import type {
  CalibrationResult,
  DataOrigin,
  Evaluation,
  LifecycleStatus,
  Metrics,
  ModelSummary,
  OutputKind,
  RunDetail,
  RunSummary,
  ValidationStatus,
} from "../types/ml";

type Tone = "positive" | "negative" | "caution" | "info" | "accent" | "neutral";

const STATUS_TONE: Record<LifecycleStatus, Tone> = {
  CANDIDATE: "neutral",
  VALIDATED: "info",
  SHADOW: "accent",
  APPROVED: "positive",
  REJECTED: "negative",
  RETIRED: "neutral",
};
const VALIDATION_TONE: Record<ValidationStatus, Tone> = {
  PASSED: "positive",
  FAILED: "negative",
  INSUFFICIENT_EVIDENCE: "caution",
};
const OUTPUT_LABEL: Record<OutputKind, string> = {
  CALIBRATED_PROBABILITY: "Calibrated probability",
  UNCALIBRATED_SCORE: "Uncalibrated score",
  REGRESSION_ESTIMATE: "Regression estimate",
};

export function num(value: unknown, digits = 4): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : MISSING;
}

export function pct(value: unknown, digits = 2): string {
  return typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(digits)}%` : MISSING;
}

function OriginBadge({ origin }: { origin: DataOrigin }) {
  return origin === "TEST_FIXTURE" ? (
    <Badge tone="caution" title="Built from synthetic test data, not market data">
      Test fixture
    </Badge>
  ) : (
    <Badge tone="info" title="Built from stored historical market data">
      Real history
    </Badge>
  );
}

function ValidationBadge({ status }: { status: ValidationStatus | null }) {
  if (!status) return <span className="text-ink-faint">{MISSING}</span>;
  return <Badge tone={VALIDATION_TONE[status]}>{humanize(status)}</Badge>;
}

export function MLPage() {
  const [refresh, setRefresh] = useState(0);
  const overview = useApiResource((signal) => ml.overview({ signal }), [refresh], { refreshMs: 30_000 });
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Machine Learning"
        description="Point-in-time datasets, chronological walk-forward training, calibration evidence and the immutable model registry. Models estimate; they never authorize, size or submit a trade."
        actions={<RefreshButton onClick={() => setRefresh((n) => n + 1)} loading={overview.loading} />}
      />
      {overview.status === "loading" && <Loading label="Loading the ML engine…" />}
      {overview.status === "error" && <ErrorState message={overview.error ?? ""} onRetry={overview.reload} />}
      {overview.data && (
        <div className="flex flex-col gap-3">
          {overview.data.notes.map((note) => (
            <Notice key={note} tone="info">
              {note}
            </Notice>
          ))}
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat
              label="Dataset versions"
              value={formatCount(Object.values(overview.data.dataset_versions).reduce((a, b) => a + b, 0))}
              hint={Object.entries(overview.data.dataset_versions)
                .map(([k, v]) => `${humanize(k)} ${v}`)
                .join(" · ") || "none built"}
            />
            <Stat
              label="Training runs"
              value={formatCount(Object.values(overview.data.training_runs).reduce((a, b) => a + b, 0))}
              hint={Object.entries(overview.data.training_runs)
                .map(([k, v]) => `${humanize(k)} ${v}`)
                .join(" · ") || "none run"}
            />
            <Stat
              label="Approved models"
              value={formatCount(overview.data.models["APPROVED"] ?? 0)}
              hint={`${overview.data.models["CANDIDATE"] ?? 0} candidate · ${overview.data.models["SHADOW"] ?? 0} shadow`}
            />
            <Stat label="Predictions recorded" value={formatCount(overview.data.predictions)} />
          </div>
          <ModelsPanel refreshKey={refresh} />
          <RunsPanel refreshKey={refresh} />
          <DatasetsPanel refreshKey={refresh} />
          <PredictionsPanel refreshKey={refresh} />
        </div>
      )}
    </section>
  );
}

function ModelsPanel({ refreshKey }: { refreshKey: number }) {
  const models = useApiResource((signal) => ml.models({ signal }), [refreshKey], {
    isEmpty: (rows) => rows.length === 0,
  });
  return (
    <Panel title="Model registry" subtitle="Status changes are explicit operator decisions; nothing is promoted automatically">
      {models.status === "loading" && <Loading />}
      {models.status === "error" && <ErrorState message={models.error ?? ""} onRetry={models.reload} />}
      {models.status === "empty" && <EmptyState title="No model has been trained yet." />}
      {models.data && models.data.length > 0 && (
        <DataTable label="Model registry">
          <thead>
            <tr>
              <th className={TH}>Model</th>
              <th className={TH}>Type</th>
              <th className={TH}>Status</th>
              <th className={TH}>Evidence</th>
              <th className={TH}>Output</th>
              <th className={TH}>Data</th>
              <th className={TH}>Test metric vs baseline</th>
              <th className={TH}>Created</th>
            </tr>
          </thead>
          <tbody>
            {models.data.map((m) => (
              <ModelRow key={m.id} model={m} />
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}

function ModelRow({ model: m }: { model: ModelSummary }) {
  const test = (m.evaluation_summary["test"] ?? {}) as Metrics;
  const base = (m.evaluation_summary["test_baseline"] ?? {}) as Metrics;
  const key = m.task === "CLASSIFICATION" ? "log_loss" : "rmse";
  return (
    <tr>
      <td className={TD}>
        <span className="font-medium">{m.model_name}</span> <span className="text-ink-faint">v{m.version}</span>
      </td>
      <td className={TD}>{humanize(m.model_type)}</td>
      <td className={TD}>
        <Badge tone={STATUS_TONE[m.status]} dot>
          {humanize(m.status)}
        </Badge>
      </td>
      <td className={TD}>
        <ValidationBadge status={m.validation_status} />
      </td>
      <td className={TD}>{OUTPUT_LABEL[m.output_kind]}</td>
      <td className={TD}>
        <OriginBadge origin={m.origin} />
      </td>
      <td className={`${TD} num`}>
        {key} {num(test[key])} <span className="text-ink-faint">vs {num(base[key])}</span>
      </td>
      <td className={TD}>
        <Timestamp iso={m.created_at} seconds={false} />
      </td>
    </tr>
  );
}

function RunsPanel({ refreshKey }: { refreshKey: number }) {
  const runs = useApiResource((signal) => ml.runs({ signal }), [refreshKey], {
    isEmpty: (rows) => rows.length === 0,
  });
  const [open, setOpen] = useState<string | null>(null);
  return (
    <Panel
      title="Training runs"
      subtitle="Test = the untouched final period, evaluated once per run; baselines are constant predictions from the training period"
    >
      {runs.status === "loading" && <Loading />}
      {runs.status === "error" && <ErrorState message={runs.error ?? ""} onRetry={runs.reload} />}
      {runs.status === "empty" && <EmptyState title="No training run yet." />}
      {runs.data && runs.data.length > 0 && (
        <DataTable label="Training runs">
          <thead>
            <tr>
              <th className={TH} aria-label="Details" />
              <th className={TH}>Dataset</th>
              <th className={TH}>Model</th>
              <th className={TH}>Status</th>
              <th className={TH}>Evidence</th>
              <th className={TH}>Test metric (baseline)</th>
              <th className={TH}>Walk-forward wins</th>
              <th className={TH}>Selected net / take-all</th>
              <th className={TH}>Test n</th>
              <th className={TH}>Started</th>
            </tr>
          </thead>
          <tbody>
            {runs.data.map((r) => (
              <Fragment key={r.id}>
                <RunRow run={r} open={open === r.id} onToggle={() => setOpen(open === r.id ? null : r.id)} />
                {open === r.id && (
                  <tr>
                    <td colSpan={10} className="border-border/60 border-b p-3">
                      <RunDetailView runId={r.id} />
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

function RunRow({ run: r, open, onToggle }: { run: RunSummary; open: boolean; onToggle: () => void }) {
  return (
    <tr>
      <td className={TD}>
        <button
          type="button"
          aria-expanded={open}
          aria-label={`Details of run ${r.id}`}
          onClick={onToggle}
          className="hover:text-accent"
        >
          <ChevronRight className={`size-3.5 transition-transform ${open ? "rotate-90" : ""}`} />
        </button>
      </td>
      <td className={TD}>
        {r.dataset} <span className="text-ink-faint">v{r.dataset_version}</span> <OriginBadge origin={r.origin} />
      </td>
      <td className={TD}>{humanize(r.model_type)}</td>
      <td className={TD}>
        <Badge tone={r.status === "SUCCEEDED" ? "positive" : r.status === "FAILED" ? "negative" : "info"}>
          {humanize(r.status)}
        </Badge>
      </td>
      <td className={TD}>
        <ValidationBadge status={r.validation_status} />
      </td>
      <td className={`${TD} num`}>
        {r.primary_metric} {num(r.test_metric)} <span className="text-ink-faint">({num(r.test_baseline_metric)})</span>
      </td>
      <td className={`${TD} num`}>{pct(r.walk_forward_win_fraction, 0)}</td>
      <td className={`${TD} num`}>
        {pct(r.test_mean_net_return)} / {pct(r.test_take_all_net_return)}
      </td>
      <td className={`${TD} num`}>
        {formatCount(r.test_observations)}
        {r.test_period_reuse > 0 && (
          <span title="Earlier runs evaluated the same test period" className="text-caution">
            {" "}
            ×{r.test_period_reuse + 1}
          </span>
        )}
      </td>
      <td className={TD}>
        <Timestamp iso={r.started_at} seconds={false} />
      </td>
    </tr>
  );
}

function RunDetailView({ runId }: { runId: string }) {
  const run = useApiResource((signal) => ml.run(runId, { signal }), [runId]);
  if (run.status === "loading") return <Loading />;
  if (run.status === "error" || !run.data) return <ErrorState message={run.error ?? ""} onRetry={run.reload} />;
  const r: RunDetail = run.data;
  const periods = (r.periods ?? {}) as Record<string, Metrics>;
  const span = (p: Metrics | undefined) =>
    p ? `${String(p["start"] ?? MISSING).slice(0, 10)} → ${String(p["end"] ?? MISSING).slice(0, 10)} (${formatCount(p["observations"] as number)})` : MISSING;
  const folds = r.evaluations.filter((e) => e.split === "WALK_FORWARD");
  const segments = r.evaluations.filter((e) => e.split === "TEST" && e.segment_kind !== "ALL");
  return (
    <div className="flex flex-col gap-3">
      {r.error && <Notice tone="negative">{r.error}</Notice>}
      {r.validation_reasons.map((reason) => (
        <Notice key={reason} tone={r.validation_status === "PASSED" ? "positive" : "caution"}>
          {reason}
        </Notice>
      ))}
      <div className="grid gap-3 lg:grid-cols-2">
        <KeyValues
          items={[
            { label: "Target", value: `${humanize(String(r.target_definition["kind"]))}, horizon ${String(r.target_definition["horizon_periods"] ?? "strategy")}, cost ${String(r.target_definition["round_trip_cost_bps"])} bps (assumption)` },
            { label: "Train", value: span(periods["train"]) },
            { label: "Validation", value: span(periods["validation"]) },
            { label: "Test (untouched)", value: span(periods["test"]) },
            { label: "Embargo", value: `${String(periods["embargo_periods"] ?? MISSING)} periods` },
            { label: "Seed", value: r.random_seed },
            { label: "Code", value: `${String(r.code_version["commit"] ?? "unknown").slice(0, 12)}${r.code_version["dirty"] ? " (uncommitted changes)" : ""}` },
          ]}
        />
        <KeyValues
          items={[
            { label: "Model", value: String((r.parameters ?? {})["family"] ?? r.model_type) },
            { label: "Hyperparameters", value: <code className="text-2xs">{JSON.stringify((r.parameters ?? {})["hyperparameters"] ?? {})}</code> },
            { label: "Selection", value: String((r.selection ?? {})["criterion"] ?? MISSING) },
            { label: "Candidates tried", value: String((r.selection ?? {})["candidates"] ?? MISSING) },
            { label: "Features kept", value: ((r.preprocessing ?? {})["numeric"] as string[] | undefined)?.length ?? MISSING },
            { label: "Features dropped", value: Object.keys(((r.preprocessing ?? {})["dropped"] as Metrics | undefined) ?? {}).length },
          ]}
        />
      </div>
      {r.notes.length > 0 && (
        <ul className="text-ink-muted list-disc pl-5 text-2xs">
          {r.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
      <FoldsTable folds={folds} metric={r.primary_metric} />
      {r.calibration.length > 0 && <CalibrationTable rows={r.calibration} />}
      <SegmentsTable rows={segments} metric={r.primary_metric} />
    </div>
  );
}

function FoldsTable({ folds, metric }: { folds: Evaluation[]; metric: string }) {
  return (
    <DataTable label="Walk-forward folds">
      <thead>
        <tr>
          <th className={TH}>Fold</th>
          <th className={TH}>Test period</th>
          <th className={TH}>n</th>
          <th className={TH}>{metric}</th>
          <th className={TH}>Baseline</th>
          <th className={TH}>Selected mean net</th>
        </tr>
      </thead>
      <tbody>
        {folds.map((f) => (
          <tr key={`${f.fold}`}>
            <td className={TD}>{f.fold}</td>
            <td className={TD}>
              {f.period_start?.slice(0, 10)} → {f.period_end?.slice(0, 10)}
            </td>
            <td className={`${TD} num`}>{formatCount(f.observations)}</td>
            <td className={`${TD} num`}>{num(f.metrics[metric])}</td>
            <td className={`${TD} num`}>{num(f.baseline_metrics[metric])}</td>
            <td className={`${TD} num`}>{pct(((f.metrics["financial"] ?? {}) as Metrics)["mean_net_return"])}</td>
          </tr>
        ))}
      </tbody>
    </DataTable>
  );
}

function CalibrationTable({ rows }: { rows: CalibrationResult[] }) {
  const test = rows.find((r) => r.evaluated_on === "TEST");
  return (
    <div className="flex flex-col gap-2">
      <DataTable label="Calibration evidence">
        <thead>
          <tr>
            <th className={TH}>Method</th>
            <th className={TH}>Measured on</th>
            <th className={TH}>n</th>
            <th className={TH}>Brier raw / calibrated / base rate</th>
            <th className={TH}>ECE raw / calibrated</th>
            <th className={TH}>Decision</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c, i) => (
            <tr key={i}>
              <td className={TD}>
                {humanize(c.method)} {c.selected && <Badge tone={c.accepted ? "positive" : "caution"}>{c.accepted ? "accepted" : "selected"}</Badge>}
              </td>
              <td className={TD}>{humanize(c.evaluated_on)}</td>
              <td className={`${TD} num`}>{formatCount(c.observations)}</td>
              <td className={`${TD} num`}>
                {num(c.brier_raw)} / {num(c.brier_calibrated)} / {num(c.brier_base_rate)}
              </td>
              <td className={`${TD} num`}>
                {num(c.ece_raw, 3)} / {num(c.ece_calibrated, 3)}
              </td>
              <td className={`${TD} whitespace-normal text-2xs`}>{c.decision}</td>
            </tr>
          ))}
        </tbody>
      </DataTable>
      {test && (
        <DataTable label="Reliability on the test period">
          <thead>
            <tr>
              <th className={TH}>Score bin</th>
              <th className={TH}>n</th>
              <th className={TH}>Mean score (calibrated)</th>
              <th className={TH}>Observed rate</th>
            </tr>
          </thead>
          <tbody>
            {test.reliability_calibrated
              .filter((b) => b.count > 0)
              .map((b) => (
                <tr key={b.lower}>
                  <td className={`${TD} num`}>
                    {b.lower.toFixed(1)}–{b.upper.toFixed(1)}
                  </td>
                  <td className={`${TD} num`}>{formatCount(b.count)}</td>
                  <td className={`${TD} num`}>{num(b.mean_score, 3)}</td>
                  <td className={`${TD} num`}>{num(b.observed_rate, 3)}</td>
                </tr>
              ))}
          </tbody>
        </DataTable>
      )}
    </div>
  );
}

function SegmentsTable({ rows, metric }: { rows: Evaluation[]; metric: string }) {
  if (rows.length === 0) return null;
  const kinds = new Set(rows.map((r) => r.segment_kind)).size;
  return (
    <details>
      <summary className="text-ink-muted cursor-pointer text-2xs font-semibold tracking-wider uppercase">
        Test period by segment ({rows.length} segments, {kinds} kinds)
      </summary>
      <DataTable label="Test period by segment">
        <thead>
          <tr>
            <th className={TH}>Segment</th>
            <th className={TH}>Value</th>
            <th className={TH}>n</th>
            <th className={TH}>{metric}</th>
            <th className={TH}>Baseline</th>
            <th className={TH}>Selected mean net / take-all</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => {
            const fin = (s.metrics["financial"] ?? {}) as Metrics;
            return (
              <tr key={`${s.segment_kind}:${s.segment_value}`}>
                <td className={TD}>{humanize(s.segment_kind)}</td>
                <td className={TD}>{s.segment_value}</td>
                <td className={`${TD} num`}>{formatCount(s.observations)}</td>
                {s.metrics["insufficient"] ? (
                  <td className={`${TD} text-ink-faint`} colSpan={3}>
                    too few observations to evaluate
                  </td>
                ) : (
                  <>
                    <td className={`${TD} num`}>{num(s.metrics[metric])}</td>
                    <td className={`${TD} num`}>{num(s.baseline_metrics[metric])}</td>
                    <td className={`${TD} num`}>
                      {pct(fin["mean_net_return"])} / {pct(fin["take_all_mean_net_return"])}
                    </td>
                  </>
                )}
              </tr>
            );
          })}
        </tbody>
      </DataTable>
    </details>
  );
}

function DatasetsPanel({ refreshKey }: { refreshKey: number }) {
  const datasets = useApiResource((signal) => ml.datasets({ signal }), [refreshKey], {
    isEmpty: (rows) => rows.length === 0,
  });
  return (
    <Panel title="Datasets" subtitle="Immutable, point-in-time versions; labels are used only once matured">
      {datasets.status === "loading" && <Loading />}
      {datasets.status === "error" && <ErrorState message={datasets.error ?? ""} onRetry={datasets.reload} />}
      {datasets.status === "empty" && <EmptyState title="No dataset has been built yet." />}
      {datasets.data && datasets.data.length > 0 && (
        <DataTable label="Dataset versions">
          <thead>
            <tr>
              <th className={TH}>Dataset</th>
              <th className={TH}>Data</th>
              <th className={TH}>Source</th>
              <th className={TH}>Target</th>
              <th className={TH}>Observations (matured)</th>
              <th className={TH}>Assets</th>
              <th className={TH}>Decisions</th>
              <th className={TH}>Data known at</th>
              <th className={TH}>Warnings</th>
            </tr>
          </thead>
          <tbody>
            {datasets.data.map((d) => (
              <tr key={d.id}>
                <td className={TD}>
                  {d.dataset} <span className="text-ink-faint">v{d.version}</span>
                </td>
                <td className={TD}>
                  <OriginBadge origin={d.origin} />
                </td>
                <td className={TD}>{humanize(d.source)}</td>
                <td className={TD}>{humanize(d.target_kind)}</td>
                <td className={`${TD} num`}>
                  {formatCount(d.observation_count)} ({formatCount(d.matured_count)})
                </td>
                <td className={`${TD} num`}>{formatCount(d.asset_count)}</td>
                <td className={TD}>
                  {d.decision_start?.slice(0, 10)} → {d.decision_end?.slice(0, 10)}
                </td>
                <td className={TD}>
                  <Timestamp iso={d.data_known_at} seconds={false} />
                </td>
                <td className={`${TD} whitespace-normal`}>
                  <details>
                    <summary className="cursor-pointer">{d.warnings.length}</summary>
                    <ul className="text-ink-muted list-disc pl-4 text-2xs">
                      {d.warnings.map((w) => (
                        <li key={w}>{w}</li>
                      ))}
                    </ul>
                  </details>
                </td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}

function PredictionsPanel({ refreshKey }: { refreshKey: number }) {
  const predictions = useApiResource((signal) => ml.predictions({ signal }), [refreshKey], {
    isEmpty: (rows) => rows.length === 0,
  });
  return (
    <Panel title="Prediction history" subtitle="Every request is recorded, including rejected ones">
      {predictions.status === "loading" && <Loading />}
      {predictions.status === "error" && <ErrorState message={predictions.error ?? ""} onRetry={predictions.reload} />}
      {predictions.status === "empty" && <EmptyState title="No prediction has been recorded." />}
      {predictions.data && predictions.data.length > 0 && (
        <DataTable label="Predictions">
          <thead>
            <tr>
              <th className={TH}>Predicted</th>
              <th className={TH}>Model</th>
              <th className={TH}>Mode</th>
              <th className={TH}>Asset</th>
              <th className={TH}>Direction</th>
              <th className={TH}>Decision time</th>
              <th className={TH}>Validity</th>
              <th className={TH}>Output</th>
            </tr>
          </thead>
          <tbody>
            {predictions.data.map((p) => (
              <tr key={p.id}>
                <td className={TD}>
                  <Timestamp iso={p.predicted_at} seconds={false} />
                </td>
                <td className={TD}>
                  {p.model_name} <span className="text-ink-faint">v{p.model_version}</span>
                </td>
                <td className={TD}>{humanize(p.mode)}</td>
                <td className={TD}>{p.symbol ?? MISSING}</td>
                <td className={TD}>{humanize(p.direction)}</td>
                <td className={TD}>
                  <Timestamp iso={p.decision_at} seconds={false} />
                </td>
                <td className={TD} title={p.problems.join("; ")}>
                  <Badge tone={p.validity === "VALID" ? "positive" : "negative"}>{humanize(p.validity)}</Badge>
                </td>
                <td className={`${TD} num`}>
                  {p.output === null
                    ? MISSING
                    : p.output_kind === "CALIBRATED_PROBABILITY"
                      ? `p = ${num(p.output["probability"], 3)}`
                      : p.output_kind === "UNCALIBRATED_SCORE"
                        ? `score ${num(p.output["raw_score"], 3)} (uncalibrated)`
                        : `estimate ${num(p.output["estimate"], 4)}`}
                </td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}
