import { renderToString } from "react-dom/server";
import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import {
  BottomWorkspace,
  OrderEntry,
  applyTradingChartData,
  measurableChartContainerSize,
  paperExecutionIssue,
  submitPaperOrderAndRefresh,
  toChartCandles,
  toChartVolumes,
  validateOrder,
} from "./App";
import type { Balance, Candle, Market, Order, OrderBook, OrderResult, Pnl, Position, Trade } from "./types";

function paperSwapMarket(symbol: "BTC-USDT-SWAP" | "ETH-USDT-SWAP"): Market {
  const isBtc = symbol === "BTC-USDT-SWAP";
  return {
    symbol,
    base_asset: isBtc ? "BTC" : "ETH",
    quote_asset: "USDT",
    venue: "PAPER",
    price_precision: isBtc ? 1 : 2,
    quantity_precision: isBtc ? 4 : 3,
    min_order_size: isBtc ? "0.0001" : "0.001",
    max_order_size: "1000",
    tick_size: isBtc ? "0.1" : "0.01",
    lot_size: isBtc ? "0.0001" : "0.001",
    maker_fee: "0.0005",
    taker_fee: "0.0005",
    is_active: true,
    metadata: {
      product_type: "perpetual",
      execution_mode: "paper",
      real_funds: false,
      venue_routing: "disabled",
    },
  };
}

function orderBook(symbol: "BTC-USDT-SWAP" | "ETH-USDT-SWAP"): OrderBook {
  const btc = symbol === "BTC-USDT-SWAP";
  return {
    symbol,
    sequence: 1,
    best_bid: btc ? "49999.9" : "2499.99",
    best_ask: btc ? "50000.1" : "2500.01",
    spread: btc ? "0.2" : "0.02",
    mid_price: btc ? "50000" : "2500",
    last_trade_price: btc ? "50000" : "2500",
    last_trade_quantity: "1",
    bids: [],
    asks: [],
    received_timestamp: new Date().toISOString(),
    status: "synced",
    venue: "PAPER",
  };
}

const balances: Balance[] = [
  { asset: "USDT", free: "1000000", locked: "0", total: "1000000" },
  { asset: "BTC", free: "10", locked: "0", total: "10" },
  { asset: "ETH", free: "100", locked: "0", total: "100" },
];

const baseOrder: Order = {
  id: "order-1",
  account_id: "account-1",
  symbol: "BTC-USDT-SWAP",
  side: "buy",
  order_type: "market",
  status: "filled",
  time_in_force: "ioc",
  quantity: "0.001",
  price: "50000",
  stop_price: "0",
  filled_quantity: "0.001",
  remaining_quantity: "0",
  average_fill_price: "50000",
  total_fees: "0.025",
  client_order_id: "client-1",
  created_at: "2026-08-28T15:00:00Z",
  updated_at: "2026-08-28T15:00:00Z",
  expires_at: null,
  metadata: {
    execution_mode: "paper",
    real_funds: false,
    venue_routing: "disabled",
  },
};

const baseTrade: Trade = {
  id: "trade-1",
  order_id: "order-1",
  account_id: "account-1",
  symbol: "BTC-USDT-SWAP",
  side: "buy",
  quantity: "0.001",
  price: "50000",
  notional: "50",
  fee: "0.025",
  fee_asset: "USDT",
  timestamp: "2026-08-28T15:00:00Z",
  is_maker: false,
  metadata: {
    execution_mode: "paper",
    real_funds: false,
    venue_routing: "disabled",
  },
};

const basePosition: Position = {
  id: "position-1",
  account_id: "account-1",
  symbol: "BTC-USDT-SWAP",
  side: "long",
  size: "0.001",
  entry_price: "50000",
  mark_price: "50100",
  unrealized_pnl: "0.1",
  realized_pnl: "0",
  leverage: "1",
  liquidation_price: "0",
  opened_at: "2026-08-28T15:00:00Z",
  updated_at: "2026-08-28T15:00:00Z",
  metadata: {
    execution_mode: "paper",
    real_funds: false,
    venue_routing: "disabled",
  },
};

const pnl: Pnl = {
  account_id: "account-1",
  total_unrealized_pnl: "0.1",
  total_realized_pnl: "0",
  total_pnl: "0.1",
};

