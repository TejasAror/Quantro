"""Tests for portfolio accounting."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from quantro.fixedpoint import FixedPoint
from quantro.models import (
    Account,
    Balance,
    Market,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
    TimeInForce,
    Trade,
)
from quantro.portfolio import Portfolio, PortfolioManager


@pytest.fixture
def market() -> Market:
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
def account() -> Account:
    acc = Account(name="test_account")
    acc = acc.set_balance(Balance(asset="USD", free=FixedPoint("100000"), locked=FixedPoint("0")))
    acc = acc.set_balance(Balance(asset="BTC", free=FixedPoint("10"), locked=FixedPoint("0")))
    return acc


@pytest.fixture
def portfolio(account: Account) -> Portfolio:
    return Portfolio(account)


def create_buy_order(account: Account, market: Market, qty: str, price: str) -> Order:
    return Order(
        account_id=account.id,
        market=market,
        symbol=market.symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=FixedPoint(qty),
        price=FixedPoint(price),
        time_in_force=TimeInForce.GTC,
    )


def create_sell_order(account: Account, market: Market, qty: str, price: str) -> Order:
    return Order(
        account_id=account.id,
        market=market,
        symbol=market.symbol,
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=FixedPoint(qty),
        price=FixedPoint(price),
        time_in_force=TimeInForce.GTC,
    )


class TestPortfolioBasics:
    """Test basic portfolio operations."""

    def test_initial_state(self, portfolio: Portfolio, account: Account):
        assert portfolio.account_id == account.id
        assert portfolio.open_orders_count == 0
        assert len(portfolio) == 0
        assert portfolio.get_total_unrealized_pnl() == FixedPoint("0")
        assert portfolio.get_total_realized_pnl() == FixedPoint("0")

    def test_get_balance(self, portfolio: Portfolio):
        usd = portfolio.get_balance("USD")
        assert usd.asset == "USD"
        assert usd.free == FixedPoint("100000")
        assert usd.locked == FixedPoint("0")

        btc = portfolio.get_balance("BTC")
        assert btc.asset == "BTC"
        assert btc.free == FixedPoint("10")

    def test_get_nonexistent_balance(self, portfolio: Portfolio):
        eth = portfolio.get_balance("ETH")
        assert eth.asset == "ETH"
        assert eth.free == FixedPoint("0")
        assert eth.locked == FixedPoint("0")

    def test_increment_decrement_open_orders(self, portfolio: Portfolio):
        p1 = portfolio.increment_open_orders()
        assert p1.open_orders_count == 1

        p2 = p1.increment_open_orders()
        assert p2.open_orders_count == 2

        p3 = p2.decrement_open_orders()
        assert p3.open_orders_count == 1

        p4 = p3.decrement_open_orders()
        assert p4.open_orders_count == 0

        # Can't go below 0
        p5 = p4.decrement_open_orders()
        assert p5.open_orders_count == 0


class TestPortfolioLockUnlockFunds:
    """Test locking and unlocking funds for orders."""

    def test_lock_funds_buy_order(self, portfolio: Portfolio, account: Account, market: Market):
        order = create_buy_order(account, market, "1.0", "50000.00")
        new_portfolio = portfolio.lock_funds_for_order(order)

        # Should lock 50000 + 0.1% fee = 50050 USD
        usd = new_portfolio.get_balance("USD")
        assert usd.free == FixedPoint("100000") - FixedPoint("50050")
        assert usd.locked == FixedPoint("50050")
        assert new_portfolio.open_orders_count == 1

    def test_lock_funds_sell_order(self, portfolio: Portfolio, account: Account, market: Market):
        order = create_sell_order(account, market, "1.0", "50000.00")
        new_portfolio = portfolio.lock_funds_for_order(order)

        # Should lock 1 BTC
        btc = new_portfolio.get_balance("BTC")
        assert btc.free == FixedPoint("10") - FixedPoint("1")
        assert btc.locked == FixedPoint("1")
        assert new_portfolio.open_orders_count == 1

    def test_unlock_funds_buy_order_cancelled(
        self, portfolio: Portfolio, account: Account, market: Market
    ):
        order = create_buy_order(account, market, "1.0", "50000.00")
        locked = portfolio.lock_funds_for_order(order)
        unlocked = locked.unlock_funds_from_order(order)

        usd = unlocked.get_balance("USD")
        assert usd.free == FixedPoint("100000")
        assert usd.locked == FixedPoint("0")
        assert unlocked.open_orders_count == 0

    def test_unlock_funds_sell_order_cancelled(
        self, portfolio: Portfolio, account: Account, market: Market
    ):
        order = create_sell_order(account, market, "1.0", "50000.00")
        locked = portfolio.lock_funds_for_order(order)
        unlocked = locked.unlock_funds_from_order(order)

        btc = unlocked.get_balance("BTC")
        assert btc.free == FixedPoint("10")
        assert btc.locked == FixedPoint("0")
        assert unlocked.open_orders_count == 0

    def test_unlock_partial_fill(self, portfolio: Portfolio, account: Account, market: Market):
        order = create_buy_order(account, market, "1.0", "50000.00")
        locked = portfolio.lock_funds_for_order(order)

        # Partially fill 0.5 out of 1.0
        # Create a filled order with 0.5 filled
        filled_order = Order(
            id=order.id,
            account_id=order.account_id,
            market=order.market,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            status=OrderStatus.PARTIALLY_FILLED,
            time_in_force=order.time_in_force,
            quantity=order.quantity,
            price=order.price,
            filled_quantity=FixedPoint("0.5"),
        )

        unlocked = locked.unlock_funds_from_order(filled_order, FixedPoint("0.5"))

        # Should unlock remaining 0.5 worth = 25025 USD
        usd = unlocked.get_balance("USD")
        assert usd.free == FixedPoint("100000") - FixedPoint("25025")  # Only 0.5 still locked
        assert usd.locked == FixedPoint("25025")


class TestPortfolioApplyTrade:
    """Test applying trades to portfolio."""

    def test_apply_buy_trade(self, portfolio: Portfolio, account: Account, market: Market):
        trade = Trade(
            order_id=uuid4(),
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=OrderSide.BUY,
            quantity=FixedPoint("1.0"),
            price=FixedPoint("50000.00"),
            fee=FixedPoint("50.00"),
            fee_asset="USD",
            timestamp=datetime.now(UTC).replace(tzinfo=None),
        )

        mark_prices = {market.symbol: FixedPoint("50000.00")}
        new_portfolio = portfolio.apply_trade(trade, mark_prices)

        # Check balances
        usd = new_portfolio.get_balance("USD")
        btc = new_portfolio.get_balance("BTC")

        # Spent 50000 + 50 fee = 50050 USD
        assert usd.free == FixedPoint("100000") - FixedPoint("50050")
        # Received 1 BTC
        assert btc.free == FixedPoint("10") + FixedPoint("1.0")

        # Check position
        pos = new_portfolio.get_position(market.symbol)
        assert pos is not None
        assert pos.side == PositionSide.LONG
        assert pos.size == FixedPoint("1.0")
        assert pos.entry_price == FixedPoint("50000.00")
        assert pos.unrealized_pnl == FixedPoint("0")

        # Check trade history
        assert len(new_portfolio) == 1
        assert new_portfolio.trade_history[0] == trade

    def test_apply_sell_trade(self, portfolio: Portfolio, account: Account, market: Market):
        trade = Trade(
            order_id=uuid4(),
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=OrderSide.SELL,
            quantity=FixedPoint("1.0"),
            price=FixedPoint("50000.00"),
            fee=FixedPoint("50.00"),
            fee_asset="USD",
            timestamp=datetime.now(UTC).replace(tzinfo=None),
        )

        mark_prices = {market.symbol: FixedPoint("50000.00")}
        new_portfolio = portfolio.apply_trade(trade, mark_prices)

        # Check balances
        usd = new_portfolio.get_balance("USD")
        btc = new_portfolio.get_balance("BTC")

        # Received 50000 - 50 fee = 49950 USD
        assert usd.free == FixedPoint("100000") + FixedPoint("49950")
        # Spent 1 BTC
        assert btc.free == FixedPoint("10") - FixedPoint("1.0")

        # Check position
        pos = new_portfolio.get_position(market.symbol)
        assert pos is not None
        assert pos.side == PositionSide.SHORT
        assert pos.size == FixedPoint("1.0")
        assert pos.entry_price == FixedPoint("50000.00")

    def test_apply_trade_add_to_long(self, portfolio: Portfolio, account: Account, market: Market):
        # Start with existing long position
        from quantro.models import PositionSide

        initial_pos = Position(
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=PositionSide.LONG,
            size=FixedPoint("1.0"),
            entry_price=FixedPoint("49000.00"),
            mark_price=FixedPoint("50000.00"),
        )
        port_with_pos = Portfolio(account, initial_positions={market.symbol: initial_pos})

        # Add 1.0 more at 50000
        trade = Trade(
            order_id=uuid4(),
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=OrderSide.BUY,
            quantity=FixedPoint("1.0"),
            price=FixedPoint("50000.00"),
            fee=FixedPoint("50.00"),
            fee_asset="USD",
            timestamp=datetime.now(UTC).replace(tzinfo=None),
        )

        mark_prices = {market.symbol: FixedPoint("50000.00")}
        new_portfolio = port_with_pos.apply_trade(trade, mark_prices)

        pos = new_portfolio.get_position(market.symbol)
        assert pos is not None
        assert pos.size == FixedPoint("2.0")
        # Weighted avg: (1*49000 + 1*50000) / 2 = 49500
        assert pos.entry_price == FixedPoint("49500.00")

    def test_apply_trade_reduce_long(self, portfolio: Portfolio, account: Account, market: Market):
        from quantro.models import PositionSide

        initial_pos = Position(
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=PositionSide.LONG,
            size=FixedPoint("2.0"),
            entry_price=FixedPoint("49000.00"),
            mark_price=FixedPoint("50000.00"),
        )
        port_with_pos = Portfolio(account, initial_positions={market.symbol: initial_pos})

        # Sell 1.0 at 51000
        trade = Trade(
            order_id=uuid4(),
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=OrderSide.SELL,
            quantity=FixedPoint("1.0"),
            price=FixedPoint("51000.00"),
            fee=FixedPoint("51.00"),
            fee_asset="USD",
            timestamp=datetime.now(UTC).replace(tzinfo=None),
        )

        mark_prices = {market.symbol: FixedPoint("51000.00")}
        new_portfolio = port_with_pos.apply_trade(trade, mark_prices)

        pos = new_portfolio.get_position(market.symbol)
        assert pos is not None
        assert pos.size == FixedPoint("1.0")
        assert pos.entry_price == FixedPoint("49000.00")  # Entry price unchanged
        # Realized PnL: (51000 - 49000) * 1 = 2000
        assert pos.realized_pnl == FixedPoint("2000.00")

    def test_apply_trade_close_long(self, portfolio: Portfolio, account: Account, market: Market):
        from quantro.models import PositionSide

        initial_pos = Position(
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=PositionSide.LONG,
            size=FixedPoint("1.0"),
            entry_price=FixedPoint("49000.00"),
            mark_price=FixedPoint("50000.00"),
        )
        port_with_pos = Portfolio(account, initial_positions={market.symbol: initial_pos})

        # Sell all 1.0 at 51000
        trade = Trade(
            order_id=uuid4(),
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=OrderSide.SELL,
            quantity=FixedPoint("1.0"),
            price=FixedPoint("51000.00"),
            fee=FixedPoint("51.00"),
            fee_asset="USD",
            timestamp=datetime.now(UTC).replace(tzinfo=None),
        )

        mark_prices = {market.symbol: FixedPoint("51000.00")}
        new_portfolio = port_with_pos.apply_trade(trade, mark_prices)

        pos = new_portfolio.get_position(market.symbol)
        assert pos is None or pos.is_flat
        assert market.symbol not in new_portfolio._positions

    def test_apply_trade_flip_long_to_short(
        self, portfolio: Portfolio, account: Account, market: Market
    ):
        from quantro.models import PositionSide

        initial_pos = Position(
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=PositionSide.LONG,
            size=FixedPoint("1.0"),
            entry_price=FixedPoint("49000.00"),
            mark_price=FixedPoint("50000.00"),
        )
        port_with_pos = Portfolio(account, initial_positions={market.symbol: initial_pos})

        # Sell 2.0 at 51000 (1 to close, 1 to go short)
        trade = Trade(
            order_id=uuid4(),
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=OrderSide.SELL,
            quantity=FixedPoint("2.0"),
            price=FixedPoint("51000.00"),
            fee=FixedPoint("102.00"),
            fee_asset="USD",
            timestamp=datetime.now(UTC).replace(tzinfo=None),
        )

        mark_prices = {market.symbol: FixedPoint("51000.00")}
        new_portfolio = port_with_pos.apply_trade(trade, mark_prices)

        pos = new_portfolio.get_position(market.symbol)
        assert pos is not None
        assert pos.side == PositionSide.SHORT
        assert pos.size == FixedPoint("1.0")
        assert pos.entry_price == FixedPoint("51000.00")
        # Realized PnL: (51000 - 49000) * 1 = 2000
        assert pos.realized_pnl == FixedPoint("2000.00")


class TestPortfolioApplyOrderFill:
    """Test applying order fills."""

    def test_apply_order_fill_buy(self, portfolio: Portfolio, account: Account, market: Market):
        order = create_buy_order(account, market, "1.0", "50000.00")

        mark_prices = {market.symbol: FixedPoint("50000.00")}
        new_portfolio, trade = portfolio.apply_order_fill(
            order, FixedPoint("1.0"), FixedPoint("50000.00"), FixedPoint("50.00"), mark_prices
        )

        assert trade.quantity == FixedPoint("1.0")
        assert trade.price == FixedPoint("50000.00")
        assert trade.side == OrderSide.BUY

        btc = new_portfolio.get_balance("BTC")
        assert btc.free == FixedPoint("11.0")

    def test_apply_order_fill_sell(self, portfolio: Portfolio, account: Account, market: Market):
        order = create_sell_order(account, market, "1.0", "50000.00")

        mark_prices = {market.symbol: FixedPoint("50000.00")}
        new_portfolio, trade = portfolio.apply_order_fill(
            order, FixedPoint("1.0"), FixedPoint("50000.00"), FixedPoint("50.00"), mark_prices
        )

        assert trade.side == OrderSide.SELL

        btc = new_portfolio.get_balance("BTC")
        assert btc.free == FixedPoint("9.0")


class TestPortfolioPnL:
    """Test P&L calculations."""

    def test_unrealized_pnl_long(self, portfolio: Portfolio, account: Account, market: Market):
        from quantro.models import PositionSide

        initial_pos = Position(
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=PositionSide.LONG,
            size=FixedPoint("1.0"),
            entry_price=FixedPoint("49000.00"),
            mark_price=FixedPoint("50000.00"),
        )
        port_with_pos = Portfolio(account, initial_positions={market.symbol: initial_pos})

        # Update mark price to 51000
        mark_prices = {market.symbol: FixedPoint("51000.00")}
        updated = port_with_pos.update_mark_prices(mark_prices)

        pos = updated.get_position(market.symbol)
        # Unrealized: (51000 - 49000) * 1 = 2000
        assert pos.unrealized_pnl == FixedPoint("2000.00")
        assert updated.get_total_unrealized_pnl() == FixedPoint("2000.00")

    def test_unrealized_pnl_short(self, portfolio: Portfolio, account: Account, market: Market):
        from quantro.models import PositionSide

        initial_pos = Position(
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=PositionSide.SHORT,
            size=FixedPoint("1.0"),
            entry_price=FixedPoint("51000.00"),
            mark_price=FixedPoint("50000.00"),
        )
        port_with_pos = Portfolio(account, initial_positions={market.symbol: initial_pos})

        # Update mark price to 49000
        mark_prices = {market.symbol: FixedPoint("49000.00")}
        updated = port_with_pos.update_mark_prices(mark_prices)

        pos = updated.get_position(market.symbol)
        # Unrealized: (51000 - 49000) * 1 = 2000
        assert pos.unrealized_pnl == FixedPoint("2000.00")

    def test_realized_pnl_accumulates(self, portfolio: Portfolio, account: Account, market: Market):
        from quantro.models import PositionSide

        initial_pos = Position(
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=PositionSide.LONG,
            size=FixedPoint("1.0"),
            entry_price=FixedPoint("49000.00"),
            mark_price=FixedPoint("50000.00"),
            realized_pnl=FixedPoint("1000.00"),
        )
        port_with_pos = Portfolio(account, initial_positions={market.symbol: initial_pos})

        # Sell at 51000
        trade = Trade(
            order_id=uuid4(),
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=OrderSide.SELL,
            quantity=FixedPoint("1.0"),
            price=FixedPoint("51000.00"),
            fee=FixedPoint("51.00"),
            fee_asset="USD",
            timestamp=datetime.now(UTC).replace(tzinfo=None),
        )

        mark_prices = {market.symbol: FixedPoint("51000.00")}
        new_portfolio = port_with_pos.apply_trade(trade, mark_prices)

        # Total realized: 1000 + 2000 = 3000
        assert new_portfolio.get_total_realized_pnl() == FixedPoint("3000.00")


class TestPortfolioTotalValue:
    """Test total portfolio value calculation."""

    def test_total_value(self, portfolio: Portfolio, account: Account, market: Market):
        mark_prices = {
            "BTC": FixedPoint("50000.00"),
            "USD": FixedPoint("1.0"),
        }
        value = portfolio.get_total_value(mark_prices)
        # 100000 USD + 10 BTC * 50000 = 600000
        assert value == FixedPoint("600000.00")

    def test_total_value_with_position(
        self, portfolio: Portfolio, account: Account, market: Market
    ):
        from quantro.models import PositionSide

        initial_pos = Position(
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=PositionSide.LONG,
            size=FixedPoint("1.0"),
            entry_price=FixedPoint("49000.00"),
            mark_price=FixedPoint("50000.00"),
        )
        port_with_pos = Portfolio(account, initial_positions={market.symbol: initial_pos})

        mark_prices = {
            "BTC": FixedPoint("50000.00"),
            "USD": FixedPoint("1.0"),
            market.symbol: FixedPoint("50000.00"),
        }
        value = port_with_pos.get_total_value(mark_prices)
        # 100000 USD + 10 BTC * 50000 + 1.0 position * 50000 = 650000
        assert value == FixedPoint("650000.00")


class TestPortfolioDepositWithdraw:
    """Test deposit and withdrawal."""

    def test_deposit(self, portfolio: Portfolio):
        new_portfolio = portfolio.deposit("USD", FixedPoint("50000.00"))
        usd = new_portfolio.get_balance("USD")
        assert usd.free == FixedPoint("150000.00")

    def test_withdraw(self, portfolio: Portfolio):
        new_portfolio = portfolio.withdraw("USD", FixedPoint("50000.00"))
        usd = new_portfolio.get_balance("USD")
        assert usd.free == FixedPoint("50000.00")

    def test_withdraw_insufficient(self, portfolio: Portfolio):
        with pytest.raises(ValueError):
            portfolio.withdraw("USD", FixedPoint("200000.00"))

    def test_withdraw_negative(self, portfolio: Portfolio):
        with pytest.raises(ValueError):
            portfolio.withdraw("USD", FixedPoint("-100.00"))


class TestPortfolioSnapshot:
    """Test portfolio snapshots."""

    def test_snapshot(self, portfolio: Portfolio, market: Market):
        mark_prices = {"BTC": FixedPoint("50000.00"), "USD": FixedPoint("1.0")}
        snap = portfolio.snapshot(mark_prices)

        assert snap.account_id == portfolio.account_id
        assert snap.total_value == FixedPoint("600000.00")
        assert snap.open_orders_count == 0
        assert snap.trade_count == 0


class TestPortfolioManager:
    """Test portfolio manager."""

    def test_create_and_get_portfolio(self, account: Account):
        manager = PortfolioManager()
        portfolio = manager.create_portfolio(account)
        assert manager.get_portfolio(account.id) == portfolio

    def test_get_or_create(self, account: Account):
        manager = PortfolioManager()
        p1 = manager.get_or_create_portfolio(account)
        p2 = manager.get_or_create_portfolio(account)
        assert p1 is p2

    def test_remove_portfolio(self, account: Account):
        manager = PortfolioManager()
        manager.create_portfolio(account)
        assert manager.remove_portfolio(account.id)
        assert manager.get_portfolio(account.id) is None

    def test_all_portfolios(self, account: Account):
        manager = PortfolioManager()
        manager.create_portfolio(account)

        account2 = Account(name="test2")
        manager.create_portfolio(account2)

        portfolios = manager.all_portfolios()
        assert len(portfolios) == 2

    def test_total_equity(self, account: Account, market: Market):
        manager = PortfolioManager()
        manager.create_portfolio(account)

        mark_prices = {"BTC": FixedPoint("50000.00"), "USD": FixedPoint("1.0")}
        equity = manager.total_equity(mark_prices)
        assert equity == FixedPoint("600000.00")


class TestPortfolioTradeHistory:
    """Test trade history queries."""

    def test_get_trades_for_order(self, portfolio: Portfolio, account: Account, market: Market):
        order_id = uuid4()

        trade1 = Trade(
            order_id=order_id,
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=OrderSide.BUY,
            quantity=FixedPoint("0.5"),
            price=FixedPoint("50000.00"),
            fee=FixedPoint("25.00"),
            fee_asset="USD",
        )
        trade2 = Trade(
            order_id=order_id,
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=OrderSide.BUY,
            quantity=FixedPoint("0.5"),
            price=FixedPoint("50100.00"),
            fee=FixedPoint("25.05"),
            fee_asset="USD",
        )

        mark_prices = {market.symbol: FixedPoint("50000.00")}
        p1 = portfolio.apply_trade(trade1, mark_prices)
        p2 = p1.apply_trade(trade2, mark_prices)

        trades = p2.get_trades_for_order(order_id)
        assert len(trades) == 2

    def test_get_trades_for_symbol(self, portfolio: Portfolio, account: Account, market: Market):
        trade1 = Trade(
            order_id=uuid4(),
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=OrderSide.BUY,
            quantity=FixedPoint("1.0"),
            price=FixedPoint("50000.00"),
            fee=FixedPoint("50.00"),
            fee_asset="USD",
        )

        mark_prices = {market.symbol: FixedPoint("50000.00")}
        p1 = portfolio.apply_trade(trade1, mark_prices)

        trades = p1.get_trades_for_symbol(market.symbol)
        assert len(trades) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
