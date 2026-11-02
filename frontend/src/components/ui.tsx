import { AlertTriangle, Inbox, Loader2, RefreshCw } from "lucide-react";
import type { ButtonHTMLAttributes, ReactNode } from "react";

import { formatAge, formatClock, formatDateTime } from "../lib/format";
import { useDisplayZone } from "../lib/time";

/** Shared visual vocabulary. Tone names match the theme tokens in styles/index.css. */
export type Tone = "positive" | "negative" | "caution" | "info" | "accent" | "neutral";

const BADGE_TONES: Record<Tone, string> = {
  positive: "border-positive/40 bg-positive/10 text-positive",
  negative: "border-negative/40 bg-negative/10 text-negative",
  caution: "border-caution/40 bg-caution/10 text-caution",
  info: "border-info/40 bg-info/10 text-info",
  accent: "border-accent/40 bg-accent/10 text-accent",
  neutral: "border-border-strong bg-panel-raised text-ink-muted",
};

const DOT_TONES: Record<Tone, string> = {
  positive: "bg-positive",
  negative: "bg-negative",
  caution: "bg-caution",
  info: "bg-info",
  accent: "bg-accent",
  neutral: "bg-ink-faint",
};

export function Badge({
  tone = "neutral",
  children,
  title,
  dot = false,
}: {
  tone?: Tone;
  children: ReactNode;
  title?: string;
  dot?: boolean;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded-sm border px-1.5 py-px text-2xs font-medium tracking-wide whitespace-nowrap uppercase ${BADGE_TONES[tone]}`}
    >
      {dot && <span aria-hidden className={`size-1.5 rounded-full ${DOT_TONES[tone]}`} />}
      {children}
    </span>
  );
}

export function StatusDot({ tone, label }: { tone: Tone; label?: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span aria-hidden className={`size-2 shrink-0 rounded-full ${DOT_TONES[tone]}`} />
      {label && <span>{label}</span>}
    </span>
  );
}

export function Panel({
  title,
  actions,
  children,
  className = "",
  bodyClassName = "p-3",
  subtitle,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={`border-border bg-panel min-w-0 rounded-sm border ${className}`}>
      {(title || actions) && (
        <div className="border-border flex min-h-9 flex-wrap items-center justify-between gap-2 border-b px-3 py-1.5">
          <div className="flex min-w-0 items-baseline gap-2">
            {title && (
              <h2 className="text-ink-muted text-2xs font-semibold tracking-wider uppercase">
                {title}
              </h2>
            )}
            {subtitle && <span className="text-ink-faint truncate text-xs">{subtitle}</span>}
          </div>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </div>
      )}
      <div className={bodyClassName}>{children}</div>
    </section>
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        <h1 id="page-title" className="text-base font-semibold tracking-tight">
          {title}
        </h1>
        {description && <p className="text-ink-muted mt-0.5 text-xs">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Button({
  children,
  variant = "default",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "default" | "primary" | "ghost" }) {
  const styles = {
    default: "border-border-strong bg-panel-raised hover:border-ink-faint text-ink",
    primary: "border-accent bg-accent/15 text-accent hover:bg-accent/25",
    ghost: "border-transparent text-ink-muted hover:text-ink hover:bg-panel-raised",
  }[variant];
  return (
    <button
      type="button"
      className={`inline-flex h-7 items-center gap-1.5 rounded-sm border px-2.5 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${styles} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}

export function RefreshButton({ onClick, loading }: { onClick: () => void; loading?: boolean }) {
  return (
    <Button variant="ghost" onClick={onClick} aria-label="Refresh" title="Refresh">
      <RefreshCw aria-hidden className={`size-3.5 ${loading ? "animate-spin" : ""}`} />
      Refresh
    </Button>
  );
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: readonly { value: T; label: string; disabled?: boolean; title?: string }[];
  value: T;
  onChange: (value: T) => void;
  label: string;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="border-border-strong inline-flex rounded-sm border">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={option.value === value}
          disabled={option.disabled}
          title={option.title}
          onClick={() => onChange(option.value)}
          className={`num h-6 px-2 text-2xs font-medium first:rounded-l-sm last:rounded-r-sm disabled:opacity-40 ${
            option.value === value
              ? "bg-accent/20 text-accent"
              : "text-ink-muted hover:bg-panel-raised hover:text-ink"
          }`}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div role="status" className="text-ink-muted flex items-center gap-2 px-1 py-6 text-xs">
      <Loader2 aria-hidden className="size-3.5 animate-spin" />
      {label}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="flex items-start gap-2.5 px-1 py-5 text-xs">
      <Inbox aria-hidden className="text-ink-faint mt-0.5 size-4 shrink-0" />
      <div>
        <p className="font-medium">{title}</p>
        {children && <div className="text-ink-muted mt-0.5">{children}</div>}
      </div>
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
  compact = false,
}: {
  message: string;
  onRetry?: () => void;
  compact?: boolean;
}) {
  return (
    <div
      role="alert"
      className={`border-negative/40 bg-negative/10 flex items-start gap-2 rounded-sm border text-xs ${compact ? "px-2 py-1.5" : "px-3 py-2.5"}`}
    >
      <AlertTriangle aria-hidden className="text-negative mt-px size-3.5 shrink-0" />
      <div className="min-w-0 flex-1">
        <span className="text-negative font-medium">Could not load data. </span>
        <span className="text-ink-muted break-words">{message}</span>
      </div>
      {onRetry && (
        <button type="button" onClick={onRetry} className="text-accent shrink-0 font-medium hover:underline">
          Retry
        </button>
      )}
    </div>
  );
}

