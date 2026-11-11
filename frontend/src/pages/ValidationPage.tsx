import { ShieldCheck } from "lucide-react";
import { type FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router";

import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  ErrorState,
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
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { STATUS_TONE } from "../lib/backtest";
import { MISSING } from "../lib/format";
import { MEASUREMENT_TONE, num, pct, VERDICT_LABEL, VERDICT_TONE } from "../lib/validation";
import { ApiError } from "../services/api";
import { validation } from "../services/validation";
import type { ExperimentBody, ModelGroup, ValidationOptions, ValidationRun } from "../types/validation";

type Tab = "runs" | "governance" | "models" | "monitoring";
const input =
  "border-border-strong bg-panel-raised text-ink h-7 w-full rounded-sm border px-2 text-xs focus:border-accent focus:outline-none";
const label = "text-ink-muted block text-2xs font-semibold uppercase";

/** Strategy Research and Validation (Phase 8B): walk-forward evidence, champions and
 * challengers, ML candidate comparisons and degradation monitoring. */
export function ValidationPage() {
  const [tab, setTab] = useState<Tab>("runs");
  const overview = useApiResource((signal) => validation.overview({ signal }), [], { refreshMs: 10_000 });
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Strategy Research & Validation"
        description="Chronological walk-forward validation, robustness and overfitting controls, champion/challenger governance and degradation monitoring."
        actions={<RefreshButton onClick={overview.reload} loading={overview.loading} />}
      />
      <div className="mb-3 flex flex-col gap-2">
        <Notice tone="caution">
          <strong>Historical simulation, not live paper trading.</strong> Validation replays stored data through the
          paper-trading rules. Out-of-sample means data not used to choose the parameters; it is not evidence of future
          performance. Live paper-trading evidence appears only under Monitoring, labelled as such.
        </Notice>
        {overview.data?.notices.slice(1).map((n) => (
          <Notice key={n}>{n}</Notice>
        ))}
      </div>
      {overview.data && (
        <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-5">
          <Stat label="Validation runs" value={Object.values(overview.data.runs_by_status).reduce((a, b) => a + b, 0)} />
          <Stat label="Trials recorded" value={overview.data.trials_recorded} hint="every parameter set tried counts" />
          <Stat label="Holdout reads" value={overview.data.holdout_evaluations} hint="each one numbered" />
          <Stat
            label="Candidates for review"
            value={overview.data.runs_by_verdict.CANDIDATE_FOR_REVIEW ?? 0}
            hint="passed every gate; a human decides"
          />
          <Stat
            label="Alerts (7 days)"
            value={overview.data.alerts_last_7_days}
            tone={overview.data.alerts_last_7_days ? "caution" : undefined}
          />
        </div>
      )}
      <div className="mb-3">
        <Segmented<Tab>
          label="Section"
          value={tab}
          onChange={setTab}
          options={[
            { value: "runs", label: "Validation runs" },
            { value: "governance", label: "Champions & challengers" },
            { value: "models", label: "Model comparison" },
            { value: "monitoring", label: "Degradation monitoring" },
          ]}
        />
      </div>
      {tab === "runs" && <RunsSection />}
      {tab === "governance" && <GovernanceSection />}
      {tab === "models" && <ModelsSection />}
      {tab === "monitoring" && <MonitoringSection />}
    </section>
  );
}

function RunsSection() {
  const runs = useApiResource((signal) => validation.runs({ signal }), [], {
    refreshMs: 5_000,
    isEmpty: (rows) => rows.length === 0,
  });
  const options = useApiResource((signal) => validation.options({ signal }), []);
  return (
    <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_22rem]">
      <Panel title="Validation runs" subtitle="newest first; out-of-sample figures are stitched test windows">
        {runs.status === "loading" && <Loading />}
        {runs.status === "error" && <ErrorState message={runs.error ?? ""} onRetry={runs.reload} />}
        {runs.status === "empty" && <EmptyState title="No validation has been run">Configure one with the form.</EmptyState>}
        {runs.data && runs.data.length > 0 && <RunsTable runs={runs.data} />}
      </Panel>
      <Panel title="Run a walk-forward validation">
        {options.status === "loading" && <Loading />}
        {options.status === "error" && <ErrorState message={options.error ?? ""} onRetry={options.reload} />}
        {options.data && <RunForm options={options.data} onCreated={runs.reload} />}
      </Panel>
    </div>
  );
}

