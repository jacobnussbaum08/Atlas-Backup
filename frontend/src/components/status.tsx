import { humanize } from "../lib/format";
import type {
  DataQualityStatus,
  DataSource,
  DimensionState,
  Freshness,
  GapStatus,
  IngestionJobStatus,
  MarketSession,
  PipelineHealthState,
  RecoveryJobStatus,
} from "../types/marketData";
import { Badge, type Tone } from "./ui";

/** Backend states mapped to the design system's tones, in one place. */

const FRESHNESS: Record<Freshness, { tone: Tone; label: string; title: string }> = {
  LIVE: { tone: "info", label: "Live", title: "Streamed by the provider within the staleness threshold" },
  DELAYED: { tone: "caution", label: "Delayed", title: "The feed is published with a fixed delay" },
  HISTORICAL: {
    tone: "accent",
    label: "Historical",
    title: "Newest data was retrieved from provider history (REST), not the live stream",
  },
  STALE: { tone: "negative", label: "Stale", title: "The feed is publishing but nothing recent was stored" },
  MARKET_CLOSED: {
    tone: "neutral",
    label: "Closed",
    title: "Outside the feed's hours: values are from the last session",
  },
  NO_DATA: { tone: "neutral", label: "No data", title: "Nothing stored for this asset" },
};

export function FreshnessBadge({ freshness }: { freshness: Freshness }) {
  const info = FRESHNESS[freshness];
  return (
    <Badge tone={info.tone} title={info.title} dot>
      {info.label}
    </Badge>
  );
}

const QUALITY_TONES: Partial<Record<DataQualityStatus, Tone>> = {
  VALID: "positive",
  RECOVERED: "positive",
  INCOMPLETE: "caution",
  STALE: "caution",
  UNCHECKED: "neutral",
  MISSING: "negative",
  INVALID: "negative",
  QUARANTINED: "negative",
  UNAVAILABLE: "negative",
};

export function QualityBadge({ status }: { status: DataQualityStatus | null | undefined }) {
  if (!status) return <span className="text-ink-faint">—</span>;
  return <Badge tone={QUALITY_TONES[status] ?? "neutral"}>{humanize(status)}</Badge>;
}

export function SourceBadge({ source }: { source: DataSource | null | undefined }) {
  if (!source) return <span className="text-ink-faint">—</span>;
  return source === "LIVE_STREAM" ? (
    <Badge tone="info" title="Received from the provider's live stream">
      Stream
    </Badge>
  ) : (
    <Badge tone="accent" title="Retrieved from the provider's historical REST API">
      History
    </Badge>
  );
}

export function FeedBadge({ feed, coverage }: { feed: string; coverage?: string | undefined }) {
  const single = coverage === "SINGLE_VENUE";
  return (
    <Badge
      tone={single ? "caution" : "neutral"}
      title={
        single
          ? `${feed.toUpperCase()} is a single venue: not consolidated US market data`
          : `${feed.toUpperCase()} feed`
      }
    >
      {feed.toUpperCase()}
      {single && " · single venue"}
    </Badge>
  );
}

const SESSION: Record<MarketSession, { tone: Tone; label: string }> = {
  REGULAR: { tone: "positive", label: "Regular session" },
  PRE_MARKET: { tone: "caution", label: "Pre-market" },
  AFTER_HOURS: { tone: "caution", label: "After hours" },
  CLOSED: { tone: "neutral", label: "Closed" },
};

export function SessionBadge({ session }: { session: MarketSession }) {
  const info = SESSION[session];
  return (
    <Badge tone={info.tone} dot>
      {info.label}
    </Badge>
  );
}

export const DIMENSION_TONES: Record<DimensionState, Tone> = {
  OK: "positive",
  WARNING: "caution",
  FAILING: "negative",
  INACTIVE: "neutral",
  UNKNOWN: "neutral",
};

export function DimensionBadge({ state }: { state: DimensionState }) {
  return (
    <Badge tone={DIMENSION_TONES[state]} dot>
      {state}
    </Badge>
  );
}

export const PIPELINE_TONES: Record<PipelineHealthState, Tone> = {
  HEALTHY: "positive",
  DEGRADED: "caution",
  FAILING: "negative",
  INACTIVE: "neutral",
};

export function streamTone(health: string | null | undefined): Tone {
  switch (health) {
    case "HEALTHY":
      return "positive";
    case "IDLE":
    case "MARKET_CLOSED":
    case "DISABLED":
      return "neutral";
    case "DEGRADED":
    case "STARTING":
    case "RECONNECTING":
      return "caution";
    case "STALE":
    case "FAILED":
    case "NOT_RUNNING":
      return "negative";
    default:
      return "neutral";
  }
}

const JOB_TONES: Record<string, Tone> = {
  PENDING: "neutral",
  RUNNING: "info",
  COMPLETED: "positive",
  PARTIALLY_COMPLETED: "caution",
  FAILED: "negative",
  CANCELLED: "neutral",
};

export function JobStatusBadge({ status }: { status: IngestionJobStatus | RecoveryJobStatus }) {
  return (
    <Badge tone={JOB_TONES[status] ?? "neutral"} dot>
      {humanize(status)}
    </Badge>
  );
}

const GAP_TONES: Record<GapStatus, Tone> = {
  OPEN: "caution",
  RECOVERING: "info",
  RECOVERED: "positive",
  PARTIALLY_RECOVERED: "caution",
  UNAVAILABLE: "negative",
};

export function GapStatusBadge({ status }: { status: GapStatus }) {
  return <Badge tone={GAP_TONES[status]}>{humanize(status)}</Badge>;
}

const SEVERITY_TONES: Record<string, Tone> = {
  INFO: "neutral",
  WARNING: "caution",
  ERROR: "negative",
  CRITICAL: "negative",
};

export function SeverityBadge({ severity }: { severity: string }) {
  return <Badge tone={SEVERITY_TONES[severity] ?? "neutral"}>{severity}</Badge>;
}
