import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import { type ReactNode, useMemo, useState } from "react";
import { useNavigate } from "react-router";

import { direction, formatAge, formatPercent, formatPrice, formatSigned, MISSING } from "../../lib/format";
import type { AssetSnapshot } from "../../types/marketData";
import { FeedBadge, FreshnessBadge, QualityBadge, SourceBadge } from "../status";
import { DataTable, TD, TH, Timestamp } from "../ui";

export type ColumnKey =
  | "symbol"
  | "name"
  | "asset_class"
  | "exchange"
  | "last"
  | "change"
  | "change_percent"
  | "previous_close"
  | "latest_event"
  | "source"
  | "feed"
  | "freshness"
  | "quality"
  | "gaps";

interface Column {
  key: ColumnKey;
  label: string;
  numeric?: boolean;
  sortValue?: (s: AssetSnapshot) => number | string | null;
  render: (s: AssetSnapshot, now: number) => ReactNode;
  /** Cannot be hidden. */
  fixed?: boolean;
}

function changeClass(value: string | null): string {
  const d = direction(value);
  return d === "up" ? "text-positive" : d === "down" ? "text-negative" : "text-ink-muted";
}

export const COLUMNS: readonly Column[] = [
  {
    key: "symbol",
    label: "Symbol",
    fixed: true,
    sortValue: (s) => s.asset.symbol,
    render: (s) => <span className="font-semibold">{s.asset.symbol}</span>,
  },
  {
    key: "name",
    label: "Name",
    sortValue: (s) => s.asset.name ?? "",
    render: (s) => <span className="text-ink-muted">{s.asset.name ?? MISSING}</span>,
  },
  {
    key: "asset_class",
    label: "Class",
    sortValue: (s) => s.asset.asset_class,
    render: (s) => <span className="text-ink-muted">{s.asset.asset_class}</span>,
  },
  {
    key: "exchange",
    label: "Exchange",
    sortValue: (s) => s.asset.exchange_code ?? "",
    render: (s) => <span className="num text-ink-muted">{s.asset.exchange_code ?? MISSING}</span>,
  },
  {
    key: "last",
    label: "Last",
    numeric: true,
    sortValue: (s) => (s.last_price ? Number(s.last_price.value) : null),
    render: (s) =>
      s.last_price ? (
        <span
          className="num font-medium"
          title={`${s.last_price.basis === "TRADE" ? "Last trade" : "1-minute bar close"} · ${s.feed.toUpperCase()} · ${s.last_price.source}`}
        >
          {formatPrice(s.last_price.value)}
        </span>
      ) : (
        <span className="text-ink-faint">{MISSING}</span>
      ),
  },
  {
    key: "change",
    label: "Chg",
    numeric: true,
    sortValue: (s) => (s.change === null ? null : Number(s.change)),
    render: (s) => (
      <span className={`num ${changeClass(s.change)}`} title={s.previous_close_note ?? undefined}>
        {formatSigned(s.change)}
      </span>
    ),
  },
  {
    key: "change_percent",
    label: "Chg %",
    numeric: true,
    sortValue: (s) => (s.change_percent === null ? null : Number(s.change_percent)),
    render: (s) => (
      <span className={`num ${changeClass(s.change_percent)}`} title={s.previous_close_note ?? undefined}>
        {formatPercent(s.change_percent)}
      </span>
    ),
  },
  {
    key: "previous_close",
    label: "Prev close",
    numeric: true,
    sortValue: (s) => (s.previous_close ? Number(s.previous_close.value) : null),
    render: (s) =>
      s.previous_close ? (
        <span className="num text-ink-muted" title={s.previous_close.basis}>
          {formatPrice(s.previous_close.value)}
        </span>
      ) : (
        <span className="text-ink-faint" title={s.previous_close_note ?? undefined}>
          {MISSING}
        </span>
      ),
  },
  {
    key: "latest_event",
    label: "Latest event",
    sortValue: (s) => (s.latest_event_at ? Date.parse(s.latest_event_at) : null),
    render: (s, now) => (
      <span className="flex flex-col leading-tight">
        <Timestamp iso={s.latest_event_at} />
        <span className="text-ink-faint text-2xs">{formatAge(s.latest_event_at, now)}</span>
      </span>
    ),
  },
  {
    key: "source",
    label: "Source",
    sortValue: (s) => s.last_price?.source ?? "",
    render: (s) => <SourceBadge source={s.last_price?.source} />,
  },
  {
    key: "feed",
    label: "Feed",
    sortValue: (s) => s.feed,
    render: (s) => <FeedBadge feed={s.feed} coverage={s.feed_coverage} />,
  },
  {
    key: "freshness",
    label: "Freshness",
    sortValue: (s) => s.freshness,
    render: (s) => <FreshnessBadge freshness={s.freshness} />,
  },
  {
    key: "quality",
    label: "Quality",
    sortValue: (s) => s.quality_status ?? "",
    render: (s) => {
      const warnings = (s.quality_events_24h.WARNING ?? 0) + (s.quality_events_24h.ERROR ?? 0) + (s.quality_events_24h.CRITICAL ?? 0);
      return (
        <span className="flex items-center gap-1.5">
          <QualityBadge status={s.quality_status} />
          {warnings > 0 && (
            <span className="num text-caution text-2xs" title="Warning, error and critical quality events in the last 24 hours">
              {warnings} evt
            </span>
          )}
        </span>
      );
    },
  },
  {
    key: "gaps",
    label: "Open gaps",
    numeric: true,
    sortValue: (s) => s.open_gaps,
    render: (s) => (
      <span className={`num ${s.open_gaps > 0 ? "text-caution" : "text-ink-faint"}`}>{s.open_gaps}</span>
    ),
  },
];

