/** /api/v1/portfolio/performance and /api/v1/notifications (read-only). */

export interface TradeStats {
  trades: number;
  wins: number;
  losses: number;
  win_rate: string | null;
  net_pnl: string;
  realized_pnl: string;
  costs: string;
  average_win: string | null;
  average_loss: string | null;
  profit_factor: string | null;
  expectancy: string | null;
}

export interface Performance {
  account_id: string;
  account_name: string;
  currency: string;
  simulated: true;
  disclaimer: string;
  curve: {
    days: number;
    checkpoints_excluded: number;
    first_day: string | null;
    last_day: string | null;
    points: { day: string; equity: string }[];
    drawdown: { day: string; drawdown: string | null }[];
  };
  summary: {
    total_return: string | null;
    max_drawdown: string | null;
    current_drawdown: string | null;
    daily_returns: number;
    minimum: number;
    sharpe: string | null;
    sortino: string | null;
    volatility_annualized: string | null;
    reason: string | null;
  };
  monthly_returns: { month: string; return: string | null; end_equity: string }[];
  trades: TradeStats;
  by_strategy: (TradeStats & { strategy_id: string })[];
  by_direction: (TradeStats & { direction: string })[];
  by_exit_reason: (TradeStats & { exit_reason: string })[];
  recent_trades: {
    strategy_id: string | null;
    direction: string;
    exit_reason: string | null;
    opened_at: string;
    closed_at: string;
    holding_days: number;
    net_pnl: string;
    costs: string;
  }[];
  benchmark: {
    symbol: string;
    return: string | null;
    start: string | null;
    end: string | null;
    account_return_same_period?: string | null;
    excess_return?: string | null;
    reason: string | null;
  };
}

export interface NotificationItem {
  id: string;
  kind: string;
  severity: "INFO" | "WARNING" | "HIGH" | "CRITICAL" | "LOW" | "MEDIUM";
  occurred_at: string;
  title: string;
  message: string;
  subject: Record<string, unknown>;
  simulated: true;
}

export interface NotificationFeed {
  account_id: string;
  kinds: string[];
  items: NotificationItem[];
  note: string;
}
