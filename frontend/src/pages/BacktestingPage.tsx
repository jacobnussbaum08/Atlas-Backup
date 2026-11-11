import { FlaskConical, GitCompare, Send } from "lucide-react";
import { type FormEvent, useMemo, useState } from "react";
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
  TD,
  TH,
  Timestamp,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { formatRatio, METRIC_STATUS_LABEL, STATUS_TONE } from "../lib/backtest";
import { MISSING } from "../lib/format";
import { formatDecimal } from "../lib/money";
import { ApiError } from "../services/api";
import { backtests } from "../services/backtests";
import type { BacktestOptions, BacktestRequestBody, BacktestRun, CompareResult } from "../types/backtests";

const input =
  "border-border-strong bg-panel-raised text-ink h-7 w-full rounded-sm border px-2 text-xs focus:border-accent focus:outline-none";
const label = "text-ink-muted block text-2xs font-semibold uppercase";

export function BacktestingPage() {
  const runs = useApiResource((signal) => backtests.list({ signal }), [], {
    refreshMs: 5_000,
    isEmpty: (rows) => rows.length === 0,
  });
  const options = useApiResource((signal) => backtests.options({ signal }), []);
  const [selected, setSelected] = useState<string[]>([]);
  const toggle = (id: string) =>
    setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id].slice(-8)));

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Backtesting"
        description="Event-driven replays of stored historical data through Atlas's own strategies, risk rules, simulated execution and accounting. Simulated results over past data are not a forecast."
        actions={<RefreshButton onClick={runs.reload} loading={runs.loading} />}
      />
      <div className="mb-3">
        <Notice tone="caution">
          In-sample results only: a profitable backtest is not evidence of future performance. Every figure is a
          simulation with documented costs and assumptions (IEX daily bars, no historical quotes).
        </Notice>
      </div>
      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_24rem]">
        <div className="flex min-w-0 flex-col gap-3">
          <Panel title="Runs" subtitle="newest first">
            {runs.status === "loading" && <Loading />}
            {runs.status === "error" && <ErrorState message={runs.error ?? ""} onRetry={runs.reload} />}
            {runs.status === "empty" && (
              <EmptyState title="No backtest has been run">Configure one with the form.</EmptyState>
            )}
            {runs.data && runs.data.length > 0 && (
              <RunsTable runs={runs.data} selected={selected} onToggle={toggle} />
            )}
          </Panel>
          {selected.length >= 2 && <ComparePanel ids={selected} />}
        </div>
        <Panel title="Configure a backtest">
          {options.status === "loading" && <Loading />}
          {options.status === "error" && <ErrorState message={options.error ?? ""} onRetry={options.reload} />}
          {options.data && <RunForm options={options.data} onCreated={runs.reload} />}
        </Panel>
      </div>
    </section>
  );
}