function RunsTable({ runs }: { runs: ValidationRun[] }) {
  return (
    <DataTable label="Validation runs">
      <thead>
        <tr>
          <th className={TH}>Run</th>
          <th className={TH}>Status</th>
          <th className={TH}>Verdict</th>
          <th className={TH}>Period / holdout</th>
          <th className={`${TH} text-right`}>Trials</th>
          <th className={`${TH} text-right`}>OOS return</th>
          <th className={`${TH} text-right`}>OOS Sharpe</th>
          <th className={`${TH} text-right`}>OOS trades</th>
          <th className={TH}>Created</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((r) => (
          <tr key={r.id}>
            <td className={`${TD} max-w-72 truncate`}>
              <Link to={`/research/runs/${r.id}`} className="text-accent hover:underline">
                {r.name}
              </Link>
              <span className="text-ink-faint ml-2">{r.strategies.join(", ")}</span>
              {r.reproduction_of && (
                <Badge tone={r.reproduced_identical ? "positive" : "caution"}>
                  {r.reproduced_identical === null ? "reproduction" : r.reproduced_identical ? "reproduced exactly" : "reproduction differs"}
                </Badge>
              )}
            </td>
            <td className={TD}>
              <Badge tone={STATUS_TONE[r.status]}>{r.status}</Badge>
              {r.status === "RUNNING" && r.progress.stage && (
                <span className="num text-ink-muted ml-2">
                  {r.progress.stage} {r.progress.done}/{r.progress.total}
                </span>
              )}
            </td>
            <td className={TD}>{r.verdict ? <Badge tone={VERDICT_TONE[r.verdict]}>{VERDICT_LABEL[r.verdict]}</Badge> : MISSING}</td>
            <td className={`${TD} num`}>
              {r.start_date} → {r.end_date}
              {r.holdout_start && <span className="text-ink-faint"> · holdout {r.holdout_start}</span>}
            </td>
            <td className={`${TD} num text-right`}>
              {r.trials ?? MISSING}
              {r.prior_trials ? <span className="text-ink-faint"> (+{r.prior_trials} earlier)</span> : null}
            </td>
            <td className={`${TD} num text-right`}>{pct(r.oos_net_return)}</td>
            <td className={`${TD} num text-right`}>{num(r.oos_sharpe)}</td>
            <td className={`${TD} num text-right`}>{r.oos_trades ?? MISSING}</td>
            <td className={TD}>
              <Timestamp iso={r.created_at} />
            </td>
          </tr>
        ))}
      </tbody>
    </DataTable>
  );
}

