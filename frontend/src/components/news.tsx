import { ExternalLink } from "lucide-react";
import { Link } from "react-router";

import { hostname, safeHref } from "../services/news";
import {
  type ArticleOrigin,
  type AssetLink,
  CATEGORY_LABELS,
  type CategoryView,
  type EventCategory,
  type NewsJobStatus,
  type StreamHealthState,
} from "../types/news";
import { Badge, type Tone } from "./ui";

const CATEGORY_TONES: Partial<Record<EventCategory, Tone>> = {
  EARNINGS: "accent",
  ANALYST_RATINGS: "info",
  MERGERS_ACQUISITIONS: "accent",
  CENTRAL_BANKS: "caution",
  INFLATION: "caution",
  EMPLOYMENT: "caution",
  MACROECONOMIC: "caution",
  GEOPOLITICAL: "negative",
  LITIGATION: "negative",
  REGULATORY: "info",
};

/** A category label; rule-based labels say they are estimates. */
export function CategoryBadge({ category, view }: { category: EventCategory; view?: CategoryView }) {
  const title = view
    ? view.source === "PROVIDER"
      ? `Provider label "${view.provider_label ?? ""}" (provider-asserted)`
      : `Keyword estimate (${view.verification.toLowerCase()}), confidence ${view.confidence.toLowerCase()}, score ${view.score}`
    : undefined;
  return (
    <Badge tone={CATEGORY_TONES[category] ?? "neutral"} {...(title ? { title } : {})}>
      {CATEGORY_LABELS[category]}
    </Badge>
  );
}

/** How Atlas obtained the article: live over the stream, or retrieved after publication. */
export function OriginBadge({ origin }: { origin: ArticleOrigin }) {
  return origin === "REALTIME_STREAM" ? (
    <Badge tone="positive" dot title="Received live over the news stream; its receipt time is real-time evidence">
      Real-time
    </Badge>
  ) : (
    <Badge tone="neutral" title="Retrieved over REST after publication: available to Atlas only from its retrieval time">
      Retrieved later
    </Badge>
  );
}

const MAPPING_TITLES: Record<AssetLink["status"], string> = {
  MAPPED: "Mapped to an Atlas asset at publication time",
  UNKNOWN_SYMBOL: "Atlas has no mapping for this symbol",
  INVALID_SYMBOL: "Not a valid provider symbol",
  NOT_EFFECTIVE: "No mapping was valid at publication time; not assigned",
  AMBIGUOUS: "The symbol changed hands near publication; not assigned",
};

/** Related symbols: mapped assets link to their asset page; others show why they are not. */
export function AssetChips({ links }: { links: AssetLink[] }) {
  if (links.length === 0) return <span className="text-ink-faint text-2xs">No related symbols</span>;
  return (
    <span className="flex flex-wrap items-center gap-1">
      {links.map((link) =>
        link.status === "MAPPED" && link.asset_id ? (
          <Link
            key={link.provider_symbol}
            to={`/discovery/assets/${link.asset_id}`}
            title={`${MAPPING_TITLES.MAPPED}${link.asset_status && link.asset_status !== "ACTIVE" ? ` (asset ${link.asset_status.toLowerCase()})` : ""}`}
            className="num border-accent/40 text-accent hover:bg-accent/10 rounded-sm border px-1 text-2xs"
          >
            {link.asset_symbol ?? link.provider_symbol}
          </Link>
        ) : (
          <span
            key={link.provider_symbol}
            title={`${MAPPING_TITLES[link.status]}: ${link.reason}`}
            className={`num border-border-strong text-ink-faint rounded-sm border px-1 text-2xs ${link.status === "AMBIGUOUS" ? "border-dashed" : ""}`}
          >
            {link.provider_symbol}
          </span>
        ),
      )}
    </span>
  );
}

/** Link to the publisher's page; rendered only for http(s) URLs, never followed by Atlas. */
export function SourceLink({ url }: { url: string | null }) {
  const href = safeHref(url);
  if (!href) return null;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer nofollow"
      referrerPolicy="no-referrer"
      className="text-ink-muted hover:text-accent inline-flex items-center gap-0.5 text-2xs"
      title={href}
    >
      {hostname(href)}
      <ExternalLink aria-hidden className="size-3" />
    </a>
  );
}

export const STREAM_TONES: Record<StreamHealthState, Tone> = {
  STREAMING: "positive",
  DEGRADED: "caution",
  CONNECTING: "info",
  FAILED: "negative",
  NOT_RUNNING: "caution",
  STOPPED: "neutral",
  DISABLED: "neutral",
};

export function StreamHealthBadge({ health }: { health: StreamHealthState }) {
  return (
    <Badge tone={STREAM_TONES[health]} dot>
      {health.replace("_", " ").toLowerCase()}
    </Badge>
  );
}

export function NewsJobStatusBadge({ status }: { status: NewsJobStatus }) {
  const tone: Record<NewsJobStatus, Tone> = {
    PENDING: "info",
    RUNNING: "info",
    COMPLETED: "positive",
    FAILED: "negative",
    CANCELLED: "neutral",
  };
  return <Badge tone={tone[status]}>{status.toLowerCase()}</Badge>;
}

export function formatSeconds(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  if (seconds < 86_400) return `${(seconds / 3600).toFixed(1)} h`;
  return `${(seconds / 86_400).toFixed(1)} d`;
}
