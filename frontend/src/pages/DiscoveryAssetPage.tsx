import { ArrowLeft } from "lucide-react";
import { Link, useParams } from "react-router";

import {
  AssetStatusBadge,
  Flag,
  InstrumentTypeLabel,
  LAYER_LABELS,
  OutcomeBadge,
  RuleStatusBadge,
} from "../components/discovery";
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
  TD,
  TH,
  Timestamp,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { humanize, MISSING } from "../lib/format";
import { discovery } from "../services/discovery";
import type { AssetDetail, AssetHistory, Metric, QualificationHistory, WatchlistScore } from "../types/discovery";

const BASIS: Record<string, string> = {
  OPERATOR_REFERENCE: "operator reference file",
  PROVIDER_HISTORY_ASSUMED: "assumed to cover the provider's history",
  OBSERVED: "valid only since Atlas observed it",
};

export function DiscoveryAssetPage() {
  const { assetId = "" } = useParams();
  const detail = useApiResource((signal) => discovery.asset(assetId, { signal }), [assetId]);
  const qualification = useApiResource((signal) => discovery.qualification(assetId, { signal }), [assetId]);
  const history = useApiResource((signal) => discovery.history(assetId, { signal }), [assetId]);
  const reload = () => {
    detail.reload();
    qualification.reload();
    history.reload();
  };
  if (detail.status === "loading") return <Loading label="Loading instrument…" />;
  if (detail.status === "error" || !detail.data)
    return <ErrorState message={detail.error ?? "Not found"} onRetry={detail.reload} />;
  const d = detail.data;
  return (
    <section aria-labelledby="page-title">
      <Link to="/discovery" className="text-ink-muted hover:text-ink mb-2 inline-flex items-center gap-1 text-xs">
        <ArrowLeft aria-hidden className="size-3.5" /> Asset Discovery
      </Link>
      <PageHeader
        title={d.asset.symbol}
        description={d.asset.name ?? undefined}
        actions={
          <>
            <AssetStatusBadge status={d.asset.status} />
            <OutcomeBadge outcome={d.latest_evaluation?.outcome} />
            <RefreshButton onClick={reload} loading={detail.loading} />
          </>
        }
      />
      <div className="grid gap-3 xl:grid-cols-3">
        <IdentityPanel d={d} />
        <ReferencePanel d={d} />
        <LayersPanel d={d} />
      </div>
      <QualificationPanel d={d} resource={qualification.data} error={qualification.error} />
      {d.latest_score ? <ScorePanel score={d.latest_score} /> : null}
      <HistoryPanel history={history.data} error={history.error} />
    </section>
  );
}

function IdentityPanel({ d }: { d: AssetDetail }) {
  const provider = d.providers[0];
  return (
    <Panel title="Identity">
      <KeyValues
        items={[
          { label: "Atlas asset ID", value: <span className="num text-2xs">{d.asset.asset_id}</span> },
          { label: "Provider ID", value: provider ? <span className="num text-2xs">{provider.provider}:{provider.provider_asset_id}</span> : MISSING },
          { label: "Provider symbol", value: d.asset.provider_symbol ?? MISSING },
          { label: "Asset class", value: humanize(d.asset.asset_class) },
          { label: "Instrument type", value: <InstrumentTypeLabel type={d.asset.instrument_type} /> },
          { label: "Exchange", value: d.asset.exchange ? `${d.asset.exchange} (${d.asset.provider_exchange ?? "—"})` : `unmapped (${d.asset.provider_exchange ?? "—"})` },
          { label: "Currency", value: d.quote_currency },
          { label: "Country", value: d.country ?? <span className="text-ink-faint">not reported</span> },
          { label: "First discovered", value: <Timestamp iso={d.asset.first_discovered_at} /> },
          { label: "Last verified", value: <Timestamp iso={d.asset.last_verified_at} /> },
          { label: "Metadata source", value: d.metadata_source ?? MISSING },
          { label: "Reference versions", value: d.reference_versions },
        ]}
      />
    </Panel>
  );
}