function RunForm({ options, onCreated }: { options: ValidationOptions; onCreated: () => void }) {
  const navigate = useNavigate();
  const [strategy, setStrategy] = useState(options.strategies[0]?.strategy_id ?? "");
  const [start, setStart] = useState("2026-06-29");
  const [end, setEnd] = useState("2026-10-30");
  const [symbols, setSymbols] = useState("");
  const [train, setTrain] = useState(25);
  const [test, setTest] = useState(10);
  const [search, setSearch] = useState("RANDOM");
  const [trials, setTrials] = useState(10);
  const [seed, setSeed] = useState(0);
  const [tokenValue, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    const body: ExperimentBody = {
      name: `${strategy} walk-forward`,
      kind: "WALK_FORWARD",
      strategies: [{ strategy_id: strategy }],
      start_date: start,
      end_date: end,
      universe: { mode: "EXPLICIT", symbols: symbols.split(/[\s,]+/).filter(Boolean) },
      timeframes: ["1Day"],
      benchmark_symbol: "SPY",
      windows: { mode: "ROLLING", train_sessions: train, test_sessions: test, validation_sessions: 0 },
      search,
      max_trials: trials,
      seed,
    };
    try {
      const created = await validation.create(body, tokenValue || undefined);
      onCreated();
      void navigate(`/research/runs/${created.id}`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={(e) => void submit(e)} className="flex flex-col gap-2" aria-label="Validation configuration">
      <p className="text-ink-faint text-2xs">
        The final 20% of sessions are reserved as an untouched holdout before anything runs; parameters are chosen on
        training windows only, with an embargo of the longest label horizon at every boundary.
      </p>
      <label className={label}>
        Strategy
        <select className={input} value={strategy} onChange={(e) => setStrategy(e.target.value)}>
          {options.strategies.map((s) => (
            <option key={s.strategy_id} value={s.strategy_id}>
              {s.name}
            </option>
          ))}
        </select>
      </label>
      <div className="grid grid-cols-2 gap-2">
        <label className={label}>
          Start
          <input className={input} type="date" value={start} onChange={(e) => setStart(e.target.value)} />
        </label>
        <label className={label}>
          End
          <input className={input} type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
        </label>
      </div>
      <label className={label}>
        Symbols
        <input className={input} value={symbols} onChange={(e) => setSymbols(e.target.value)} placeholder="ADI, NKE, …" />
      </label>
      <div className="grid grid-cols-2 gap-2">
        <label className={label}>
          Train sessions
          <input className={input} type="number" min={10} value={train} onChange={(e) => setTrain(Number(e.target.value))} />
        </label>
        <label className={label}>
          Test sessions
          <input className={input} type="number" min={5} value={test} onChange={(e) => setTest(Number(e.target.value))} />
        </label>
        <label className={label}>
          Search
          <select className={input} value={search} onChange={(e) => setSearch(e.target.value)}>
            {options.search_methods
              .filter((m) => m !== "NONE")
              .map((m) => (
                <option key={m}>{m}</option>
              ))}
          </select>
        </label>
        <label className={label}>
          Max trials
          <input className={input} type="number" min={1} max={60} value={trials} onChange={(e) => setTrials(Number(e.target.value))} />
        </label>
        <label className={label}>
          Seed
          <input className={input} type="number" min={0} value={seed} onChange={(e) => setSeed(Number(e.target.value))} />
        </label>
        <label className={label}>
          Operator token
          <input className={input} type="password" value={tokenValue} onChange={(e) => setToken(e.target.value)} autoComplete="off" />
        </label>
      </div>
      {error && <p className="text-negative text-xs" role="alert">{error}</p>}
      <Button type="submit" variant="primary" disabled={busy || !symbols.trim()}>
        Run validation
      </Button>
    </form>
  );
}

function GovernanceSection() {
  const champions = useApiResource((signal) => validation.champions({ signal }), []);
  const promotions = useApiResource((signal) => validation.promotions({ signal }), []);
  return (
    <div className="flex flex-col gap-3">
      <Notice>
        Champions act; challengers are evaluated. A challenger reaches SHADOW only with a CANDIDATE_FOR_REVIEW
        validation and its first holdout read, and APPROVED only with forward shadow evidence and an operator's
        explicit decision naming the champion it replaces. Approval and rollback create new strategy versions; nothing
        is promoted automatically.
      </Notice>
      <Panel title="Champions and challengers">
        {champions.status === "loading" && <Loading />}
        {champions.status === "error" && <ErrorState message={champions.error ?? ""} onRetry={champions.reload} />}
        {champions.data && (
          <DataTable label="Champions">
            <thead>
              <tr>
                <th className={TH}>Subject</th>
                <th className={TH}>Champion</th>
                <th className={TH}>Version</th>
                <th className={TH}>Challengers</th>
              </tr>
            </thead>
            <tbody>
              {champions.data.map((c) => (
                <tr key={`${c.subject_kind}:${c.subject_key}`}>
                  <td className={TD}>
                    <Badge tone="neutral">{c.subject_kind}</Badge> {c.subject_key}
                  </td>
                  <td className={TD}>
                    <Badge tone={c.governed ? "positive" : c.label === "NONE APPROVED" ? "neutral" : "caution"}>{c.label}</Badge>
                  </td>
                  <td className={`${TD} num`}>
                    {c.strategy_version !== null ? `v${c.strategy_version}` : c.model_version_id ? c.model_version_id.slice(0, 8) : "none approved"}
                  </td>
                  <td className={TD}>
                    {c.challengers.length === 0
                      ? MISSING
                      : c.challengers.map((ch) => (
                          <Badge key={ch.id} tone={ch.status === "REJECTED" || ch.status === "RETIRED" ? "neutral" : "info"}>
                            {ch.status}
                          </Badge>
                        ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        )}
      </Panel>
      <Panel title="Promotion history" subtitle="every approval, rejection, retirement and rollback">
        {promotions.data && promotions.data.length === 0 && <EmptyState title="No decision has been recorded" />}
        {promotions.data && promotions.data.length > 0 && (
          <DataTable label="Promotion history">
            <thead>
              <tr>
                <th className={TH}>When</th>
                <th className={TH}>Decision</th>
                <th className={TH}>Subject</th>
                <th className={TH}>By</th>
                <th className={TH}>Reason</th>
              </tr>
            </thead>
            <tbody>
              {promotions.data.map((p) => (
                <tr key={p.id}>
                  <td className={TD}>
                    <Timestamp iso={p.created_at} />
                  </td>
                  <td className={TD}>
                    <Badge tone={p.decision === "APPROVE" ? "positive" : p.decision === "ROLLBACK" ? "caution" : "neutral"}>
                      {p.decision}
                    </Badge>
                  </td>
                  <td className={TD}>{p.subject_key}</td>
                  <td className={TD}>{p.approver}</td>
                  <td className={`${TD} max-w-96 truncate`}>{p.reason}</td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        )}
      </Panel>
    </div>
  );
}

function ModelsSection() {
  const models = useApiResource((signal) => validation.models({ signal }), [], {
    isEmpty: (rows) => rows.length === 0,
  });
  return (
    <div className="flex flex-col gap-3">
      <Notice>
        Predictive performance and financial usefulness are judged separately. A model shows incremental value only if its
        trade/no-trade policy beats taking every observation and taking none, stays positive at twice the assumed costs,
        and is significant after overlapping labels and every hyperparameter tried. Accuracy is never used.
      </Notice>
      {models.status === "loading" && <Loading />}
      {models.status === "error" && <ErrorState message={models.error ?? ""} onRetry={models.reload} />}
      {models.status === "empty" && <EmptyState title="No 5A training run to compare" />}
      {models.data?.map((g) => <ModelGroupPanel key={g.dataset_version_id} group={g} />)}
    </div>
  );
}

function ModelGroupPanel({ group }: { group: ModelGroup }) {
  return (
    <Panel
      title={`Dataset ${group.dataset_version_id.slice(0, 8)}`}
      subtitle={`${group.runs} runs · ${group.hyperparameter_candidates_tried} candidates tried · label concurrency ${num(group.mean_label_concurrency, 1)}`}
    >
      <DataTable label={`Models on dataset ${group.dataset_version_id.slice(0, 8)}`}>
        <thead>
          <tr>
            <th className={TH}>Model</th>
            <th className={TH}>5A verdict</th>
            <th className={`${TH} text-right`}>Log loss / base</th>
            <th className={`${TH} text-right`}>Test ECE</th>
            <th className={`${TH} text-right`}>WF folds beating base</th>
            <th className={`${TH} text-right`}>Policy mean net</th>
            <th className={`${TH} text-right`}>Take-all mean net</th>
            <th className={`${TH} text-right`}>Effective n</th>
            <th className={TH}>Incremental value</th>
          </tr>
        </thead>
        <tbody>
          {group.models.map((m) => {
            const p = m.predictive as Record<string, number | null>;
            const f = m.financial as Record<string, number | null>;
            const c = m.calibration as Record<string, number | null>;
            return (
              <tr key={m.training_run_id}>
                <td className={TD}>
                  {m.model_type} <span className="text-ink-faint">{m.task.toLowerCase()}</span>
                  {m.test_period_reuse > 0 && (
                    <span className="text-caution ml-1 text-2xs" title="earlier runs had already evaluated this test period">
                      test reuse {m.test_period_reuse}
                    </span>
                  )}
                  {m.approved_champion && <Badge tone="positive">champion</Badge>}
                </td>
                <td className={TD}>{m.validation_status_5a ?? MISSING}</td>
                <td className={`${TD} num text-right`}>
                  {p.log_loss !== undefined ? `${num(p.log_loss, 4)} / ${num(p.baseline_log_loss, 4)}` : `R² ${num(p.r2, 4)}`}
                </td>
                <td className={`${TD} num text-right`}>{num(c.test_ece, 3)}</td>
                <td className={`${TD} num text-right`}>
                  {m.walk_forward.beating_baseline}/{m.walk_forward.folds}{" "}
                  <span className="text-ink-faint">{m.walk_forward.metric}</span>
                </td>
                <td className={`${TD} num text-right`}>{pct(f.mean_net_return, { places: 3 })}</td>
                <td className={`${TD} num text-right`}>{pct(f.take_all_mean_net_return, { places: 3 })}</td>
                <td className={`${TD} num text-right`}>{num(f.effective_observations, 0)}</td>
                <td className={`${TD} max-w-96 whitespace-normal`}>
                  <Badge tone={m.incremental_value ? "info" : "neutral"}>{m.incremental_value ? "shown" : "not shown"}</Badge>
                  <span className="text-ink-faint ml-1 text-2xs">{m.reasons[0] ?? ""}</span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </DataTable>
      <p className="text-ink-faint mt-2 text-2xs">{group.statement}</p>
    </Panel>
  );
}

function MonitoringSection() {
  const mon = useApiResource((signal) => validation.monitoring({ signal }), []);
  return (
    <div className="flex flex-col gap-3">
      <Notice>
        <ShieldCheck className="mr-1 inline size-3.5" aria-hidden />
        Compares <strong>live paper-trading results</strong> (closed simulated positions) and live feature snapshots with
        the validation evidence. Measurements below their minimum sample are INSUFFICIENT_DATA and never alert. The
        monitor can suspend a strategy only under an operator-approved rule, and never changes risk limits or approves
        anything.
      </Notice>
      {mon.status === "loading" && <Loading />}
      {mon.status === "error" && <ErrorState message={mon.error ?? ""} onRetry={mon.reload} />}
      {mon.data && (
        <>
          <Panel title="Degradation alerts">
            {mon.data.alerts.length === 0 ? (
              <EmptyState title="No alert has been raised" />
            ) : (
              <DataTable label="Degradation alerts">
                <thead>
                  <tr>
                    <th className={TH}>When</th>
                    <th className={TH}>Severity</th>
                    <th className={TH}>Message</th>
                    <th className={TH}>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {mon.data.alerts.map((a) => (
                    <tr key={a.id}>
                      <td className={TD}>
                        <Timestamp iso={a.created_at} />
                      </td>
                      <td className={TD}>
                        <Badge tone={a.severity === "CRITICAL" ? "negative" : "caution"}>{a.severity}</Badge>
                      </td>
                      <td className={`${TD} max-w-xl whitespace-normal`}>{a.message}</td>
                      <td className={TD}>{a.action}</td>
                    </tr>
                  ))}
                </tbody>
              </DataTable>
            )}
          </Panel>
          <Panel title="Latest monitoring pass" subtitle={mon.data.latest_monitor_run_id ?? "never run"}>
            {mon.data.measurements.length === 0 ? (
              <EmptyState title="No measurement yet" />
            ) : (
              <DataTable label="Drift measurements">
                <thead>
                  <tr>
                    <th className={TH}>Subject</th>
                    <th className={TH}>Measurement</th>
                    <th className={`${TH} text-right`}>Value</th>
                    <th className={`${TH} text-right`}>p</th>
                    <th className={`${TH} text-right`}>n ref / now</th>
                    <th className={TH}>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {mon.data.measurements.map((m) => (
                    <tr key={m.id}>
                      <td className={TD}>{m.subject_key}</td>
                      <td className={TD}>
                        {m.metric}
                        {m.feature && <span className="text-ink-faint"> {m.feature}</span>}
                      </td>
                      <td className={`${TD} num text-right`}>{num(m.value, 3)}</td>
                      <td className={`${TD} num text-right`}>{num(m.p_value, 4)}</td>
                      <td className={`${TD} num text-right`}>
                        {m.n_reference} / {m.n_current}
                      </td>
                      <td className={TD}>
                        <Badge tone={MEASUREMENT_TONE[m.status]}>{m.status}</Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </DataTable>
            )}
          </Panel>
          <Panel title="Approved monitoring rules">
            {mon.data.rules.length === 0 ? (
              <p className="text-ink-muted text-xs">None: the default rule applies and only warns.</p>
            ) : (
              <ul className="text-xs">
                {mon.data.rules.map((r) => (
                  <li key={r.id}>
                    {r.subject_key} v{r.version}: <Badge tone={r.action === "SUSPEND" ? "caution" : "neutral"}>{r.action}</Badge>{" "}
                    {r.active ? "active" : "inactive"} · approved by {r.approved_by}
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </>
      )}
    </div>
  );
}
