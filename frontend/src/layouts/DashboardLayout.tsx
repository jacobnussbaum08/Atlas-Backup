import { NavLink, Outlet, useOutletContext } from "react-router";

import { PaperModeBadge } from "../components/PaperModeBadge";
import { SystemStatusIndicator } from "../components/SystemStatusIndicator";
import { type SystemStatus, useSystemStatus } from "../hooks/useSystemStatus";
import { NAV_SECTIONS } from "../navigation";

export function DashboardLayout({ pollIntervalMs }: { pollIntervalMs?: number }) {
  const status = useSystemStatus(pollIntervalMs);

  return (
    <div className="flex min-h-full flex-col md:flex-row">
      <aside className="border-border bg-panel shrink-0 border-b md:w-60 md:border-r md:border-b-0">
        <div className="flex items-center gap-2.5 px-4 py-4">
          <img src="/atlas.svg" alt="" className="size-7" />
          <div className="leading-tight">
            <p className="text-sm font-semibold tracking-wide">Atlas AI Trader</p>
            <p className="text-ink-muted font-mono text-[11px]">
              {status.health ? `v${status.health.version}` : "research terminal"}
            </p>
          </div>
        </div>
        <nav aria-label="Dashboard sections" className="pb-3">
          <ul className="flex gap-1 overflow-x-auto px-2 md:flex-col md:overflow-visible">
            {NAV_SECTIONS.map(({ path, title, icon: Icon }) => (
              <li key={path}>
                <NavLink
                  to={path}
                  end={path === "/"}
                  className={({ isActive }) =>
                    `flex items-center gap-2.5 rounded px-3 py-2 text-sm whitespace-nowrap transition-colors ${
                      isActive
                        ? "bg-panel-raised text-ink"
                        : "text-ink-muted hover:bg-panel-raised/60 hover:text-ink"
                    }`
                  }
                >
                  <Icon aria-hidden className="size-4" />
                  {title}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="border-border bg-panel/60 flex flex-wrap items-center justify-between gap-3 border-b px-4 py-3 md:px-6">
          <PaperModeBadge />
          <div className="flex items-center gap-3">
            {status.health && (
              <span className="text-ink-muted font-mono text-xs">
                env: {status.health.environment}
              </span>
            )}
            <SystemStatusIndicator status={status} />
          </div>
        </header>
        <main className="flex-1 px-4 py-6 md:px-6">
          <Outlet context={status satisfies SystemStatus} />
        </main>
      </div>
    </div>
  );
}

export function useDashboardStatus(): SystemStatus {
  return useOutletContext<SystemStatus>();
}
