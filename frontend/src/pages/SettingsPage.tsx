import { Badge, ErrorState, KeyValues, Loading, Notice, PageHeader, Panel, Segmented } from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { humanize, MISSING } from "../lib/format";
import { type DisplayZone, useDisplayZone, zoneLabel } from "../lib/time";
import { marketData } from "../services/marketData";

const CREDENTIAL_TONES = {
  NOT_CONFIGURED: "negative",
  CONFIGURED: "caution",
  VERIFIED: "positive",
  REJECTED: "negative",
} as const;

function yesNo(value: boolean | null | undefined) {
  if (value === null || value === undefined) return <span className="text-ink-faint">{MISSING}</span>;
  return value ? <Badge tone="positive">Enabled</Badge> : <Badge>Disabled</Badge>;
}

function list(values: readonly string[]) {
  return values.length ? <span className="num">{values.join(", ")}</span> : <span className="text-ink-faint">none</span>;
}

export function SettingsPage() {
  const { zone, setZone } = useDisplayZone();
  const config = useApiResource((signal) => marketData.configuration({ signal }), []);
  const providers = useApiResource((signal) => marketData.providers({ signal }), []);
  const c = config.data;
  const provider = providers.data?.[0];

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Settings"
        description="Effective market-data configuration as reported by the backend. Read-only: provider, feed and streaming change only through the server environment and a restart, never silently from the browser."
      />

      <Panel title="Display" className="mb-3">
        <div className="flex flex-wrap items-center gap-3 text-xs">
          <span className="text-ink-muted">Time zone for all timestamps</span>
          <Segmented<DisplayZone>
            label="Display time zone"
            value={zone}
            onChange={setZone}
            options={[
              { value: "ET", label: "Exchange (ET)" },
              { value: "UTC", label: "UTC" },
              { value: "LOCAL", label: `Local (${zoneLabel("LOCAL")})` },
            ]}
          />
          <span className="text-ink-faint">Stored in this browser only. Data is always UTC in the backend.</span>
        </div>
      </Panel>

      {config.error && <ErrorState message={config.error} onRetry={config.reload} />}
      {!c && !config.error && <Loading />}
      {c && (
        <div className="grid gap-3 lg:grid-cols-2">
          <Panel title="Provider and feed">
            <KeyValues
              items={[
                { label: "Provider", value: `${c.provider_display_name} (${c.provider})` },
                { label: "Feed", value: <span><span className="num">{c.feed.toUpperCase()}</span> · {c.feed_display_name}</span> },
                { label: "Feed scope", value: <span>{humanize(c.feed_coverage)} · {c.feed_description}</span> },
                { label: "Feed delay", value: c.feed_delay_seconds > 0 ? `${c.feed_delay_seconds / 60} min` : "real-time" },
                { label: "Price adjustment", value: c.adjustment },
                {
                  label: "Credentials",
                  value: (
                    <span className="flex items-center gap-2">
                      <Badge tone={CREDENTIAL_TONES[c.credentials]}>{humanize(c.credentials)}</Badge>
                      <span className="text-ink-faint">status only; values are never sent to the browser</span>
                    </span>
                  ),
                },
                {
                  label: "Configuration check",
                  value: provider ? (
                    provider.configuration.issues.length === 0 ? (
                      <span className="text-positive">no issues</span>
                    ) : (
                      <ul className="space-y-0.5">
                        {provider.configuration.issues.map((i) => (
                          <li
                            key={i.message}
                            className={
                              i.severity === "ERROR" || i.severity === "CRITICAL"
                                ? "text-negative"
                                : i.severity === "WARNING"
                                  ? "text-caution"
                                  : "text-ink-muted"
                            }
                          >
                            {i.severity}: {i.message}
                          </li>
                        ))}
                      </ul>
                    )
                  ) : (
                    MISSING
                  ),
                },
                { label: "Capabilities checked", value: provider?.capabilities.verified_on ?? MISSING },
              ]}
            />
          </Panel>

          <Panel title="Environment">
            <KeyValues
              items={[
                { label: "Environment", value: <span className="num">{c.environment}</span> },
                { label: "Trading mode", value: <Badge tone="caution">{c.trading_mode} · simulated only</Badge> },
                { label: "Order execution", value: <span>None. Atlas has no broker or order endpoints.</span> },
                {
                  label: "Operator token",
                  value: c.operator_token_configured
                    ? "Required for operational actions (e.g. historical retrieval)"
                    : "Not configured: operational actions allowed in development/test only",
                },
                { label: "Calendar verified", value: <span className="num">{c.calendar_verified_years.join(", ")}</span> },
              ]}
            />
          </Panel>

          <Panel title="Live streaming">
            <KeyValues
              items={[
                { label: "Enabled (worker)", value: yesNo(c.stream.enabled_in_worker) },
                { label: "Enabled (API env)", value: yesNo(c.stream.enabled_in_api) },
                { label: "Configured symbols", value: list(c.configured_symbols) },
                { label: "Channels", value: list(c.stream.channels) },
                { label: "Streamed bar timeframes", value: list(c.stream_bar_timeframes) },
                { label: "Stream stale after", value: <span className="num">{c.stream.stale_after_seconds}s</span> },
                { label: "Late observation", value: <span className="num">{c.stale_observation_seconds > 0 ? `${c.stale_observation_seconds}s` : "check disabled"}</span> },
                { label: "Heartbeat timeout", value: <span className="num">{c.stream.heartbeat_timeout_seconds}s</span> },
              ]}
            />
            {c.stream.enabled_in_worker !== null && c.stream.enabled_in_worker !== c.stream.enabled_in_api && (
              <div className="mt-2">
                <Notice tone="caution">
                  The stream worker reports a different setting than the API's environment (the worker may run with its own overrides).
                </Notice>
              </div>
            )}
          </Panel>

          <Panel title="Recovery and completeness">
            <KeyValues
              items={[
                { label: "Recovery worker", value: yesNo(c.recovery.enabled) },
                { label: "Recovered data types", value: list(c.recovery.data_types) },
                { label: "Tracked timeframes", value: list(c.recovery.timeframes) },
                { label: "Look-back", value: <span className="num">{c.recovery.lookback_days} day(s)</span> },
                { label: "Settle delay", value: <span className="num">{c.recovery.settle_seconds}s</span> },
                { label: "Interval", value: <span className="num">{c.recovery.interval_seconds}s</span> },
                { label: "Max attempts", value: <span className="num">{c.recovery.max_attempts}</span> },
                { label: "Trade reconciliation", value: <span className="num">{c.recovery.trade_reconciliation_minutes}-min windows</span> },
              ]}
            />
          </Panel>

          <Panel title="Timeframes">
            <KeyValues
              items={[
                { label: "Dashboard charts", value: list(c.dashboard_timeframes) },
                { label: "Live (streamed)", value: list(c.stream_bar_timeframes) },
                {
                  label: "Provider history",
                  value: (
                    <span className="num">
                      {Object.entries(c.historical_timeframes)
                        .map(([unit, amounts]) => `${unit}: ${amounts.length > 6 ? `${amounts[0]}–${amounts[amounts.length - 1]}` : amounts.join(", ")}`)
                        .join(" · ")}
                    </span>
                  ),
                },
              ]}
            />
          </Panel>

          <Panel title="Provider limitations">
            <ul className="text-ink-muted list-disc space-y-1 pl-4 text-xs">
              {c.limitations.map((l) => (
                <li key={l}>{l}</li>
              ))}
            </ul>
          </Panel>
        </div>
      )}
    </section>
  );
}