function ReferencePanel({ d }: { d: AssetDetail }) {
  const r = d.reference;
  if (!r) return <Panel title="Reference data"><EmptyState title="No provider reference data" /></Panel>;
  return (
    <Panel title="Reference data" subtitle={`version ${r.version}`}>
      <KeyValues
        items={[
          { label: "Provider status", value: humanize(r.status) },
          { label: "Tradable", value: <Flag value={r.tradable} label="Tradable" /> },
          { label: "Marginable", value: <Flag value={r.marginable} label="Marginable" /> },
          { label: "Shortable", value: <Flag value={r.shortable} label="Shortable" /> },
          { label: "Easy to borrow", value: <Flag value={r.easy_to_borrow} label="Easy to borrow" /> },
          { label: "Borrow status", value: r.borrow_status ?? MISSING },
          { label: "Fractionable", value: <Flag value={r.fractionable} label="Fractionable" /> },
          { label: "Margin (long/short)", value: `${r.margin_requirement_long ?? "—"}% / ${r.margin_requirement_short ?? "—"}%` },
          { label: "Tick / lot size", value: r.price_increment ?? <span className="text-ink-faint">not reported for US equities</span> },
          { label: "Attributes", value: r.attributes.length ? r.attributes.join(", ") : MISSING },
          { label: "Observed", value: <Timestamp iso={r.observed_at} /> },
          ...(r.delisting_evidence ? [{ label: "Delisting", value: r.delisting_evidence }] : []),
        ]}
      />
      <div className="mt-2">
        <Notice tone="caution">Short and borrow flags are reference data as observed; borrow must be revalidated before any short is proposed.</Notice>
      </div>
    </Panel>
  );
}

function LayersPanel({ d }: { d: AssetDetail }) {
  return (
    <Panel title="Universe membership">
      <ul className="space-y-2 text-xs">
        {d.layers.map((layer) => (
          <li key={layer.layer} className="flex flex-wrap items-baseline justify-between gap-2">
            <span className="font-medium">{LAYER_LABELS[layer.layer]}</span>
            <span className="flex items-center gap-2">
              <Badge tone={layer.member ? "positive" : "neutral"} dot>
                {layer.member ? "Member" : layer.since ? "Removed" : "Never"}
              </Badge>
              {layer.since && <Timestamp iso={layer.since} seconds={false} />}
            </span>
            {layer.reason && <span className="text-ink-muted w-full">{layer.reason}</span>}
          </li>
        ))}
      </ul>
      <div className="mt-3">
        <p className="text-ink-muted mb-1 text-2xs font-semibold uppercase">Symbol mappings</p>
        <ul className="space-y-1 text-xs">
          {d.mappings.map((m) => (
            <li key={`${m.provider}-${m.valid_from}`}>
              <span className="font-medium">{m.provider_symbol}</span>{" "}
              <span className="text-ink-muted">
                from <Timestamp iso={m.valid_from} seconds={false} /> {m.valid_to ? <>to <Timestamp iso={m.valid_to} seconds={false} /></> : "(current)"} ·{" "}
                {BASIS[m.valid_from_basis] ?? m.valid_from_basis}
              </span>
            </li>
          ))}
          {d.mappings.length === 0 && <li className="text-ink-faint">No provider symbol mapping (inactive when discovered).</li>}
        </ul>
      </div>
    </Panel>
  );
}

