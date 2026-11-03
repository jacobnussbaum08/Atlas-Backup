import { ArrowLeft } from "lucide-react";
import { Link, useParams } from "react-router";

import { AssetChips, CategoryBadge, OriginBadge, SourceLink } from "../components/news";
import {
  Badge,
  DataTable,
  ErrorState,
  KeyValues,
  Loading,
  Notice,
  PageHeader,
  Panel,
  TD,
  TH,
  Timestamp,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { formatCount, humanize } from "../lib/format";
import { news } from "../services/news";
import type { ArticleDetail, CategoryView } from "../types/news";

export function NewsArticlePage() {
  const { articleId = "" } = useParams();
  const detail = useApiResource((signal) => news.article(articleId, { signal }), [articleId]);
  return (
    <section aria-labelledby="page-title">
      <Link to="/news" className="text-ink-muted hover:text-accent mb-2 inline-flex items-center gap-1 text-xs">
        <ArrowLeft aria-hidden className="size-3.5" />
        News & Events
      </Link>
      {detail.status === "loading" && <Loading label="Loading article…" />}
      {detail.status === "error" && <ErrorState message={detail.error ?? ""} onRetry={detail.reload} />}
      {detail.data && <Article detail={detail.data} />}
    </section>
  );
}

function Article({ detail }: { detail: ArticleDetail }) {
  const a = detail.article;
  const categories = a.event?.categories ?? [];
  return (
    <>
      <PageHeader title={a.headline} description={a.summary ?? "No summary was provided."} />
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Article" actions={<OriginBadge origin={a.first_origin} />}>
          <KeyValues
            items={[
              { label: "Source", value: a.source ?? "not reported" },
              { label: "Author", value: a.author ?? "not reported" },
              { label: "Link", value: a.url ? <SourceLink url={a.url} /> : "not provided" },
              { label: "Provider article", value: <span className="num">{`${a.provider} #${a.provider_article_id}`}</span> },
              { label: "Published (provider)", value: <Timestamp iso={a.published_at} /> },
              { label: "Updated (provider)", value: <Timestamp iso={a.provider_updated_at} /> },
              { label: "First received by Atlas", value: <Timestamp iso={a.first_received_at} /> },
              { label: "First available to Atlas", value: <Timestamp iso={a.first_available_at} /> },
              {
                label: "Received live",
                value: a.first_live_received_at ? <Timestamp iso={a.first_live_received_at} /> : "never (retrieved later)",
              },
              {
                label: "Revisions",
                value: (
                  <span className="num">
                    {a.current_revision} · seen {detail.observation_count === 1 ? "once" : `${formatCount(detail.observation_count)} times`}
                  </span>
                ),
              },
            ]}
          />
          {a.first_origin === "HISTORICAL_REST" && (
            <div className="mt-2">
              <Notice tone="info">
                Retrieved after publication: simulations treat it as known only from{" "}
                <Timestamp iso={a.first_available_at} />, not from its publication time.
              </Notice>
            </div>
          )}
        </Panel>
        <Panel title="Related assets" subtitle="mapped at publication time">
          <AssetChips links={a.assets} />
          {a.assets.length > 0 && (
            <div className="mt-2">
              <DataTable label="Symbol mappings">
                <thead>
                  <tr>
                    <th className={TH}>Symbol</th>
                    <th className={TH}>Outcome</th>
                    <th className={TH}>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {a.assets.map((link) => (
                    <tr key={link.provider_symbol}>
                      <td className={`${TD} num`}>{link.provider_symbol}</td>
                      <td className={TD}>
                        <Badge tone={link.status === "MAPPED" ? "positive" : link.status === "AMBIGUOUS" ? "caution" : "neutral"}>
                          {humanize(link.status)}
                        </Badge>
                      </td>
                      <td className={`${TD} text-ink-muted whitespace-normal`}>{link.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </DataTable>
            </div>
          )}
          {a.relevance.length > 0 && (
            <p className="text-ink-faint mt-2 text-2xs">
              Relevance ({a.relevance[0]?.version}):{" "}
              {a.relevance.map((r) => `${r.asset_symbol ?? r.asset_id} ${r.static_relevance}`).join(", ")} — how
              directly the article concerns each asset, not its importance or impact.
            </p>
          )}
        </Panel>
      </div>
      <Panel
        title="Event classification"
        subtitle={a.event ? `${a.event.classifier_version} · ${humanize(a.event.scope)}` : undefined}
        className="mt-3"
      >
        {categories.length === 0 ? (
          <p className="text-ink-faint text-xs">Not classified.</p>
        ) : (
          <DataTable label="Event categories">
            <thead>
              <tr>
                <th className={TH}>Category</th>
                <th className={TH}>Source</th>
                <th className={TH}>Confidence</th>
                <th className={TH}>Status</th>
                <th className={TH}>Evidence</th>
              </tr>
            </thead>
            <tbody>
              {categories.map((c) => (
                <tr key={`${c.source}-${c.category}`}>
                  <td className={TD}>
                    <span className="inline-flex items-center gap-1">
                      <CategoryBadge category={c.category} view={c} />
                      {c.is_primary && <span className="text-ink-faint text-2xs">primary</span>}
                    </span>
                  </td>
                  <td className={TD}>{c.source === "ATLAS" ? `Atlas ${humanize(c.method).toLowerCase()}` : "Provider"}</td>
                  <td className={TD}>{humanize(c.confidence)}</td>
                  <td className={TD}>{humanize(c.verification)}</td>
                  <td className={`${TD} text-ink-muted whitespace-normal`}>{evidence(c)}</td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        )}
        <p className="text-ink-faint mt-2 text-2xs">
          Rule-based keyword estimates; they say what an article is about and nothing about its
          importance, sentiment or market impact.
        </p>
      </Panel>
      <Panel title="Revision history" subtitle="every version Atlas stored; none is overwritten" className="mt-3">
        <DataTable label="Article revisions">
          <thead>
            <tr>
              <th className={TH}>Rev</th>
              <th className={TH}>Reason</th>
              <th className={TH}>Received via</th>
              <th className={TH}>Provider updated</th>
              <th className={TH}>Received</th>
              <th className={TH}>Available</th>
              <th className={TH}>Headline</th>
            </tr>
          </thead>
          <tbody>
            {detail.revisions.map((r) => (
              <tr key={r.revision}>
                <td className={`${TD} num`}>{r.revision}</td>
                <td className={TD}>{humanize(r.reason)}</td>
                <td className={TD}>
                  <OriginBadge origin={r.origin} />
                </td>
                <td className={TD}>
                  <Timestamp iso={r.provider_updated_at} />
                </td>
                <td className={TD}>
                  <Timestamp iso={r.received_at} />
                </td>
                <td className={TD}>
                  <Timestamp iso={r.available_at} />
                </td>
                <td className={`${TD} whitespace-normal`}>{r.headline}</td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      </Panel>
    </>
  );
}

function evidence(c: CategoryView): string {
  if (c.source === "PROVIDER") return `provider label: ${c.provider_label ?? "—"}`;
  const matches = c.evidence.matches ?? [];
  if (matches.length === 0) return "no rule matched";
  return matches.map((m) => `${m.field}: “${m.text}” (${m.rule})`).join("; ");
}
