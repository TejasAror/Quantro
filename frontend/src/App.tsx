import {
  Bell,
  CandlestickChart,
  Check,
  ChevronDown,
  CircleUserRound,
  Clock3,
  Code2,
  Crosshair,
  DatabaseZap,
  Landmark,
  Maximize2,
  Search,
  Settings,
  SlidersHorizontal,
  Star,
  X,
  Zap,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  ColorType,
  CrosshairMode,
  LineStyle,
  createChart,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { api, QuantroApiError, sessionStore, type OrderPayload } from "./api";
import { usePollingResource } from "./hooks";
import type {
  Account,
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
  Toast,
  Trade,
} from "./types";
import {
  balanceMap,
  balanceValue,
  chartFeedStatus,
  clamp,
  formatCrypto,
  formatDateTime,
  formatNumber,
  formatTime,
  formatUsd,
  levelTotal,
  marketPrice,
  orderFillPct,
  pct,
  portfolioBalanceAsset,
  shortId,
  toNumber,
} from "./utils";

type Page = "trade" | "markets" | "portfolio" | "orders" | "account" | "api";
type BottomTab = "open" | "orders" | "trades" | "positions" | "pnl";

const refreshMs = 4500;
const allowSandboxAuth = import.meta.env.VITE_ENABLE_SANDBOX_AUTH === "true";
const terminalSymbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"];

export async function submitPaperOrderAndRefresh({
  accountId,
  payload,
  submitOrder,
  onAccepted,
  refreshAccountData,
  onRefreshError,
}: {
  accountId: string;
  payload: OrderPayload;
  submitOrder: (payload: OrderPayload) => Promise<OrderResult>;
  onAccepted: (result: OrderResult) => void;
  refreshAccountData: () => Promise<void>;
  onRefreshError?: (error: unknown) => void;
}): Promise<Order> {
  const result = await submitOrder({ ...payload, account_id: accountId });
  onAccepted(result);
  void refreshAccountData().catch((error) => {
    onRefreshError?.(error);
  });
  return result.order;
}

export default function App() {
  const [account, setAccount] = useState<Account | null>(null);
  const [page, setPage] = useState<Page>(() => pageFromPath(window.location.pathname));
  const [selectedSymbol, setSelectedSymbol] = useState("BTC-USDT-SWAP");
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [authStatus, setAuthStatus] = useState<"loading" | "authenticated" | "unauthenticated">("loading");
  const [authNotice, setAuthNotice] = useState<string | null>(null);

  const pushToast = (tone: Toast["tone"], message: string) => {
    const id = Date.now();
    setToasts((items) => [...items, { id, tone, message }]);
    window.setTimeout(() => setToasts((items) => items.filter((toast) => toast.id !== id)), 3600);
  };

  const markets = usePollingResource(() => api.markets(), [], refreshMs);
  const orderBook = usePollingResource(() => api.orderBook(selectedSymbol, 24), [selectedSymbol], 2500);
  const ticker = usePollingResource(() => api.ticker(selectedSymbol), [selectedSymbol], 2500);
  const publicTrades = usePollingResource(
    () => api.marketTrades(selectedSymbol, 40).then((r) => r.trades),
    [selectedSymbol],
    2500,
  );
  const balances = usePollingResource(
    () => account ? api.balances(account.id).then((r) => r.balances) : Promise.resolve([]),
    [account?.id],
    refreshMs,
  );
  const orders = usePollingResource(
    () => account ? api.orders(account.id).then((r) => r.orders) : Promise.resolve([]),
    [account?.id],
    refreshMs,
  );
  const trades = usePollingResource(
    () => account ? api.trades(account.id).then((r) => r.trades) : Promise.resolve([]),
    [account?.id],
    refreshMs,
  );
  const positions = usePollingResource(
    () => account ? api.positions(account.id).then((r) => r.positions) : Promise.resolve([]),
    [account?.id],
    refreshMs,
  );
  const pnl = usePollingResource(
    () => account ? api.pnl(account.id) : Promise.resolve(null as unknown as Pnl),
    [account?.id],
    refreshMs,
  );

  const navigate = (next: Page) => {
    setPage(next);
    window.history.pushState(null, "", next === "trade" ? "/trade" : `/${next}`);
  };

  useEffect(() => {
    let cancelled = false;
    async function restoreSession() {
      const token = sessionStore.getToken();
      const cachedAccount = sessionStore.getAccount();
      if (!token) {
        if (allowSandboxAuth && cachedAccount) {
          setAccount(cachedAccount);
          setAuthStatus("authenticated");
          return;
        }
        sessionStore.clear();
        setAuthStatus("unauthenticated");
        window.history.replaceState(null, "", "/login");
        return;
      }
      if (sessionStore.isExpired()) {
        sessionStore.clear();
        setAuthNotice("Session expired. Sign in again.");
        setAuthStatus("unauthenticated");
        window.history.replaceState(null, "", "/login");
        return;
      }
      try {
        const next = await api.me();
        if (cancelled) return;
        sessionStore.setAccount(next);
        setAccount(next);
        setAuthStatus("authenticated");
        if (window.location.pathname === "/" || window.location.pathname === "/login" || window.location.pathname === "/signup") {
          window.history.replaceState(null, "", "/trade");
          setPage("trade");
        }
      } catch (err) {
        if (cancelled) return;
        sessionStore.clear();
        setAccount(null);
        setAuthNotice(err instanceof QuantroApiError ? err.message : "Authentication required.");
        setAuthStatus("unauthenticated");
        window.history.replaceState(null, "", "/login");
      }
    }
    void restoreSession();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    function handleInvalidAuth(event: Event) {
      sessionStore.clear();
      setAccount(null);
      setAuthNotice(event instanceof CustomEvent && typeof event.detail === "string" ? event.detail : "Session expired. Sign in again.");
      setAuthStatus("unauthenticated");
      window.history.replaceState(null, "", "/login");
    }
    window.addEventListener("quantro:auth-invalid", handleInvalidAuth);
    return () => window.removeEventListener("quantro:auth-invalid", handleInvalidAuth);
  }, []);

  useEffect(() => {
    const terminalMarket = markets.data?.find((market) => terminalSymbols.includes(market.symbol));
    const first = terminalMarket?.symbol ?? markets.data?.[0]?.symbol;
    if (first && !markets.data?.some((market) => market.symbol === selectedSymbol)) {
      setSelectedSymbol(first);
    }
  }, [markets.data, selectedSymbol]);

  const activeMarket = markets.data?.find((market) => market.symbol === selectedSymbol) ?? markets.data?.[0] ?? null;
  const visiblePage = account ? page : "trade";

  if (authStatus === "loading") {
    return <AuthLoading />;
  }

  if (!account || authStatus === "unauthenticated") {
    return (
      <>
        <AuthScreen
          onAuthenticated={(next) => {
            setAccount(next);
            setAuthStatus("authenticated");
            navigate("trade");
            pushToast("success", "Session ready");
          }}
          onToast={pushToast}
          notice={authNotice}
          allowSandbox={allowSandboxAuth}
        />
        <ToastStack toasts={toasts} />
      </>
    );
  }

  const refreshAccountData = async () => {
    await Promise.all([
      balances.refresh(),
      orders.refresh(),
      trades.refresh(),
      positions.refresh(),
      pnl.refresh(),
      orderBook.refresh(),
    ]);
  };

  return (
    <div className="app-shell">
      <TopNav
        account={account}
        balances={balances.data ?? []}
        page={page}
        setPage={navigate}
        onLogout={async () => {
          try {
            if (sessionStore.getToken()) await api.logout();
          } catch {
            pushToast("info", "Session cleared locally");
          }
          sessionStore.clear();
          setAccount(null);
          setAuthStatus("unauthenticated");
          window.history.replaceState(null, "", "/login");
          pushToast("info", "Signed out");
        }}
      />

      {visiblePage === "trade" && (
        <TradingLayout
          account={account}
          markets={markets.data ?? []}
          marketsLoading={markets.loading}
          activeMarket={activeMarket}
          selectedSymbol={selectedSymbol}
          setSelectedSymbol={setSelectedSymbol}
          setPage={navigate}
          orderBook={orderBook.data}
          orderBookLoading={orderBook.loading}
          orderBookError={orderBook.error}
          ticker={ticker.data}
          tickerError={ticker.error}
          publicTrades={publicTrades.data ?? []}
          publicTradesError={publicTrades.error}
          balances={balances.data ?? []}
          orders={orders.data ?? []}
          trades={trades.data ?? []}
          positions={positions.data ?? []}
          pnl={pnl.data}
          onSubmitOrder={async (payload) => {
            return submitPaperOrderAndRefresh({
              accountId: account.id,
              payload,
              submitOrder: api.submitOrder,
              refreshAccountData,
              onAccepted: (result) => {
                orders.setData((items) => [
                  result.order,
                  ...(items ?? []).filter((order) => order.id !== result.order.id),
                ]);
                trades.setData((items) => [
                  ...result.trades.filter(
                    (trade) => !(items ?? []).some((item) => item.id === trade.id),
                  ),
                  ...(items ?? []),
                ]);
                pushToast("success", `${result.order.side.toUpperCase()} ${result.order.symbol} accepted`);
              },
              onRefreshError: () => {
                pushToast("info", "Order accepted; portfolio refresh is still syncing");
              },
            });
          }}
          onCancelOrder={async (orderId) => {
            await api.cancelOrder(orderId);
            orders.setData((items) => items?.map((order) => order.id === orderId ? { ...order, status: "cancelled" } : order) ?? []);
            pushToast("success", "Order cancelled");
            await refreshAccountData();
          }}
          onToast={pushToast}
        />
      )}

      {visiblePage === "markets" && (
        <MainPanel>
          <MarketTable
            markets={markets.data ?? []}
            books={selectedSymbol === orderBook.data?.symbol ? [orderBook.data] : []}
            ticker={ticker.data}
            loading={markets.loading}
            onTrade={(symbol) => {
              setSelectedSymbol(symbol);
              navigate("trade");
            }}
          />
        </MainPanel>
      )}

      {visiblePage === "portfolio" && (
        <MainPanel>
          <PortfolioOverview
            account={account}
            balances={balances.data ?? []}
            markets={markets.data ?? []}
            pnl={pnl.data}
            positions={positions.data ?? []}
            orders={orders.data ?? []}
            trades={trades.data ?? []}
            loading={balances.loading || pnl.loading}
          />
        </MainPanel>
      )}

      {visiblePage === "orders" && (
        <MainPanel>
          <BottomWorkspace
            defaultTab="orders"
            orders={orders.data ?? []}
            trades={trades.data ?? []}
            positions={positions.data ?? []}
            pnl={pnl.data}
            onCancelOrder={async (orderId) => {
              await api.cancelOrder(orderId);
              pushToast("success", "Order cancelled");
              await refreshAccountData();
            }}
          />
        </MainPanel>
      )}

      {visiblePage === "account" && (
        <MainPanel>
          <AccountPanel account={account} balances={balances.data ?? []} />
        </MainPanel>
      )}

      {visiblePage === "api" && (
        <MainPanel>
          <ApiPanel />
        </MainPanel>
      )}

      <ToastStack toasts={toasts} />
    </div>
  );
}

function pageFromPath(pathname: string): Page {
  const page = pathname.replace("/", "");
  if (page === "markets" || page === "portfolio" || page === "orders" || page === "account" || page === "api") {
    return page;
  }
  return "trade";
}

function AuthLoading() {
  return (
    <main className="auth-page">
      <section className="auth-terminal">
        <div className="brand-lockup">
          <div className="brand-mark">Q</div>
          <div>
            <strong>Quantro</strong>
            <span>Loading authenticated session</span>
          </div>
        </div>
        <div className="skeleton-stack"><span /><span /><span /></div>
      </section>
    </main>
  );
}

function AuthScreen({ onAuthenticated, onToast, notice, allowSandbox }: {
  onAuthenticated: (account: Account) => void;
  onToast: (tone: Toast["tone"], message: string) => void;
  notice: string | null;
  allowSandbox: boolean;
}) {
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    if (!email.includes("@")) return setError("Enter a valid email address.");
    if (password.length < 6) return setError("Password must be at least 6 characters.");
    setLoading(true);
    try {
      const session = mode === "login" ? await api.login(email, password) : await api.signup(email, password, name || undefined);
      sessionStore.set(session);
      if (!session.access_token) {
        throw new QuantroApiError(401, "session_unavailable", "Authentication succeeded but no access token was returned.");
      }
      const account = await api.me();
      sessionStore.setAccount(account);
      onAuthenticated(account);
    } catch (err) {
      const message = err instanceof QuantroApiError ? err.message : "Authentication failed";
      setError(message);
    } finally {
      setLoading(false);
    }
  }

  async function startSandbox() {
    setLoading(true);
    setError(null);
    try {
      const account = await api.createSandboxAccount("Quantro Sandbox");
      sessionStore.setAccount(account);
      onAuthenticated(account);
      onToast("info", "Using local sandbox account");
    } catch (err) {
      setError(err instanceof QuantroApiError ? err.message : "Unable to create sandbox account");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="auth-page">
      <section className="auth-terminal">
        <div className="brand-lockup">
          <div className="brand-mark">Q</div>
          <div>
            <strong>Quantro</strong>
            <span>Institutional crypto execution sandbox</span>
          </div>
        </div>
        {notice && <div className="auth-notice">{notice}</div>}
        <div className="auth-copy">
          <p>Trading infrastructure for order routing, portfolio state, and deterministic execution.</p>
        </div>
        <form className="auth-form" onSubmit={submit}>
          <div className="segmented">
            <button type="button" className={mode === "login" ? "active" : ""} onClick={() => setMode("login")}>LOGIN</button>
            <button type="button" className={mode === "signup" ? "active" : ""} onClick={() => setMode("signup")}>SIGN UP</button>
          </div>
          {mode === "signup" && (
            <label>
              Name
              <input value={name} onChange={(event) => setName(event.target.value)} autoComplete="name" />
            </label>
          )}
          <label>
            Email
            <input value={email} onChange={(event) => setEmail(event.target.value)} type="email" autoComplete="email" />
          </label>
          <label>
            Password
            <input value={password} onChange={(event) => setPassword(event.target.value)} type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} />
          </label>
          {error && <div className="form-error">{error}</div>}
          <button className="primary-action" type="submit" disabled={loading}>{loading ? "AUTHENTICATING" : mode === "login" ? "LOGIN" : "CREATE ACCOUNT"}</button>
          {allowSandbox && <button className="ghost-action" type="button" onClick={startSandbox} disabled={loading}>START SANDBOX ACCOUNT</button>}
        </form>
      </section>
    </main>
  );
}

