"""Tests for order book implementation."""

from uuid import uuid4

import pytest

from quantro.fixedpoint import FixedPoint
from quantro.models import (
    Account,
    Market,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from quantro.orderbook import OrderBook, OrderBookSide


@pytest.fixture
def market() -> Market:
    """Create test market."""
    return Market(
        symbol="BTC-USD",
        base_asset="BTC",
        quote_asset="USD",
        venue="TEST",
        price_precision=2,
        quantity_precision=8,
        min_order_size=FixedPoint("0.0001"),
        max_order_size=FixedPoint("1000"),
        tick_size=FixedPoint("0.01"),
        lot_size=FixedPoint("0.0001"),
        maker_fee=FixedPoint("0.001"),
        taker_fee=FixedPoint("0.001"),
    )


@pytest.fixture
def orderbook(market: Market) -> OrderBook:
    """Create empty order book."""
    return OrderBook(market)


@pytest.fixture
def account() -> Account:
    """Create test account."""
    return Account(name="test_account")


def create_limit_order(
    account: Account,
    market: Market,
    side: OrderSide,
    quantity: str,
    price: str,
    time_in_force: TimeInForce = TimeInForce.GTC,
) -> Order:
    """Helper to create limit order."""
    return Order(
        account_id=account.id,
        market=market,
        symbol=market.symbol,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=FixedPoint(quantity),
        price=FixedPoint(price),
        time_in_force=time_in_force,
    )


def create_market_order(
    account: Account,
    market: Market,
    side: OrderSide,
    quantity: str,
    time_in_force: TimeInForce = TimeInForce.IOC,
) -> Order:
    """Helper to create market order."""
    return Order(
        account_id=account.id,
        market=market,
        symbol=market.symbol,
        side=side,
        order_type=OrderType.MARKET,
        quantity=FixedPoint(quantity),
        time_in_force=time_in_force,
    )


class TestOrderBookBasics:
    """Test basic order book operations."""

    def test_empty_book(self, orderbook: OrderBook):
        assert orderbook.best_bid is None
        assert orderbook.best_ask is None
        assert orderbook.spread is None
        assert orderbook.mid_price is None
        assert len(orderbook) == 0

    def test_add_bid_order(self, orderbook: OrderBook, account: Account, market: Market):
        order = create_limit_order(account, market, OrderSide.BUY, "1.0", "50000.00")
        order = orderbook.add_order(order)

        assert order.status == OrderStatus.OPEN
        assert orderbook.best_bid == FixedPoint("50000.00")
        assert orderbook.best_ask is None
        assert len(orderbook) == 1

    def test_add_ask_order(self, orderbook: OrderBook, account: Account, market: Market):
        order = create_limit_order(account, market, OrderSide.SELL, "1.0", "50100.00")
        order = orderbook.add_order(order)

        assert order.status == OrderStatus.OPEN
        assert orderbook.best_ask == FixedPoint("50100.00")
        assert orderbook.best_bid is None
        assert len(orderbook) == 1

    def test_price_priority_bids(self, orderbook: OrderBook, account: Account, market: Market):
        # Add bids at different prices
        order1 = create_limit_order(account, market, OrderSide.BUY, "1.0", "50000.00")
        order2 = create_limit_order(account, market, OrderSide.BUY, "1.0", "50100.00")
        order3 = create_limit_order(account, market, OrderSide.BUY, "1.0", "49900.00")

        orderbook.add_order(order1)
        orderbook.add_order(order2)
        orderbook.add_order(order3)

        # Best bid should be highest price
        assert orderbook.best_bid == FixedPoint("50100.00")

        depth = orderbook.get_bid_depth(3)
        assert len(depth) == 3
        assert depth[0].price == FixedPoint("50100.00")
        assert depth[1].price == FixedPoint("50000.00")
        assert depth[2].price == FixedPoint("49900.00")

    def test_price_priority_asks(self, orderbook: OrderBook, account: Account, market: Market):
        # Add asks at different prices
        order1 = create_limit_order(account, market, OrderSide.SELL, "1.0", "50100.00")
        order2 = create_limit_order(account, market, OrderSide.SELL, "1.0", "50000.00")
        order3 = create_limit_order(account, market, OrderSide.SELL, "1.0", "50200.00")

        orderbook.add_order(order1)
        orderbook.add_order(order2)
        orderbook.add_order(order3)

        # Best ask should be lowest price
        assert orderbook.best_ask == FixedPoint("50000.00")

        depth = orderbook.get_ask_depth(3)
        assert len(depth) == 3
        assert depth[0].price == FixedPoint("50000.00")
        assert depth[1].price == FixedPoint("50100.00")
        assert depth[2].price == FixedPoint("50200.00")

    def test_fifo_at_same_price(self, orderbook: OrderBook, account: Account, market: Market):
        # Add multiple orders at same price
        order1 = create_limit_order(account, market, OrderSide.BUY, "1.0", "50000.00")
        order2 = create_limit_order(account, market, OrderSide.BUY, "2.0", "50000.00")
        order3 = create_limit_order(account, market, OrderSide.BUY, "3.0", "50000.00")

        orderbook.add_order(order1)
        orderbook.add_order(order2)
        orderbook.add_order(order3)

        level = orderbook.get_price_level(OrderBookSide.BID, FixedPoint("50000.00"))
        assert level is not None
        orders = list(level.orders)
        assert len(orders) == 3
        # FIFO: first added should be first in queue
        assert orders[0].id == order1.id
        assert orders[1].id == order2.id
        assert orders[2].id == order3.id


class TestMarketOrderExecution:
    """Test market order execution."""

    def test_market_buy_crosses_spread(
        self, orderbook: OrderBook, account: Account, market: Market
    ):
        # Add asks (sell orders)
        ask1 = create_limit_order(account, market, OrderSide.SELL, "1.0", "50100.00")
        ask2 = create_limit_order(account, market, OrderSide.SELL, "2.0", "50200.00")
        orderbook.add_order(ask1)
        orderbook.add_order(ask2)

        # Market buy for 2.5 BTC
        buy_order = create_market_order(account, market, OrderSide.BUY, "2.5")
        trades, updated_order = orderbook.execute_order(buy_order)

        # Should fill 1.0 @ 50100 and 1.5 @ 50200
        assert len(trades) == 4  # 2 trades per fill (taker + maker)

        # Check fills
        assert updated_order.filled_quantity == FixedPoint("2.5")
        assert updated_order.status == OrderStatus.FILLED

        # Verify remaining ask
        remaining_ask = orderbook.get_price_level(OrderBookSide.ASK, FixedPoint("50200.00"))
        assert remaining_ask is not None
        assert remaining_ask.total_quantity == FixedPoint("0.5")

    def test_market_sell_crosses_spread(
        self, orderbook: OrderBook, account: Account, market: Market
    ):
        # Add bids (buy orders)
        bid1 = create_limit_order(account, market, OrderSide.BUY, "1.0", "50000.00")
        bid2 = create_limit_order(account, market, OrderSide.BUY, "2.0", "49900.00")
        orderbook.add_order(bid1)
        orderbook.add_order(bid2)

        # Market sell for 2.5 BTC
        sell_order = create_market_order(account, market, OrderSide.SELL, "2.5")
        trades, updated_order = orderbook.execute_order(sell_order)

        # Should fill 1.0 @ 50000 and 1.5 @ 49900
        assert len(trades) == 4
        assert updated_order.filled_quantity == FixedPoint("2.5")
        assert updated_order.status == OrderStatus.FILLED

    def test_market_order_partial_fill_ioc(
        self, orderbook: OrderBook, account: Account, market: Market
    ):
        # Only 1 BTC available
        ask = create_limit_order(account, market, OrderSide.SELL, "1.0", "50100.00")
        orderbook.add_order(ask)

        # Market buy for 5 BTC with IOC
        buy_order = create_market_order(account, market, OrderSide.BUY, "5.0")
        buy_order = Order(
            id=buy_order.id,
            account_id=buy_order.account_id,
            market=buy_order.market,
            symbol=buy_order.symbol,
            side=buy_order.side,
            order_type=buy_order.order_type,
            status=buy_order.status,
            time_in_force=TimeInForce.IOC,
            quantity=buy_order.quantity,
            price=buy_order.price,
        )
        trades, updated_order = orderbook.execute_order(buy_order)

        # Should fill 1.0 only
        assert updated_order.filled_quantity == FixedPoint("1.0")
        assert updated_order.status == OrderStatus.CANCELLED  # IOC cancels remainder
        assert len(trades) == 2

    def test_market_order_fok_rejected(
        self, orderbook: OrderBook, account: Account, market: Market
    ):
        # Only 1 BTC available
        ask = create_limit_order(account, market, OrderSide.SELL, "1.0", "50100.00")
        orderbook.add_order(ask)

        # Market buy for 5 BTC with FOK
        buy_order = create_market_order(account, market, OrderSide.BUY, "5.0")
        buy_order = Order(
            id=buy_order.id,
            account_id=buy_order.account_id,
            market=buy_order.market,
            symbol=buy_order.symbol,
            side=buy_order.side,
            order_type=buy_order.order_type,
            status=buy_order.status,
            time_in_force=TimeInForce.FOK,
            quantity=buy_order.quantity,
            price=buy_order.price,
        )
        trades, updated_order = orderbook.execute_order(buy_order)

        # Should be rejected - FOK requires full fill or no trades
        assert len(trades) == 0
        assert updated_order.status == OrderStatus.REJECTED


class TestLimitOrderExecution:
    """Test limit order execution."""

    def test_limit_buy_fills_market(self, orderbook: OrderBook, account: Account, market: Market):
        # Add asks
        ask = create_limit_order(account, market, OrderSide.SELL, "1.0", "50100.00")
        orderbook.add_order(ask)

        # Limit buy at 50200 (crosses spread)
        buy_order = create_limit_order(account, market, OrderSide.BUY, "1.0", "50200.00")
        trades, updated_order = orderbook.execute_order(buy_order)

        assert len(trades) == 2
        assert updated_order.filled_quantity == FixedPoint("1.0")
        assert updated_order.status == OrderStatus.FILLED
        assert updated_order.average_fill_price == FixedPoint("50100.00")

    def test_limit_buy_partial_fill_rests(
        self, orderbook: OrderBook, account: Account, market: Market
    ):
        # Add ask for 0.5 BTC
        ask = create_limit_order(account, market, OrderSide.SELL, "0.5", "50100.00")
        orderbook.add_order(ask)

        # Limit buy for 1.0 BTC at 50200
        buy_order = create_limit_order(account, market, OrderSide.BUY, "1.0", "50200.00")
        trades, updated_order = orderbook.execute_order(buy_order)

        assert len(trades) == 2
        assert updated_order.filled_quantity == FixedPoint("0.5")
        assert updated_order.status == OrderStatus.PARTIALLY_FILLED

        # Remaining should rest on book
        bid_level = orderbook.get_price_level(OrderBookSide.BID, FixedPoint("50200.00"))
        assert bid_level is not None
        assert bid_level.total_quantity == FixedPoint("0.5")

    def test_limit_buy_no_cross_rests(self, orderbook: OrderBook, account: Account, market: Market):
        # Add ask at 50200
        ask = create_limit_order(account, market, OrderSide.SELL, "1.0", "50200.00")
        orderbook.add_order(ask)

        # Limit buy at 50000 (below ask, doesn't cross)
        buy_order = create_limit_order(account, market, OrderSide.BUY, "1.0", "50000.00")
        trades, updated_order = orderbook.execute_order(buy_order)

        assert len(trades) == 0
        assert updated_order.status == OrderStatus.OPEN
        assert updated_order.filled_quantity == FixedPoint("0.0")

        # Should be on book at 50000
        bid_level = orderbook.get_price_level(OrderBookSide.BID, FixedPoint("50000.00"))
        assert bid_level is not None
        assert bid_level.total_quantity == FixedPoint("1.0")

    def test_limit_sell_fills_market(self, orderbook: OrderBook, account: Account, market: Market):
        # Add bids
        bid = create_limit_order(account, market, OrderSide.BUY, "1.0", "50000.00")
        orderbook.add_order(bid)

        # Limit sell at 49900 (crosses spread)
        sell_order = create_limit_order(account, market, OrderSide.SELL, "1.0", "49900.00")
        trades, updated_order = orderbook.execute_order(sell_order)

        assert len(trades) == 2
        assert updated_order.filled_quantity == FixedPoint("1.0")
        assert updated_order.status == OrderStatus.FILLED
        assert updated_order.average_fill_price == FixedPoint("50000.00")

    def test_limit_ioc_partial(self, orderbook: OrderBook, account: Account, market: Market):
        # Add ask for 0.5 BTC
        ask = create_limit_order(account, market, OrderSide.SELL, "0.5", "50100.00")
        orderbook.add_order(ask)

        # IOC limit buy for 1.0 BTC
        buy_order = create_limit_order(
            account, market, OrderSide.BUY, "1.0", "50200.00", TimeInForce.IOC
        )
        trades, updated_order = orderbook.execute_order(buy_order)

        assert updated_order.filled_quantity == FixedPoint("0.5")
        assert updated_order.status == OrderStatus.CANCELLED

        # Nothing should rest
        bid_level = orderbook.get_price_level(OrderBookSide.BID, FixedPoint("50200.00"))
        assert bid_level is None or bid_level.is_empty()

    def test_limit_fok_partial_rejected(
        self, orderbook: OrderBook, account: Account, market: Market
    ):
        # Add ask for 0.5 BTC
        ask = create_limit_order(account, market, OrderSide.SELL, "0.5", "50100.00")
        orderbook.add_order(ask)

        # FOK limit buy for 1.0 BTC
        buy_order = create_limit_order(
            account, market, OrderSide.BUY, "1.0", "50200.00", TimeInForce.FOK
        )
        trades, updated_order = orderbook.execute_order(buy_order)

        # Should be rejected - FOK requires full fill or no trades
        assert len(trades) == 0
        assert updated_order.status == OrderStatus.REJECTED


class TestOrderCancellation:
    """Test order cancellation."""

    def test_cancel_existing_order(self, orderbook: OrderBook, account: Account, market: Market):
        order = create_limit_order(account, market, OrderSide.BUY, "1.0", "50000.00")
        orderbook.add_order(order)

        cancelled = orderbook.cancel_order(order.id)

        assert cancelled is not None
        assert cancelled.status == OrderStatus.CANCELLED
        assert len(orderbook) == 0
        assert orderbook.best_bid is None

    def test_cancel_nonexistent_order(self, orderbook: OrderBook):
        cancelled = orderbook.cancel_order(uuid4())
        assert cancelled is None

    def test_cancel_partial_fill(self, orderbook: OrderBook, account: Account, market: Market):
        # Add ask
        ask = create_limit_order(account, market, OrderSide.SELL, "2.0", "50100.00")
        orderbook.add_order(ask)

        # Market buy 1.0
        buy_order = create_market_order(account, market, OrderSide.BUY, "1.0")
        orderbook.execute_order(buy_order)

        # Cancel remaining ask
        cancelled = orderbook.cancel_order(ask.id)

        assert cancelled is not None
        assert cancelled.filled_quantity == FixedPoint("1.0")
        assert cancelled.remaining_quantity == FixedPoint("1.0")
        assert cancelled.status == OrderStatus.CANCELLED
        assert len(orderbook) == 0

    def test_cancel_all_for_account(self, orderbook: OrderBook, account: Account, market: Market):
        account2 = Account(name="account2")

        # Add orders from both accounts
        order1 = create_limit_order(account, market, OrderSide.BUY, "1.0", "50000.00")
        order2 = create_limit_order(account, market, OrderSide.BUY, "2.0", "49900.00")
        order3 = create_limit_order(account2, market, OrderSide.SELL, "1.0", "50100.00")

        orderbook.add_order(order1)
        orderbook.add_order(order2)
        orderbook.add_order(order3)

        cancelled = orderbook.cancel_all_orders(account.id)

        assert len(cancelled) == 2
        assert all(o.account_id == account.id for o in cancelled)
        assert len(orderbook) == 1
        assert orderbook.best_ask == FixedPoint("50100.00")


class TestMarketDepth:
    """Test market depth queries."""

    def test_get_bid_depth(self, orderbook: OrderBook, account: Account, market: Market):
        # Add multiple bid levels
        for price in ["50000.00", "49900.00", "49800.00", "49700.00"]:
            order = create_limit_order(account, market, OrderSide.BUY, "1.0", price)
            orderbook.add_order(order)

        depth = orderbook.get_bid_depth(2)
        assert len(depth) == 2
        assert depth[0].price == FixedPoint("50000.00")
        assert depth[1].price == FixedPoint("49900.00")

        depth_all = orderbook.get_bid_depth(10)
        assert len(depth_all) == 4

    def test_get_ask_depth(self, orderbook: OrderBook, account: Account, market: Market):
        # Add multiple ask levels
        for price in ["50100.00", "50200.00", "50300.00", "50400.00"]:
            order = create_limit_order(account, market, OrderSide.SELL, "1.0", price)
            orderbook.add_order(order)

        depth = orderbook.get_ask_depth(2)
        assert len(depth) == 2
        assert depth[0].price == FixedPoint("50100.00")
        assert depth[1].price == FixedPoint("50200.00")

    def test_get_full_depth(self, orderbook: OrderBook, account: Account, market: Market):
        # Add bids and asks
        for price in ["50000.00", "49900.00"]:
            orderbook.add_order(create_limit_order(account, market, OrderSide.BUY, "1.0", price))
        for price in ["50100.00", "50200.00"]:
            orderbook.add_order(create_limit_order(account, market, OrderSide.SELL, "1.0", price))

        bids, asks = orderbook.get_full_depth()
        assert len(bids) == 2
        assert len(asks) == 2
        assert bids[0].price == FixedPoint("50000.00")
        assert asks[0].price == FixedPoint("50100.00")

    def test_snapshot(self, orderbook: OrderBook, account: Account, market: Market):
        orderbook.add_order(create_limit_order(account, market, OrderSide.BUY, "1.0", "50000.00"))
        orderbook.add_order(create_limit_order(account, market, OrderSide.SELL, "1.0", "50100.00"))

        snap = orderbook.snapshot()

        assert snap.symbol == "BTC-USD"
        assert len(snap.bids) == 1
        assert len(snap.asks) == 1
        assert snap.bids[0].price == FixedPoint("50000.00")
        assert snap.asks[0].price == FixedPoint("50100.00")
        assert snap.sequence == 2


class TestTradeFeeCalculation:
    """Test fee calculation on trades."""

    def test_maker_taker_fees(self, orderbook: OrderBook, account: Account, market: Market):
        # Add ask (maker)
        ask = create_limit_order(account, market, OrderSide.SELL, "1.0", "50100.00")
        orderbook.add_order(ask)

        # Market buy (taker)
        buy_order = create_market_order(account, market, OrderSide.BUY, "1.0")
        trades, _ = orderbook.execute_order(buy_order)

        assert len(trades) == 2
        taker_trade = next(t for t in trades if not t.is_maker)
        maker_trade = next(t for t in trades if t.is_maker)

        # Taker fee: 1.0 * 50100 * 0.001 = 50.1
        # Maker fee: 1.0 * 50100 * 0.001 = 50.1
        expected_fee = FixedPoint("50.1")

        assert taker_trade.fee == expected_fee
        assert maker_trade.fee == expected_fee
        assert taker_trade.fee_asset == "USD"
        assert maker_trade.fee_asset == "USD"

    def test_fee_on_partial_fill(self, orderbook: OrderBook, account: Account, market: Market):
        # Add ask for 2.0
        ask = create_limit_order(account, market, OrderSide.SELL, "2.0", "50100.00")
        orderbook.add_order(ask)

        # Market buy for 1.0
        buy_order = create_market_order(account, market, OrderSide.BUY, "1.0")
        trades, _ = orderbook.execute_order(buy_order)

        expected_fee = FixedPoint("1.0") * FixedPoint("50100.00") * FixedPoint("0.001")

        for trade in trades:
            assert trade.fee == expected_fee
            assert trade.quantity == FixedPoint("1.0")


class TestMultiplePriceLevels:
    """Test multiple price level handling."""

    def test_walk_book_market_buy(self, orderbook: OrderBook, account: Account, market: Market):
        # Add multiple ask levels
        for i, price in enumerate(["50100.00", "50200.00", "50300.00", "50400.00"]):
            order = create_limit_order(account, market, OrderSide.SELL, "1.0", price)
            orderbook.add_order(order)

        # Market buy 3.5 BTC
        buy_order = create_market_order(account, market, OrderSide.BUY, "3.5")
        trades, updated_order = orderbook.execute_order(buy_order)

        # Should fill: 1.0 @ 50100, 1.0 @ 50200, 1.0 @ 50300, 0.5 @ 50400
        assert updated_order.filled_quantity == FixedPoint("3.5")
        assert updated_order.status == OrderStatus.FILLED

        # Check VWAP
        expected_vwap = (
            FixedPoint("1.0") * FixedPoint("50100.00")
            + FixedPoint("1.0") * FixedPoint("50200.00")
            + FixedPoint("1.0") * FixedPoint("50300.00")
            + FixedPoint("0.5") * FixedPoint("50400.00")
        ) / FixedPoint("3.5")

        assert updated_order.average_fill_price == expected_vwap

    def test_walk_book_limit_sell(self, orderbook: OrderBook, account: Account, market: Market):
        # Add multiple bid levels
        for i, price in enumerate(["50000.00", "49900.00", "49800.00", "49700.00"]):
            order = create_limit_order(account, market, OrderSide.BUY, "1.0", price)
            orderbook.add_order(order)

        # Limit sell 3.5 BTC at 49600 (crosses all)
        sell_order = create_limit_order(account, market, OrderSide.SELL, "3.5", "49600.00")
        trades, updated_order = orderbook.execute_order(sell_order)

        assert updated_order.filled_quantity == FixedPoint("3.5")
        assert updated_order.status == OrderStatus.FILLED


class TestEdgeCases:
    """Test edge cases."""

    def test_invalid_price_precision(self, orderbook: OrderBook, account: Account, market: Market):
        # Price not matching tick size
        order = create_limit_order(account, market, OrderSide.BUY, "1.0", "50000.005")

        with pytest.raises(ValueError, match="Invalid price"):
            orderbook.add_order(order)

    def test_order_not_found_after_fill(
        self, orderbook: OrderBook, account: Account, market: Market
    ):
        ask = create_limit_order(account, market, OrderSide.SELL, "1.0", "50100.00")
        orderbook.add_order(ask)

        buy_order = create_market_order(account, market, OrderSide.BUY, "1.0")
        orderbook.execute_order(buy_order)

        # Filled order should be removed from book
        assert orderbook.get_order(ask.id) is None
        assert ask.id not in orderbook

    def test_sequence_increments(self, orderbook: OrderBook, account: Account, market: Market):
        initial_seq = orderbook.sequence

        orderbook.add_order(create_limit_order(account, market, OrderSide.BUY, "1.0", "50000.00"))
        assert orderbook.sequence == initial_seq + 1

        orderbook.cancel_order(list(orderbook._order_index.keys())[0])
        assert orderbook.sequence == initial_seq + 2

        orderbook.add_order(create_limit_order(account, market, OrderSide.SELL, "1.0", "50100.00"))
        orderbook.execute_order(create_market_order(account, market, OrderSide.BUY, "1.0"))
        assert orderbook.sequence == initial_seq + 4


class TestOrderLifecycle:
    """Test complete order lifecycle."""

    def test_full_lifecycle_limit_order(
        self, orderbook: OrderBook, account: Account, market: Market
    ):
        # Add liquidity
        ask = create_limit_order(account, market, OrderSide.SELL, "2.0", "50100.00")
        orderbook.add_order(ask)

        # Place limit buy that partially fills
        buy_order = create_limit_order(account, market, OrderSide.BUY, "3.0", "50200.00")
        trades, updated_order = orderbook.execute_order(buy_order)

        assert updated_order.filled_quantity == FixedPoint("2.0")
        assert updated_order.status == OrderStatus.PARTIALLY_FILLED
        assert updated_order.remaining_quantity == FixedPoint("1.0")
        assert len(trades) == 2  # 1 fill x 2 sides (taker + maker)

        # Resting order should be on book
        bid_level = orderbook.get_price_level(OrderBookSide.BID, FixedPoint("50200.00"))
        assert bid_level is not None
        assert bid_level.total_quantity == FixedPoint("1.0")

        # Add matching sell order
        sell_order = create_limit_order(account, market, OrderSide.SELL, "1.0", "50200.00")
        trades2, updated_sell = orderbook.execute_order(sell_order)

        assert len(trades2) == 2
        assert updated_sell.filled_quantity == FixedPoint("1.0")
        assert updated_sell.status == OrderStatus.FILLED
        # The buy order should now be fully filled (it was resting on the book)
        # Since it was fully filled, it's removed from the book but we can verify
        # by checking that the best bid is now None
        assert orderbook.best_bid is None  # No more bids at 50200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
