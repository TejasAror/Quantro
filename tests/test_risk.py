"""Tests for risk engine."""

import pytest

from quantro.fixedpoint import FixedPoint
from quantro.models import (
    Account,
    Market,
    Order,
    OrderSide,
    OrderType,
    TimeInForce,
)
from quantro.risk import RiskCheckResult, RiskEngine, create_default_risk_engine


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
    # Add some balances
    from quantro.models import Balance

    acc = acc.set_balance(Balance(asset="USD", free=FixedPoint("1000000"), locked=FixedPoint("0")))
    acc = acc.set_balance(Balance(asset="BTC", free=FixedPoint("100"), locked=FixedPoint("0")))
    return acc


@pytest.fixture
def risk_engine() -> RiskEngine:
    return RiskEngine(
        max_position_size=FixedPoint("100"),
        max_order_size=FixedPoint("10"),
        max_exposure=FixedPoint("50000"),
        max_leverage=FixedPoint("5"),
    )


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


class TestRiskEngineOrderSize:
    """Test order size limit checks."""

    def test_order_size_within_limits(
        self, risk_engine: RiskEngine, account: Account, market: Market
    ):
        order = create_buy_order(account, market, "1.0", "50000.00")
        report = risk_engine.check_order(order, account, market)
        assert report.passed

    def test_order_exceeds_max_order_size(
        self, risk_engine: RiskEngine, account: Account, market: Market
    ):
        order = create_buy_order(account, market, "20.0", "50000.00")  # Max is 10
        report = risk_engine.check_order(order, account, market)
        assert not report.passed
        failed = report.failed_checks
        assert any(c.name == "max_order_size" for c in failed)

    def test_order_below_min_order_size(
        self, risk_engine: RiskEngine, account: Account, market: Market
    ):
        order = create_buy_order(account, market, "0.00001", "50000.00")  # Min is 0.0001
        report = risk_engine.check_order(order, account, market)
        assert not report.passed
        failed = report.failed_checks
        assert any(c.name == "min_order_size" for c in failed)

    def test_order_size_warning_at_80_percent(
        self, risk_engine: RiskEngine, account: Account, market: Market
    ):
        order = create_buy_order(account, market, "9.0", "50000.00")  # 90% of max 10
        report = risk_engine.check_order(order, account, market)
        # Warning doesn't fail - should have no failed checks
        assert not any(c.result == RiskCheckResult.FAILED for c in report.checks)
        warnings = report.warnings
        assert any(c.name == "max_order_size_warning" for c in warnings)


class TestRiskEngineAvailableBalance:
    """Test available balance checks."""

    def test_buy_order_sufficient_quote_balance(
        self, risk_engine: RiskEngine, account: Account, market: Market
    ):
        # 1 BTC @ 50000 = 50000 USD + 0.1% fee = 50050 USD
        # Account has 100000 USD free
        order = create_buy_order(account, market, "1.0", "50000.00")
        report = risk_engine.check_order(order, account, market)
        assert report.passed

    def test_buy_order_insufficient_quote_balance(
        self, risk_engine: RiskEngine, account: Account, market: Market
    ):
        # Need 50050 USD but only have 10000
        poor_account = Account(name="poor")
        from quantro.models import Balance

        poor_account = poor_account.set_balance(
            Balance(asset="USD", free=FixedPoint("10000"), locked=FixedPoint("0"))
        )

        order = create_buy_order(poor_account, market, "1.0", "50000.00")
        report = risk_engine.check_order(order, poor_account, market)
        assert not report.passed
        failed = report.failed_checks
        assert any(c.name == "available_balance" for c in failed)

    def test_sell_order_sufficient_base_balance(
        self, risk_engine: RiskEngine, account: Account, market: Market
    ):
        # Account has 10 BTC free
        order = create_sell_order(account, market, "1.0", "50000.00")
        report = risk_engine.check_order(order, account, market)
        assert report.passed

    def test_sell_order_insufficient_base_balance(
        self, risk_engine: RiskEngine, account: Account, market: Market
    ):
        poor_account = Account(name="poor")
        from quantro.models import Balance

        poor_account = poor_account.set_balance(
            Balance(asset="BTC", free=FixedPoint("0.5"), locked=FixedPoint("0"))
        )

        order = create_sell_order(poor_account, market, "1.0", "50000.00")
        report = risk_engine.check_order(order, poor_account, market)
        assert not report.passed