function RunsTable({
  runs,
  selected,
  onToggle,
}: {
  runs: BacktestRun[];
  selected: string[];
  onToggle: (id: string) => void;
}) {
  return (
    <DataTable label="Backtest runs">
      <thead>
        <tr>
          <th className={TH}>
            <span className="sr-only">Compare</span>
          </th>
          <th className={TH}>Run</th>
          <th className={TH}>Status</th>
          <th className={TH}>Period</th>
          <th className={TH}>Strategies</th>
          <th className={`${TH} text-right`}>Final equity</th>
          <th className={`${TH} text-right`}>Return (net)</th>
          <th className={`${TH} text-right`}>Trades</th>
          <th className={TH}>Created</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => {
          const summary = run.summary ?? {};
          return (
            <tr key={run.id}>
              <td className={TD}>
                <input
                  type="checkbox"
                  aria-label={`Compare ${run.name}`}
                  checked={selected.includes(run.id)}
                  disabled={run.status !== "COMPLETED"}
                  onChange={() => onToggle(run.id)}
                />
              </td>
              <td className={`${TD} max-w-72 truncate`}>
                <Link to={`/backtesting/${run.id}`} className="text-accent hover:underline">
                  {run.name}
                </Link>
                {run.reproduction_of && (
                  <Badge tone={run.reproduction_matches ? "positive" : "caution"}>
                    {run.reproduction_matches === null
                      ? "reproduction"
                      : run.reproduction_matches
                        ? "reproduced exactly"
                        : "reproduction differs"}
                  </Badge>
                )}
              </td>
              <td className={TD}>
                <Badge tone={STATUS_TONE[run.status]}>{run.status}</Badge>
                {run.status === "RUNNING" && run.progress !== null && (
                  <span className="num text-ink-muted ml-2">{Math.round(run.progress * 100)}%</span>
                )}
              </td>
              <td className={`${TD} num`}>
                {run.start_date} → {run.end_date}
              </td>
              <td className={`${TD} max-w-56 truncate`} title={run.strategies.join(", ")}>
                {run.strategies.join(", ")}
              </td>
              <td className={`${TD} num text-right`}>
                {formatDecimal((summary.final_equity as string | undefined) ?? null, { places: 2 })}
              </td>
              <td className={`${TD} num text-right`}>
                {formatRatio((summary.total_return as string | undefined) ?? null)}
              </td>
              <td className={`${TD} num text-right`}>{(summary.trades as number | undefined) ?? MISSING}</td>
              <td className={TD}>
                <Timestamp iso={run.created_at} seconds={false} />
              </td>
            </tr>
          );
        })}
      </tbody>
    </DataTable>
  );
}

function ComparePanel({ ids }: { ids: string[] }) {
  const compare = useApiResource((signal) => backtests.compare(ids, { signal }), [ids.join(",")]);
  return (
    <Panel
      title={
        <span className="inline-flex items-center gap-1">
          <GitCompare aria-hidden className="size-3" /> Compare runs
        </span>
      }
      subtitle="the same metric definitions for every run"
    >
      {compare.status === "loading" && <Loading />}
      {compare.status === "error" && <ErrorState message={compare.error ?? ""} />}
      {compare.data && <CompareTable data={compare.data} />}
    </Panel>
  );
}