export function Notice({ tone = "neutral", children }: { tone?: Tone | undefined; children: ReactNode }) {
  const styles: Record<Tone, string> = {
    positive: "border-positive/30 text-positive",
    negative: "border-negative/40 text-negative",
    caution: "border-caution/40 text-caution",
    info: "border-info/30 text-info",
    accent: "border-accent/30 text-accent",
    neutral: "border-border-strong text-ink-muted",
  };
  return <div className={`border-l-2 bg-panel-raised/40 px-2.5 py-1.5 text-xs ${styles[tone]}`}>{children}</div>;
}

/** Time in the display zone; the exact UTC instant is in the tooltip. */
export function Timestamp({
  iso,
  age = false,
  now,
  seconds = true,
  timeOnly = false,
}: {
  iso: string | null | undefined;
  age?: boolean;
  now?: number;
  seconds?: boolean;
  timeOnly?: boolean;
}) {
  const { zone } = useDisplayZone();
  if (!iso) return <span className="text-ink-faint">—</span>;
  return (
    <time dateTime={iso} title={`${iso} (UTC)`} className="num whitespace-nowrap">
      {timeOnly ? formatClock(iso, zone) : formatDateTime(iso, zone, { seconds })}
      {age && now !== undefined && <span className="text-ink-faint"> · {formatAge(iso, now)}</span>}
    </time>
  );
}

export function KeyValues({ items }: { items: { label: ReactNode; value: ReactNode; title?: string }[] }) {
  return (
    <dl className="grid grid-cols-[minmax(7rem,auto)_1fr] gap-x-4 gap-y-1.5 text-xs">
      {items.map((item, index) => (
        <div key={index} className="contents" title={item.title}>
          <dt className="text-ink-muted">{item.label}</dt>
          <dd className="min-w-0 break-words">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: Tone | undefined;
}) {
  const color = tone
    ? { positive: "text-positive", negative: "text-negative", caution: "text-caution", info: "text-info", accent: "text-accent", neutral: "text-ink" }[tone]
    : "text-ink";
  return (
    <div className="border-border bg-panel min-w-0 rounded-sm border px-3 py-2">
      <p className="text-ink-muted text-2xs font-semibold tracking-wider uppercase">{label}</p>
      <p className={`num mt-1 truncate text-base font-medium ${color}`}>{value}</p>
      {hint && <p className="text-ink-faint mt-0.5 truncate text-2xs">{hint}</p>}
    </div>
  );
}

/** Dense table shell with consistent header/row styles. */
export function DataTable({ children, label }: { children: ReactNode; label: string }) {
  return (
    <div className="overflow-x-auto">
      <table aria-label={label} className="w-full border-collapse text-xs">
        {children}
      </table>
    </div>
  );
}

export const TH = "text-ink-muted border-border h-7 border-b px-2 text-left text-2xs font-semibold tracking-wider whitespace-nowrap uppercase";
export const TD = "border-border/60 h-8 border-b px-2 align-middle whitespace-nowrap";