export const DEFAULT_COLUMNS: readonly ColumnKey[] = [
  "symbol",
  "name",
  "asset_class",
  "exchange",
  "last",
  "change",
  "change_percent",
  "latest_event",
  "feed",
  "freshness",
  "quality",
];

type Sort = { key: ColumnKey; dir: "asc" | "desc" };

export function sortSnapshots(rows: readonly AssetSnapshot[], sort: Sort): AssetSnapshot[] {
  const column = COLUMNS.find((c) => c.key === sort.key);
  if (!column?.sortValue) return [...rows];
  const value = column.sortValue;
  return [...rows].sort((a, b) => {
    const va = value(a);
    const vb = value(b);
    // Missing values always sort last, whatever the direction.
    if (va === null && vb === null) return 0;
    if (va === null) return 1;
    if (vb === null) return -1;
    const cmp = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
    return sort.dir === "asc" ? cmp : -cmp;
  });
}

export function AssetTable({
  rows,
  columns,
  now,
  label = "Monitored assets",
}: {
  rows: readonly AssetSnapshot[];
  columns: readonly ColumnKey[];
  now: number;
  label?: string;
}) {
  const [sort, setSort] = useState<Sort>({ key: "symbol", dir: "asc" });
  const navigate = useNavigate();
  const sorted = useMemo(() => sortSnapshots(rows, sort), [rows, sort]);
  const visible = COLUMNS.filter((c) => columns.includes(c.key));

  const toggle = (key: ColumnKey) =>
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));

  return (
    <DataTable label={label}>
      <thead>
        <tr>
          {visible.map((c) => {
            const active = sort.key === c.key;
            const Icon = active ? (sort.dir === "asc" ? ArrowUp : ArrowDown) : ArrowUpDown;
            return (
              <th
                key={c.key}
                scope="col"
                aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
                className={`${TH} ${c.numeric ? "text-right" : ""}`}
              >
                <button
                  type="button"
                  onClick={() => toggle(c.key)}
                  className={`hover:text-ink inline-flex items-center gap-1 uppercase ${active ? "text-ink" : ""}`}
                >
                  {c.label}
                  <Icon aria-hidden className={`size-3 ${active ? "" : "opacity-40"}`} />
                </button>
              </th>
            );
          })}
        </tr>
      </thead>
      <tbody>
        {sorted.map((s) => (
          <tr
            key={s.asset.asset_id}
            onClick={() => navigate(`/markets/${encodeURIComponent(s.asset.symbol)}`)}
            onKeyDown={(event) => {
              if (event.key === "Enter") navigate(`/markets/${encodeURIComponent(s.asset.symbol)}`);
            }}
            tabIndex={0}
            aria-label={`Open ${s.asset.symbol}`}
            className="hover:bg-panel-raised focus:bg-panel-raised cursor-pointer"
          >
            {visible.map((c) => (
              <td key={c.key} className={`${TD} ${c.numeric ? "text-right" : ""}`}>
                {c.render(s, now)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </DataTable>
  );
}
