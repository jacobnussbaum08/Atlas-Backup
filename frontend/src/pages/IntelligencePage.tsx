import { Play } from "lucide-react";
import { type FormEvent, useState } from "react";

import {
  CorrelationHeatmap,
  LabelBadge,
  ProbabilityBar,
  RegimeTimeline,
  regimeChanges,
  share,
  signedShare,
} from "../components/intelligence";
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
  Segmented,
  Stat,
  TD,
  TH,
  Timestamp,
} from "../components/ui";
import { type Resource, useApiResource } from "../hooks/useApiResource";
import { formatCount, humanize, MISSING, toNumber } from "../lib/format";
import { ApiError } from "../services/api";
import { intelligence } from "../services/intelligence";
import {
  type AssetSort,
  type Classification,
  type MarketConditions,
  type RunKind,
  STABILITY_LABELS,
  type Timeframe,
  TREND_LABELS,
  VOLATILITY_LABELS,
} from "../types/intelligence";

const TIMEFRAMES: readonly { value: Timeframe; label: string }[] = [
  { value: "1Day", label: "Daily" },
  { value: "1Week", label: "Weekly" },
];
const LABELS = { TREND: TREND_LABELS, VOLATILITY: VOLATILITY_LABELS, STABILITY: STABILITY_LABELS };
const HEATMAP_LIMIT = 20;

export function IntelligencePage() {
  const market = useApiResource((signal) => intelligence.market({ signal }), []);
  const [refresh, setRefresh] = useState(0);
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Market Intelligence"
        description="Measured trend, volatility and stability of the broad US equity market (through its benchmark) and the qualified universe. Probabilities are normalized evidence weights, not forecasts; nothing here is a trading signal."
        actions={
          <RefreshButton
            onClick={() => {
              market.reload();
              setRefresh((n) => n + 1);
            }}
            loading={market.loading}
          />
        }
      />
      <MarketPanel resource={market} />
      <div className="mt-3 grid gap-3 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <HistoryPanel refreshKey={refresh} />
        <RunPanel onStarted={() => setRefresh((n) => n + 1)} />
      </div>
      <div className="mt-3 grid gap-3 2xl:grid-cols-2">
        <RankingPanel refreshKey={refresh} />
        <CorrelationPanel refreshKey={refresh} />
      </div>
    </section>
  );
}

function ClassificationCard({ c, benchmark }: { c: Classification; benchmark: string | null }) {
  const evidence = c.evidence;
  const facts: { label: string; value: string }[] =
    c.dimension === "TREND"
      ? [
          { label: "Direction score", value: toNumber(evidence["direction_score"] as string)?.toFixed(2) ?? MISSING },
          { label: "ADX", value: toNumber(evidence["adx"] as string)?.toFixed(1) ?? MISSING },
          { label: "Trend strength", value: share(evidence["trend_strength"] as string) },
        ]
      : c.dimension === "VOLATILITY"
        ? [
            { label: "Realized (annual)", value: share(evidence["realized_volatility"] as string, 1) },
            { label: "Own-history percentile", value: share(evidence["volatility_percentile"] as string) },
            { label: "Range", value: humanize(String(evidence["range_state"] ?? "")) || MISSING },
          ]
        : [
            { label: "Stress score", value: toNumber(evidence["stress_score"] as string)?.toFixed(2) ?? MISSING },
            {
              label: "Inputs missing",
              value: ((evidence["missing_inputs"] as string[] | undefined) ?? []).map(humanize).join(", ") || "none",
            },
          ];
  return (
    <Panel
      title={humanize(c.dimension)}
      subtitle={c.dimension === "STABILITY" ? "benchmark and universe" : benchmark ?? undefined}
      actions={<LabelBadge label={c.label} title={`confidence ${share(c.confidence)}`} />}
    >
      <ProbabilityBar classification={c} labels={LABELS[c.dimension]} />
      <div className="mt-2">
        <KeyValues items={facts} />
      </div>
      {c.quality !== "OK" && (
        <p className="text-caution mt-2 text-2xs">
          {humanize(c.quality)}
          {c.reasons.length > 0 && `: ${c.reasons.join("; ")}`}
        </p>
      )}
      <p className="text-ink-faint mt-2 text-2xs">
        {c.algorithm} · decision <Timestamp iso={c.as_of} seconds={false} />
      </p>
    </Panel>
  );
}

