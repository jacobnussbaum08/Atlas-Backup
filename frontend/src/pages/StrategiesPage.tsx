import { ChevronRight } from "lucide-react";
import { Fragment, useState } from "react";
import { Link } from "react-router";

import {
  ConflictsTable,
  EvaluationsTable,
  FamilyBadge,
  ProposalsTable,
  SignalStateCounts,
  StrategyStatusBadges,
} from "../components/strategies";
import {
  Badge,
  DataTable,
  ErrorState,
  KeyValues,
  Loading,
  Notice,
  PageHeader,
  Panel,
  RefreshButton,
  Segmented,
  TD,
  TH,
  Timestamp,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { formatCount, humanize } from "../lib/format";
import { strategies } from "../services/strategies";
import type { Direction, ParameterValue, ProposalStatus, SignalState, StrategySummary } from "../types/strategies";

type ProposalFilter = "ALL" | ProposalStatus;
type DirectionFilter = "ALL" | Direction;
const DIRECTION_FILTERS: readonly { value: DirectionFilter; label: string }[] = [
  { value: "ALL", label: "Both" },
  { value: "LONG", label: "Long" },
  { value: "SHORT", label: "Short" },
];
type ResultFilter = "ALL" | SignalState;
const RESULT_FILTERS: readonly { value: ResultFilter; label: string }[] = [
  { value: "ALL", label: "All" },
  { value: "SIGNAL", label: "Signal" },
  { value: "NO_SIGNAL", label: "No signal" },
  { value: "INSUFFICIENT_DATA", label: "Insufficient" },
  { value: "MISSING_DATA", label: "Missing" },
  { value: "INELIGIBLE_ASSET", label: "Ineligible" },
  { value: "FAILED", label: "Failed" },
];
// Parameters shown on a module card; the full configuration is on the strategy page.
const SHARED_PARAMETERS = new Set(["timeframes", "directions", "min_signal_strength"]);
const PROPOSAL_FILTERS: readonly { value: ProposalFilter; label: string }[] = [
  { value: "ALL", label: "All" },
  { value: "ACTIVE", label: "Active" },
  { value: "EXPIRED", label: "Expired" },
  { value: "SUPERSEDED", label: "Superseded" },
];

export function StrategiesPage() {
  const [refresh, setRefresh] = useState(0);
  const catalog = useApiResource((signal) => strategies.catalog({ signal }), [refresh], { refreshMs: 30_000 });
  const all = catalog.data?.strategies ?? [];
  const interfaceOnly = all.length > 0 && all.every((s) => !s.implemented);
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Strategy Laboratory"
        description="Six independent strategy modules with immutable versions, validated parameters and point-in-time feature inputs. A proposal is a hypothesis: it carries no position size, is not approved and is never sent anywhere."
        actions={<RefreshButton onClick={() => setRefresh((n) => n + 1)} loading={catalog.loading} />}
      />
      {catalog.status === "loading" && <Loading label="Loading strategies…" />}
      {catalog.status === "error" && <ErrorState message={catalog.error ?? ""} onRetry={catalog.reload} />}
      {catalog.data && (
        <div className="flex flex-col gap-3">
          {interfaceOnly && (
            <Notice tone="info">
              Phase 4A: every family has its interface, parameter schema and feature dependencies, but no
              evaluation logic yet (Phase 4B). None is evaluated and none can be enabled until then.
            </Notice>
          )}
          {catalog.data.sync_problems.map((problem) => (
            <Notice key={problem} tone="caution">
              {problem}
            </Notice>
          ))}
          <p className="text-ink-faint text-2xs">
            No performance statistics are shown: no strategy has traded or been backtested (Phase 8).
            Default parameters are conventional starting points, not optimized values. Signal strength is
            an uncalibrated score, not a probability.
          </p>
          <ul aria-label="Strategy catalog" className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3">
            {catalog.data.strategies.map((s) => (
              <li key={s.strategy_id} className="min-w-0">
                <StrategyCard strategy={s} />
              </li>
            ))}
          </ul>
          <ProposalsPanel refreshKey={refresh} />
          <ConflictsPanel refreshKey={refresh} />
          <EvaluationsSection refreshKey={refresh} />
          <FeatureRegistryPanel version={catalog.data.feature_registry_version} />
        </div>
      )}
    </section>
  );
}

