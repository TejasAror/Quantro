import type {
  Account,
  ApiError,
  AuthSession,
  Balance,
  Candle,
  Market,
  MarketTicker,
  Order,
  OrderBook,
  OrderResult,
  OrderSide,
  OrderType,
  Pnl,
  Position,
  PublicTrade,
  TimeInForce,
  Trade,
  UUID,
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";
const TOKEN_KEY = "quantro.accessToken";
const REFRESH_TOKEN_KEY = "quantro.refreshToken";
const EXPIRES_AT_KEY = "quantro.expiresAt";
const ACCOUNT_KEY = "quantro.account";
const REQUEST_TIMEOUT_MS = 12000;

export class QuantroApiError extends Error {
  code: string;
  status: number;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "QuantroApiError";
    this.status = status;
    this.code = code;
  }
}

export type OrderPayload = {
  account_id?: UUID;
  symbol: string;
  side: OrderSide;
  order_type: OrderType;
  time_in_force: TimeInForce;
  quantity: string;
  price?: string;
  stop_price?: string;
  metadata?: Record<string, unknown>;
};

function authHeaders(): HeadersInit {
  const token = localStorage.getItem(TOKEN_KEY);
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const controller = new AbortController();
  const timeoutId = globalThis.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  const signal = options.signal;
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener("abort", () => controller.abort(), { once: true });
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...options,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(),
        ...options.headers,
      },
    });
  } catch (err) {
    if (controller.signal.aborted) {
      throw new QuantroApiError(0, "request_timeout", "Request timed out. Check the Quantro backend connection.");
    }
    throw err;
  } finally {
    globalThis.clearTimeout(timeoutId);
  }

  if (!response.ok) {
    let body: ApiError | undefined;
    try {
      body = (await response.json()) as ApiError;
    } catch {
      body = undefined;
    }
    const error = new QuantroApiError(
      response.status,
      body?.error?.code ?? "request_failed",
      body?.error?.message ?? `Request failed with ${response.status}`,
    );
    if (
      response.status === 401
      && path !== "/auth/login"
      && path !== "/auth/signup"
      && typeof window !== "undefined"
    ) {
      window.dispatchEvent(new CustomEvent("quantro:auth-invalid", { detail: error.message }));
    }
    throw error;
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const sessionStore = {
  getToken: () => localStorage.getItem(TOKEN_KEY),
  getRefreshToken: () => localStorage.getItem(REFRESH_TOKEN_KEY),
  isExpired: () => {
    const expiresAt = Number(localStorage.getItem(EXPIRES_AT_KEY) ?? "0");
    return Boolean(expiresAt) && Date.now() >= expiresAt;
  },
  getAccount: (): Account | null => {
    const raw = localStorage.getItem(ACCOUNT_KEY);
    if (!raw) return null;
    try {
      return JSON.parse(raw) as Account;
    } catch {
      localStorage.removeItem(ACCOUNT_KEY);
      return null;
    }
  },
  set(session: AuthSession) {
    if (session.access_token) localStorage.setItem(TOKEN_KEY, session.access_token);
    if (session.refresh_token) localStorage.setItem(REFRESH_TOKEN_KEY, session.refresh_token);
    if (session.expires_in) {
      localStorage.setItem(EXPIRES_AT_KEY, String(Date.now() + session.expires_in * 1000));
    }
    localStorage.setItem(ACCOUNT_KEY, JSON.stringify(session.account));
  },
  setAccount(account: Account) {
    localStorage.setItem(ACCOUNT_KEY, JSON.stringify(account));
  },
  clear() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(REFRESH_TOKEN_KEY);
    localStorage.removeItem(EXPIRES_AT_KEY);
    localStorage.removeItem(ACCOUNT_KEY);
  },
};

export const api = {
  signup(email: string, password: string, name?: string) {
    return request<AuthSession>("/auth/signup", {
      method: "POST",
      body: JSON.stringify({ email, password, name }),
    });
  },
  login(email: string, password: string) {
    return request<AuthSession>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
  },
  logout() {
    return request<void>("/auth/logout", { method: "POST" });
  },
  me() {
    return request<Account>("/me/account");
  },
  createSandboxAccount(name: string) {
    return request<Account>("/accounts", {
      method: "POST",
      body: JSON.stringify({
        name,
        initial_balances: { USD: "100000", USDT: "1000000", BTC: "10", ETH: "100" },
      }),
    });
  },
  deposit(accountId: UUID, asset: string, amount: string) {
    return request<Account>(`/accounts/${accountId}/deposit`, {
      method: "POST",
      body: JSON.stringify({ asset: asset.toUpperCase(), amount }),
    });
  },
  markets() {
    return request<Market[]>("/markets");
  },
  orderBook(symbol: string, depth = 20) {
    return request<OrderBook>(`/markets/${encodeURIComponent(symbol)}/orderbook?depth=${depth}`);
  },
  ticker(symbol: string) {
    return request<MarketTicker>(`/markets/${encodeURIComponent(symbol)}/ticker`);
  },
  marketTrades(symbol: string, limit = 50) {
    return request<{ symbol: string; trades: PublicTrade[] }>(
      `/markets/${encodeURIComponent(symbol)}/trades?limit=${limit}`,
    );
  },
  candles(symbol: string, interval: string, limit = 300) {
    return request<{ symbol: string; interval: string; candles: Candle[] }>(
      `/markets/${encodeURIComponent(symbol)}/candles?interval=${encodeURIComponent(interval)}&limit=${limit}`,
    );
  },
  submitOrder(payload: OrderPayload) {
    return request<OrderResult>("/orders", {
      method: "POST",
      body: JSON.stringify({
        ...payload,
        price: payload.price ?? "0",
        stop_price: payload.stop_price ?? "0",
      }),
    });
  },
  getOrder(orderId: UUID) {
    return request<Order>(`/orders/${orderId}`);
  },
  cancelOrder(orderId: UUID) {
    return request<Order>(`/orders/${orderId}`, { method: "DELETE" });
  },
  orders(accountId: UUID) {
    return request<{ orders: Order[] }>(`/accounts/${accountId}/orders`);
  },
  trades(accountId: UUID) {
    return request<{ trades: Trade[] }>(`/accounts/${accountId}/trades`);
  },
  balances(accountId: UUID) {
    return request<{ account_id: UUID; balances: Balance[] }>(`/accounts/${accountId}/balances`);
  },
  positions(accountId: UUID) {
    return request<{ account_id: UUID; positions: Position[] }>(
      `/accounts/${accountId}/positions`,
    );
  },
  pnl(accountId: UUID) {
    return request<Pnl>(`/accounts/${accountId}/pnl`);
  },
};