const acceptedOrderResult: OrderResult = {
  accepted: true,
  reject_reason: null,
  order: baseOrder,
  trades: [baseTrade],
  risk_report: {
    overall: "passed",
    passed: true,
    checks: [],
  },
};

describe("TradingChart candle rendering inputs", () => {
  const apiCandles: Candle[] = [
    {
      symbol: "BTC-USDT-SWAP",
      interval: "1h",
      timestamp: "2026-08-29T13:00:00Z",
      open: "50100.1",
      high: "50220.5",
      low: "50090.0",
      close: "50180.2",
      volume: "18.25",
      is_closed: false,
    },
    {
      symbol: "BTC-USDT-SWAP",
      interval: "1h",
      timestamp: "2026-08-29T12:00:00Z",
      open: "50000.0",
      high: "50140.8",
      low: "49980.4",
      close: "50100.1",
      volume: "24.5",
      is_closed: true,
    },
  ];

  it("maps API candle responses to finite chronological lightweight-chart data", () => {
    const candleData = toChartCandles(apiCandles);
    const volumeData = toChartVolumes(apiCandles);

    expect(candleData).toMatchObject([
      { time: 1788004800, open: 50000, high: 50140.8, low: 49980.4, close: 50100.1 },
      { time: 1788008400, open: 50100.1, high: 50220.5, low: 50090, close: 50180.2 },
    ]);
    expect(volumeData).toMatchObject([
      { time: 1788004800, value: 24.5 },
      { time: 1788008400, value: 18.25 },
    ]);
  });

  it("does not initialize the chart before the container is measurable", () => {
    const element = {
      clientWidth: 0,
      clientHeight: 0,
      getBoundingClientRect: () => ({ width: 0, height: 0 }),
    } as HTMLElement;

    expect(measurableChartContainerSize(element)).toBeNull();
  });

  it("applies valid API candles to both lightweight-chart series", () => {
    const chart = {
      timeScale: () => ({
        fitContent: vi.fn(),
      }),
    };
    const priceLine = { applyOptions: vi.fn(), options: vi.fn() };
    const candleSeries = {
      setData: vi.fn(),
      setMarkers: vi.fn(),
      createPriceLine: vi.fn(() => priceLine),
      removePriceLine: vi.fn(),
    };
    const volumeSeries = {
      setData: vi.fn(),
    };

    const nextPriceLine = applyTradingChartData({
      candles: apiCandles,
      chart: chart as never,
      candleSeries: candleSeries as never,
      volumeSeries: volumeSeries as never,
      currentCandle: apiCandles[0],
      livePriceLine: null,
    });

    expect(candleSeries.setData).toHaveBeenCalledWith([
      expect.objectContaining({ time: 1788004800, open: 50000, high: 50140.8, low: 49980.4, close: 50100.1 }),
      expect.objectContaining({ time: 1788008400, open: 50100.1, high: 50220.5, low: 50090, close: 50180.2 }),
    ]);
    expect(volumeSeries.setData).toHaveBeenCalledWith([
      expect.objectContaining({ time: 1788004800, value: 24.5 }),
      expect.objectContaining({ time: 1788008400, value: 18.25 }),
    ]);
    expect(candleSeries.setMarkers).toHaveBeenCalledWith([
      expect.objectContaining({ time: 1788008400, text: "Live" }),
    ]);
    expect(candleSeries.createPriceLine).toHaveBeenCalledWith(expect.objectContaining({
      price: 50180.2,
      title: "Live",
    }));
    expect(nextPriceLine).toBe(priceLine);
  });
});

