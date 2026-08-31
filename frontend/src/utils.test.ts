import { describe, expect, it } from "vitest";
import type { Market } from "./types";
import { balanceValue, chartFeedStatus, portfolioBalanceAsset } from "./utils";

const paperSwapMarket: Market = {
  symbol: "BTC-USDT-SWAP",
  base_asset: "BTC",
  quote_asset: "USDT",
  venue: "PAPER",
  price_precision: 1,
  quantity_precision: 4,
  min_order_size: "0.0001",
  max_order_size: "1000",
  tick_size: "0.1",
  lot_size: "0.0001",
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

describe("portfolio balance asset helpers", () => {
  it("uses USDT for paper swap markets", () => {
    expect(portfolioBalanceAsset([paperSwapMarket])).toBe("USDT");
  });

  it("values the selected virtual balance asset consistently", () => {
    expect(
      balanceValue(
        { asset: "USDT", free: "1000000", locked: "0", total: "1000000" },
        [paperSwapMarket],
        "USDT",
      ),
    ).toBe(1000000);
    expect(
      balanceValue(
        { asset: "USD", free: "100000", locked: "0", total: "100000" },
        [paperSwapMarket],
        "USDT",
      ),
    ).toBe(0);
  });

  it("keeps the chart ready while refreshing existing candle data", () => {
    expect(chartFeedStatus(240, true, null)).toBe("Ready");
    expect(chartFeedStatus(0, true, null)).toBe("Syncing");
    expect(chartFeedStatus(0, false, "provider unavailable")).toBe("Unavailable");
  });
});