function TopNav({ account, balances, page, setPage, onLogout }: {
  account: Account;
  balances: Balance[];
  page: Page;
  setPage: (page: Page) => void;
  onLogout: () => void | Promise<void>;
}) {
  const usdt = balances.find((balance) => balance.asset === "USDT");
  return (
    <header className="top-nav">
      <button className="nav-brand" onClick={() => setPage("trade")}>
        <span className="brand-mark small">Q</span>
        <span>Quantro</span>
      </button>
      <nav>
        {(["markets", "trade", "portfolio", "orders", "api"] as Page[]).map((item) => (
          <button key={item} className={page === item ? "selected" : ""} onClick={() => setPage(item)}>{item}</button>
        ))}
      </nav>
      <div className="search-box">
        <Search size={15} />
        <input placeholder="Search markets" />
      </div>
      <div className="nav-balance">
        <span>Virtual USDT</span>
        <strong>{formatNumber(usdt?.free ?? 0)} USDT</strong>
      </div>
      <button className="icon-button" aria-label="Notifications"><Bell size={17} /></button>
      <button className="user-menu" onClick={() => setPage("account")}>
        <CircleUserRound size={17} />
        <span>{account.name}</span>
        <ChevronDown size={14} />
      </button>
      <button className="ghost-nav" onClick={onLogout}>Logout</button>
    </header>
  );
}