class TestRiskEnginePositionLimits:
    """Test position size limits."""

    def test_new_position_within_limit(
        self, risk_engine: RiskEngine, account: Account, market: Market
    ):
        # Use quantity within max_order_size (10) but test position limit logic
        order = create_buy_order(account, market, "5.0", "50000.00")
        report = risk_engine.check_order(order, account, market)
        assert report.passed

    def test_new_position_exceeds_limit(
        self, risk_engine: RiskEngine, account: Account, market: Market
    ):
        # Max position is 100. Opening 150 also exceeds max_order_size, but the
        # position check should still catch it.
        order = create_buy_order(account, market, "150.0", "50000.00")
        report = risk_engine.check_order(order, account, market)
        assert not report.passed
        # Should fail on max_position_size or max_order_size

    def test_adding_to_existing_position(
        self, risk_engine: RiskEngine, account: Account, market: Market
    ):
        from quantro.models import Position, PositionSide

        position = Position(
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=PositionSide.LONG,
            size=FixedPoint("60.0"),
            entry_price=FixedPoint("49000.00"),
            mark_price=FixedPoint("50000.00"),
        )
        positions = {market.symbol: position}

        # Adding 5 more would make 65 < 100 limit, but order size is 5
        order = create_buy_order(account, market, "5.0", "50000.00")
        report = risk_engine.check_order(order, account, market, positions=positions)
        assert report.passed

        # Test exceeding position limit
        position2 = Position(
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=PositionSide.LONG,
            size=FixedPoint("95.0"),
            entry_price=FixedPoint("49000.00"),
            mark_price=FixedPoint("50000.00"),
        )
        positions2 = {market.symbol: position2}
        order2 = create_buy_order(account, market, "10.0", "50000.00")  # Would make 105 > 100
        report2 = risk_engine.check_order(order2, account, market, positions=positions2)
        assert not report2.passed

    def test_reducing_position_allowed(
        self, risk_engine: RiskEngine, account: Account, market: Market
    ):
        from quantro.models import Position, PositionSide

        position = Position(
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=PositionSide.LONG,
            size=FixedPoint("10.0"),
            entry_price=FixedPoint("49000.00"),
            mark_price=FixedPoint("50000.00"),
        )
        positions = {market.symbol: position}

        # Selling 5 reduces to 5, within limit
        order = create_sell_order(account, market, "5.0", "50000.00")
        report = risk_engine.check_order(order, account, market, positions=positions)
        assert report.passed


class TestRiskEngineExposureLimits:
    """Test portfolio exposure limits."""

    def test_exposure_within_limit(self, risk_engine: RiskEngine, account: Account, market: Market):
        from quantro.models import Position, PositionSide

        position = Position(
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=PositionSide.LONG,
            size=FixedPoint("1.0"),
            entry_price=FixedPoint("49000.00"),
            mark_price=FixedPoint("50000.00"),
        )
        positions = {market.symbol: position}
        mark_prices = {market.symbol: FixedPoint("50000.00")}

        # Current exposure: 1 * 50000 = 50000
        # New order: 0.5 * 50000 = 25000
        # Total: 75000 > 50000 limit
        order = create_buy_order(account, market, "0.5", "50000.00")
        report = risk_engine.check_order(
            order, account, market, positions=positions, mark_prices=mark_prices
        )
        assert not report.passed

    def test_exposure_multiple_positions(
        self, risk_engine: RiskEngine, account: Account, market: Market
    ):
        from quantro.models import Position, PositionSide

        pos1 = Position(
            account_id=account.id,
            market=market,
            symbol="ETH-USD",
            side=PositionSide.LONG,
            size=FixedPoint("100.0"),
            entry_price=FixedPoint("3000.00"),
            mark_price=FixedPoint("3000.00"),
        )
        pos2 = Position(
            account_id=account.id,
            market=market,
            symbol="BTC-USD",
            side=PositionSide.LONG,
            size=FixedPoint("1.0"),
            entry_price=FixedPoint("49000.00"),
            mark_price=FixedPoint("50000.00"),
        )
        positions = {"ETH-USD": pos1, "BTC-USD": pos2}
        mark_prices = {"ETH-USD": FixedPoint("3000.00"), "BTC-USD": FixedPoint("50000.00")}

        # ETH exposure: 100 * 3000 = 300000
        # BTC exposure: 1 * 50000 = 50000
        # Total: 350000 > 50000 limit
        order = create_buy_order(account, market, "0.1", "50000.00")
        report = risk_engine.check_order(
            order, account, market, positions=positions, mark_prices=mark_prices
        )
        assert not report.passed


