import type { Balance, Market, Order, PriceLevel } from "./types";

export const paperSwapBalanceAsset = "USDT";

export function toNumber(value: string | number | null | undefined): number {
  if (value === null || value === undefined || value === "") return 0;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function formatNumber(value: string | number | null | undefined, precision = 2): string {
  const parsed = toNumber(value);
  return new Intl.NumberFormat("en-US", {
    minimumFractionDigits: precision,
    maximumFractionDigits: precision,
  }).format(parsed);
}

export function formatCompact(value: string | number | null | undefined): string {
  return new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: 2,
  }).format(toNumber(value));
}

export function formatUsd(value: string | number | null | undefined, precision = 2): string {
  return `$${formatNumber(value, precision)}`;
}

export function formatCrypto(value: string | number | null | undefined, precision = 8): string {
  const parsed = toNumber(value);
  return new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: precision,
  }).format(parsed);
}

export function pct(value: number): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

export function shortId(id: string): string {
  return `${id.slice(0, 8)}...${id.slice(-4)}`;
}

export function orderFillPct(order: Order): number {
  const qty = toNumber(order.quantity);
  if (qty <= 0) return 0;
  return Math.min(100, (toNumber(order.filled_quantity) / qty) * 100);
}

export function marketPrice(bestBid?: string | null, bestAsk?: string | null): number {
  const bid = toNumber(bestBid);
  const ask = toNumber(bestAsk);
  if (bid && ask) return (bid + ask) / 2;
  return bid || ask || 0;
}

export function balanceMap(balances: Balance[]): Record<string, Balance> {
  return Object.fromEntries(balances.map((balance) => [balance.asset, balance]));
}

export function portfolioBalanceAsset(markets: Market[]): string {
  return markets.some(
    (market) =>
      market.quote_asset === paperSwapBalanceAsset &&
      market.metadata.product_type === "perpetual" &&
      market.metadata.execution_mode === "paper",
  )
    ? paperSwapBalanceAsset
    : "USD";
}

export function balanceValue(balance: Balance, markets: Market[], valuationAsset = "USD"): number {
  if (balance.asset === valuationAsset) return toNumber(balance.total);
  const market = markets.find(
    (item) => item.base_asset === balance.asset && item.quote_asset === valuationAsset,
  );
  return market ? 0 : 0;
}

export function chartFeedStatus(candleCount: number, loading: boolean, error?: string | null): string {
  if (error) return "Unavailable";
  if (loading && candleCount === 0) return "Syncing";
  return "Ready";
}

export function levelTotal(levels: PriceLevel[], index: number): number {
  return levels.slice(0, index + 1).reduce((sum, level) => sum + toNumber(level.total_quantity), 0);
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function formatTime(value: string): string {
  return new Intl.DateTimeFormat("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

export function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}