function TradingLayout(props: {
  account: Account;
  markets: Market[];
  marketsLoading: boolean;
  activeMarket: Market | null;
  selectedSymbol: string;
  setSelectedSymbol: (symbol: string) => void;
  setPage: (page: Page) => void;
  orderBook: OrderBook | null;
  orderBookLoading: boolean;
  orderBookError: string | null;
  ticker: MarketTicker | null;
  tickerError: string | null;
  publicTrades: PublicTrade[];
  publicTradesError: string | null;
  balances: Balance[];
  orders: Order[];
  trades: Trade[];
  positions: Position[];
  pnl: Pnl | null;
  onSubmitOrder: (payload: OrderPayload) => Promise<Order>;
  onCancelOrder: (orderId: string) => Promise<void>;
  onToast: (tone: Toast["tone"], message: string) => void;
}) {
  const terminalMarkets = props.markets.filter((market) => terminalSymbols.includes(market.symbol));
  const currentPrice = toNumber(props.ticker?.mark_price) || toNumber(props.ticker?.last_price) || marketPrice(props.orderBook?.best_bid, props.orderBook?.best_ask);
  const tickerStatus = marketDataStatus(props.ticker?.status, props.ticker?.received_timestamp ?? null, props.tickerError);
  const bookStatus = marketDataStatus(props.orderBook?.status, props.orderBook?.received_timestamp ?? null, props.orderBookError);
  const marketDataHealthy = tickerStatus.label === "Healthy" && bookStatus.label === "Healthy";
  const marketDataIssue = marketDataHealthy
    ? "Healthy"
    : `Ticker ${tickerStatus.label} (${tickerStatus.detail}); Book ${bookStatus.label} (${bookStatus.detail})`;
  return (
    <main className="terminal-grid">
      <MarketSidebar
        markets={terminalMarkets}
        loading={props.marketsLoading}
        selectedSymbol={props.selectedSymbol}
        ticker={props.ticker}
        orderBook={props.orderBook}
        onSelect={props.setSelectedSymbol}
      />
      <section className="terminal-center">
        <DemoSafetyBanner account={props.account} />
        <MarketSelector
          markets={terminalMarkets}
          selectedSymbol={props.selectedSymbol}
          onSelect={props.setSelectedSymbol}
        />
        <MarketHeader
          market={props.activeMarket}
          book={props.orderBook}
          ticker={props.ticker}
          bookError={props.orderBookError}
          tickerError={props.tickerError}
        />
        <TradingChart market={props.activeMarket} />
        <BottomWorkspace
          defaultTab="open"
          orders={props.orders}
          trades={props.trades}
          positions={props.positions}
          pnl={props.pnl}
          symbol={props.selectedSymbol}
          currentPrice={currentPrice}
          onCancelOrder={props.onCancelOrder}
        />
      </section>
      <aside className="terminal-right">
        <OrderBookPanel book={props.orderBook} loading={props.orderBookLoading} error={props.orderBookError} />
        <RecentTrades trades={props.publicTrades} symbol={props.selectedSymbol} error={props.publicTradesError} />
      </aside>
      <aside className="terminal-ticket">
        <OrderEntry
          market={props.activeMarket}
          balances={props.balances}
          book={props.orderBook}
          marketDataHealthy={marketDataHealthy}
          marketDataIssue={marketDataIssue}
          onSubmit={props.onSubmitOrder}
          onToast={props.onToast}
        />
      </aside>
    </main>
  );
}

function DemoSafetyBanner({ account }: { account: Account }) {
  return (
    <div className="demo-safety-banner">
      <strong>DEMO TRADING — NO REAL FUNDS</strong>
      <span>Virtual sandbox account {shortId(account.id)}. Order routing to live venues is disabled.</span>
    </div>
  );
}

function MarketSelector({ markets, selectedSymbol, onSelect }: {
  markets: Market[];
  selectedSymbol: string;
  onSelect: (symbol: string) => void;
}) {
  return (
    <div className="market-selector panel">
      <span>Market</span>
      <div>
        {markets.map((market) => (
          <button
            key={market.symbol}
            className={selectedSymbol === market.symbol ? "active" : ""}
            onClick={() => onSelect(market.symbol)}
          >
            {market.symbol}
          </button>
        ))}
      </div>
    </div>
  );
}

