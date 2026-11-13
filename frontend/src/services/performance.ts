import type { NotificationFeed, Performance } from "../types/performance";
import { buildUrl, getJson } from "./api";

type Opts = { signal?: AbortSignal };
const account = (accountId: string | null) => (accountId ? { account_id: accountId } : {});

/** Paper-account performance (spec 22.6) and the notification feed (spec 23); read-only. */
export const performance = {
  report: (accountId: string | null, opts: Opts = {}) =>
    getJson<Performance>(buildUrl("/api/v1/portfolio/performance", account(accountId)), opts),
  notifications: (accountId: string | null, kind: string | null, opts: Opts = {}) =>
    getJson<NotificationFeed>(
      buildUrl("/api/v1/notifications", { ...account(accountId), limit: 200, ...(kind ? { kind } : {}) }),
      opts,
    ),
};