function MarketPanel({ resource }: { resource: Resource<MarketConditions> }) {
  const [timeframe, setTimeframe] = useState<Timeframe>("1Day");
  if (resource.status === "loading") return <Loading label="Loading market conditions…" />;
  if (resource.status === "error" || !resource.data)
    return <ErrorState message={resource.error ?? "No data"} onRetry={resource.reload} />;
  const m = resource.data;
  if (!m.available)
    return (
      <EmptyState title="No market intelligence yet">
        Start a live run (operator) to compute it from stored market data.
      </EmptyState>
    );
  const shown = m.classifications.filter((c) => c.timeframe === timeframe);
  const p = m.participation;
  const metrics = p?.metrics ?? {};
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-ink-muted text-xs">
          As of <Timestamp iso={m.as_of ?? ""} seconds={false} /> ·{" "}
          <Badge tone={m.run?.basis === "RECORDED" ? "positive" : "caution"}>{humanize(m.run?.basis ?? "")}</Badge>{" "}
          {m.run?.status !== "COMPLETED" && m.run && <Badge tone="caution">{humanize(m.run.status)}</Badge>}
        </p>
        <Segmented options={TIMEFRAMES} value={timeframe} onChange={setTimeframe} label="Timeframe" />
      </div>
      {m.run?.error && <Notice tone="caution">{m.run.error}</Notice>}
      <div className="grid gap-3 lg:grid-cols-3" aria-label="Broad market conditions">
        {shown.map((c) => (
          <ClassificationCard key={`${c.timeframe}-${c.dimension}`} c={c} benchmark={m.benchmark_symbol} />
        ))}
        {timeframe === "1Week" && (
          <Notice>Market stability is assessed on daily data only (it uses daily participation and correlation).</Notice>
        )}
      </div>
      <Panel title="Universe participation" subtitle={p ? `${p.members_evaluated} of ${p.members_total} qualified members` : undefined}>
        {!p || p.status !== "OK" ? (
          <Notice tone="caution">
            Not enough qualified-universe members with usable history to report participation
            {p ? ` (${p.members_evaluated} of ${p.members_total})` : ""}.
          </Notice>
        ) : (
          <div className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8">
            <Stat label="Above slow avg" value={share(metrics["above_slow_ma"])} hint={`change ${signedShare(metrics["above_slow_ma_change"], 0)}`} />
            <Stat label="Above fast avg" value={share(metrics["above_fast_ma"])} />
            <Stat label="Positive momentum" value={share(metrics["positive_momentum"])} />
            <Stat
              label="Advancing / declining"
              value={`${formatCount(metrics["advancers"] as number)} / ${formatCount(metrics["decliners"] as number)}`}
              hint={`latest session · ${formatCount(metrics["stale_excluded"] as number)} stale excluded`}
            />
            <Stat label="Bullish trends" value={share(metrics["bullish_share"])} tone="positive" />
            <Stat label="Bearish trends" value={share(metrics["bearish_share"])} tone="negative" />
            <Stat label="New highs / lows" value={`${formatCount(metrics["new_highs"] as number)} / ${formatCount(metrics["new_lows"] as number)}`} />
            <Stat
              label="Move concentration"
              value={share(metrics["concentration_top_share"])}
              hint={`top ${formatCount(metrics["concentration_top_count"] as number)} of absolute moves`}
            />
          </div>
        )}
        <ul className="mt-2 space-y-1">
          {(p?.limitations ?? []).map((text) => (
            <li key={text}>
              <Notice tone="info">{text}</Notice>
            </li>
          ))}
        </ul>
      </Panel>
      {m.notes.map((note) => (
        <Notice key={note}>{note}</Notice>
      ))}
    </div>
  );
}

function HistoryPanel({ refreshKey }: { refreshKey: number }) {
  const history = useApiResource((signal) => intelligence.history("1Day", { signal }), [refreshKey], {
    isEmpty: (h) => h.points.length === 0,
  });
  const changes = history.data ? regimeChanges(history.data.points).slice(0, 12) : [];
  return (
    <Panel title="Market regime history" subtitle="daily decision times, last 180 days">
      {history.status === "loading" && <Loading />}
      {history.status === "error" && <ErrorState message={history.error ?? ""} onRetry={history.reload} />}
      {history.status === "empty" && (
        <EmptyState title="No history">Runs (live or reconstruction) add decision times here.</EmptyState>
      )}
      {history.data && history.data.points.length > 0 && (
        <>
          <RegimeTimeline points={history.data.points} />
          <p className="text-ink-faint mt-1 text-2xs">
            {history.data.points.length} decision times · hatched = reconstructed from data retrieved later
          </p>
          <h3 className="text-ink-muted mt-3 mb-1 text-2xs font-semibold uppercase">Regime changes</h3>
          {changes.length === 0 ? (
            <p className="text-ink-faint text-xs">No label changes in this period.</p>
          ) : (
            <DataTable label="Regime changes">
              <thead>
                <tr>
                  <th className={TH}>Decision</th>
                  <th className={TH}>Dimension</th>
                  <th className={TH}>Change</th>
                  <th className={TH}>Basis</th>
                </tr>
              </thead>
              <tbody>
                {changes.map((c) => (
                  <tr key={`${c.as_of}-${c.dimension}`}>
                    <td className={TD}>
                      <Timestamp iso={c.as_of} seconds={false} />
                    </td>
                    <td className={TD}>{humanize(c.dimension)}</td>
                    <td className={TD}>
                      <LabelBadge label={c.from} /> → <LabelBadge label={c.to} />
                    </td>
                    <td className={TD}>{humanize(c.basis)}</td>
                  </tr>
                ))}
              </tbody>
            </DataTable>
          )}
        </>
      )}
    </Panel>
  );
}