describe("OrderEntry paper submission control", () => {
  it("renders a reachable enabled footer action for healthy BTC-USDT-SWAP BUY MARKET", () => {
    const html = renderToString(
      <OrderEntry
        market={paperSwapMarket("BTC-USDT-SWAP")}
        balances={balances}
        book={orderBook("BTC-USDT-SWAP")}
        marketDataHealthy={true}
        marketDataIssue="Healthy"
        onSubmit={vi.fn()}
        onToast={vi.fn()}
        initialType="market"
        initialAmount="0.001"
      />,
    );
    const submitButton = html.match(/<button[^>]*data-testid="paper-order-submit"[^>]*>.*?<\/button>/)?.[0] ?? "";

    expect(html).toContain('class="order-entry-footer"');
    expect(html.indexOf('class="order-entry-footer"')).toBeGreaterThan(html.indexOf('class="order-entry-body"'));
    expect(submitButton).toContain("BUY BTC");
    expect(submitButton).not.toContain("disabled");
    expect(html).toContain("Paper market data ready");
    expect(html).toContain("0.001");
  });

  it("keeps the ticket body scrollable while the submit footer stays visible", () => {
    const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");

    expect(css).toMatch(/\.order-entry\s*{[^}]*overflow:\s*hidden;/s);
    expect(css).toMatch(/\.order-entry-body\s*{[^}]*overflow-y:\s*auto;/s);
    expect(css).toMatch(/\.order-entry-footer\s*{[^}]*flex:\s*0 0 auto;/s);
    expect(css).toMatch(/grid-template-rows:\s*minmax\(0,\s*1fr\)\s*minmax\(260px,\s*42vh\);/);
  });

  it("renders the paper order action and blocks it with a clear stale market-data reason", () => {
    const html = renderToString(
      <OrderEntry
        market={paperSwapMarket("BTC-USDT-SWAP")}
        balances={balances}
        book={orderBook("BTC-USDT-SWAP")}
        marketDataHealthy={false}
        marketDataIssue="Ticker Stale (32s ago); Book Healthy (1s ago)"
        onSubmit={vi.fn()}
        onToast={vi.fn()}
      />,
    );

    expect(html).toContain('data-testid="paper-order-submit"');
    expect(html).toContain("BUY BTC");
    expect(html).toContain("disabled");
    expect(html).toContain("Paper order blocked");
    expect(html).toContain("Market data not ready for paper trading");
  });

  it.each([
    ["BTC-USDT-SWAP", "0.0100"],
    ["ETH-USDT-SWAP", "1.000"],
  ] as const)("allows healthy paper BUY MARKET validation for %s", (symbol, amount) => {
    const market = paperSwapMarket(symbol);
    const book = orderBook(symbol);

    expect(paperExecutionIssue(market)).toBeNull();
    expect(
      validateOrder({
        market,
        side: "buy",
        type: "market",
        price: "",
        amount,
        available: "1000000",
        referencePrice: Number(book.mid_price),
        marketDataHealthy: true,
        marketDataIssue: "Healthy",
      }),
    ).toBeNull();
  });

  it("keeps non-paper execution metadata blocked in the frontend ticket", () => {
    const market = paperSwapMarket("BTC-USDT-SWAP");
    market.metadata.execution_mode = "live";

    expect(paperExecutionIssue(market)).toBe("Only paper execution is enabled.");
  });
});

describe("paper order submit completion", () => {
  it("resolves after the successful /orders response even when post-submit refresh is still pending", async () => {
    const submitOrder = vi.fn<Parameters<typeof submitPaperOrderAndRefresh>[0]["submitOrder"]>()
      .mockResolvedValue(acceptedOrderResult);
    const onAccepted = vi.fn();
    const refreshAccountData = vi.fn(() => new Promise<void>(() => undefined));

    const order = await submitPaperOrderAndRefresh({
      accountId: "account-1",
      payload: {
        symbol: "BTC-USDT-SWAP",
        side: "buy",
        order_type: "market",
        time_in_force: "ioc",
        quantity: "0.001",
        price: "50000",
        metadata: {
          paper_reference_price: "50000",
          source: "quantro_terminal",
        },
      },
      submitOrder,
      onAccepted,
      refreshAccountData,
    });

    expect(order).toEqual(baseOrder);
    expect(submitOrder).toHaveBeenCalledWith(expect.objectContaining({
      account_id: "account-1",
      symbol: "BTC-USDT-SWAP",
      side: "buy",
      order_type: "market",
      quantity: "0.001",
    }));
    expect(onAccepted).toHaveBeenCalledWith(acceptedOrderResult);
    expect(refreshAccountData).toHaveBeenCalledOnce();
  });

  it("surfaces /orders failures so OrderEntry can clear submitting in its finally block", async () => {
    const error = new Error("backend rejected");

    await expect(submitPaperOrderAndRefresh({
      accountId: "account-1",
      payload: {
        symbol: "BTC-USDT-SWAP",
        side: "buy",
        order_type: "market",
        time_in_force: "ioc",
        quantity: "0.001",
        price: "50000",
      },
      submitOrder: vi.fn().mockRejectedValue(error),
      onAccepted: vi.fn(),
      refreshAccountData: vi.fn(),
    })).rejects.toThrow("backend rejected");
  });
});

