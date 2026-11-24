import { Lock } from "lucide-react";
import { NavLink, Outlet, useOutletContext } from "react-router";

import { PaperModeBadge } from "../components/PaperModeBadge";
import { streamTone } from "../components/status";
import { SystemStatusIndicator } from "../components/SystemStatusIndicator";
import { Badge, StatusDot } from "../components/ui";
import { type Resource, useApiResource } from "../hooks/useApiResource";
import { useNow } from "../hooks/useNow";
import { type SystemStatus, useSystemStatus } from "../hooks/useSystemStatus";
import { formatClock, formatDateTime, humanize } from "../lib/format";
import { useDisplayZone } from "../lib/time";
import { NAV_SECTIONS, UPCOMING_SECTIONS } from "../navigation";
import { marketData } from "../services/marketData";
import type { MarketStatus, StreamStatus } from "../types/marketData";

export const STREAM_POLL_MS = 15_000;
export const MARKET_POLL_MS = 60_000;

export interface DashboardStatus extends SystemStatus {
  stream: Resource<StreamStatus>;
  market: Resource<MarketStatus>;
}

export function DashboardLayout({ pollIntervalMs }: { pollIntervalMs?: number }) {
  const system = useSystemStatus(pollIntervalMs);
  const polling = pollIntervalMs === undefined || pollIntervalMs > 0;
  const stream = useApiResource((signal) => marketData.stream({ signal }), [], {
    refreshMs: polling ? STREAM_POLL_MS : 0,
  });
  const market = useApiResource((signal) => marketData.marketStatus({ signal }), [], {
    refreshMs: polling ? MARKET_POLL_MS : 0,
  });
  const context: DashboardStatus = { ...system, stream, market };

  return (
    <div className="flex min-h-full flex-col md:flex-row">
      <Sidebar version={system.health?.version ?? null} />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar status={context} />
        <main className="flex-1 px-3 py-4 md:px-5">
          <Outlet context={context} />
        </main>
      </div>
    </div>
  );
}

function Sidebar({ version }: { version: string | null }) {
  return (
    <aside className="border-border bg-panel shrink-0 border-b md:sticky md:top-0 md:h-screen md:w-52 md:border-r md:border-b-0">
      <div className="flex items-center gap-2 px-3 py-3">
        <img src="/atlas.svg" alt="" className="size-6" />
        <div className="leading-tight">
          <p className="text-[13px] font-semibold tracking-wide">Atlas AI Trader</p>
          <p className="text-ink-faint num text-2xs">{version ? `v${version}` : "research terminal"}</p>
        </div>
      </div>
      <nav aria-label="Dashboard sections" className="pb-3">
        <ul className="flex gap-0.5 overflow-x-auto px-2 md:flex-col md:overflow-visible">
          {NAV_SECTIONS.map(({ path, title, icon: Icon }) => (
            <li key={path}>
              <NavLink
                to={path}
                // Exact match when another section lives below this path (/positions vs
                // /positions/closed), so only one entry is highlighted.
                end={path === "/" || NAV_SECTIONS.some((s) => s.path.startsWith(`${path}/`))}
                className={({ isActive }) =>
                  `flex items-center gap-2 rounded-sm px-2.5 py-1.5 text-xs whitespace-nowrap transition-colors ${
                    isActive
                      ? "bg-accent/12 text-ink shadow-[inset_2px_0_0_var(--color-accent)]"
                      : "text-ink-muted hover:bg-panel-raised hover:text-ink"
                  }`
                }
              >
                <Icon aria-hidden className="size-3.5" />
                {title}
              </NavLink>
            </li>
          ))}
        </ul>
        <p className="text-ink-faint mt-4 hidden px-4 text-2xs font-semibold tracking-wider uppercase md:block">
          Later phases
        </p>
        <ul aria-label="Not yet built" className="mt-1 hidden flex-col gap-0.5 px-2 md:flex">
          {UPCOMING_SECTIONS.map(({ path, title, icon: Icon, phase }) => (
            <li key={path}>
              <span
                aria-disabled="true"
                title={`Not built yet: roadmap Phase ${phase}`}
                className="text-ink-faint flex cursor-not-allowed items-center gap-2 px-2.5 py-1.5 text-xs"
              >
                <Icon aria-hidden className="size-3.5" />
                <span className="flex-1">{title}</span>
                <span className="num text-2xs">P{phase}</span>
              </span>
            </li>
          ))}
        </ul>
      </nav>
    </aside>
  );
}

function TopBar({ status }: { status: DashboardStatus }) {
  const now = useNow(1_000);
  const { zone } = useDisplayZone();
  const stream = status.stream.data;
  const market = status.market.data;
  const nowIso = new Date(now).toISOString();
  return (
    <header className="border-border bg-panel/80 sticky top-0 z-10 flex flex-wrap items-center gap-x-4 gap-y-2 border-b px-3 py-2 backdrop-blur md:px-5">
      <PaperModeBadge />
      <span className="text-ink-muted hidden items-center gap-1 text-2xs lg:inline-flex">
        <Lock aria-hidden className="size-3" />
        No real order execution · simulated paper orders only
      </span>
      <div className="ml-auto flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs">
        <span className="flex items-center gap-1.5" title="Configured market-data provider and feed">
          <span className="text-ink-muted">Provider</span>
          {stream ? (
            <>
              <span className="font-medium">{humanize(stream.provider)}</span>
              <Badge tone={stream.feed_coverage === "SINGLE_VENUE" ? "caution" : "neutral"}>
                {stream.feed.toUpperCase()}
              </Badge>
            </>
          ) : (
            <span className="text-ink-faint">{status.stream.error ? "unknown" : "…"}</span>
          )}
        </span>
        <span className="flex items-center gap-1.5" title="Live stream health (derived by the backend)">
          <span className="text-ink-muted">Stream</span>
          {stream ? (
            <StatusDot tone={streamTone(stream.health)} label={humanize(stream.health)} />
          ) : (
            <StatusDot tone="neutral" label={status.stream.error ? "unknown" : "…"} />
          )}
        </span>
        {market && (
          <span className="flex items-center gap-1.5" title={market.calendar}>
            <span className="text-ink-muted">NYSE</span>
            <span className="font-medium">{humanize(market.session)}</span>
          </span>
        )}
        <SystemStatusIndicator status={status} />
        <span className="num text-ink-muted" title="Current time in the display time zone (Settings) and the browser's local time">
          {formatDateTime(nowIso, zone)}
          {zone !== "LOCAL" && <span className="text-ink-faint"> · {formatClock(nowIso, "LOCAL")} local</span>}
        </span>
      </div>
    </header>
  );
}

export function useDashboardStatus(): DashboardStatus {
  return useOutletContext<DashboardStatus>();
}
