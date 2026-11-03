import { ArrowLeft } from "lucide-react";
import { Link, useParams } from "react-router";

import { FamilyBadge, ProposalsTable, SignalStateCounts, StrategyStatusBadges } from "../components/strategies";
import {
  Badge,
  DataTable,
  ErrorState,
  KeyValues,
  Loading,
  Notice,
  PageHeader,
  Panel,
  TD,
  TH,
  Timestamp,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { humanize } from "../lib/format";
import { strategies } from "../services/strategies";
import type { ParameterValue, StrategyDetail } from "../types/strategies";
import { ConflictsPanel, EvaluationsSection } from "./StrategiesPage";

export function StrategyDetailPage() {
  const { strategyId = "" } = useParams();
  const detail = useApiResource((signal) => strategies.detail(strategyId, { signal }), [strategyId]);
  return (
    <section aria-labelledby="page-title">
      <Link to="/strategies" className="text-ink-muted hover:text-accent mb-2 inline-flex items-center gap-1 text-xs">
        <ArrowLeft aria-hidden className="size-3.5" />
        Strategy Laboratory
      </Link>
      {detail.status === "loading" && <Loading label="Loading strategy…" />}
      {detail.status === "error" && <ErrorState message={detail.error ?? ""} onRetry={detail.reload} />}
      {detail.data && <Strategy detail={detail.data} />}
    </section>
  );
}

function show(value: ParameterValue): string {
  if (Array.isArray(value)) return value.map(show).join(", ");
  if (value === null) return "—";
  return String(value);
}

function Strategy({ detail }: { detail: StrategyDetail }) {
  const s = detail.strategy;
  const schema = detail.parameters_schema.properties;
  const proposals = useApiResource(
    (signal) => strategies.proposals({ strategy_id: s.strategy_id, limit: 25 }, { signal }),
    [s.strategy_id],
  );
  return (
    <>
      <PageHeader title={s.name} description={s.description} actions={<StrategyStatusBadges strategy={s} />} />
      {!s.implemented && <Notice tone="info">{s.implementation_note}</Notice>}
      <div className="mt-3 grid gap-3 xl:grid-cols-2">
        <Panel title="Identity" actions={<FamilyBadge family={s.family} />}>
          <KeyValues
            items={[
              { label: "Strategy id", value: <span className="num">{s.strategy_id}</span> },
              {
                label: "Current version",
                value: s.current_version ? (
                  <span className="num">
                    v{s.current_version.version} · {s.current_version.logic_version}
                  </span>
                ) : (
                  "not registered"
                ),
              },
              { label: "Markets", value: s.supported_asset_classes.map(humanize).join(", ") },
              { label: "Supported timeframes", value: s.supported_timeframes.join(", ") },
              { label: "Configured timeframes", value: s.configured_timeframes.join(", ") || "—" },
              { label: "Evaluated", value: s.schedule.description },
              { label: "Results", value: <SignalStateCounts counts={s.signal_states} /> },
              {
                label: "Status since",
                value: s.status_changed_at ? <Timestamp iso={s.status_changed_at} /> : "never enabled",
              },
            ]}
          />
        </Panel>
        <Panel title="Parameters" subtitle={s.current_version ? `version ${s.current_version.version}` : undefined}>
          {detail.parameters ? (
            <KeyValues
              items={Object.keys(detail.parameters)
                .sort()
                .map((name) => ({
                  label: <span className="num">{name}</span>,
                  value: <span className="num">{show(detail.parameters?.[name] ?? null)}</span>,
                  ...(schema[name]?.description ? { title: schema[name]?.description } : {}),
                }))}
            />
          ) : (
            <p className="text-ink-faint text-xs">Not registered yet.</p>
          )}
          <p className="text-ink-faint mt-2 text-2xs">
            Changing a parameter creates a new immutable version (operator API); earlier versions and their
            results are kept.
          </p>
        </Panel>
      </div>
      <Panel title="Required features" subtitle="resolved keys in the feature registry" className="mt-3">
        {s.configured_timeframes.map((tf) => (
          <div key={tf} className="mb-2">
            <p className="text-ink-muted mb-1 text-2xs font-semibold tracking-wider uppercase">{tf}</p>
            <ul aria-label={`Features on ${tf}`} className="flex flex-wrap gap-1">
              {(s.required_features[tf] ?? []).map((f) => (
                <li key={f.key}>
                  <Badge tone={f.optional ? "neutral" : "info"} title={f.optional ? "optional" : "required"}>
                    <span className="normal-case">{f.key}</span>
                  </Badge>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </Panel>
      <div className="mt-3 grid gap-3 xl:grid-cols-2">
        <Panel title="Versions" subtitle="immutable; newest is current">
          <DataTable label="Strategy versions">
            <thead>
              <tr>
                <th className={TH}>Version</th>
                <th className={TH}>Logic</th>
                <th className={TH}>Configuration</th>
                <th className={TH}>Reason</th>
                <th className={TH}>Created</th>
              </tr>
            </thead>
            <tbody>
              {detail.versions.map((v) => (
                <tr key={v.id}>
                  <td className={`${TD} num`}>v{v.version}</td>
                  <td className={`${TD} num`}>{v.logic_version}</td>
                  <td className={`${TD} num`} title={v.parameters_sha256}>
                    {v.parameters_sha256.slice(0, 12)}
                  </td>
                  <td className={`${TD} whitespace-normal`}>{v.reason}</td>
                  <td className={TD}>
                    <Timestamp iso={v.created_at} /> <span className="text-ink-faint">{v.created_by}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        </Panel>
        <Panel title="Status history">
          {detail.status_history.length === 0 ? (
            <p className="text-ink-faint text-xs">No status change recorded: the strategy is disabled.</p>
          ) : (
            <DataTable label="Status history">
              <thead>
                <tr>
                  <th className={TH}>When</th>
                  <th className={TH}>Status</th>
                  <th className={TH}>Reason</th>
                  <th className={TH}>By</th>
                </tr>
              </thead>
              <tbody>
                {detail.status_history.map((e) => (
                  <tr key={e.created_at + e.reason}>
                    <td className={TD}>
                      <Timestamp iso={e.created_at} />
                    </td>
                    <td className={TD}>
                      <Badge tone={e.enabled ? "positive" : "neutral"}>{e.enabled ? "Enabled" : "Disabled"}</Badge>
                    </td>
                    <td className={`${TD} whitespace-normal`}>{e.reason}</td>
                    <td className={TD}>{e.actor}</td>
                  </tr>
                ))}
              </tbody>
            </DataTable>
          )}
        </Panel>
      </div>
      <Panel title="Proposals" subtitle="hypotheses, not trades" className="mt-3">
        {proposals.status === "loading" && <Loading />}
        {proposals.status === "error" && (
          <ErrorState message={proposals.error ?? ""} onRetry={proposals.reload} compact />
        )}
        {proposals.data && <ProposalsTable proposals={proposals.data.proposals} showStrategy={false} />}
      </Panel>
      <div className="mt-3 flex flex-col gap-3">
        <ConflictsPanel refreshKey={0} strategyId={s.strategy_id} />
        <EvaluationsSection refreshKey={0} strategyId={s.strategy_id} />
      </div>
    </>
  );
}