function showValue(value: ParameterValue): string {
  if (Array.isArray(value)) return value.map(showValue).join(", ");
  if (value === null) return "—";
  return String(value);
}

function StrategyCard({ strategy: s }: { strategy: StrategySummary }) {
  const features = s.configured_timeframes.flatMap((tf) => s.required_features[tf] ?? []);
  const parameters = Object.entries(s.parameters ?? {}).filter(([name]) => !SHARED_PARAMETERS.has(name));
  return (
    <Panel
      title={
        <Link to={`/strategies/${s.strategy_id}`} className="hover:text-accent inline-flex items-center gap-1">
          {s.name}
          <ChevronRight aria-hidden className="size-3" />
        </Link>
      }
      actions={<StrategyStatusBadges strategy={s} />}
      className="h-full"
    >
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        <FamilyBadge family={s.family} />
        <span className="text-ink-faint num text-2xs">{s.strategy_id}</span>
      </div>
      <p className="text-ink-muted mb-2 text-xs">{s.description}</p>
      <KeyValues
        items={[
          {
            label: "Version",
            value: s.current_version ? (
              <span className="num">
                v{s.current_version.version} · {s.current_version.logic_version}
              </span>
            ) : (
              "not registered"
            ),
          },
          { label: "Markets", value: s.supported_asset_classes.map(humanize).join(", ") },
          {
            label: "Timeframes",
            value: (
              <span>
                {s.configured_timeframes.join(", ") || "—"}
                <span className="text-ink-faint"> (supports {s.supported_timeframes.join(", ")})</span>
              </span>
            ),
          },
          { label: "Evaluated", value: s.schedule.triggers.map(humanize).join(" + "), title: s.schedule.description },
          {
            label: "Features",
            value: (
              <span className="num" title={features.map((f) => f.key + (f.optional ? " (optional)" : "")).join("\n")}>
                {features.filter((f) => !f.optional).length} required
                {features.some((f) => f.optional) && `, ${features.filter((f) => f.optional).length} optional`}
              </span>
            ),
          },
          {
            label: "Directions",
            value: showValue(s.parameters?.["directions"] ?? null),
          },
          {
            label: "Activity",
            value: (
              <span className="num">
                {formatCount(s.evaluations)} evaluations · {formatCount(s.active_proposals)} active proposals
              </span>
            ),
          },
          {
            label: "Last evaluated",
            value: s.last_evaluation_at ? <Timestamp iso={s.last_evaluation_at} /> : "never",
          },
          { label: "Results", value: <SignalStateCounts counts={s.signal_states} /> },
        ]}
      />
      {parameters.length > 0 && (
        <details className="mt-2 text-xs">
          <summary className="text-ink-muted hover:text-accent cursor-pointer">
            Configuration ({parameters.length} parameters)
          </summary>
          <dl aria-label={`${s.name} configuration`} className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
            {parameters.map(([name, value]) => (
              <Fragment key={name}>
                <dt className="text-ink-muted num">{name}</dt>
                <dd className="num">{showValue(value)}</dd>
              </Fragment>
            ))}
          </dl>
        </details>
      )}
    </Panel>
  );
}

function ProposalsPanel({ refreshKey }: { refreshKey: number }) {
  const [status, setStatus] = useState<ProposalFilter>("ALL");
  const [direction, setDirection] = useState<DirectionFilter>("ALL");
  const proposals = useApiResource(
    (signal) =>
      strategies.proposals(
        {
          status: status === "ALL" ? undefined : status,
          direction: direction === "ALL" ? undefined : direction,
          limit: 50,
        },
        { signal },
      ),
    [refreshKey, status, direction],
    { refreshMs: 30_000 },
  );
  return (
    <Panel
      title="Recent proposals"
      subtitle="hypotheses, not trades"
      actions={
        <span className="inline-flex flex-wrap gap-2">
          <Segmented label="Proposal direction" options={DIRECTION_FILTERS} value={direction} onChange={setDirection} />
          <Segmented label="Proposal status" options={PROPOSAL_FILTERS} value={status} onChange={setStatus} />
        </span>
      }
    >
      {proposals.status === "loading" && <Loading />}
      {proposals.status === "error" && <ErrorState message={proposals.error ?? ""} onRetry={proposals.reload} compact />}
      {proposals.data && <ProposalsTable proposals={proposals.data.proposals} />}
    </Panel>
  );
}

