import { humanize } from "../lib/format";
import type {
  AssetStatus,
  DiscoveryJobStatus,
  InstrumentType,
  QualificationOutcome,
  RuleStatus,
  UniverseLayer,
} from "../types/discovery";
import { Badge, type Tone } from "./ui";

/** Asset discovery states mapped to the design system's tones, in one place. */

const OUTCOME: Record<QualificationOutcome, { tone: Tone; label: string; title: string }> = {
  QUALIFIED: { tone: "positive", label: "Qualified", title: "Every mandatory rule passed" },
  DISQUALIFIED: { tone: "negative", label: "Disqualified", title: "At least one mandatory rule failed" },
  INSUFFICIENT_DATA: {
    tone: "caution",
    label: "Insufficient data",
    title: "Nothing failed, but a mandatory input (e.g. daily history) does not exist yet",
  },
};

export function OutcomeBadge({ outcome }: { outcome: QualificationOutcome | null | undefined }) {
  if (!outcome) return <span className="text-ink-faint" title="Not evaluated yet">Not evaluated</span>;
  const info = OUTCOME[outcome];
  return (
    <Badge tone={info.tone} title={info.title}>
      {info.label}
    </Badge>
  );
}

const STATUS_TONES: Record<AssetStatus, Tone> = { ACTIVE: "positive", INACTIVE: "neutral", DELISTED: "negative" };

export function AssetStatusBadge({ status }: { status: AssetStatus }) {
  return (
    <Badge tone={STATUS_TONES[status]} dot>
      {humanize(status)}
    </Badge>
  );
}

const RULE_TONES: Record<RuleStatus, Tone> = {
  PASS: "positive",
  FAIL: "negative",
  UNAVAILABLE: "caution",
  NOT_APPLICABLE: "neutral",
};

export function RuleStatusBadge({ status }: { status: RuleStatus }) {
  return <Badge tone={RULE_TONES[status]}>{status === "NOT_APPLICABLE" ? "n/a" : humanize(status)}</Badge>;
}

export const LAYER_LABELS: Record<UniverseLayer, string> = {
  DISCOVERY_POOL: "Discovery pool",
  QUALIFIED_UNIVERSE: "Qualified universe",
  ACTIVE_WATCHLIST: "Active watchlist",
};

const JOB_TONES: Record<DiscoveryJobStatus, Tone> = {
  PENDING: "info",
  RUNNING: "info",
  COMPLETED: "positive",
  PARTIALLY_COMPLETED: "caution",
  FAILED: "negative",
};

export function DiscoveryJobBadge({ status }: { status: DiscoveryJobStatus }) {
  return (
    <Badge tone={JOB_TONES[status]} dot>
      {humanize(status)}
    </Badge>
  );
}

export function InstrumentTypeLabel({ type }: { type: InstrumentType }) {
  return type === "UNCLASSIFIED" ? (
    <span
      className="text-ink-faint"
      title="The provider does not separate stocks, ETFs, preferreds, warrants, units and rights"
    >
      Unclassified
    </span>
  ) : (
    <span>{type === "ETF" ? "ETF" : "Stock"}</span>
  );
}

/** A reference-data flag: never shows a missing value as "No". */
export function Flag({ value, label }: { value: boolean | null | undefined; label: string }) {
  if (value === null || value === undefined) {
    return (
      <span className="text-ink-faint" title={`${label}: not reported by the provider`}>
        —
      </span>
    );
  }
  return (
    <span className={value ? "text-positive" : "text-ink-muted"} title={`${label}: ${value ? "yes" : "no"}`}>
      {value ? "Yes" : "No"}
    </span>
  );
}