function MarketSidebar({ markets, loading, selectedSymbol, ticker, orderBook, onSelect }: {
  markets: Market[];
  loading: boolean;
  selectedSymbol: string;
  ticker: MarketTicker | null;
  orderBook: OrderBook | null;
  onSelect: (symbol: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<"perpetuals" | "all">("perpetuals");
  const filteredMarkets = markets.filter((market) => {
    const matchesQuery = market.symbol.toLowerCase().includes(query.toLowerCase());
    const isPerp = market.metadata.product_type === "perpetual";
    if (category === "perpetuals") return matchesQuery && isPerp;
    return matchesQuery;
  });

  return (
    <aside className="market-sidebar panel">
      <div className="panel-title">
        <span>Watchlist</span>
        <Star size={15} />
      </div>
      <div className="sidebar-search">
        <Search size={14} />
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search markets" />
      </div>
      <div className="sidebar-tabs">
        {(["perpetuals", "all"] as const).map((item) => (
          <button key={item} className={category === item ? "active" : ""} onClick={() => setCategory(item)}>{item}</button>
        ))}
      </div>
      {loading && <SkeletonRows count={8} />}
      <div className="market-list-head"><span>Pair</span><span>Last</span><span>24h</span><span>Vol</span></div>
      {!loading && filteredMarkets.map((market) => {
        const price = market.symbol === ticker?.symbol
          ? toNumber(ticker.last_price)
          : market.symbol === orderBook?.symbol ? marketPrice(orderBook.best_bid, orderBook.best_ask) : 0;
        const change = market.symbol === ticker?.symbol ? toNumber(ticker.change_24h) : 0;
        return (
          <button
            className={`market-row ${selectedSymbol === market.symbol ? "active" : ""}`}
            key={market.symbol}
            onClick={() => onSelect(market.symbol)}
          >
            <span>
              <strong>{market.symbol}</strong>
              <small>{market.venue} PAPER</small>
            </span>
            <span className="mono">{price ? formatUsd(price) : "Unavailable"}</span>
            <span className={change >= 0 ? "positive" : "negative"}>{ticker?.symbol === market.symbol && ticker.change_24h ? pct(change) : "--"}</span>
            <span className="muted mono">{ticker?.symbol === market.symbol && ticker.volume_24h ? formatCrypto(ticker.volume_24h, 0) : "--"}</span>
          </button>
        );
      })}
      {!loading && filteredMarkets.length === 0 && <EmptyState title="No markets found" compact />}
    </aside>
  );
}

function MarketHeader({ market, book, ticker, bookError, tickerError }: {
  market: Market | null;
  book: OrderBook | null;
  ticker: MarketTicker | null;
  bookError: string | null;
  tickerError: string | null;
}) {
  const price = toNumber(ticker?.last_price) || marketPrice(book?.best_bid, book?.best_ask);
  const change = toNumber(ticker?.change_24h);
  const status = marketDataStatus(ticker?.status ?? book?.status, ticker?.received_timestamp ?? book?.received_timestamp ?? null, tickerError ?? bookError);
  return (
    <div className="market-header panel">
      <div className="symbol-block">
        <button className="icon-button favorite" aria-label="Favorite"><Star size={16} /></button>
        <div>
          <h1>{market?.symbol ?? "MARKET"}</h1>
          <span>{market?.base_asset}/{market?.quote_asset} · Quantro paper execution</span>
        </div>
      </div>
      <div className="price-block">
        <strong className="mono">{price ? formatUsd(price, market?.price_precision ?? 2) : "--"}</strong>
        <span className={change >= 0 ? "positive" : "negative"}>{ticker?.change_24h ? pct(change) : "24h Unavailable"}</span>
      </div>
      <Metric label="Bid" value={ticker?.bid_price ? formatUsd(ticker.bid_price, market?.price_precision ?? 2) : "Unavailable"} />
      <Metric label="Ask" value={ticker?.ask_price ? formatUsd(ticker.ask_price, market?.price_precision ?? 2) : "Unavailable"} />
      <Metric label="24h High" value={ticker?.high_24h ? formatUsd(ticker.high_24h) : "Unavailable"} />
      <Metric label="24h Low" value={ticker?.low_24h ? formatUsd(ticker.low_24h) : "Unavailable"} />
      <Metric label="24h Volume" value={ticker?.volume_24h ? formatCrypto(ticker.volume_24h, 2) : "Unavailable"} />
      <Metric label="Mark Price" value={ticker?.mark_price ? formatUsd(ticker.mark_price) : "Unavailable"} />
      <Metric label="Index Price" value={ticker?.index_price ? formatUsd(ticker.index_price) : "Unavailable"} />
      <Metric label="Funding" value={ticker?.funding_rate ? `${formatNumber(toNumber(ticker.funding_rate) * 100, 4)}%` : "Unavailable"} />
      <Metric label="Open Interest" value={ticker?.open_interest ? formatCrypto(ticker.open_interest, 2) : "Unavailable"} />
      <Metric label="Spread" value={book?.spread ? formatUsd(book.spread) : "Unavailable"} />
      <div className={`execution-status ${status.tone}`}>
        <span />
        <strong>{status.label}</strong>
        <small>{status.detail}</small>
      </div>
    </div>
  );
}

function TradingChart({ market }: { market: Market | null }) {
  const [timeframe, setTimeframe] = useState("1h");
  const [fullscreen, setFullscreen] = useState(false);
  const [chartReady, setChartReady] = useState(false);
  const chartContainerRef = useRef<HTMLDivElement | null>(null);
  const chartHostRef = useRef<HTMLDivElement | null>(null);
  const chartApiRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const livePriceLineRef = useRef<IPriceLine | null>(null);
  const candleInterval = timeframe.toLowerCase();
  const candles = usePollingResource(
    () => market
      ? api.candles(market.symbol, candleInterval, 240).then((response) => {
        recordTradingChartDebug("api.candles:resolved", {
          symbol: market.symbol,
          interval: candleInterval,
          ...inspectCandles(response.candles),
        });
        return response.candles;
      })
      : Promise.resolve([]),
    [market?.symbol, candleInterval],
    15000,
  );
  const candleCount = candles.data?.length ?? 0;
  const currentCandle = (candles.data ?? []).slice().reverse().find((candle) => !candle.is_closed) ?? null;

  useEffect(() => {
    const container = chartContainerRef.current;
    const host = chartHostRef.current;
    if (!container || !host) return;

    let disposed = false;
    let chart: IChartApi | null = null;
    let animationFrame: number | null = null;
    const resizeChart = () => {
      if (disposed) return;
      const size = measurableChartContainerSize(container);
      if (!size) {
        recordTradingChartDebug("chart:waiting-for-size", {
          clientWidth: container.clientWidth,
          clientHeight: container.clientHeight,
        });
        return;
      }

      if (!chart) {
        recordTradingChartDebug("chart:init", size);
        chart = createChart(host, {
          width: size.width,
          height: size.height,
          layout: {
            background: { type: ColorType.Solid, color: "#080c12" },
            textColor: "#8792a3",
          },
          grid: {
            vertLines: { color: "#17202d" },
            horzLines: { color: "#17202d" },
          },
          crosshair: { mode: CrosshairMode.Normal },
          rightPriceScale: { borderColor: "#202939" },
          timeScale: {
            borderColor: "#202939",
            timeVisible: true,
            secondsVisible: false,
            rightOffset: 8,
          },
        });
        const candleSeries = chart.addCandlestickSeries({
          upColor: "#17b26a",
          downColor: "#f04438",
          borderUpColor: "#17b26a",
          borderDownColor: "#f04438",
          wickUpColor: "#17b26a",
          wickDownColor: "#f04438",
          priceLineVisible: true,
          lastValueVisible: true,
          priceLineColor: "#3b82f6",
        });
        const volumeSeries = chart.addHistogramSeries({
          color: "rgba(59, 130, 246, 0.32)",
          priceFormat: { type: "volume" },
          priceScaleId: "",
        });
        volumeSeries.priceScale().applyOptions({
          scaleMargins: { top: 0.78, bottom: 0 },
        });
        chartApiRef.current = chart;
        candleSeriesRef.current = candleSeries;
        volumeSeriesRef.current = volumeSeries;
        setChartReady(true);
      } else {
        recordTradingChartDebug("chart:resize", size);
        chart.resize(size.width, size.height, true);
      }
    };
    const scheduleResize = () => {
      if (animationFrame !== null) window.cancelAnimationFrame(animationFrame);
      animationFrame = window.requestAnimationFrame(() => {
        animationFrame = null;
        resizeChart();
      });
    };
    const observer = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(scheduleResize);
    observer?.observe(container);
observer?.observe(container.parentElement ?? container);
scheduleResize();

const delayedResize = window.setTimeout(scheduleResize, 0);
const delayedResize2 = window.setTimeout(scheduleResize, 100);

window.addEventListener("resize", scheduleResize);

    return () => {
      disposed = true;
      if (animationFrame !== null) window.cancelAnimationFrame(animationFrame);
window.clearTimeout(delayedResize);
window.clearTimeout(delayedResize2);
window.removeEventListener("resize", scheduleResize);
observer?.disconnect();
      chart?.remove();
      setChartReady(false);
      chartApiRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      livePriceLineRef.current = null;
    };
  }, []);

  useEffect(() => {
    const candleSeries = candleSeriesRef.current;
    const volumeSeries = volumeSeriesRef.current;
    const chart = chartApiRef.current;
    if (!chartReady || !chart || !candleSeries || !volumeSeries) return;
    if (candles.data === null) {
      recordTradingChartDebug("chart:setData:skip-null-state", {
        loading: candles.loading,
        error: candles.error,
      });
      return;
    }

    livePriceLineRef.current = applyTradingChartData({
      candles: candles.data,
      chart,
      candleSeries,
      volumeSeries,
      currentCandle,
      livePriceLine: livePriceLineRef.current,
    });
  }, [candles.data, candles.error, candles.loading, currentCandle, chartReady]);

  return (
    <section className={fullscreen ? "chart-panel panel fullscreen-chart" : "chart-panel panel"}>
      <div className="chart-toolbar">
        <div className="timeframes">
          {["1m", "5m", "15m", "1h", "4h", "1d"].map((item) => (
            <button key={item} className={timeframe === item ? "active" : ""} onClick={() => setTimeframe(item)}>{item}</button>
          ))}
        </div>
        <div className="tool-buttons">
          <button type="button" title="Indicators unavailable"><SlidersHorizontal size={15} />Indicators</button>
          <button type="button" title="Backend candle source"><CandlestickChart size={15} />Quantro OHLCV</button>
          <button type="button" title="Crosshair enabled"><Crosshair size={15} />Crosshair</button>
          <button aria-label="Fullscreen" onClick={() => setFullscreen((value) => !value)}><Maximize2 size={15} /></button>
          <button aria-label="Settings"><Settings size={15} /></button>
        </div>
      </div>
      <div ref={chartContainerRef} className="chart-canvas">
        <div className="chart-render-area">
          <div ref={chartHostRef} className="lightweight-chart" />
        </div>
        <div className="chart-status">
          <span>{timeframe.toUpperCase()}</span>
          <span>{currentCandle ? "Current candle updating" : "Closed candles"}</span>
          <span>{chartFeedStatus(candleCount, candles.loading, candles.error)}</span>
        </div>
        {candles.data && candles.data.length > 0
          ? null
          : (
            <div className="chart-empty">
              <CandlestickChart size={24} />
              <strong>{market?.symbol ?? "Market"} candle feed unavailable</strong>
              <span>{candles.error ?? "Historical OHLCV unavailable from the market-data provider."}</span>
            </div>
          )}
      </div>
    </section>
  );
}

export type ChartContainerSize = {
  width: number;
  height: number;
};

export function measurableChartContainerSize(element: HTMLElement): ChartContainerSize | null {
  const rect = element.getBoundingClientRect();
  const width = Math.floor(rect.width || element.clientWidth);
  const height = Math.floor(rect.height || element.clientHeight);
  if (width <= 0 || height < 100) return null;
  return {
    width,
    height,
  };
}

export function candleTime(candle: Candle): UTCTimestamp {
  return Math.floor(new Date(candle.timestamp).getTime() / 1000) as UTCTimestamp;
}

export type CandleInspection = {
  count: number;
  validCount: number;
  invalidCount: number;
  chronological: boolean;
  firstTime: number | null;
  lastTime: number | null;
};

export function inspectCandles(candles: Candle[]): CandleInspection {
  const times = candles.map(candleTime);
  const validCount = candles.filter((candle, index) => {
    const values = [
      times[index],
      toNumber(candle.open),
      toNumber(candle.high),
      toNumber(candle.low),
      toNumber(candle.close),
      toNumber(candle.volume),
    ];
    return values.every(Number.isFinite);
  }).length;
  return {
    count: candles.length,
    validCount,
    invalidCount: candles.length - validCount,
    chronological: times.every((time, index) => index === 0 || times[index - 1] <= time),
    firstTime: times.length > 0 ? times[0] : null,
    lastTime: times.length > 0 ? times[times.length - 1] : null,
  };
}

export function toChartCandles(candles: Candle[]): CandlestickData<Time>[] {
  return [...candles].sort((left, right) => candleTime(left) - candleTime(right)).flatMap((candle) => {
    const time = candleTime(candle);
    const open = toNumber(candle.open);
    const high = toNumber(candle.high);
    const low = toNumber(candle.low);
    const close = toNumber(candle.close);
    if (![time, open, high, low, close].every(Number.isFinite)) return [];

    const rising = toNumber(candle.close) >= toNumber(candle.open);
    const currentStyle = candle.is_closed ? {} : {
      borderColor: "#f59e0b",
      wickColor: "#f59e0b",
      color: rising ? "rgba(23, 178, 106, 0.58)" : "rgba(240, 68, 56, 0.58)",
    };
    return {
      time,
      open,
      high,
      low,
      close,
      ...currentStyle,
    };
  });
}

export function toChartVolumes(candles: Candle[]): HistogramData<Time>[] {
  return [...candles].sort((left, right) => candleTime(left) - candleTime(right)).flatMap((candle) => {
    const time = candleTime(candle);
    const value = toNumber(candle.volume);
    const open = toNumber(candle.open);
    const close = toNumber(candle.close);
    if (![time, value, open, close].every(Number.isFinite)) return [];

    return {
      time,
      value,
      color: close >= open
        ? "rgba(23, 178, 106, 0.22)"
        : "rgba(240, 68, 56, 0.22)",
    };
  });
}

export type TradingChartDataResult = {
  candleData: CandlestickData<Time>[];
  volumeData: HistogramData<Time>[];
  livePriceLine: IPriceLine | null;
};

export function applyTradingChartData({
  candles,
  chart,
  candleSeries,
  volumeSeries,
  currentCandle,
  livePriceLine,
}: {
  candles: Candle[];
  chart: IChartApi;
  candleSeries: ISeriesApi<"Candlestick">;
  volumeSeries: ISeriesApi<"Histogram">;
  currentCandle: Candle | null;
  livePriceLine: IPriceLine | null;
}): IPriceLine | null {
  const candleData = toChartCandles(candles);
  const volumeData = toChartVolumes(candles);
  recordTradingChartDebug("chart:setData", {
    ...inspectCandles(candles),
    candleDataCount: candleData.length,
    volumeDataCount: volumeData.length,
    currentCandleTime: currentCandle ? candleTime(currentCandle) : null,
  });
  candleSeries.setData(candleData);
  volumeSeries.setData(volumeData);
  candleSeries.setMarkers(
    currentCandle
      ? [{
        time: candleTime(currentCandle),
        position: "aboveBar",
        color: "#f59e0b",
        shape: "circle",
        text: "Live",
      }]
      : [],
  );

  if (livePriceLine) candleSeries.removePriceLine(livePriceLine);
  const latestCandle = candleData.at(-1);
  const nextPriceLine = latestCandle
    ? candleSeries.createPriceLine({
      price: latestCandle.close,
      color: "#3b82f6",
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      title: currentCandle ? "Live" : "Last",
    })
    : null;

  if (candleData.length) chart.timeScale().fitContent();
  return nextPriceLine;
}

export type TradingChartDebugEvent = {
  name: string;
  at: string;
  payload: Record<string, unknown>;
};

declare global {
  interface Window {
    __quantroChartDebug?: TradingChartDebugEvent[];
  }
}

export function recordTradingChartDebug(name: string, payload: Record<string, unknown> = {}) {
  if (typeof window === "undefined") return;
  const event = { name, at: new Date().toISOString(), payload };
  const events = window.__quantroChartDebug ?? [];
  events.push(event);
  window.__quantroChartDebug = events.slice(-100);
  const debugEnabled = new URLSearchParams(window.location.search).has("chartDebug")
    || window.localStorage.getItem("quantro.chartDebug") === "1";
  if (debugEnabled) console.debug("[TradingChart]", name, payload);
}

export function marketDataStatus(status?: string | null, received?: string | null, error?: string | null) {
  if (error) return { label: "Unavailable", detail: error, tone: "bad" };
  if (!status) return { label: "Syncing", detail: "Waiting for backend data", tone: "warn" };
  const age = received ? Math.max(0, Math.round((Date.now() - new Date(received).getTime()) / 1000)) : null;
  const detail = age === null ? "No timestamp" : `${age}s ago`;
  if (status === "synced" && age !== null && age <= 12) return { label: "Healthy", detail, tone: "good" };
  if (status === "synced") return { label: "Stale", detail, tone: "warn" };
  if (status === "syncing" || status === "resyncing") return { label: status, detail, tone: "warn" };
  return { label: status, detail, tone: "bad" };
}

function OrderBookPanel({ book, loading, error }: { book: OrderBook | null; loading: boolean; error: string | null }) {
  const maxSize = Math.max(...[...(book?.asks ?? []), ...(book?.bids ?? [])].map((level) => toNumber(level.total_quantity)), 1);
  const status = marketDataStatus(book?.status, book?.received_timestamp, error);
  return (
    <section className="orderbook panel">
      <div className="panel-title">
        <span>Order Book</span>
        <span className={`status-dot ${status.tone}`}>{status.label}</span>
      </div>
      <div className="book-meta">
        <span className="muted mono">SEQ {book?.sequence ?? "--"}</span>
        <span className="muted">{status.detail}</span>
      </div>
      <div className="book-head"><span>Price</span><span>Size</span><span>Total</span></div>
      {loading && !book && <SkeletonRows count={10} />}
      {error && !book && <EmptyState title={error} compact />}
      {(book?.asks ?? []).slice().reverse().map((level, index, list) => (
        <BookLevel key={`ask-${level.price}`} level={level} tone="ask" maxSize={maxSize} total={levelTotal(list, index)} />
      ))}
      <div className="mid-price">
        <strong>{book?.mid_price ? formatUsd(book.mid_price) : "--"}</strong>
        <span>{book?.spread ? `Spread ${formatUsd(book.spread)}` : "No spread"}</span>
      </div>
      {(book?.bids ?? []).map((level, index, list) => (
        <BookLevel key={`bid-${level.price}`} level={level} tone="bid" maxSize={maxSize} total={levelTotal(list, index)} />
      ))}
      {!loading && book && book.asks.length === 0 && book.bids.length === 0 && <EmptyState title="Empty order book" />}
    </section>
  );
}

function BookLevel({ level, tone, maxSize, total }: { level: { price: string; total_quantity: string }; tone: "bid" | "ask"; maxSize: number; total: number }) {
  const width = clamp((toNumber(level.total_quantity) / maxSize) * 100, 4, 100);
  return (
    <div className={`book-row ${tone}`}>
      <div className="depth-fill" style={{ width: `${width}%` }} />
      <span className="mono">{formatNumber(level.price, 2)}</span>
      <span className="mono">{formatCrypto(level.total_quantity, 6)}</span>
      <span className="mono">{formatCrypto(total, 6)}</span>
    </div>
  );
}

function RecentTrades({ trades, symbol, error }: { trades: PublicTrade[]; symbol: string; error: string | null }) {
  const visible = trades.filter((trade) => trade.symbol === symbol).slice(0, 18);
  return (
    <section className="recent-trades panel">
      <div className="panel-title"><span>Recent Trades</span><Clock3 size={14} /></div>
      <div className="book-head"><span>Price</span><span>Size</span><span>Side/Time</span></div>
      {error && <EmptyState title={error} compact />}
      {visible.map((trade) => (
        <div className="trade-row" key={trade.id}>
          <span className={`mono ${trade.side === "buy" ? "positive" : "negative"}`}>{formatUsd(trade.price)}</span>
          <span className="mono">{formatCrypto(trade.quantity, 6)}</span>
          <span className="trade-side-time">
            <strong className={trade.side === "buy" ? "positive" : "negative"}>{trade.side.toUpperCase()}</strong>
            <small className="mono muted">{formatTime(trade.exchange_timestamp ?? trade.received_timestamp)}</small>
          </span>
        </div>
      ))}
      {visible.length === 0 && <EmptyState title="No public trades" compact />}
    </section>
  );
}

export function OrderEntry({
  market,
  balances,
  book,
  marketDataHealthy,
  marketDataIssue,
  onSubmit,
  onToast,
  initialSide = "buy",
  initialType = "limit",
  initialAmount = "",
}: {
  market: Market | null;
  balances: Balance[];
  book: OrderBook | null;
  marketDataHealthy: boolean;
  marketDataIssue: string;
  onSubmit: (payload: OrderPayload) => Promise<Order>;
  onToast: (tone: Toast["tone"], message: string) => void;
  initialSide?: OrderSide;
  initialType?: OrderType;
  initialAmount?: string;
}) {
  const [side, setSide] = useState<OrderSide>(initialSide);
  const [type, setType] = useState<OrderType>(initialType);
  const [price, setPrice] = useState("");
  const [amount, setAmount] = useState(initialAmount);
  const [takeProfit, setTakeProfit] = useState("");
  const [stopLoss, setStopLoss] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const balancesByAsset = useMemo(() => balanceMap(balances), [balances]);
  const quote = market?.quote_asset ?? "USD";
  const base = market?.base_asset ?? "BTC";
  const available = side === "buy" ? balancesByAsset[quote]?.free : balancesByAsset[base]?.free;
  const referencePrice = type === "market" ? marketPrice(book?.best_bid, book?.best_ask) : toNumber(price);
  const total = toNumber(amount) * referencePrice;
  const fee = total * toNumber(market?.taker_fee ?? "0.001");
  const validation = validateOrder({
    market,
    side,
    type,
    price,
    amount,
    available,
    referencePrice,
    marketDataHealthy,
    marketDataIssue,
  });
  const executionUnsupported = market?.metadata.execution_supported === false;
  const paperSafetyIssue = paperExecutionIssue(market);
  const submitBlockedReason = validation ?? (executionUnsupported ? "Execution unsupported for this market-data symbol." : null);

  function applyPct(percent: number) {
    const availableNumber = toNumber(available);
    if (!market || availableNumber <= 0) return;
    if (side === "sell") {
      setAmount(formatCrypto(availableNumber * percent, market.quantity_precision));
      return;
    }
    const px = type === "market" ? referencePrice : toNumber(price);
    if (px > 0) setAmount(formatCrypto((availableNumber * percent) / px, market.quantity_precision));
  }

  useEffect(() => {
    if (!price && book?.mid_price) setPrice(book.mid_price);
  }, [book?.mid_price, price]);

  useEffect(() => {
    setConfirming(false);
    setPrice(book?.mid_price ?? "");
    setAmount("");
    // Reset only when switching markets; book refreshes must not wipe the active ticket.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [market?.symbol]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (validation) return onToast("error", validation);
    if (!confirming) {
      setConfirming(true);
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit({
        symbol: market!.symbol,
        side,
        order_type: type,
        time_in_force: type === "market" ? "ioc" : "gtc",
        quantity: amount,
        price: type === "market" ? String(referencePrice) : price,
        metadata: {
          paper_reference_price: String(referencePrice),
          source: "quantro_terminal",
        },
      });
      setAmount("");
      setConfirming(false);
    } catch (err) {
      onToast("error", err instanceof QuantroApiError ? err.message : "Order rejected");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="order-entry panel" onSubmit={submit}>
      <div className="order-entry-body">
        <div className="ticket-safety">DEMO TRADING — NO REAL FUNDS</div>
        <div className="side-tabs">
          <button type="button" className={side === "buy" ? "buy active" : ""} onClick={() => { setSide("buy"); setConfirming(false); }}>BUY</button>
          <button type="button" className={side === "sell" ? "sell active" : ""} onClick={() => { setSide("sell"); setConfirming(false); }}>SELL</button>
        </div>
        <div className="type-grid">
          {(["limit", "market"] as OrderType[]).map((item) => (
            <button type="button" key={item} className={type === item ? "active" : ""} onClick={() => { setType(item); setConfirming(false); }}>{item.replace("_", " ").toUpperCase()}</button>
          ))}
        </div>
        <div className={`market-gate ${marketDataHealthy ? "healthy" : "blocked"}`}>
          <strong>{marketDataHealthy ? "Paper market data ready" : "Paper order blocked"}</strong>
          <span>{marketDataHealthy ? "Ticker and order book are current enough for a simulated fill." : marketDataIssue}</span>
        </div>
        {type !== "market" && (
          <label>Price
            <div className="input-row"><input value={price} onChange={(event) => { setPrice(event.target.value); setConfirming(false); }} inputMode="decimal" /><span>{quote}</span></div>
          </label>
        )}
        <label>Amount
          <div className="input-row"><input value={amount} onChange={(event) => { setAmount(event.target.value); setConfirming(false); }} inputMode="decimal" /><span>{base}</span></div>
        </label>
        <div className="pct-row">
          {[0.25, 0.5, 0.75, 1].map((value) => <button type="button" key={value} onClick={() => applyPct(value)}>{Math.round(value * 100)}%</button>)}
        </div>
        <div className="risk-controls">
          <label>Take Profit
            <div className="input-row disabled-field"><input value={takeProfit} onChange={(event) => setTakeProfit(event.target.value)} inputMode="decimal" placeholder="Unsupported" disabled /><span>{quote}</span></div>
          </label>
          <label>Stop Loss
            <div className="input-row disabled-field"><input value={stopLoss} onChange={(event) => setStopLoss(event.target.value)} inputMode="decimal" placeholder="Unsupported" disabled /><span>{quote}</span></div>
          </label>
        </div>
        <div className="perp-box">
          <div><Zap size={14} /><strong>Perpetual controls</strong></div>
          <span>{market?.metadata.product_type === "perpetual" ? "Paper perpetual using public OKX market data" : "Sandbox execution"}</span>
          <div className="leverage-track"><span /></div>
          <div className="perp-lines"><span>Leverage</span><strong>Unavailable</strong><span>Margin</span><strong>Unavailable</strong><span>Liq. price</span><strong>Unavailable</strong></div>
        </div>
        <div className="ticket-lines">
          <div><span>Virtual available</span><strong className="mono">{formatCrypto(available ?? 0, 8)} {side === "buy" ? quote : base}</strong></div>
          <div><span>Market data</span><strong className="mono">{marketDataHealthy ? "Healthy" : marketDataIssue}</strong></div>
          <div><span>Estimated total</span><strong className="mono">{formatUsd(total)}</strong></div>
          <div><span>Estimated fee</span><strong className="mono">{formatUsd(fee)}</strong></div>
          <div><span>Min size</span><strong className="mono">{market?.min_order_size ?? "--"} {base}</strong></div>
          <div><span>Execution mode</span><strong className="mono">{paperSafetyIssue ?? "paper / no real funds / routing disabled"}</strong></div>
        </div>
        {confirming && (
          <div className="confirmation-box">
            Confirm simulated {side.toUpperCase()} {formatCrypto(amount, market?.quantity_precision ?? 4)} {base} {type === "market" ? "at market" : `at ${formatUsd(price, market?.price_precision ?? 2)}`}. No live venue order will be sent.
          </div>
        )}
        {submitBlockedReason && <div className="form-error" id="paper-order-blocked-reason">{submitBlockedReason}</div>}
      </div>
      <div className="order-entry-footer">
        <button
          className={`primary-action ${side}`}
          data-testid="paper-order-submit"
          aria-describedby={submitBlockedReason ? "paper-order-blocked-reason" : undefined}
          disabled={submitting || Boolean(submitBlockedReason)}
          type="submit"
        >
          {submitting ? "SUBMITTING" : confirming ? "CONFIRM PAPER ORDER" : `${side.toUpperCase()} ${base}`}
        </button>
      </div>
    </form>
  );
}

export function paperExecutionIssue(market: Market | null): string | null {
  if (!market) return "Select a valid symbol.";
  if (market.metadata.execution_mode !== "paper") return "Only paper execution is enabled.";
  if (market.metadata.real_funds !== false) return "Real-funds markets are not supported.";
  if (market.metadata.venue_routing !== "disabled") return "Venue routing must remain disabled.";
  return null;
}

export function validateOrder(input: {
  market: Market | null;
  side: OrderSide;
  type: OrderType;
  price: string;
  amount: string;
  available?: string;
  referencePrice: number;
  marketDataHealthy: boolean;
  marketDataIssue: string;
}) {
  if (!input.market) return "Select a valid symbol.";
  const paperIssue = paperExecutionIssue(input.market);
  if (paperIssue) return paperIssue;
  if (input.market.metadata.execution_supported === false) return "Execution unsupported for this market-data symbol.";
  if (!input.marketDataHealthy) return `Market data not ready for paper trading (${input.marketDataIssue}).`;
  if (toNumber(input.amount) <= 0) return "Invalid quantity.";
  if (toNumber(input.amount) < toNumber(input.market.min_order_size)) return "Minimum order size not met.";
  if (input.type !== "market" && toNumber(input.price) <= 0) return "Invalid price.";
  if (input.type === "market" && input.referencePrice <= 0) return "Market price unavailable.";
  const total = toNumber(input.amount) * (input.type === "market" ? input.referencePrice : toNumber(input.price));
  if (input.side === "buy" && total > toNumber(input.available)) return "Insufficient balance.";
  if (input.side === "sell" && toNumber(input.amount) > toNumber(input.available)) return "Insufficient balance.";
  return null;
}

export function BottomWorkspace({ defaultTab, orders, trades, positions, pnl, symbol, currentPrice, onCancelOrder }: {
  defaultTab: BottomTab;
  orders: Order[];
  trades: Trade[];
  positions: Position[];
  pnl: Pnl | null;
  symbol?: string;
  currentPrice?: number;
  onCancelOrder: (orderId: string) => Promise<void>;
}) {
  const [tab, setTab] = useState<BottomTab>(defaultTab);
  const scopedOrders = symbol ? orders.filter((order) => order.symbol === symbol) : orders;
  const scopedTrades = symbol ? trades.filter((trade) => trade.symbol === symbol) : trades;
  const scopedPositions = symbol ? positions.filter((position) => position.symbol === symbol) : positions;
  const openOrders = scopedOrders.filter((order) => ["open", "partially_filled", "pending"].includes(order.status));
  const scopedPnl = symbol ? pnlForPositions(pnl, scopedPositions) : pnl;
  return (
    <section className="bottom-workspace panel">
      <div className="workspace-tabs">
        {[
          ["open", `OPEN ORDERS ${openOrders.length}`],
          ["orders", "ORDER HISTORY"],
          ["trades", "TRADE HISTORY"],
          ["positions", "POSITIONS"],
          ["pnl", "PNL"],
        ].map(([id, label]) => <button key={id} className={tab === id ? "active" : ""} onClick={() => setTab(id as BottomTab)}>{label}</button>)}
      </div>
      <div className="workspace-content">
        {tab === "open" && <OrdersTable orders={openOrders} onCancelOrder={onCancelOrder} showAction />}
        {tab === "orders" && <OrdersTable orders={scopedOrders.slice().reverse()} onCancelOrder={onCancelOrder} showAction={false} />}
        {tab === "trades" && <TradesTable trades={scopedTrades.slice().reverse()} />}
        {tab === "positions" && <PositionsTable positions={scopedPositions} currentPrice={currentPrice} />}
        {tab === "pnl" && <PnLPanel pnl={scopedPnl} trades={scopedTrades} />}
      </div>
    </section>
  );
}

function pnlForPositions(pnl: Pnl | null, positions: Position[]): Pnl | null {
  if (positions.length === 0) {
    return pnl ? { ...pnl, total_unrealized_pnl: "0", total_realized_pnl: "0", total_pnl: "0" } : null;
  }
  const unrealized = positions.reduce((sum, position) => sum + toNumber(position.unrealized_pnl), 0);
  const realized = positions.reduce((sum, position) => sum + toNumber(position.realized_pnl), 0);
  return {
    account_id: pnl?.account_id ?? "",
    total_unrealized_pnl: String(unrealized),
    total_realized_pnl: String(realized),
    total_pnl: String(unrealized + realized),
  };
}

function OrdersTable({ orders, onCancelOrder, showAction }: { orders: Order[]; onCancelOrder: (orderId: string) => Promise<void>; showAction: boolean }) {
  if (orders.length === 0) return <EmptyState title={showAction ? "No open orders" : "No order history"} />;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Market</th><th>Side</th><th>Type</th><th>Price</th><th>Amount</th><th>Filled</th><th>Remaining</th><th>Status</th><th>Created</th>{showAction && <th>Action</th>}</tr></thead>
        <tbody>
          {orders.map((order) => (
            <tr key={order.id}>
              <td>{order.symbol}</td>
              <td className={order.side === "buy" ? "positive" : "negative"}>{order.side.toUpperCase()}</td>
              <td>{order.order_type.toUpperCase()}</td>
              <td className="mono">{formatUsd(order.price)}</td>
              <td className="mono">{formatCrypto(order.quantity, 6)}</td>
              <td><div className="fill-cell"><span style={{ width: `${orderFillPct(order)}%` }} />{formatCrypto(order.filled_quantity, 6)}</div></td>
              <td className="mono">{formatCrypto(order.remaining_quantity, 6)}</td>
              <td><StatusPill status={order.status} /></td>
              <td className="mono muted">{formatDateTime(order.created_at)}</td>
              {showAction && <td><button className="table-action" onClick={() => void onCancelOrder(order.id)}>Cancel</button></td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TradesTable({ trades }: { trades: Trade[] }) {
  if (trades.length === 0) return <EmptyState title="No trade history" />;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Market</th><th>Side</th><th>Price</th><th>Size</th><th>Notional</th><th>Fee</th><th>Liquidity</th><th>Time</th></tr></thead>
        <tbody>{trades.map((trade) => <tr key={trade.id}><td>{trade.symbol}</td><td className={trade.side === "buy" ? "positive" : "negative"}>{trade.side.toUpperCase()}</td><td className="mono">{formatUsd(trade.price)}</td><td className="mono">{formatCrypto(trade.quantity, 6)}</td><td className="mono">{formatUsd(trade.notional)}</td><td className="mono">{formatCrypto(trade.fee, 6)} {trade.fee_asset}</td><td>{trade.is_maker ? "Maker" : "Taker"}</td><td className="mono muted">{formatDateTime(trade.timestamp)}</td></tr>)}</tbody>
      </table>
    </div>
  );
}

function PositionsTable({ positions, currentPrice }: { positions: Position[]; currentPrice?: number }) {
  if (positions.length === 0) return <EmptyState title="No open positions" />;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Market</th><th>Side</th><th>Quantity</th><th>Entry Price</th><th>Current Price</th><th>Liq. Price</th><th>Margin</th><th>Unrealized PnL</th><th>Realized PnL</th><th>ROI</th></tr></thead>
        <tbody>{positions.map((position) => {
          const mark = currentPrice && currentPrice > 0 ? currentPrice : toNumber(position.mark_price);
          const size = toNumber(position.size);
          const entry = toNumber(position.entry_price);
          const margin = entry * size / Math.max(toNumber(position.leverage), 1);
          const liveUnrealized = position.side === "short" ? (entry - mark) * size : (mark - entry) * size;
          const unrealized = currentPrice ? liveUnrealized : toNumber(position.unrealized_pnl);
          const roi = margin ? (unrealized / margin) * 100 : 0;
          return <tr key={position.id}><td>{position.symbol}</td><td>{position.side.toUpperCase()}</td><td className="mono">{formatCrypto(position.size, 6)}</td><td className="mono">{formatUsd(position.entry_price)}</td><td className="mono">{mark ? formatUsd(mark) : "Unavailable"}</td><td className="mono">{toNumber(position.liquidation_price) ? formatUsd(position.liquidation_price) : "--"}</td><td className="mono">{formatUsd(margin)}</td><td className={unrealized >= 0 ? "positive mono" : "negative mono"}>{formatUsd(unrealized)}</td><td className={toNumber(position.realized_pnl) >= 0 ? "positive mono" : "negative mono"}>{formatUsd(position.realized_pnl)}</td><td className={roi >= 0 ? "positive mono" : "negative mono"}>{pct(roi)}</td></tr>;
        })}</tbody>
      </table>
    </div>
  );
}

function PnLPanel({ pnl, trades = [] }: { pnl: Pnl | null; trades?: Trade[] }) {
  const volume = trades.reduce((sum, trade) => sum + toNumber(trade.notional), 0);
  const fees = trades.reduce((sum, trade) => sum + toNumber(trade.fee), 0);
  return (
    <div className="pnl-grid">
      <Metric label="Realized PnL" value={formatUsd(pnl?.total_realized_pnl ?? 0)} tone={toNumber(pnl?.total_realized_pnl) >= 0 ? "positive" : "negative"} />
      <Metric label="Unrealized PnL" value={formatUsd(pnl?.total_unrealized_pnl ?? 0)} tone={toNumber(pnl?.total_unrealized_pnl) >= 0 ? "positive" : "negative"} />
      <Metric label="Total PnL" value={formatUsd(pnl?.total_pnl ?? 0)} tone={toNumber(pnl?.total_pnl) >= 0 ? "positive" : "negative"} />
      <Metric label="Trading Volume" value={formatUsd(volume)} />
      <Metric label="Fees" value={formatCrypto(fees, 8)} />
      <Metric label="PnL Source" value={pnl ? "Backend" : "Unavailable"} />
      <div className="pnl-viz"><span style={{ width: `${clamp(Math.abs(toNumber(pnl?.total_pnl)), 2, 100)}%` }} /></div>
    </div>
  );
}

function MarketTable({ markets, books, ticker, loading, onTrade }: { markets: Market[]; books: OrderBook[]; ticker: MarketTicker | null; loading: boolean; onTrade: (symbol: string) => void }) {
  return (
    <section className="page-section panel">
      <div className="page-head"><h2>Markets</h2><div className="sidebar-tabs"><button className="active">Favorites</button><button>Spot</button><button>Perpetuals</button><button>All</button></div></div>
      {loading ? <SkeletonRows count={10} /> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Pair</th><th>Price</th><th>24h Change</th><th>24h High</th><th>24h Low</th><th>Volume</th><th>Action</th></tr></thead>
            <tbody>{markets.map((market) => {
              const book = books.find((item) => item.symbol === market.symbol);
              const realTicker = ticker?.symbol === market.symbol ? ticker : null;
              const price = toNumber(realTicker?.last_price) || marketPrice(book?.best_bid, book?.best_ask);
              const change = toNumber(realTicker?.change_24h);
              return <tr key={market.symbol}><td><strong>{market.symbol}</strong><span className="subcell">{market.base_asset}/{market.quote_asset}</span></td><td className="mono">{price ? formatUsd(price) : "Unavailable"}</td><td className={change >= 0 ? "positive" : "negative"}>{realTicker?.change_24h ? pct(change) : "Unavailable"}</td><td>{realTicker?.high_24h ? formatUsd(realTicker.high_24h) : "Unavailable"}</td><td>{realTicker?.low_24h ? formatUsd(realTicker.low_24h) : "Unavailable"}</td><td>{realTicker?.volume_24h ? formatCrypto(realTicker.volume_24h, 2) : "Unavailable"}</td><td><button className="table-action" onClick={() => onTrade(market.symbol)}>Trade</button></td></tr>;
            })}</tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function PortfolioOverview({ account, balances, markets, pnl, positions, loading, orders, trades }: { account: Account; balances: Balance[]; markets: Market[]; pnl: Pnl | null; positions: Position[]; loading: boolean; orders: Order[]; trades: Trade[] }) {
  const balanceAsset = portfolioBalanceAsset(markets);
  const totalBalanceValue = balances.reduce((sum, balance) => sum + balanceValue(balance, markets, balanceAsset), 0);
  const availableBalanceAsset = balances.reduce((sum, balance) => sum + (balance.asset === balanceAsset ? toNumber(balance.free) : 0), 0);
  const lockedBalanceAsset = balances.reduce((sum, balance) => sum + (balance.asset === balanceAsset ? toNumber(balance.locked) : 0), 0);
  const volume = trades.reduce((sum, trade) => sum + toNumber(trade.notional), 0);
  const fees = trades.reduce((sum, trade) => sum + toNumber(trade.fee), 0);
  return (
    <section className="portfolio-grid">
      <div className="portfolio-hero panel">
        <span>Total Equity</span>
        <strong className="mono">{loading ? "--" : `${formatNumber(totalBalanceValue)} ${balanceAsset}`}</strong>
        <div><Metric label={`Available ${balanceAsset}`} value={`${formatNumber(availableBalanceAsset)} ${balanceAsset}`} /><Metric label={`Locked ${balanceAsset}`} value={`${formatNumber(lockedBalanceAsset)} ${balanceAsset}`} /><Metric label="Total PnL" value={formatUsd(pnl?.total_pnl ?? 0)} tone={toNumber(pnl?.total_pnl) >= 0 ? "positive" : "negative"} /><Metric label="Trading Volume" value={formatUsd(volume)} /><Metric label="Fees" value={formatCrypto(fees, 8)} /><Metric label="Account" value={shortId(account.id)} /><Metric label="Orders" value={String(orders.length)} /><Metric label="Positions" value={String(positions.length)} /></div>
      </div>
      <div className="allocation panel">
        <div className="panel-title"><span>Allocation</span></div>
        {balances.length === 0 ? <EmptyState title="No balances" compact /> : balances.map((balance) => {
          const value = balanceValue(balance, markets, balanceAsset);
          const width = totalBalanceValue ? (value / totalBalanceValue) * 100 : 0;
          return <div className="allocation-row" key={balance.asset}><span>{balance.asset}</span><div><span style={{ width: `${width}%` }} /></div><strong className="mono">{formatNumber(width, 1)}%</strong></div>;
        })}
      </div>
      <div className="assets panel">
        <div className="panel-title"><span>Assets</span></div>
        <AssetsTable balances={balances} markets={markets} valuationAsset={balanceAsset} />
      </div>
      <div className="assets panel">
        <div className="panel-title"><span>Positions</span></div>
        <PositionsTable positions={positions} />
      </div>
    </section>
  );
}

function AccountPanel({ account, balances }: { account: Account; balances: Balance[] }) {
  return (
    <section className="account-grid">
      <div className="panel account-card"><div className="panel-title"><span>Account</span></div><Metric label="Account ID" value={account.id} /><Metric label="Name" value={account.name} /><Metric label="Created" value={formatDateTime(account.created_at)} /></div>
      <div className="panel account-card"><div className="panel-title"><span>Safety</span></div><Metric label="Mode" value="Demo trading" /><Metric label="Real Funds" value="Disabled" /><Metric label="Venue Routing" value="Paper only" /></div>
      <div className="panel assets"><div className="panel-title"><span>Available Balances</span></div><AssetsTable balances={balances} markets={[]} /></div>
    </section>
  );
}

function ApiPanel() {
  const endpoints = [
    "POST /auth/signup",
    "POST /auth/login",
    "GET /me/account",
    "GET /markets",
    "GET /markets/{symbol}/ticker",
    "GET /markets/{symbol}/orderbook",
    "GET /markets/{symbol}/trades",
    "GET /markets/{symbol}/candles",
    "WS /ws/market-data",
    "POST /orders",
    "GET /orders/{order_id}",
    "DELETE /orders/{order_id}",
    "GET /accounts/{account_id}/orders",
    "GET /accounts/{account_id}/balances",
    "GET /accounts/{account_id}/positions",
    "GET /accounts/{account_id}/trades",
    "GET /accounts/{account_id}/pnl",
  ];

  return (
    <section className="api-grid">
      <div className="panel api-hero">
        <div className="panel-title"><span>API Infrastructure</span><Code2 size={15} /></div>
        <div className="api-copy">
          <DatabaseZap size={24} />
          <strong>REST execution surface connected</strong>
          <span>Frontend services use Quantro backend contracts for normalized market data and paper execution. Live venue order routing is disabled.</span>
        </div>
      </div>
      <div className="panel">
        <div className="panel-title"><span>Available Endpoints</span><Landmark size={15} /></div>
        <div className="endpoint-list">{endpoints.map((endpoint) => <code key={endpoint}>{endpoint}</code>)}</div>
      </div>
    </section>
  );
}

function AssetsTable({ balances, markets, valuationAsset = "USD" }: { balances: Balance[]; markets: Market[]; valuationAsset?: string }) {
  if (balances.length === 0) return <EmptyState title="No balances" />;
  const total = balances.reduce((sum, balance) => sum + balanceValue(balance, markets, valuationAsset), 0);
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Asset</th><th>Total</th><th>Available</th><th>Locked</th><th>Value {valuationAsset}</th><th>Allocation</th></tr></thead>
        <tbody>{balances.map((balance) => {
          const value = balanceValue(balance, markets, valuationAsset);
          return <tr key={balance.asset}><td><strong>{balance.asset}</strong></td><td className="mono">{formatCrypto(balance.total, 8)}</td><td className="mono">{formatCrypto(balance.free, 8)}</td><td className="mono">{formatCrypto(balance.locked, 8)}</td><td className="mono">{markets.length ? `${formatNumber(value)} ${valuationAsset}` : "--"}</td><td className="mono">{total ? `${formatNumber((value / total) * 100, 1)}%` : "--"}</td></tr>;
        })}</tbody>
      </table>
    </div>
  );
}

function MainPanel({ children }: { children: React.ReactNode }) {
  return <main className="page-shell">{children}</main>;
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: "positive" | "negative" }) {
  return <div className="metric"><span>{label}</span><strong className={tone ? `${tone} mono` : "mono"}>{value}</strong></div>;
}

function StatusPill({ status }: { status: string }) {
  const good = ["filled", "open"].includes(status);
  const bad = ["cancelled", "rejected", "expired"].includes(status);
  return <span className={`status-pill ${good ? "good" : bad ? "bad" : ""}`}>{status.replace("_", " ")}</span>;
}

function EmptyState({ title, compact = false }: { title: string; compact?: boolean }) {
  return <div className={compact ? "empty-state compact" : "empty-state"}>{title}</div>;
}

function SkeletonRows({ count }: { count: number }) {
  return <div className="skeleton-stack">{Array.from({ length: count }).map((_, index) => <span key={index} />)}</div>;
}

function ToastStack({ toasts }: { toasts: Toast[] }) {
  return (
    <div className="toast-stack">
      {toasts.map((toast) => <div className={`toast ${toast.tone}`} key={toast.id}>{toast.tone === "success" ? <Check size={15} /> : <X size={15} />}{toast.message}</div>)}
    </div>
  );
}
