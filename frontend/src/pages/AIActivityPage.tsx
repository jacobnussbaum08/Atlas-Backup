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
import { ai } from "../services/ai";
import type {
  AnalysisView,
  ComponentView,
  DecisionDetail,
  DecisionOutcome,
  DecisionSummary,
  EvaluationGroup,
  EvaluationView,
  EvidenceView,
} from "../types/ai";

type Tone = "positive" | "negative" | "caution" | "info" | "accent" | "neutral";

/** Outcome colours. "Prioritized" is informational (blue), never "approved" (green). */
export const OUTCOME_TONE: Record<DecisionOutcome, Tone> = {
  PRIORITIZE_FOR_REVIEW: "info",
  DEPRIORITIZE: "neutral",
  REJECT: "negative",
  DEFER: "caution",
  INSUFFICIENT_DATA: "caution",
  NO_OPPORTUNITY: "neutral",
};
const STANCE_TONE: Record<EvidenceView["stance"], Tone> = {
  SUPPORTS: "positive",
  OPPOSES: "negative",
  NEUTRAL: "neutral",
  CONTEXT: "info",
};

function score(value: unknown, digits = 3): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : MISSING;
}

function pct(value: unknown, digits = 1): string {
  return typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(digits)}%` : MISSING;
}

function OutcomeBadge({ outcome }: { outcome: DecisionOutcome }) {
  const label = outcome === "PRIORITIZE_FOR_REVIEW" ? "Prioritized for review" : humanize(outcome);
  return (
    <Badge tone={OUTCOME_TONE[outcome]} dot title="An analytical outcome; no risk review has happened">
      {label}
    </Badge>
  );
}

export function AIActivityPage() {
  const [refresh, setRefresh] = useState(0);
  const overview = useApiResource((signal) => ai.overview({ signal }), [refresh], { refreshMs: 30_000 });
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Central AI Activity"
        description="How the decision coordinator combined strategy proposals, model outputs, market regimes, cross-asset context and news analysis, and why it ranked, deferred or rejected each opportunity."
        actions={<RefreshButton onClick={() => setRefresh((n) => n + 1)} loading={overview.loading} />}
      />
      {overview.status === "loading" && <Loading label="Loading the decision engine…" />}
      {overview.status === "error" && <ErrorState message={overview.error ?? ""} onRetry={overview.reload} />}
      {overview.data && (
        <div className="flex flex-col gap-3">
          <Notice tone="caution">
            <strong>Analytical prioritization is not risk approval.</strong> {overview.data.disclaimer}
          </Notice>
          <Notice tone="info">{overview.data.llm_note}</Notice>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat
              label="Decision runs"
              value={formatCount(Object.values(overview.data.runs).reduce((a, b) => a + b, 0))}
              hint={
                Object.entries(overview.data.runs)
                  .map(([k, v]) => `${humanize(k)} ${v}`)
                  .join(" · ") || "none yet"
              }
            />
            <Stat
              label="Latest run"
              value={overview.data.latest_run ? humanize(overview.data.latest_run.outcome ?? "") : MISSING}
              hint={
                overview.data.latest_run
                  ? `${overview.data.latest_run.candidates_count} evaluated · ${overview.data.latest_run.mode.toLowerCase()} · ${overview.data.latest_run.basis.toLowerCase()}`
                  : "no completed run"
              }
            />
            <Stat
              label="Prioritized for review"
              value={formatCount(overview.data.decisions["PRIORITIZE_FOR_REVIEW"] ?? 0)}
              hint={`${overview.data.decisions["REJECT"] ?? 0} rejected · ${overview.data.decisions["DEFER"] ?? 0} deferred (all runs)`}
            />
            <Stat
              label="LLM provider"
              value={overview.data.llm.configured ? overview.data.llm.provider : "Not configured"}
              tone={overview.data.llm.configured ? undefined : "caution"}
              hint={
                overview.data.llm.configured
                  ? `${overview.data.llm.model ?? MISSING}${overview.data.llm.priced ? "" : " · unpriced: calls refused"}`
                  : "LLM analysis unavailable; deterministic fallbacks apply"
              }
            />
          </div>
          <RankingsPanel refreshKey={refresh} />
          <RunsPanel refreshKey={refresh} />
          <AnalysesPanel refreshKey={refresh} />
          <UsagePanel refreshKey={refresh} />
          <EvaluationsPanel refreshKey={refresh} />
        </div>
      )}
    </section>
  );
}

function RankingsPanel({ refreshKey }: { refreshKey: number }) {
  const rankings = useApiResource((signal) => ai.rankings({ signal }), [refreshKey], {
    isEmpty: (r) => r.run === null,
  });
  const [open, setOpen] = useState<string | null>(null);
  const run = rankings.data?.run;
  return (
    <Panel
      title="Evaluated opportunities (latest run)"
      subtitle="Ranked by the heuristic ranking score; scores order opportunities and are not probabilities of profit"
    >
      {rankings.status === "loading" && <Loading />}
      {rankings.status === "error" && <ErrorState message={rankings.error ?? ""} onRetry={rankings.reload} />}
      {rankings.status === "empty" && <EmptyState title="No decision run has completed yet." />}
      {run && rankings.data && (
        <div className="flex flex-col gap-2">
          <KeyValues
            items={[
              { label: "Decision time", value: <Timestamp iso={run.decision_at} /> },
              { label: "Knowledge", value: `${humanize(run.basis)} (as of ${run.knowledge_at.slice(0, 16)}Z)` },
              { label: "Policy", value: `${run.policy_code} v${run.policy_version}` },
              { label: "Proposals considered", value: formatCount(run.proposals_considered) },
            ]}
          />
          {run.notes.map((note) => (
            <Notice key={note}>{note}</Notice>
          ))}
          <DecisionTable
            label="Ranked opportunities"
            decisions={rankings.data.decisions}
            open={open}
            onToggle={(id) => setOpen(open === id ? null : id)}
          />
        </div>
      )}
    </Panel>
  );
}

function DecisionTable({
  label,
  decisions,
  open,
  onToggle,
}: {
  label: string;
  decisions: DecisionSummary[];
  open: string | null;
  onToggle: (id: string) => void;
}) {
  return (
    <DataTable label={label}>
      <thead>
        <tr>
          <th className={TH} aria-label="Details" />
          <th className={TH}>Rank</th>
          <th className={TH}>Asset</th>
          <th className={TH}>Outcome</th>
          <th className={TH}>Strategies</th>
          <th className={TH}>Heuristic</th>
          <th className={TH}>Ranking</th>
          <th className={TH}>Coverage</th>
          <th className={TH}>Net reward/risk</th>
          <th className={TH}>Expected value</th>
          <th className={TH}>Main reason</th>
        </tr>
      </thead>
      <tbody>
        {decisions.map((d) => (
          <Fragment key={d.id}>
            <tr>
              <td className={TD}>
                {d.asset_id && (
                  <button
                    type="button"
                    className="text-ink-muted hover:text-ink"
                    aria-label={`Details of ${d.symbol ?? ""} ${d.direction ?? ""}`}
                    aria-expanded={open === d.id}
                    onClick={() => onToggle(d.id)}
                  >
                    <ChevronRight className={`h-3.5 w-3.5 transition-transform ${open === d.id ? "rotate-90" : ""}`} />
                  </button>
                )}
              </td>
              <td className={`${TD} num`}>{d.rank ?? MISSING}</td>
              <td className={TD}>
                {d.symbol ? (
                  <>
                    <span className="font-medium">{d.symbol}</span>{" "}
                    <Badge tone={d.direction === "LONG" ? "positive" : "negative"}>{d.direction}</Badge>
                  </>
                ) : (
                  <span className="text-ink-muted">Run</span>
                )}
              </td>
              <td className={TD}>
                <OutcomeBadge outcome={d.outcome} />
              </td>
              <td className={TD}>{d.strategies.map(humanize).join(", ") || MISSING}</td>
              <td className={`${TD} num`}>{score(d.heuristic_score)}</td>
              <td className={`${TD} num`}>{score(d.ranking_score)}</td>
              <td className={`${TD} num`}>{pct(d.component_coverage, 0)}</td>
              <td className={`${TD} num`}>{score(d.reward_risk_net, 2)}</td>
              <td className={TD}>
                {d.expected_value_status === "SUPPORTED" ? (
                  <Badge tone="info">Supported</Badge>
                ) : (
                  <span className="text-ink-faint" title="No validated probability: no expected value is claimed">
                    Not claimed
                  </span>
                )}
              </td>
              <td className={`${TD} max-w-[22rem] truncate`} title={d.reasons.map((r) => r.message).join("\n")}>
                {d.reasons[0] ? `${humanize(d.reasons[0].code)}: ${d.reasons[0].message}` : MISSING}
              </td>
            </tr>
            {open === d.id && (
              <tr>
                <td colSpan={11} className="border-border/60 border-b p-3">
                  <DecisionDetailView decisionId={d.id} />
                </td>
              </tr>
            )}
          </Fragment>
        ))}
      </tbody>
    </DataTable>
  );
}

function DecisionDetailView({ decisionId }: { decisionId: string }) {
  const detail = useApiResource((signal) => ai.decision(decisionId, { signal }), [decisionId]);
  if (detail.status === "loading") return <Loading />;
  if (detail.status === "error") return <ErrorState message={detail.error ?? ""} onRetry={detail.reload} />;
  const d = detail.data;
  if (!d) return null;
  return (
    <div className="flex flex-col gap-3 text-xs">
      <p className="text-ink">{d.explanation}</p>
      <ContextGrid decision={d} />
      <ComponentsTable components={d.components} />
      <EvidenceList evidence={d.evidence} />
      {d.conflicts.length > 0 && (
        <div>
          <p className="text-ink-muted mb-1 font-semibold">Conflicts (recorded, not forced to consensus)</p>
          <ul className="list-disc pl-5">
            {d.conflicts.map((c, i) => (
              <li key={i}>
                <span className="font-medium">{humanize(String(c["type"]))}</span>: {String(c["description"])}{" "}
                <span className="text-ink-faint">— {String(c["handling"])}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <ProposalsTable decision={d} />
      {d.llm_analyses.length > 0 && <AnalysesTable label="News analyses used" analyses={d.llm_analyses} />}
      <p className="text-ink-faint">
        Record {d.id} · content sha256 {d.content_sha256.slice(0, 16)}… · expires <Timestamp iso={d.expires_at} />
      </p>
    </div>
  );
}

function ContextGrid({ decision: d }: { decision: DecisionDetail }) {
  const ev = (d.expected["expected_value"] ?? {}) as Record<string, unknown>;
  const market = d.regime_snapshot.market ?? {};
  return (
    <KeyValues
      items={[
        {
          label: "Market regime",
          value:
            Object.entries(market)
              .map(([dim, r]) => `${humanize(dim)} ${humanize(r.label)}`)
              .join(" · ") || "unavailable at the decision time",
        },
        {
          label: "Potential reward / adverse (net)",
          value: `${pct(d.expected["potential_reward_net"], 2)} / ${pct(d.expected["potential_adverse_net"], 2)}`,
          title: String(d.expected["cost_basis"] ?? ""),
        },
        {
          label: "Expected value",
          value:
            ev["status"] === "SUPPORTED"
              ? `${pct(ev["expected_value"], 2)} (${String(ev["method"])})`
              : "not claimed: no validated probability",
        },
        {
          label: "Calibrated probability",
          value:
            typeof d.calibrated_probability === "number"
              ? `${pct(d.calibrated_probability)} — ${String(d.scores["calibrated_probability_meaning"] ?? "")}`
              : "none (no approved, calibrated model)",
        },
        {
          label: "Uncertainty index",
          value: `${score(d.uncertainty_index, 2)} (heuristic)`,
          title: String(d.uncertainty["kind"] ?? ""),
        },
        {
          label: "Models",
          value:
            d.model_versions.length === 0
              ? "no model prediction recorded"
              : d.model_versions
                  .map((m) => `${m.model_name} v${m.version} (${humanize(m.status_at_decision)}${m.used ? ", used" : ", not used"})`)
                  .join("; "),
        },
        {
          label: "Strategy versions",
          value: d.strategy_versions.map((s) => `${s.strategy_id} v${s.version} (${s.logic_version})`).join("; "),
        },
        {
          label: "News events",
          value: d.news_refs.length ? `${d.news_refs[0]?.event_count ?? 0} in the look-back window` : "none recorded",
        },
      ]}
    />
  );
}

function ComponentsTable({ components }: { components: ComponentView[] }) {
  return (
    <DataTable label="Scoring components">
      <thead>
        <tr>
          <th className={TH}>Component</th>
          <th className={TH}>Kind</th>
          <th className={TH}>Status</th>
          <th className={TH}>Score</th>
          <th className={TH}>Weight</th>
          <th className={TH}>Contribution</th>
          <th className={TH}>Why</th>
        </tr>
      </thead>
      <tbody>
        {components.map((c) => (
          <tr key={c.name}>
            <td className={TD}>{humanize(c.name)}</td>
            <td className={TD}>{humanize(c.kind)}</td>
            <td className={TD}>
              <Badge tone={c.status === "AVAILABLE" ? "neutral" : "caution"}>{humanize(c.status)}</Badge>
            </td>
            <td className={`${TD} num`}>{score(c.score)}</td>
            <td className={`${TD} num`}>{score(c.weight, 2)}</td>
            <td className={`${TD} num`}>{score(c.contribution)}</td>
            <td className={`${TD} max-w-[28rem] truncate`} title={c.reason}>
              {c.reason}
            </td>
          </tr>
        ))}
      </tbody>
    </DataTable>
  );
}

function EvidenceList({ evidence }: { evidence: EvidenceView[] }) {
  if (evidence.length === 0) return null;
  return (
    <div>
      <p className="text-ink-muted mb-1 font-semibold">Evidence</p>
      <ul className="flex flex-col gap-0.5">
        {evidence.map((e) => (
          <li key={e.id}>
            <Badge tone={STANCE_TONE[e.stance]}>{humanize(e.stance)}</Badge>{" "}
            <span className="text-ink-faint">{humanize(e.kind)}</span> {e.summary}
          </li>
        ))}
      </ul>
    </div>
  );
}

function ProposalsTable({ decision: d }: { decision: DecisionDetail }) {
  return (
    <DataTable label="Strategy proposals considered">
      <thead>
        <tr>
          <th className={TH}>Strategy</th>
          <th className={TH}>Direction</th>
          <th className={TH}>Timeframe</th>
          <th className={TH}>Entry</th>
          <th className={TH}>Invalidation</th>
          <th className={TH}>Target</th>
          <th className={TH}>Strength (uncalibrated)</th>
          <th className={TH}>Data cutoff</th>
        </tr>
      </thead>
      <tbody>
        {d.proposals.map((p) => (
          <tr key={p.id}>
            <td className={TD}>{humanize(p.strategy_id)}</td>
            <td className={TD}>{p.direction}</td>
            <td className={TD}>{p.timeframe}</td>
            <td className={`${TD} num`}>{p.reference_entry}</td>
            <td className={`${TD} num`}>{p.invalidation_level ?? MISSING}</td>
            <td className={`${TD} num`}>{p.target_reference ?? MISSING}</td>
            <td className={`${TD} num`}>{p.signal_strength}</td>
            <td className={TD}>
              <Timestamp iso={p.data_cutoff} seconds={false} />
            </td>
          </tr>
        ))}
      </tbody>
    </DataTable>
  );
}

function RunsPanel({ refreshKey }: { refreshKey: number }) {
  const runs = useApiResource((signal) => ai.runs({ signal }), [refreshKey], { isEmpty: (r) => r.length === 0 });
  const [open, setOpen] = useState<string | null>(null);
  return (
    <Panel title="Decision runs" subtitle="Every run is immutable and replayable from its stored inputs">
      {runs.status === "loading" && <Loading />}
      {runs.status === "error" && <ErrorState message={runs.error ?? ""} onRetry={runs.reload} />}
      {runs.status === "empty" && <EmptyState title="No decision run yet." />}
      {runs.data && runs.data.length > 0 && (
        <DataTable label="Decision runs">
          <thead>
            <tr>
              <th className={TH} aria-label="Details" />
              <th className={TH}>Decision time</th>
              <th className={TH}>Mode</th>
              <th className={TH}>Status</th>
              <th className={TH}>Outcome</th>
              <th className={TH}>Proposals</th>
              <th className={TH}>Opportunities</th>
              <th className={TH}>Prioritized</th>
              <th className={TH}>Outcomes</th>
            </tr>
          </thead>
          <tbody>
            {runs.data.map((r) => (
              <Fragment key={r.id}>
                <tr>
                  <td className={TD}>
                    <button
                      type="button"
                      className="text-ink-muted hover:text-ink"
                      aria-label={`Details of run ${r.id}`}
                      aria-expanded={open === r.id}
                      onClick={() => setOpen(open === r.id ? null : r.id)}
                    >
                      <ChevronRight className={`h-3.5 w-3.5 transition-transform ${open === r.id ? "rotate-90" : ""}`} />
                    </button>
                  </td>
                  <td className={TD}>
                    <Timestamp iso={r.decision_at} seconds={false} />
                  </td>
                  <td className={TD}>
                    {humanize(r.mode)} · {humanize(r.basis)}
                  </td>
                  <td className={TD}>
                    <Badge tone={r.status === "FAILED" ? "negative" : "neutral"}>{humanize(r.status)}</Badge>
                  </td>
                  <td className={TD}>{r.outcome ? humanize(r.outcome) : MISSING}</td>
                  <td className={`${TD} num`}>{formatCount(r.proposals_considered)}</td>
                  <td className={`${TD} num`}>{formatCount(r.candidates_count)}</td>
                  <td className={`${TD} num`}>{formatCount(r.prioritized_count)}</td>
                  <td className={TD}>
                    {Object.entries(r.outcome_counts)
                      .map(([k, v]) => `${humanize(k)} ${v}`)
                      .join(" · ") || MISSING}
                  </td>
                </tr>
                {open === r.id && (
                  <tr>
                    <td colSpan={9} className="border-border/60 border-b p-3">
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

function RunDetailView({ runId }: { runId: string }) {
  const run = useApiResource((signal) => ai.run(runId, { signal }), [runId]);
  const [open, setOpen] = useState<string | null>(null);
  if (run.status === "loading") return <Loading />;
  if (run.status === "error") return <ErrorState message={run.error ?? ""} onRetry={run.reload} />;
  const r = run.data;
  if (!r) return null;
  return (
    <div className="flex flex-col gap-2 text-xs">
      <KeyValues
        items={[
          { label: "Inputs read", value: Object.entries(r.input_counts).map(([k, v]) => `${humanize(k)} ${v}`).join(" · ") || "none" },
          { label: "Inputs sha256", value: r.inputs_sha256 ? `${r.inputs_sha256.slice(0, 16)}…` : MISSING },
          { label: "Policy", value: `${r.policy_code} v${r.policy_version}` },
          { label: "Error", value: r.error ?? "none" },
        ]}
      />
      {r.conflicts.length > 0 && (
        <DataTable label="Strategy agreement and conflicts">
          <thead>
            <tr>
              <th className={TH}>Type</th>
              <th className={TH}>Asset</th>
              <th className={TH}>What</th>
              <th className={TH}>Handling</th>
            </tr>
          </thead>
          <tbody>
            {r.conflicts.map((c) => (
              <tr key={c.id}>
                <td className={TD}>{humanize(c.type)}</td>
                <td className={TD}>{c.symbol ?? MISSING}</td>
                <td className={`${TD} max-w-[26rem] truncate`} title={c.description}>
                  {c.description}
                </td>
                <td className={`${TD} max-w-[22rem] truncate`} title={c.handling}>
                  {c.handling}
                </td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
      {r.explanations.map((e) => (
        <Notice key={e.id} tone="info">
          LLM explanation ({e.model}, {e.prompt_version}; reading material, not evidence):{" "}
          {String(e.analysis["overview"] ?? "")}
        </Notice>
      ))}
      <DecisionTable
        label={`Decisions of run ${r.id}`}
        decisions={r.decisions}
        open={open}
        onToggle={(id) => setOpen(open === id ? null : id)}
      />
    </div>
  );
}

function AnalysesPanel({ refreshKey }: { refreshKey: number }) {
  const analyses = useApiResource((signal) => ai.analyses({ signal }), [refreshKey], {
    isEmpty: (r) => r.length === 0,
  });
  return (
    <Panel
      title="News analysis (LLM)"
      subtitle="Unverified LLM estimates, usable by decisions only after their availability time; flagged analyses are never used"
    >
      {analyses.status === "loading" && <Loading />}
      {analyses.status === "error" && <ErrorState message={analyses.error ?? ""} onRetry={analyses.reload} />}
      {analyses.status === "empty" && (
        <EmptyState title="No LLM analysis has been stored.">
          With no LLM provider configured, analysis is unavailable and the coordinator uses its deterministic fallbacks.
        </EmptyState>
      )}
      {analyses.data && analyses.data.length > 0 && <AnalysesTable label="LLM analyses" analyses={analyses.data} />}
    </Panel>
  );
}

function AnalysesTable({ label, analyses }: { label: string; analyses: AnalysisView[] }) {
  return (
    <DataTable label={label}>
      <thead>
        <tr>
          <th className={TH}>Available</th>
          <th className={TH}>Task</th>
          <th className={TH}>Subject</th>
          <th className={TH}>Assets</th>
          <th className={TH}>Event</th>
          <th className={TH}>Tone</th>
          <th className={TH}>Use</th>
          <th className={TH}>Model · prompt</th>
        </tr>
      </thead>
      <tbody>
        {analyses.map((a) => {
          const assets = (a.analysis["assets"] ?? []) as { symbol: string; sentiment: string }[];
          return (
            <tr key={a.id}>
              <td className={TD}>
                <Timestamp iso={a.available_at} seconds={false} />
              </td>
              <td className={TD}>{humanize(a.task)}</td>
              <td className={`${TD} max-w-[24rem] truncate`} title={a.headline ?? a.subject_ref}>
                {a.headline ?? a.subject_ref}
              </td>
              <td className={TD}>{a.symbols.join(", ") || MISSING}</td>
              <td className={TD}>{a.analysis["event_type"] ? humanize(String(a.analysis["event_type"])) : MISSING}</td>
              <td className={TD}>{assets.map((x) => `${x.symbol} ${humanize(x.sentiment)}`).join(", ") || MISSING}</td>
              <td className={TD}>
                {a.usable ? (
                  <Badge tone="neutral">Usable</Badge>
                ) : (
                  <Badge tone="negative" title={a.flags.join("\n")}>
                    Flagged
                  </Badge>
                )}
              </td>
              <td className={TD}>
                {a.model} · {a.prompt_version}
              </td>
            </tr>
          );
        })}
      </tbody>
    </DataTable>
  );
}

function UsagePanel({ refreshKey }: { refreshKey: number }) {
  const usage = useApiResource((signal) => ai.usage({ signal }), [refreshKey]);
  const u = usage.data;
  return (
    <Panel title="AI API usage and estimated cost" subtitle="Tokens as reported by the provider; costs computed from configured prices">
      {usage.status === "loading" && <Loading />}
      {usage.status === "error" && <ErrorState message={usage.error ?? ""} onRetry={usage.reload} />}
      {u && (
        <div className="flex flex-col gap-2">
          <KeyValues
            items={[
              { label: "Provider · model", value: `${u.configuration.provider} · ${u.configuration.model ?? MISSING}` },
              {
                label: "Prices (USD / M tokens)",
                value: u.configuration.priced
                  ? `${u.configuration.input_usd_per_mtok} in · ${u.configuration.output_usd_per_mtok} out (${u.configuration.pricing_source ?? "source not stated"})`
                  : "not configured: every call is refused",
              },
              { label: "Today", value: `$${u.budget.spent_today_usd} of $${u.budget.daily_budget_usd} · ${u.budget.requests_today}/${u.budget.max_requests_per_day} requests` },
              { label: "This month", value: `$${u.budget.spent_month_usd} of $${u.budget.monthly_budget_usd}` },
              {
                label: "Requests",
                value:
                  Object.entries(u.request_statuses)
                    .map(([k, v]) => `${humanize(k)} ${v}`)
                    .join(" · ") || "none",
              },
            ]}
          />
          {u.by_task.length === 0 ? (
            <EmptyState title="No LLM tokens have been used." />
          ) : (
            <DataTable label="Usage by task">
              <thead>
                <tr>
                  <th className={TH}>Task</th>
                  <th className={TH}>Model</th>
                  <th className={TH}>Responses</th>
                  <th className={TH}>Input tokens</th>
                  <th className={TH}>Output tokens</th>
                  <th className={TH}>Estimated cost (USD)</th>
                </tr>
              </thead>
              <tbody>
                {u.by_task.map((t) => (
                  <tr key={`${t.task}-${t.model}`}>
                    <td className={TD}>{humanize(t.task)}</td>
                    <td className={TD}>{t.model}</td>
                    <td className={`${TD} num`}>{formatCount(t.responses)}</td>
                    <td className={`${TD} num`}>{formatCount(t.input_tokens)}</td>
                    <td className={`${TD} num`}>{formatCount(t.output_tokens)}</td>
                    <td className={`${TD} num`}>{t.cost_usd}</td>
                  </tr>
                ))}
              </tbody>
            </DataTable>
          )}
        </div>
      )}
    </Panel>
  );
}

const GROUP_LABEL: Record<string, string> = {
  coordinator: "Coordinator: prioritized",
  take_all: "Baseline: take every opportunity",
  strongest_signal: "Baseline: strongest signal per run",
  declined: "Not prioritized",
};

function EvaluationsPanel({ refreshKey }: { refreshKey: number }) {
  const evals = useApiResource((signal) => ai.evaluations({ signal }), [refreshKey], {
    isEmpty: (r) => r.length === 0,
  });
  const latest: EvaluationView | undefined = evals.data?.[0];
  return (
    <Panel
      title="Coordinator against simple baselines"
      subtitle="Realized net outcomes of past decisions under a stated cost assumption; overlapping and unsized, not portfolio returns"
    >
      {evals.status === "loading" && <Loading />}
      {evals.status === "error" && <ErrorState message={evals.error ?? ""} onRetry={evals.reload} />}
      {evals.status === "empty" && <EmptyState title="No baseline comparison has been run." />}
      {latest && (
        <div className="flex flex-col gap-2">
          <Notice tone={latest.verdict === "INCREMENTAL_VALUE_OBSERVED" ? "info" : "caution"}>
            Verdict: <strong>{humanize(latest.verdict)}</strong> — {latest.runs_evaluated} runs evaluated.{" "}
            {latest.notes.join(" ")}
          </Notice>
          <DataTable label="Baseline comparison">
            <thead>
              <tr>
                <th className={TH}>Group</th>
                <th className={TH}>Matured outcomes</th>
                <th className={TH}>Mean net return</th>
                <th className={TH}>Median</th>
                <th className={TH}>Hit rate</th>
              </tr>
            </thead>
            <tbody>
              {Object.keys(GROUP_LABEL).map((key) => {
                const g = (latest.results[key] ?? {}) as EvaluationGroup;
                return (
                  <tr key={key}>
                    <td className={TD}>{GROUP_LABEL[key]}</td>
                    <td className={`${TD} num`}>{g.observations === null || g.observations === undefined ? MISSING : formatCount(g.observations)}</td>
                    <td className={`${TD} num`}>{pct(g.mean, 2)}</td>
                    <td className={`${TD} num`}>{pct(g.median, 2)}</td>
                    <td className={`${TD} num`}>{pct(g.hit_rate, 0)}</td>
                  </tr>
                );
              })}
            </tbody>
          </DataTable>
        </div>
      )}
    </Panel>
  );
}