function QualificationPanel({ d, resource, error }: { d: AssetDetail; resource: QualificationHistory | null; error: string | null }) {
  const evaluation = d.latest_evaluation;
  return (
    <Panel
      className="mt-3"
      title="Qualification"
      subtitle={evaluation ? `as of ${evaluation.as_of.slice(0, 16).replace("T", " ")} UTC · rule set ${evaluation.rule_set_code ?? "?"} v${evaluation.rule_set_version ?? "?"}` : undefined}
      bodyClassName=""
    >
      {!evaluation && <EmptyState title="Not evaluated yet">The next qualification run evaluates this instrument.</EmptyState>}
      {evaluation && (
        <DataTable label="Qualification rules">
          <thead>
            <tr>
              {["Rule", "Category", "Kind", "Result", "Explanation"].map((h) => (
                <th key={h} className={TH} scope="col">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {evaluation.rule_results.map((r) => (
              <tr key={r.rule} aria-label={r.rule}>
                <td className={`${TD} font-medium`}>{humanize(r.rule)}</td>
                <td className={`${TD} text-ink-muted`}>{humanize(r.category)}</td>
                <td className={`${TD} text-ink-muted`}>{r.mandatory ? "mandatory" : "optional"}</td>
                <td className={TD}>
                  <RuleStatusBadge status={r.status} />
                </td>
                <td className={`${TD} text-ink-muted whitespace-normal`}>{r.message}</td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
      {error && (
        <div className="p-3">
          <ErrorState message={error} compact />
        </div>
      )}
      {resource && resource.metrics.length > 0 && <MetricsTable metrics={resource.metrics} />}
      {resource?.notes.map((note) => (
        <div key={note} className="px-3 pb-2">
          <Notice>{note}</Notice>
        </div>
      ))}
    </Panel>
  );
}

function MetricsTable({ metrics }: { metrics: Metric[] }) {
  return (
    <DataTable label="Measurements">
      <thead>
        <tr>
          {["Measurement", "Window", "Value", "Status", "Observations", "Source range", "Version"].map((h) => (
            <th key={h} className={TH} scope="col">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {metrics.map((m) => (
          <tr key={m.metric}>
            <td className={TD}>{humanize(m.metric)}</td>
            <td className={`${TD} text-ink-muted`}>{m.window}</td>
            <td className={`${TD} num`}>{m.value ?? MISSING}</td>
            <td className={TD}>
              <Badge tone={m.status === "OK" ? "positive" : m.status === "NO_DATA" ? "neutral" : "caution"}>{humanize(m.status)}</Badge>
            </td>
            <td className={`${TD} num text-ink-muted`}>
              {m.observations}/{m.expected_observations}
            </td>
            <td className={`${TD} text-ink-muted`}>
              {m.source_start ? (
                <>
                  <Timestamp iso={m.source_start} seconds={false} /> – <Timestamp iso={m.source_end} seconds={false} />
                </>
              ) : (
                MISSING
              )}
            </td>
            <td className={`${TD} text-ink-faint`}>{m.feature_version}</td>
          </tr>
        ))}
      </tbody>
    </DataTable>
  );
}

function ScorePanel({ score }: { score: WatchlistScore }) {
  return (
    <Panel
      className="mt-3"
      title="Watchlist ranking factors"
      subtitle={score.rank ? `rank ${score.rank} · score ${Number(score.overall_score).toFixed(1)} / 100` : "not ranked"}
      bodyClassName=""
    >
      <div className="px-3 pt-2">
        <Notice tone="caution">{score.explanation}</Notice>
      </div>
      <DataTable label="Ranking factors">
        <thead>
          <tr>
            {["Factor", "Weight", "Value", "Score", "Explanation"].map((h) => (
              <th key={h} className={TH} scope="col">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {Object.entries(score.factor_scores).map(([factor, f]) => (
            <tr key={factor} aria-label={factor}>
              <td className={TD}>{humanize(factor)}</td>
              <td className={`${TD} num`}>{f.weight}</td>
              <td className={`${TD} num`}>{f.raw === null ? MISSING : f.raw.toLocaleString("en-US", { maximumSignificantDigits: 6 })}</td>
              <td className={`${TD} num`}>{f.score === null ? <span className="text-ink-faint">not scored</span> : f.score.toFixed(1)}</td>
              <td className={`${TD} text-ink-muted whitespace-normal`}>{f.explanation}</td>
            </tr>
          ))}
        </tbody>
      </DataTable>
    </Panel>
  );
}

function HistoryPanel({ history, error }: { history: AssetHistory | null; error: string | null }) {
  return (
    <div className="mt-3 grid gap-3 xl:grid-cols-2">
      <Panel title="Lifecycle" subtitle="dates are when Atlas observed the change" bodyClassName="">
        {error && <ErrorState message={error} compact />}
        {history && history.lifecycle.length === 0 && <EmptyState title="No lifecycle events" />}
        {history && history.lifecycle.length > 0 && (
          <DataTable label="Lifecycle events">
            <thead>
              <tr>
                {["When", "Event", "Reason"].map((h) => (
                  <th key={h} className={TH} scope="col">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {history.lifecycle.map((e) => (
                <tr key={e.id}>
                  <td className={TD}>
                    <Timestamp iso={e.effective_at} seconds={false} />
                  </td>
                  <td className={TD}>{humanize(e.event_type)}</td>
                  <td className={`${TD} text-ink-muted whitespace-normal`}>{e.reason}</td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        )}
      </Panel>
      <Panel title="Membership history" subtitle="append-only; reconstructable at any past time" bodyClassName="">
        {history && history.membership.length === 0 && <EmptyState title="Never a member of any layer" />}
        {history && history.membership.length > 0 && (
          <DataTable label="Membership history">
            <thead>
              <tr>
                {["Effective", "Layer", "Change", "Reason"].map((h) => (
                  <th key={h} className={TH} scope="col">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {history.membership.map((e) => (
                <tr key={e.id}>
                  <td className={TD}>
                    <Timestamp iso={e.effective_at} seconds={false} />
                  </td>
                  <td className={TD}>{LAYER_LABELS[e.layer]}</td>
                  <td className={TD}>
                    <Badge tone={e.action === "ADDED" ? "positive" : "caution"}>{humanize(e.action)}</Badge>
                  </td>
                  <td className={`${TD} text-ink-muted whitespace-normal`}>{e.reason}</td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        )}
      </Panel>
    </div>
  );
}