function ConflictsPanel({ refreshKey, strategyId }: { refreshKey: number; strategyId?: string }) {
  const conflicts = useApiResource(
    (signal) => strategies.conflicts({ strategy_id: strategyId, limit: 50 }, { signal }),
    [refreshKey, strategyId],
    { refreshMs: 30_000 },
  );
  return (
    <Panel title="Proposal conflicts" subtitle="recorded, not resolved (Phase 5 coordinator)">
      {conflicts.status === "loading" && <Loading />}
      {conflicts.status === "error" && <ErrorState message={conflicts.error ?? ""} onRetry={conflicts.reload} compact />}
      {conflicts.data && <ConflictsTable conflicts={conflicts.data.conflicts} />}
    </Panel>
  );
}

function EvaluationsSection({ refreshKey, strategyId }: { refreshKey: number; strategyId?: string }) {
  const [result, setResult] = useState<ResultFilter>("ALL");
  return (
    <Panel
      title="Evaluation history"
      subtitle="newest first"
      actions={<Segmented label="Evaluation result" options={RESULT_FILTERS} value={result} onChange={setResult} />}
    >
      <EvaluationsPanel
        refreshKey={refreshKey}
        {...(strategyId === undefined ? {} : { strategyId })}
        {...(result === "ALL" ? {} : { signalState: result })}
      />
    </Panel>
  );
}

function EvaluationsPanel({
  refreshKey,
  strategyId,
  signalState,
}: {
  refreshKey: number;
  strategyId?: string;
  signalState?: SignalState;
}) {
  const evaluations = useApiResource(
    (signal) =>
      strategies.evaluations({ strategy_id: strategyId, signal_state: signalState, limit: 50 }, { signal }),
    [refreshKey, strategyId, signalState],
    { refreshMs: 30_000 },
  );
  if (evaluations.status === "loading") return <Loading />;
  if (evaluations.status === "error") {
    return <ErrorState message={evaluations.error ?? ""} onRetry={evaluations.reload} compact />;
  }
  return evaluations.data ? (
    <EvaluationsTable evaluations={evaluations.data.evaluations} showStrategy={strategyId === undefined} />
  ) : null;
}

function FeatureRegistryPanel({ version }: { version: string }) {
  const [open, setOpen] = useState(false);
  const registry = useApiResource((signal) => strategies.features({ signal }), [open], { enabled: open });
  return (
    <Panel
      title="Feature registry"
      subtitle={version}
      actions={
        <button type="button" className="text-ink-muted hover:text-accent text-xs" onClick={() => setOpen((o) => !o)}>
          {open ? "Hide definitions" : "Show definitions"}
        </button>
      }
    >
      {!open && (
        <p className="text-ink-faint text-xs">
          Every feature a strategy reads, with its calculation, parameters, required data and availability rule.
          Bar features use completed, settled periods only.
        </p>
      )}
      {open && registry.status === "loading" && <Loading />}
      {open && registry.status === "error" && (
        <ErrorState message={registry.error ?? ""} onRetry={registry.reload} compact />
      )}
      {open && registry.data && (
        <DataTable label="Feature definitions">
          <thead>
            <tr>
              <th className={TH}>Feature</th>
              <th className={TH}>Source</th>
              <th className={TH}>Timeframes</th>
              <th className={TH}>Window</th>
              <th className={TH}>Calculation</th>
              <th className={TH}>Registered</th>
            </tr>
          </thead>
          <tbody>
            {registry.data.definitions.map((d) => (
              <tr key={`${d.name}-${d.version}`} title={d.description}>
                <td className={`${TD} num`}>
                  {d.name} <span className="text-ink-faint">{d.version}</span>
                </td>
                <td className={TD}>
                  <Badge tone={d.source === "BARS" ? "neutral" : "info"}>{humanize(d.source)}</Badge>
                </td>
                <td className={TD}>{d.timeframes.join(", ")}</td>
                <td className={`${TD} num`}>{d.window_rule ?? "—"}</td>
                <td className={`${TD} text-ink-muted max-w-96 truncate`} title={`${d.method}\nAvailable: ${d.availability}`}>
                  {d.method}
                </td>
                <td className={TD}>{d.registered_at ? <Timestamp iso={d.registered_at} /> : "not registered"}</td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}

export { ConflictsPanel, EvaluationsPanel, EvaluationsSection };