function CompareTable({ data }: { data: CompareResult }) {
  return (
    <DataTable label="Run comparison">
      <thead>
        <tr>
          <th className={TH}>Metric</th>
          {data.runs.map((r) => (
            <th key={r.run.id} className={`${TH} text-right`}>
              {r.run.name}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {data.metrics.map((metric) => (
          <tr key={metric}>
            <td className={TD}>{metric.replaceAll("_", " ")}</td>
            {data.runs.map((r) => {
              const m = r.portfolio[metric];
              const ratio = /return|drawdown|rate/.test(metric);
              return (
                <td key={r.run.id} className={`${TD} num text-right`}>
                  {!m || m.value === null
                    ? MISSING
                    : ratio
                      ? formatRatio(m.value, { signed: /return/.test(metric) })
                      : formatDecimal(m.value, { places: metric === "trades_closed" ? 0 : 2 })}
                  {m && m.status !== "OK" && (
                    <span className="text-ink-faint ml-1 text-2xs">{METRIC_STATUS_LABEL[m.status]}</span>
                  )}
                </td>
              );
            })}
          </tr>
        ))}
        <tr>
          <td className={TD}>benchmark excess return</td>
          {data.runs.map((r) => (
            <td key={r.run.id} className={`${TD} num text-right`}>
              {formatRatio(r.benchmarks.find((b) => b.kind === "BENCHMARK")?.excess_return ?? null)}
            </td>
          ))}
        </tr>
      </tbody>
    </DataTable>
  );
}

function RunForm({ options, onCreated }: { options: BacktestOptions; onCreated: () => void }) {
  const navigate = useNavigate();
  const replayable = options.strategies.filter((s) => s.replayable);
  const withData = options.assets.filter((a) => a.raw !== null);
  const latest = withData.reduce((m, a) => (a.raw && a.raw.last > m ? a.raw.last : m), "");
  const earliest = withData.reduce((m, a) => (a.raw && (m === "" || a.raw.first < m) ? a.raw.first : m), "");
  const [values, setValues] = useState({
    name: "",
    start: earliest,
    end: latest,
    capital: "100000",
    symbols: "",
    mode: "EXPLICIT" as "EXPLICIT" | "POINT_IN_TIME",
    layer: "QUALIFIED_UNIVERSE",
    benchmark: "SPY",
    entry: "LIMIT",
    stop: "STOP",
    trail: "",
    seed: "0",
  });
  const [strategies, setStrategies] = useState<string[]>(replayable.map((s) => s.strategy_id));
  const [timeframes, setTimeframes] = useState<string[]>(["1Day"]);
  const [directions, setDirections] = useState<string[]>(["LONG", "SHORT"]);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (key: keyof typeof values) => (e: { target: { value: string } }) =>
    setValues((v) => ({ ...v, [key]: e.target.value }));
  const flip = (list: string[], value: string) =>
    list.includes(value) ? list.filter((x) => x !== value) : [...list, value];

  const symbols = useMemo(
    () =>
      Array.from(
        new Set(
          values.symbols
            .split(/[\s,]+/)
            .map((s) => s.trim().toUpperCase())
            .filter(Boolean),
        ),
      ),
    [values.symbols],
  );
  const problem =
    strategies.length === 0
      ? "Select at least one strategy."
      : timeframes.length === 0
        ? "Select at least one timeframe."
        : directions.length === 0
          ? "Select at least one direction."
          : values.mode === "EXPLICIT" && symbols.length === 0
            ? "Enter at least one symbol."
            : !values.start || !values.end || values.end < values.start
              ? "Choose a valid date range."
              : values.stop === "TRAILING_STOP" && !values.trail
                ? "A trailing stop needs a trail percentage."
                : null;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (problem) return;
    const body: BacktestRequestBody = {
      name: values.name.trim() || `${strategies.join("+")} ${values.start}→${values.end}`,
      start_date: values.start,
      end_date: values.end,
      initial_capital: values.capital,
      universe:
        values.mode === "EXPLICIT" ? { mode: "EXPLICIT", symbols } : { mode: "POINT_IN_TIME", layer: values.layer },
      strategies: strategies.map((strategy_id) => ({ strategy_id })),
      timeframes,
      directions,
      benchmark_symbol: values.benchmark.trim() ? values.benchmark.trim().toUpperCase() : null,
      execution: {
        entry_order_type: values.entry,
        stop_order_type: values.stop,
        ...(values.stop === "TRAILING_STOP" ? { trail_percent: values.trail } : {}),
      },
      seed: Number(values.seed) || 0,
    };
    setBusy(true);
    setError(null);
    try {
      const created = await backtests.create(body, token || undefined);
      onCreated();
      void navigate(`/backtesting/${created.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={(e) => void submit(e)} className="flex flex-col gap-3" aria-label="Backtest configuration">
      <label className={label}>
        Name
        <input value={values.name} onChange={set("name")} className={`${input} mt-1`} aria-label="Run name" />
      </label>
      <div className="grid grid-cols-2 gap-2">
        <label className={label}>
          Start
          <input type="date" value={values.start} onChange={set("start")} className={`${input} num mt-1`} aria-label="Start date" />
        </label>
        <label className={label}>
          End
          <input type="date" value={values.end} onChange={set("end")} className={`${input} num mt-1`} aria-label="End date" />
        </label>
      </div>
      <label className={label}>
        Initial capital (USD)
        <input value={values.capital} onChange={set("capital")} className={`${input} num mt-1`} aria-label="Initial capital" />
      </label>
      <fieldset>
        <legend className={label}>Strategies (latest stored versions)</legend>
        <div className="mt-1 grid grid-cols-2 gap-1 text-xs">
          {options.strategies.map((s) => (
            <label key={s.strategy_id} className="flex items-center gap-1.5" title={s.replayable ? s.logic_version : "no replayable stored version"}>
              <input
                type="checkbox"
                disabled={!s.replayable}
                checked={strategies.includes(s.strategy_id)}
                onChange={() => setStrategies((l) => flip(l, s.strategy_id))}
              />
              {s.name} {s.latest_version !== null && <span className="text-ink-faint num">v{s.latest_version}</span>}
            </label>
          ))}
        </div>
      </fieldset>
      <div className="grid grid-cols-2 gap-2">
        <fieldset>
          <legend className={label}>Timeframes</legend>
          {options.timeframes.map((t) => (
            <label key={t} className="mr-3 inline-flex items-center gap-1 text-xs">
              <input type="checkbox" checked={timeframes.includes(t)} onChange={() => setTimeframes((l) => flip(l, t))} />
              {t}
            </label>
          ))}
        </fieldset>
        <fieldset>
          <legend className={label}>Directions</legend>
          {["LONG", "SHORT"].map((d) => (
            <label key={d} className="mr-3 inline-flex items-center gap-1 text-xs">
              <input type="checkbox" checked={directions.includes(d)} onChange={() => setDirections((l) => flip(l, d))} />
              {d}
            </label>
          ))}
        </fieldset>
      </div>
      <label className={label}>
        Universe
        <select value={values.mode} onChange={set("mode")} className={`${input} mt-1`} aria-label="Universe mode">
          <option value="EXPLICIT">Explicit symbols (chosen today: may carry selection bias)</option>
          <option value="POINT_IN_TIME">Point-in-time universe layer</option>
        </select>
      </label>
      {values.mode === "EXPLICIT" ? (
        <label className={label}>
          Symbols
          <textarea
            value={values.symbols}
            onChange={set("symbols")}
            rows={2}
            className={`${input} num mt-1 h-auto py-1`}
            aria-label="Symbols"
            placeholder={withData.slice(0, 6).map((a) => a.symbol).join(", ")}
          />
          <span className="text-ink-faint mt-0.5 block font-normal normal-case">
            {withData.length} assets have stored daily bars ({options.data.feed.toUpperCase()}).
          </span>
        </label>
      ) : (
        <label className={label}>
          Layer
          <select value={values.layer} onChange={set("layer")} className={`${input} mt-1`} aria-label="Universe layer">
            {options.layers.map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
        </label>
      )}
      <div className="grid grid-cols-2 gap-2">
        <label className={label}>
          Entry orders
          <select value={values.entry} onChange={set("entry")} className={`${input} mt-1`} aria-label="Entry order type">
            {["LIMIT", "MARKET", "STOP", "STOP_LIMIT"].map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
        </label>
        <label className={label}>
          Protective stop
          <select value={values.stop} onChange={set("stop")} className={`${input} mt-1`} aria-label="Stop order type">
            <option value="STOP">STOP</option>
            <option value="TRAILING_STOP">TRAILING_STOP</option>
          </select>
        </label>
      </div>
      {values.stop === "TRAILING_STOP" && (
        <label className={label}>
          Trail (%)
          <input value={values.trail} onChange={set("trail")} className={`${input} num mt-1`} aria-label="Trail percent" />
        </label>
      )}
      <div className="grid grid-cols-2 gap-2">
        <label className={label}>
          Benchmark
          <input value={values.benchmark} onChange={set("benchmark")} className={`${input} num mt-1`} aria-label="Benchmark symbol" />
        </label>
        <label className={label}>
          Seed
          <input value={values.seed} onChange={set("seed")} className={`${input} num mt-1`} aria-label="Seed" />
        </label>
      </div>
      <label className={label}>
        Operator token
        <input
          type="password"
          autoComplete="off"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          className={`${input} mt-1`}
          aria-label="Operator token"
        />
        <span className="text-ink-faint mt-0.5 block font-normal normal-case">
          Required when the server sets one. Kept in memory only.
        </span>
      </label>
      {problem && <p className="text-caution text-2xs">{problem}</p>}
      {error && <ErrorState message={error} compact />}
      <Button type="submit" variant="primary" disabled={busy || problem !== null}>
        {busy ? <FlaskConical aria-hidden className="size-3.5 animate-pulse" /> : <Send aria-hidden className="size-3.5" />}
        {busy ? "Submitting…" : "Run backtest"}
      </Button>
      <p className="text-ink-faint text-2xs">
        Risk limits and execution costs are the stored 6B configuration and the 7B execution model; the run records
        both. Risk overrides and cost overrides are available through the API.
      </p>
    </form>
  );
}
