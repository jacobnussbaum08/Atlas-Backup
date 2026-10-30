import { Construction } from "lucide-react";

import type { NavSection } from "../navigation";

/**
 * Shell for a dashboard section whose backend does not exist yet.
 * Deliberately shows no numbers: Atlas never displays fabricated data.
 */
export function SectionPlaceholderPage({ section }: { section: NavSection }) {
  const Icon = section.icon;
  return (
    <section aria-labelledby="page-title" className="max-w-3xl">
      <div className="flex items-center gap-3">
        <Icon aria-hidden className="text-accent size-6" />
        <h1 id="page-title" className="text-xl font-semibold">
          {section.title}
        </h1>
      </div>
      <p className="text-ink-muted mt-2 text-sm">{section.summary}</p>

      <div className="border-border bg-panel mt-6 flex items-start gap-3 rounded-lg border border-dashed p-5">
        <Construction aria-hidden className="text-ink-muted mt-0.5 size-5 shrink-0" />
        <div className="text-sm">
          <p className="font-medium">No data yet</p>
          <p className="text-ink-muted mt-1">
            This section is delivered in roadmap Phase {section.phase} (spec §{section.specSection}).
            Nothing is shown until real data exists.
          </p>
        </div>
      </div>
    </section>
  );
}