const SORTS: readonly { value: AssetSort; label: string }[] = [
  { value: "relative_strength", label: "Relative strength" },
  { value: "roc", label: "Momentum" },
  { value: "realized_volatility", label: "Volatility" },
  { value: "adx", label: "ADX" },
  { value: "symbol", label: "Symbol" },
];

function RankingPanel({ refreshKey }: { refreshKey: number }) {
  const [timeframe, setTimeframe] = useState<Timeframe>("1Day");
  const [sort, setSort] = useState<AssetSort>("relative_strength");
  const assets = useApiResource(
    (signal) => intelligence.assets({ timeframe, sort, limit: 200 }, { signal }),
    [timeframe, sort, refreshKey],
    { isEmpty: (list) => list.items.length === 0 },
  );
  return (
    <Panel
      title="Relative strength and conditions"
      subtitle="qualified universe and benchmark"
      actions={
        <>
          <Segmented options={TIMEFRAMES} value={timeframe} onChange={setTimeframe} label="Ranking timeframe" />
          <select
            aria-label="Sort by"
            className="border-border-strong bg-panel-raised h-7 rounded-sm border px-1 text-xs"
            value={sort}
            onChange={(e) => setSort(e.target.value as AssetSort)}
          >
            {SORTS.map((s) => (
              <option key={s.value} value={s.value}>
                Sort: {s.label}
              </option>
            ))}
          </select>
        </>
      }
      bodyClassName="p-0"
    >
      {assets.status === "loading" && <Loading />}
      {assets.status === "error" && <ErrorState message={assets.error ?? ""} onRetry={assets.reload} />}
      {assets.status === "empty" && <EmptyState title="No instruments">No live run has measured any yet.</EmptyState>}
      {assets.data && assets.data.items.length > 0 && (
        <div className="max-h-[32rem] overflow-y-auto">
          <DataTable label="Instrument conditions">
            <thead>
              <tr>
                <th className={TH}>Symbol</th>
                <th className={TH}>Rel. strength</th>
                <th className={TH}>Momentum</th>
                <th className={TH}>Trend</th>
                <th className={TH}>Volatility</th>
                <th className={TH}>Realized vol</th>
                <th className={TH}>ADX</th>
                <th className={TH}>Data</th>
              </tr>
            </thead>
            <tbody>
              {assets.data.items.map((item) => (
                <tr key={item.asset_id}>
                  <td className={`${TD} font-medium`}>{item.symbol}</td>
                  <td className={`${TD} num`}>{signedShare(item.features["relative_strength"])}</td>
                  <td className={`${TD} num`}>{signedShare(item.features["roc"])}</td>
                  <td className={TD}>
                    {item.trend ? <LabelBadge label={item.trend.label} title={`confidence ${share(item.trend.confidence)}`} /> : MISSING}
                  </td>
                  <td className={TD}>{item.volatility ? <LabelBadge label={item.volatility.label} /> : MISSING}</td>
                  <td className={`${TD} num`}>{share(item.features["realized_volatility"], 1)}</td>
                  <td className={`${TD} num`}>{toNumber(item.features["adx"])?.toFixed(1) ?? MISSING}</td>
                  <td className={TD}>
                    <Badge
                      tone={item.quality === "OK" ? "positive" : item.quality === "PARTIAL" ? "caution" : "neutral"}
                      title={item.notes.join("; ") || `${item.bars_used} of ${item.expected_bars} bars`}
                    >
                      {humanize(item.quality)}
                    </Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        </div>
      )}
      {assets.data?.notes.map((n) => (
        <p key={n} className="text-ink-faint px-3 py-1 text-2xs">
          {n}
        </p>
      ))}
    </Panel>
  );
}

function CorrelationPanel({ refreshKey }: { refreshKey: number }) {
  const data = useApiResource((signal) => intelligence.correlations({ signal }), [refreshKey], {
    isEmpty: (c) => c.cells.length === 0,
  });
  const c = data.data;
  const symbols = c ? c.symbols.slice(0, HEATMAP_LIMIT) : [];
  return (
    <Panel
      title="Cross-asset correlation"
      subtitle={c?.window ? `daily log returns, ${c.window} sessions` : undefined}
    >
      {data.status === "loading" && <Loading />}
      {data.status === "error" && <ErrorState message={data.error ?? ""} onRetry={data.reload} />}
      {data.status === "empty" && <EmptyState title="No correlations">A live run with at least two qualified members computes them.</EmptyState>}
      {c && c.cells.length > 0 && (
        <>
          <div className="mb-2 grid grid-cols-2 gap-2 md:grid-cols-3">
            <Stat label="Average correlation" value={toNumber(c.summary["average_correlation"] as string)?.toFixed(2) ?? MISSING} />
            <Stat
              label="Common exposure"
              value={share(c.summary["absorption_ratio"] as string)}
              hint={`first component, ${formatCount(c.summary["absorption_assets"] as number)} instruments`}
            />
            <Stat
              label="Pairs"
              value={`${formatCount(c.summary["pairs_computed"] as number)}`}
              hint={`${formatCount(c.summary["pairs_insufficient"] as number)} without enough overlap`}
            />
          </div>
          <CorrelationHeatmap symbols={symbols} cells={c.cells} />
          {c.symbols.length > HEATMAP_LIMIT && (
            <p className="text-ink-faint mt-1 text-2xs">
              Showing {HEATMAP_LIMIT} of {c.symbols.length} instruments (alphabetical).
            </p>
          )}
          <ul className="mt-2 space-y-1">
            {c.notes.map((n) => (
              <li key={n} className="text-ink-faint text-2xs">
                {n}
              </li>
            ))}
          </ul>
        </>
      )}
    </Panel>
  );
}

function RunPanel({ onStarted }: { onStarted: () => void }) {
  const [kind, setKind] = useState<RunKind>("LIVE");
  const [sessions, setSessions] = useState("60");
  const [token, setToken] = useState("");
  const [message, setMessage] = useState<{ tone: "positive" | "negative"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const runs = useApiResource((signal) => intelligence.runs({ signal }), [message?.text]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const body = kind === "LIVE" ? { kind } : { kind, sessions: Number(sessions) };
      const run = await intelligence.start(body, token || undefined);
      setMessage({ tone: "positive", text: `${humanize(run.kind)} run started (${run.id.slice(0, 8)}).` });
      onStarted();
    } catch (error) {
      setMessage({ tone: "negative", text: error instanceof ApiError ? error.message : String(error) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel title="Runs">
      <form onSubmit={submit} className="space-y-2">
        <Segmented
          options={[
            { value: "LIVE" as RunKind, label: "Live" },
            { value: "RECONSTRUCTION" as RunKind, label: "Reconstruct" },
          ]}
          value={kind}
          onChange={setKind}
          label="Run kind"
        />
        {kind === "RECONSTRUCTION" && (
          <label className="text-ink-muted block text-2xs">
            Past sessions
            <input
              type="number"
              min={1}
              max={750}
              value={sessions}
              onChange={(e) => setSessions(e.target.value)}
              className="border-border-strong bg-panel-raised mt-0.5 block h-7 w-full rounded-sm border px-2 text-xs"
            />
          </label>
        )}
        <label className="text-ink-muted block text-2xs">
          Operator token
          <input
            type="password"
            autoComplete="off"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            className="border-border-strong bg-panel-raised mt-0.5 block h-7 w-full rounded-sm border px-2 text-xs"
          />
        </label>
        <Button type="submit" variant="primary" disabled={busy}>
          <Play className="size-3.5" aria-hidden /> Start run
        </Button>
        <p className="text-ink-faint text-2xs">
          Live: re-retrieves daily history (read-only market data), then measures now. Reconstruct: recomputes
          past decision times from data retrieved later (labelled as such). No orders are involved.
        </p>
      </form>
      {message && (
        <div className="mt-2">
          <Notice tone={message.tone}>{message.text}</Notice>
        </div>
      )}
      <ul className="mt-3 space-y-1 text-xs" aria-label="Recent runs">
        {(runs.data ?? []).map((run) => (
          <li key={run.id} className="flex items-center justify-between gap-2">
            <span>
              {humanize(run.kind)} · <Timestamp iso={run.as_of_end} seconds={false} />
            </span>
            <Badge tone={run.status === "COMPLETED" ? "positive" : run.status === "RUNNING" ? "info" : run.status === "FAILED" ? "negative" : "caution"}>
              {humanize(run.status)}
            </Badge>
          </li>
        ))}
      </ul>
    </Panel>
  );
}
