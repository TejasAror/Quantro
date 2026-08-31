export type UUID = string;

export type Balance = {
  asset: string;
  free: string;
  locked: string;
  total: string;
};

export type Account = {
  id: UUID;
  name: string;
  balances: Record<string, Balance>;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
};

export type AuthSession = {
  access_token: string | null;
  refresh_token: string | null;
  token_type: string;
  expires_in: number | null;
  user_id: UUID;
  account: Account;
};

export type Market = {
  symbol: string;
  base_asset: string;
  quote_asset: string;
  venue: string;
  price_precision: number;
  quantity_precision: number;
  min_order_size: string;
  max_order_size: string;
  tick_size: string;
  lot_size: string;
  maker_fee: string;
  taker_fee: string;
  is_active: boolean;
  metadata: Record<string, unknown>;
};

export type OrderSide = "buy" | "sell";
export type OrderType = "limit" | "market" | "stop" | "stop_limit";
export type TimeInForce = "gtc" | "ioc" | "fok" | "gtd";

export type Order = {
  id: UUID;
  account_id: UUID;
  symbol: string;
  side: OrderSide;
  order_type: OrderType;
  status: string;
  time_in_force: TimeInForce;
  quantity: string;
  price: string;
  stop_price: string;
  filled_quantity: string;
  remaining_quantity: string;
  average_fill_price: string;
  total_fees: string;
  client_order_id: string;
  created_at: string;
  updated_at: string;
  expires_at: string | null;
  metadata: Record<string, unknown>;
};

export type Trade = {
  id: UUID;
  order_id: UUID;
  account_id: UUID;
  symbol: string;
  side: OrderSide;
  quantity: string;
  price: string;
  notional: string;
  fee: string;
  fee_asset: string;
  timestamp: string;
  is_maker: boolean;
  metadata: Record<string, unknown>;
};

export type Position = {
  id: UUID;
  account_id: UUID;
  symbol: string;
  side: string;
  size: string;
  entry_price: string;
  mark_price: string;
  unrealized_pnl: string;
  realized_pnl: string;
  leverage: string;
  liquidation_price: string;
  opened_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
};

export type Pnl = {
  account_id: UUID;
  total_unrealized_pnl: string;
  total_realized_pnl: string;
  total_pnl: string;
};

export type PriceLevel = {
  price: string;
  total_quantity: string;
  order_count: number;
  orders: Order[];
};

export type OrderBook = {
  symbol: string;
  sequence: number | null;
  best_bid: string | null;
  best_ask: string | null;
  spread: string | null;
  mid_price: string | null;
  last_trade_price: string;
  last_trade_quantity: string;
  bids: PriceLevel[];
  asks: PriceLevel[];
  exchange_timestamp?: string | null;
  received_timestamp?: string | null;
  status?: "syncing" | "synced" | "stale" | "resyncing" | "disconnected" | "unavailable";
  venue?: string | null;
};

export type MarketTicker = {
  symbol: string;
  last_price: string | null;
  bid_price: string | null;
  ask_price: string | null;
  high_24h: string | null;
  low_24h: string | null;
  volume_24h: string | null;
  change_24h: string | null;
  mark_price: string | null;
  index_price: string | null;
  funding_rate: string | null;
  open_interest: string | null;
  exchange_timestamp: string | null;
  received_timestamp: string;
  status: "syncing" | "synced" | "stale" | "resyncing" | "disconnected" | "unavailable";
};

export type PublicTrade = {
  id: string;
  symbol: string;
  price: string;
  quantity: string;
  side: string;
  exchange_timestamp: string | null;
  received_timestamp: string;
};

export type Candle = {
  symbol: string;
  interval: string;
  timestamp: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
  is_closed: boolean;
};

export type OrderResult = {
  accepted: boolean;
  reject_reason: string | null;
  order: Order;
  risk_report: {
    overall: string;
    passed: boolean;
    checks: Array<{
      name: string;
      result: string;
      message: string;
      limit: string | null;
      current: string | null;
    }>;
  };
  trades: Trade[];
};

export type ApiError = {
  error: {
    code: string;
    message: string;
  };
  details?: unknown;
};

export type Toast = {
  id: number;
  tone: "success" | "error" | "info";
  message: string;
};