class TestRiskEngineLeverageLimits:
    """Test leverage limits."""

    def test_leverage_within_limit(self, risk_engine: RiskEngine, account: Account, market: Market):
        from quantro.models import Position, PositionSide

        position = Position(
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=PositionSide.LONG,
            size=FixedPoint("1.0"),
            entry_price=FixedPoint("49000.00"),
            mark_price=FixedPoint("50000.00"),
            leverage=FixedPoint("2"),
        )
        positions = {market.symbol: position}
        # Use asset symbols as keys for mark_prices
        mark_prices = {"BTC": FixedPoint("50000.00"), "USD": FixedPoint("1.0")}

        # Equity: 1000000 USD + 100 BTC * 50000 = 6000000
        # Leveraged exposure: 1 * 50000 * 2 = 100000
        # Leverage: 100000 / 6000000 = 0.0167 < 5
        order = create_buy_order(account, market, "1.0", "50000.00")
        report = risk_engine.check_order(
            order, account, market, positions=positions, mark_prices=mark_prices
        )
        assert report.passed


class TestRiskEngineMarketLimits:
    """Test market-specific limits."""

    def test_inactive_market_rejected(self, risk_engine: RiskEngine, account: Account):
        inactive_market = Market(
            symbol="BTC-USD",
            base_asset="BTC",
            quote_asset="USD",
            venue="TEST",
            is_active=False,
        )
        order = create_buy_order(account, inactive_market, "1.0", "50000.00")
        report = risk_engine.check_order(order, account, inactive_market)
        assert not report.passed

    def test_invalid_price_precision(
        self, risk_engine: RiskEngine, account: Account, market: Market
    ):
        # Price 50000.005 doesn't match tick size 0.01
        order = Order(
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=FixedPoint("1.0"),
            price=FixedPoint("50000.005"),
        )
        report = risk_engine.check_order(order, account, market)
        assert not report.passed

    def test_invalid_quantity_precision(
        self, risk_engine: RiskEngine, account: Account, market: Market
    ):
        # Quantity 1.00001 doesn't match lot size 0.0001
        order = Order(
            account_id=account.id,
            market=market,
            symbol=market.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=FixedPoint("1.00001"),
            price=FixedPoint("50000.00"),
        )
        report = risk_engine.check_order(order, account, market)
        assert not report.passed


class TestRiskEngineMaxOpenOrders:
    """Test max open orders limit."""

    def test_within_limit(self, risk_engine: RiskEngine, account: Account, market: Market):
        order = create_buy_order(account, market, "1.0", "50000.00")
        report = risk_engine.check_order(order, account, market, open_orders_count=100)
        assert report.passed

    def test_at_limit(self, risk_engine: RiskEngine, account: Account, market: Market):
        order = create_buy_order(account, market, "1.0", "50000.00")
        report = risk_engine.check_order(order, account, market, open_orders_count=1000)
        assert not report.passed


class TestRiskEngineOrderValidity:
    """Test basic order validity."""

    def test_zero_quantity_rejected(
        self, risk_engine: RiskEngine, account: Account, market: Market
    ):
        # Order construction validates quantity > 0
        with pytest.raises(ValueError, match="Quantity must be positive"):
            Order(
                account_id=account.id,
                market=market,
                symbol=market.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=FixedPoint("0"),
                price=FixedPoint("50000.00"),
            )

    def test_zero_price_for_limit_rejected(
        self, risk_engine: RiskEngine, account: Account, market: Market
    ):
        # Order construction validates price > 0 for limit orders
        with pytest.raises(ValueError, match="Limit price must be positive"):
            Order(
                account_id=account.id,
                market=market,
                symbol=market.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=FixedPoint("1.0"),
                price=FixedPoint("0"),
            )


class TestDefaultRiskEngine:
    """Test default risk engine factory."""

    def test_create_default(self):
        engine = create_default_risk_engine()
        assert engine.max_position_size == FixedPoint("1000000")
        assert engine.max_order_size == FixedPoint("100000")
        assert engine.max_exposure == FixedPoint("10000000")
        assert engine.max_leverage == FixedPoint("10")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