describe("Trade bottom workspace tabs", () => {
  it.each([
    ["orders", "ORDER HISTORY", "Filled"],
    ["trades", "TRADE HISTORY", "Taker"],
    ["positions", "POSITIONS", "LONG"],
    ["pnl", "PNL", "Backend"],
  ] as const)("renders %s tab content inside the accessible workspace pane", (tab, label, expectedContent) => {
    const html = renderToString(
      <BottomWorkspace
        defaultTab={tab}
        orders={[baseOrder]}
        trades={[baseTrade]}
        positions={[basePosition]}
        pnl={pnl}
        symbol="BTC-USDT-SWAP"
        currentPrice={50100}
        onCancelOrder={vi.fn()}
      />,
    );

    expect(html).toContain("workspace-tabs");
    expect(html).toContain(label);
    expect(html).toContain('class="workspace-content"');
    expect(html).toContain(expectedContent);
    expect(html.indexOf(expectedContent)).toBeGreaterThan(html.indexOf('class="workspace-content"'));
  });

  it("keeps bottom workspace content bounded and scrollable in the fixed terminal layout", () => {
    const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");

    expect(css).toMatch(/\.terminal-grid\s*{[^}]*height:\s*calc\(100vh - 54px\);[^}]*overflow:\s*hidden;/s);
    expect(css).toMatch(/\.terminal-center\s*{[^}]*grid-template-rows:\s*auto auto minmax\(70px,\s*max-content\) minmax\(0,\s*1fr\) minmax\(178px,\s*30vh\);[^}]*overflow:\s*hidden;/s);
    expect(css).toMatch(/\.chart-panel\s*{[^}]*grid-template-rows:\s*40px minmax\(0,\s*1fr\);[^}]*overflow:\s*hidden;/s);
    expect(css).toMatch(/\.chart-canvas\s*{[^}]*width:\s*100%;[^}]*height:\s*100%;[^}]*min-width:\s*0;[^}]*min-height:\s*0;[^}]*overflow:\s*hidden;/s);
    expect(css).toMatch(/\.chart-render-area,\s*\.lightweight-chart\s*{[^}]*inset:\s*0;[^}]*z-index:\s*1;[^}]*width:\s*100%;[^}]*height:\s*100%;/s);
    expect(css).toMatch(/\.chart-status\s*{[^}]*z-index:\s*2;[^}]*pointer-events:\s*none;/s);
    expect(css).toMatch(/\.bottom-workspace\s*{[^}]*grid-template-rows:\s*39px minmax\(0,\s*1fr\);/s);
    expect(css).toMatch(/\.workspace-content\s*{[^}]*overflow:\s*auto;/s);
    expect(css).toMatch(/\.table-wrap\s*{[^}]*height:\s*100%;/s);
  });

  it("keeps the bottom tab bar before scrollable content so it cannot be clipped by content overflow", () => {
    const html = renderToString(
      <BottomWorkspace
        defaultTab="open"
        orders={[{ ...baseOrder, status: "open", remaining_quantity: "0.001", filled_quantity: "0" }]}
        trades={[baseTrade]}
        positions={[basePosition]}
        pnl={pnl}
        symbol="BTC-USDT-SWAP"
        currentPrice={50100}
        onCancelOrder={vi.fn()}
      />,
    );

    const tabsIndex = html.indexOf('class="workspace-tabs"');
    const contentIndex = html.indexOf('class="workspace-content"');

    expect(tabsIndex).toBeGreaterThan(-1);
    expect(contentIndex).toBeGreaterThan(tabsIndex);
    expect(html).toContain("OPEN ORDERS 1");
    expect(html).toContain("ORDER HISTORY");
    expect(html).toContain("TRADE HISTORY");
    expect(html).toContain("POSITIONS");
    expect(html).toContain("PNL");
    expect(html.indexOf("BTC-USDT-SWAP")).toBeGreaterThan(contentIndex);
  });
});
