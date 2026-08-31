"""Phase 1 integration verification for Quantro trading engine.

Tests the complete lifecycle:
account creation → deterministic test deposit → BUY limit order → risk validation
→ fund locking → order-book insertion → compatible SELL order → risk validation
→ price-time-priority matching → trade/fill creation → balance updates
→ position updates → realized/unrealized P&L → trade history
→ final order statuses → final order-book state.
"""

from uuid import NAMESPACE_DNS, UUID, uuid5

import pytest

from quantro import (
    Account,
    FixedPoint,
    Market,
    Order,
    OrderBookSide,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    RiskEngine,
    TimeInForce,
    TradingEngine,
    create_engine,
)

# ============================================================================
# FIXTURES - Deterministic test setup
# ============================================================================


@pytest.fixture
def btc_usd_market() -> Market:
    """BTC-USD market with deterministic precision and fees."""
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
        maker_fee=FixedPoint("0.001"),  # 0.1%
        taker_fee=FixedPoint("0.001"),  # 0.1%
    )


@pytest.fixture
def risk_engine() -> RiskEngine:
    """Risk engine configured for integration test limits."""
    return RiskEngine(
        max_position_size=FixedPoint("100"),  # 100 BTC max position
        max_order_size=FixedPoint("10"),  # 10 BTC max order
        max_exposure=FixedPoint("10000000"),  # 10M USD max exposure
        max_leverage=FixedPoint("5"),
        allow_short=True,
        max_open_orders=1000,
    )


@pytest.fixture
def engine_with_accounts(
    risk_engine: RiskEngine, btc_usd_market: Market
) -> tuple[TradingEngine, Account, Account]:
    """Trading engine with BTC-USD market and both buyer/seller accounts created on same state."""
    eng = create_engine(risk_engine=risk_engine)
    eng = eng.add_market(btc_usd_market)

    # Create buyer account on this engine
    eng, buyer = eng.create_account(
        "buyer",
        {
            "USD": FixedPoint("100000"),
            "BTC": FixedPoint("0"),
        },
    )

    # Create seller account on the SAME engine state (returned from buyer creation)
    eng, seller = eng.create_account(
        "seller",
        {
            "USD": FixedPoint("0"),
            "BTC": FixedPoint("10"),
        },
    )

    return eng, buyer, seller


@pytest.fixture
def buyer_account(engine_with_accounts: tuple[TradingEngine, Account, Account]) -> Account:
    """Buyer account with 100,000 USD, 0 BTC."""
    return engine_with_accounts[1]


@pytest.fixture
def seller_account(engine_with_accounts: tuple[TradingEngine, Account, Account]) -> Account:
    """Seller account with 10 BTC, 0 USD."""
    return engine_with_accounts[2]


@pytest.fixture
def engine(engine_with_accounts: tuple[TradingEngine, Account, Account]) -> TradingEngine:
    """Trading engine with both accounts."""
    return engine_with_accounts[0]


@pytest.fixture
def test_price() -> FixedPoint:
    """Deterministic test price: 50,000.00 USD per BTC."""
    return FixedPoint("50000.00")


@pytest.fixture
def test_quantity() -> FixedPoint:
    """Deterministic test quantity: 1.0 BTC."""
    return FixedPoint("1.0")


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


def create_buy_limit_order(
    account: Account,
    market: Market,
    quantity: FixedPoint,
    price: FixedPoint,
) -> Order:
    """Create a BUY limit order."""
    return Order(
        account_id=account.id,
        market=market,
        symbol=market.symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        price=price,
        time_in_force=TimeInForce.GTC,
    )


def create_sell_limit_order(
    account: Account,
    market: Market,
    quantity: FixedPoint,
    price: FixedPoint,
) -> Order:
    """Create a SELL limit order."""
    return Order(
        account_id=account.id,
        market=market,
        symbol=market.symbol,
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        price=price,
        time_in_force=TimeInForce.GTC,
    )


def get_portfolio(engine: TradingEngine, account_id: UUID):
    """Get portfolio for account."""
    return engine.get_portfolio(account_id)


def get_orderbook(engine: TradingEngine, symbol: str):
    """Get order book for symbol."""
    return engine.get_order_book(symbol)


# ============================================================================
# INTEGRATION TEST CLASS - Complete lifecycle
# ============================================================================


class TestCompleteTradingLifecycle:
    """Test the complete trading lifecycle from account creation to final state."""

    def test_account_creation_and_deposit(
        self,
        engine: TradingEngine,
        buyer_account: Account,
        seller_account: Account,
    ):
        """Verify accounts created with correct initial balances."""
        buyer_portfolio = engine.get_portfolio(buyer_account.id)
        seller_portfolio = engine.get_portfolio(seller_account.id)

        # Buyer: 100,000 USD free, 0 BTC
        assert buyer_portfolio.get_balance("USD").free == FixedPoint("100000")
        assert buyer_portfolio.get_balance("USD").locked == FixedPoint("0")
        assert buyer_portfolio.get_balance("BTC").free == FixedPoint("0")
        assert buyer_portfolio.get_balance("BTC").locked == FixedPoint("0")

        # Seller: 10 BTC free, 0 USD
        assert seller_portfolio.get_balance("BTC").free == FixedPoint("10")
        assert seller_portfolio.get_balance("BTC").locked == FixedPoint("0")
        assert seller_portfolio.get_balance("USD").free == FixedPoint("0")
        assert seller_portfolio.get_balance("USD").locked == FixedPoint("0")

    def test_buy_order_submission_risk_validation_and_locking(
        self,
        engine: TradingEngine,
        buyer_account: Account,
        btc_usd_market: Market,
        test_quantity: FixedPoint,
        test_price: FixedPoint,
    ):
        """Submit BUY limit order → verify risk passed, funds locked, order on book."""
        buyer = buyer_account

        # Create and submit BUY limit order: 1.0 BTC @ 50,000.00
        buy_order = create_buy_limit_order(buyer, btc_usd_market, test_quantity, test_price)
        result = engine.submit_order(buy_order)

        # Order accepted
        assert result.accepted is True
        assert result.reject_reason is None

        # Risk validation passed
        assert result.risk_report.passed is True
        assert len(result.risk_report.failed_checks) == 0

        # Order status is OPEN (resting on book)
        assert result.order.status == OrderStatus.OPEN
        assert result.order.filled_quantity == FixedPoint("0")
        assert result.order.remaining_quantity == test_quantity

        # No trades yet (limit order resting)
        assert len(result.trades) == 0

        # USD funds locked: quantity * price + taker_fee
        # 1.0 * 50000.00 = 50000.00 + 0.1% fee = 50050.00
        fee_mult = FixedPoint("1") + btc_usd_market.taker_fee
        expected_locked_usd = test_quantity * test_price * fee_mult
        buyer_portfolio = engine.get_portfolio(buyer.id)
        usd_balance = buyer_portfolio.get_balance("USD")

        assert usd_balance.free == FixedPoint("100000") - expected_locked_usd
        assert usd_balance.locked == expected_locked_usd
        assert usd_balance.total == FixedPoint("100000")

        # BTC balance unchanged (no fills)
        btc_balance = buyer_portfolio.get_balance("BTC")
        assert btc_balance.free == FixedPoint("0")
        assert btc_balance.locked == FixedPoint("0")

        # Order resting on book at bid side
        orderbook = engine.get_order_book("BTC-USD")
        assert orderbook.best_bid == test_price
        bid_level = orderbook.get_price_level(OrderBookSide.BID, test_price)
        assert bid_level is not None
        assert bid_level.total_quantity == test_quantity
        assert len(bid_level.orders) == 1
        assert bid_level.orders[0].id == result.order.id

    def test_cancel_open_buy_order_unlocks_funds_and_removes_order(
        self,
        engine: TradingEngine,
        buyer_account: Account,
        btc_usd_market: Market,
        test_quantity: FixedPoint,
        test_price: FixedPoint,
    ):
        """Cancel a resting BUY through the engine and verify accounting state."""
        buy_order = create_buy_limit_order(
            buyer_account,
            btc_usd_market,
            test_quantity,
            test_price,
        )
        buy_result = engine.submit_order(buy_order)

        fee_mult = FixedPoint("1") + btc_usd_market.taker_fee
        expected_locked_usd = test_quantity * test_price * fee_mult

        assert buy_result.accepted is True
        assert buy_result.order.status == OrderStatus.OPEN
        assert len(buy_result.trades) == 0

        buyer_portfolio_before_cancel = engine.get_portfolio(buyer_account.id)
        usd_before_cancel = buyer_portfolio_before_cancel.get_balance("USD")
        assert usd_before_cancel.free == FixedPoint("100000") - expected_locked_usd
        assert usd_before_cancel.locked == expected_locked_usd
        assert usd_before_cancel.total == FixedPoint("100000")
        assert buyer_portfolio_before_cancel.open_orders_count == 1

        cancelled_order = engine.cancel_order(buy_result.order.id, btc_usd_market.symbol)

        assert cancelled_order is not None
        assert cancelled_order.id == buy_result.order.id
        assert cancelled_order.status == OrderStatus.CANCELLED
        assert cancelled_order.filled_quantity == FixedPoint("0")
        assert cancelled_order.remaining_quantity == test_quantity

        orderbook = engine.get_order_book(btc_usd_market.symbol)
        assert orderbook.get_order(buy_result.order.id) is None
        assert orderbook.best_bid is None
        assert orderbook.best_ask is None
        assert len(orderbook) == 0

        buyer_portfolio = engine.get_portfolio(buyer_account.id)
        buyer_usd = buyer_portfolio.get_balance("USD")
        buyer_btc = buyer_portfolio.get_balance("BTC")

        assert buyer_usd.free == FixedPoint("100000")
        assert buyer_usd.locked == FixedPoint("0")
        assert buyer_usd.total == FixedPoint("100000")
        assert buyer_btc.free == FixedPoint("0")
        assert buyer_btc.locked == FixedPoint("0")
        assert buyer_portfolio.open_orders_count == 0
        assert buyer_portfolio.trade_history == []
        assert buyer_portfolio.get_position(btc_usd_market.symbol) is None

    def test_buy_order_rejected_when_required_funds_exceed_balance(
        self,
        risk_engine: RiskEngine,
        btc_usd_market: Market,
        test_quantity: FixedPoint,
        test_price: FixedPoint,
    ):
        """Reject an underfunded BUY before locking funds or entering the book."""
        eng = create_engine(risk_engine=risk_engine).add_market(btc_usd_market)
        limited_usd = FixedPoint("50000")
        eng, limited_buyer = eng.create_account(
            "limited_buyer",
            {
                "USD": limited_usd,
                "BTC": FixedPoint("0"),
            },
        )

        fee_mult = FixedPoint("1") + btc_usd_market.taker_fee
        required_usd = test_quantity * test_price * fee_mult
        assert required_usd > limited_usd

        buy_order = create_buy_limit_order(
            limited_buyer,
            btc_usd_market,
            test_quantity,
            test_price,
        )
        result = eng.submit_order(buy_order)

        assert result.accepted is False
        assert result.reject_reason == "Risk check failed"
        assert result.order.status == OrderStatus.REJECTED
        assert result.order.filled_quantity == FixedPoint("0")
        assert result.order.remaining_quantity == test_quantity
        assert len(result.trades) == 0

        assert result.risk_report.passed is False
        failed_checks = result.risk_report.failed_checks
        assert len(failed_checks) == 1
        failed_check = failed_checks[0]
        assert failed_check.name == "available_balance"
        assert "Insufficient USD balance" in failed_check.message
        assert failed_check.limit == limited_usd
        assert failed_check.current == required_usd

        orderbook = eng.get_order_book(btc_usd_market.symbol)
        assert orderbook.get_order(buy_order.id) is None
        assert orderbook.best_bid is None
        assert orderbook.best_ask is None
        assert len(orderbook) == 0

        buyer_portfolio = eng.get_portfolio(limited_buyer.id)
        buyer_usd = buyer_portfolio.get_balance("USD")
        buyer_btc = buyer_portfolio.get_balance("BTC")

        assert buyer_usd.free == limited_usd
        assert buyer_usd.locked == FixedPoint("0")
        assert buyer_usd.total == limited_usd
        assert buyer_btc.free == FixedPoint("0")
        assert buyer_btc.locked == FixedPoint("0")
        assert buyer_portfolio.open_orders_count == 0
        assert buyer_portfolio.trade_history == []
        assert buyer_portfolio.get_position(btc_usd_market.symbol) is None
        assert buyer_portfolio.get_total_unrealized_pnl() == FixedPoint("0")
        assert buyer_portfolio.get_total_realized_pnl() == FixedPoint("0")

    def test_order_rejected_when_quantity_exceeds_max_order_size(
        self,
        test_price: FixedPoint,
    ):
        """Reject an oversized order before locking funds or entering the book."""
        max_order_size = FixedPoint("2.0")
        oversized_quantity = FixedPoint("2.5")
        market = Market(
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
        risk_engine = RiskEngine(
            max_position_size=FixedPoint("100"),
            max_order_size=max_order_size,
            max_exposure=FixedPoint("10000000"),
            max_leverage=FixedPoint("5"),
            allow_short=True,
            max_open_orders=1000,
        )
        eng = create_engine(risk_engine=risk_engine).add_market(market)
        initial_usd = FixedPoint("1000000")
        eng, buyer = eng.create_account(
            "oversized_buyer",
            {
                "USD": initial_usd,
                "BTC": FixedPoint("0"),
            },
        )

        buy_order = create_buy_limit_order(
            buyer,
            market,
            oversized_quantity,
            test_price,
        )
        result = eng.submit_order(buy_order)

        assert result.accepted is False
        assert result.reject_reason == "Risk check failed"
        assert result.order.status == OrderStatus.REJECTED
        assert result.order.filled_quantity == FixedPoint("0")
        assert result.order.remaining_quantity == oversized_quantity
        assert len(result.trades) == 0

        assert result.risk_report.passed is False
        failed_checks = result.risk_report.failed_checks
        assert len(failed_checks) == 1
        failed_check = failed_checks[0]
        assert failed_check.name == "max_order_size"
        assert "exceeds max" in failed_check.message
        assert failed_check.limit == max_order_size
        assert failed_check.current == oversized_quantity
        assert "Order quantity 2.5 exceeds max 2" in result.order.metadata["reject_reason"]

        orderbook = eng.get_order_book(market.symbol)
        assert orderbook.get_order(buy_order.id) is None
        assert orderbook.best_bid is None
        assert orderbook.best_ask is None
        assert len(orderbook) == 0

        buyer_portfolio = eng.get_portfolio(buyer.id)
        buyer_usd = buyer_portfolio.get_balance("USD")
        buyer_btc = buyer_portfolio.get_balance("BTC")

        assert buyer_usd.free == initial_usd
        assert buyer_usd.locked == FixedPoint("0")
        assert buyer_usd.total == initial_usd
        assert buyer_btc.free == FixedPoint("0")
        assert buyer_btc.locked == FixedPoint("0")
        assert buyer_portfolio.open_orders_count == 0
        assert buyer_portfolio.trade_history == []
        assert buyer_portfolio.get_position(market.symbol) is None
        assert buyer_portfolio.get_total_unrealized_pnl() == FixedPoint("0")
        assert buyer_portfolio.get_total_realized_pnl() == FixedPoint("0")

    def test_identical_trading_sequences_produce_identical_replay_state(self):
        """Run two identical deterministic engine sequences and compare resulting state."""
        market = Market(
            symbol="BTC-USD",
            base_asset="BTC",
            quote_asset="USD",
            venue="TEST",
            price_precision=2,
            quantity_precision=8,
            min_order_size=FixedPoint("0.0001"),
            max_order_size=FixedPoint("10"),
            tick_size=FixedPoint("0.01"),
            lot_size=FixedPoint("0.0001"),
            maker_fee=FixedPoint("0.001"),
            taker_fee=FixedPoint("0.001"),
        )
        mark_prices = {
            "BTC": FixedPoint("50000.00"),
            "USD": FixedPoint("1.0"),
            market.symbol: FixedPoint("50000.00"),
        }
        account_roles: dict[UUID, str] = {}
        order_roles: dict[UUID, str] = {}

        def make_risk_engine() -> RiskEngine:
            return RiskEngine(
                max_position_size=FixedPoint("100"),
                max_order_size=FixedPoint("10"),
                max_exposure=FixedPoint("10000000"),
                max_leverage=FixedPoint("5"),
                allow_short=True,
                max_open_orders=1000,
            )

        def normalize_order(order: Order) -> dict:
            return {
                "account_role": account_roles[order.account_id],
                "order_role": order_roles[order.id],
                "symbol": order.symbol,
                "side": order.side.value,
                "type": order.order_type.value,
                "status": order.status.value,
                "time_in_force": order.time_in_force.value,
                "quantity": order.quantity,
                "price": order.price,
                "filled_quantity": order.filled_quantity,
                "remaining_quantity": order.remaining_quantity,
                "average_fill_price": order.average_fill_price,
                "total_fees": order.total_fees,
            }

        def normalize_trade(trade) -> dict:
            return {
                "account_role": account_roles[trade.account_id],
                "order_role": order_roles[trade.order_id],
                "symbol": trade.symbol,
                "side": trade.side.value,
                "quantity": trade.quantity,
                "price": trade.price,
                "notional": trade.notional,
                "fee": trade.fee,
                "fee_asset": trade.fee_asset,
                "is_maker": trade.is_maker,
            }

        def normalize_balance(balance) -> dict:
            return {
                "free": balance.free,
                "locked": balance.locked,
                "total": balance.total,
            }

        def normalize_position(position) -> dict | None:
            if position is None:
                return None
            return {
                "account_role": account_roles[position.account_id],
                "symbol": position.symbol,
                "side": position.side.value,
                "size": position.size,
                "entry_price": position.entry_price,
                "mark_price": position.mark_price,
                "unrealized_pnl": position.unrealized_pnl,
                "realized_pnl": position.realized_pnl,
                "leverage": position.leverage,
                "liquidation_price": position.liquidation_price,
            }

        def normalize_portfolio(portfolio) -> dict:
            balances = {
                asset: normalize_balance(balance)
                for asset, balance in sorted(portfolio.get_balances().items())
            }
            positions = {
                symbol: normalize_position(position)
                for symbol, position in sorted(portfolio.positions.items())
            }
            snapshot = portfolio.snapshot(mark_prices)
            return {
                "balances": balances,
                "positions": positions,
                "unrealized_pnl": portfolio.get_total_unrealized_pnl(),
                "realized_pnl": portfolio.get_total_realized_pnl(),
                "open_orders_count": portfolio.open_orders_count,
                "trade_history": [normalize_trade(trade) for trade in portfolio.trade_history],
                "snapshot": {
                    "total_value": snapshot.total_value,
                    "total_unrealized_pnl": snapshot.total_unrealized_pnl,
                    "total_realized_pnl": snapshot.total_realized_pnl,
                    "open_orders_count": snapshot.open_orders_count,
                    "trade_count": snapshot.trade_count,
                },
            }

        def normalize_price_level(level) -> dict:
            return {
                "price": level.price,
                "total_quantity": level.total_quantity,
                "orders": [normalize_order(order) for order in level.orders],
            }

        def normalize_orderbook(orderbook) -> dict:
            snapshot = orderbook.snapshot()
            return {
                "symbol": snapshot.symbol,
                "bids": [normalize_price_level(level) for level in snapshot.bids],
                "asks": [normalize_price_level(level) for level in snapshot.asks],
                "best_bid": orderbook.best_bid,
                "best_ask": orderbook.best_ask,
                "spread": orderbook.spread,
                "mid_price": orderbook.mid_price,
                "last_trade_price": snapshot.last_trade_price,
                "last_trade_quantity": snapshot.last_trade_quantity,
                "sequence": snapshot.sequence,
                "order_count": len(orderbook),
            }

        def normalize_engine_snapshot(snapshot) -> dict:
            return {
                "sequence": snapshot.sequence,
                "portfolios": {
                    account_roles[account_id]: normalize_portfolio(portfolio)
                    for account_id, portfolio in sorted(
                        snapshot.portfolios.items(),
                        key=lambda item: account_roles[item[0]],
                    )
                },
                "order_books": {
                    symbol: normalize_orderbook(orderbook)
                    for symbol, orderbook in sorted(snapshot.order_books.items())
                },
                "account_balances": {
                    account_roles[account_id]: {
                        asset: normalize_balance(balance)
                        for asset, balance in sorted(balances.items())
                    }
                    for account_id, balances in sorted(
                        snapshot.account_balances.items(),
                        key=lambda item: account_roles[item[0]],
                    )
                },
            }

        def run_sequence() -> dict:
            nonlocal account_roles, order_roles

            eng = create_engine(risk_engine=make_risk_engine()).add_market(market)
            eng, buyer = eng.create_account(
                "buyer",
                {
                    "USD": FixedPoint("100000"),
                    "BTC": FixedPoint("0"),
                },
            )
            eng, seller = eng.create_account(
                "seller",
                {
                    "USD": FixedPoint("0"),
                    "BTC": FixedPoint("10"),
                },
            )

            buy_order = Order(
                id=uuid5(NAMESPACE_DNS, "phase1-replay:buy"),
                account_id=buyer.id,
                market=market,
                symbol=market.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=FixedPoint("1.0"),
                price=FixedPoint("50000.00"),
                time_in_force=TimeInForce.GTC,
            )
            buy_result = eng.submit_order(buy_order)

            sell_order = Order(
                id=uuid5(NAMESPACE_DNS, "phase1-replay:sell"),
                account_id=seller.id,
                market=market,
                symbol=market.symbol,
                side=OrderSide.SELL,
                order_type=OrderType.LIMIT,
                quantity=FixedPoint("1.0"),
                price=FixedPoint("50000.00"),
                time_in_force=TimeInForce.GTC,
            )
            sell_result = eng.submit_order(sell_order)

            account_roles = {
                buyer.id: "buyer",
                seller.id: "seller",
            }
            order_roles = {
                buy_order.id: "buy",
                sell_order.id: "sell",
            }

            buyer_portfolio = eng.get_portfolio(buyer.id)
            seller_portfolio = eng.get_portfolio(seller.id)
            orderbook = eng.get_order_book(market.symbol)
            engine_snapshot = eng.snapshot()

            return {
                "fills": {
                    "buy_result_order": normalize_order(buy_result.order),
                    "sell_result_order": normalize_order(sell_result.order),
                },
                "trades": [normalize_trade(trade) for trade in sell_result.trades],
                "balances": {
                    "buyer": {
                        asset: normalize_balance(balance)
                        for asset, balance in sorted(buyer_portfolio.get_balances().items())
                    },
                    "seller": {
                        asset: normalize_balance(balance)
                        for asset, balance in sorted(seller_portfolio.get_balances().items())
                    },
                },
                "positions": {
                    "buyer": normalize_position(buyer_portfolio.get_position(market.symbol)),
                    "seller": normalize_position(seller_portfolio.get_position(market.symbol)),
                },
                "pnl": {
                    "buyer": {
                        "realized": buyer_portfolio.get_total_realized_pnl(),
                        "unrealized": buyer_portfolio.get_total_unrealized_pnl(),
                    },
                    "seller": {
                        "realized": seller_portfolio.get_total_realized_pnl(),
                        "unrealized": seller_portfolio.get_total_unrealized_pnl(),
                    },
                },
                "trade_history": {
                    "buyer": [normalize_trade(trade) for trade in buyer_portfolio.trade_history],
                    "seller": [normalize_trade(trade) for trade in seller_portfolio.trade_history],
                },
                "order_book_snapshot": normalize_orderbook(orderbook),
                "engine_snapshot": normalize_engine_snapshot(engine_snapshot),
                "replayed_snapshot": normalize_engine_snapshot(eng.replay_events([]).snapshot()),
            }

        run_a = run_sequence()
        run_b = run_sequence()

        assert run_a["fills"] == run_b["fills"]
        assert run_a["trades"] == run_b["trades"]
        assert run_a["balances"] == run_b["balances"]
        assert run_a["positions"] == run_b["positions"]
        assert run_a["pnl"] == run_b["pnl"]
        assert run_a["trade_history"] == run_b["trade_history"]
        assert run_a["order_book_snapshot"] == run_b["order_book_snapshot"]
        assert run_a["engine_snapshot"] == run_b["engine_snapshot"]
        assert run_a["replayed_snapshot"] == run_b["replayed_snapshot"]

    def test_sell_order_submission_risk_validation_and_matching(
        self,
        engine: TradingEngine,
        buyer_account: Account,
        seller_account: Account,
        btc_usd_market: Market,
        test_quantity: FixedPoint,
        test_price: FixedPoint,
    ):
        """Submit SELL limit order → verify risk passed, matching, trades created."""
        # First, submit BUY order to establish resting order on book
        buy_order = create_buy_limit_order(buyer_account, btc_usd_market, test_quantity, test_price)
        buy_result = engine.submit_order(buy_order)

        assert buy_result.accepted is True
        assert buy_result.order.status == OrderStatus.OPEN

        # Now submit SELL order from seller - this should match with BUY
        sell_order = create_sell_limit_order(
            seller_account, btc_usd_market, test_quantity, test_price
        )
        sell_result = engine.submit_order(sell_order)

        # Order accepted
        assert sell_result.accepted is True
        assert sell_result.reject_reason is None

        # Risk validation passed
        assert sell_result.risk_report.passed is True
        assert len(sell_result.risk_report.failed_checks) == 0

        # Order matched and filled
        assert sell_result.order.status == OrderStatus.FILLED
        assert sell_result.order.filled_quantity == test_quantity
        assert sell_result.order.remaining_quantity == FixedPoint("0")

        # Trades created (2 trades: taker + maker)
        assert len(sell_result.trades) == 2

        # Verify trade details
        taker_trade = next(t for t in sell_result.trades if not t.is_maker)
        maker_trade = next(t for t in sell_result.trades if t.is_maker)

        # Taker is the SELL order (crosses spread)
        assert taker_trade.side == OrderSide.SELL
        assert taker_trade.quantity == test_quantity
        assert taker_trade.price == test_price
        assert taker_trade.account_id == seller_account.id

        # Maker is the BUY order (resting)
        assert maker_trade.side == OrderSide.BUY
        assert maker_trade.quantity == test_quantity
        assert maker_trade.price == test_price
        assert maker_trade.account_id == buyer_account.id

        # Fees calculated correctly
        expected_fee = (
            test_quantity * test_price * btc_usd_market.taker_fee
        )  # 1.0 * 50000 * 0.001 = 50.0
        assert taker_trade.fee == expected_fee
        assert maker_trade.fee == expected_fee

        # Verify BUY order also shows FILLED (we need to check its state via portfolio)
        buyer_portfolio = engine.get_portfolio(buyer_account.id)
        buy_trades = buyer_portfolio.get_trades_for_order(buy_result.order.id)
        assert len(buy_trades) == 1
        assert buy_trades[0].quantity == test_quantity
        assert buy_trades[0].price == test_price

        # Order book should be empty after full match
        orderbook = engine.get_order_book("BTC-USD")
        assert orderbook.best_bid is None
        assert orderbook.best_ask is None
        assert len(orderbook) == 0

    def test_partial_fill_resting_buy_keeps_remaining_locked_and_active(
        self,
        engine: TradingEngine,
        buyer_account: Account,
        seller_account: Account,
        btc_usd_market: Market,
        test_price: FixedPoint,
    ):
        """Verify partial maker BUY fill accounting and remaining book state."""
        buy_quantity = FixedPoint("1.5")
        sell_quantity = FixedPoint("0.5")
        remaining_buy_quantity = buy_quantity - sell_quantity
        fee_mult = FixedPoint("1") + btc_usd_market.taker_fee
        expected_initial_locked = buy_quantity * test_price * fee_mult
        expected_fill_fee = sell_quantity * test_price * btc_usd_market.taker_fee
        expected_fill_value_with_fee = sell_quantity * test_price * fee_mult
        expected_remaining_locked = remaining_buy_quantity * test_price * fee_mult

        buy_order = create_buy_limit_order(
            buyer_account,
            btc_usd_market,
            buy_quantity,
            test_price,
        )
        buy_result = engine.submit_order(buy_order)

        assert buy_result.accepted is True
        assert buy_result.order.status == OrderStatus.OPEN
        buyer_portfolio_after_buy = engine.get_portfolio(buyer_account.id)
        assert buyer_portfolio_after_buy.get_balance("USD").locked == expected_initial_locked
        assert buyer_portfolio_after_buy.open_orders_count == 1

        sell_order = create_sell_limit_order(
            seller_account,
            btc_usd_market,
            sell_quantity,
            test_price,
        )
        sell_result = engine.submit_order(sell_order)

        assert sell_result.accepted is True
        assert sell_result.order.status == OrderStatus.FILLED
        assert sell_result.order.filled_quantity == sell_quantity
        assert sell_result.order.remaining_quantity == FixedPoint("0")
        assert len(sell_result.trades) == 2

        taker_trade = next(t for t in sell_result.trades if not t.is_maker)
        maker_trade = next(t for t in sell_result.trades if t.is_maker)

        assert taker_trade.side == OrderSide.SELL
        assert taker_trade.account_id == seller_account.id
        assert taker_trade.quantity == sell_quantity
        assert taker_trade.price == test_price

        assert maker_trade.side == OrderSide.BUY
        assert maker_trade.account_id == buyer_account.id
        assert maker_trade.order_id == buy_result.order.id
        assert maker_trade.quantity == sell_quantity
        assert maker_trade.price == test_price

        orderbook = engine.get_order_book("BTC-USD")
        remaining_buy_order = orderbook.get_order(buy_result.order.id)
        assert remaining_buy_order is not None
        assert remaining_buy_order.status == OrderStatus.PARTIALLY_FILLED
        assert remaining_buy_order.filled_quantity == sell_quantity
        assert remaining_buy_order.remaining_quantity == remaining_buy_quantity

        bid_level = orderbook.get_price_level(OrderBookSide.BID, test_price)
        assert bid_level is not None
        assert bid_level.total_quantity == remaining_buy_quantity
        assert len(bid_level.orders) == 1
        assert bid_level.orders[0].id == buy_result.order.id
        assert orderbook.best_bid == test_price
        assert orderbook.best_ask is None

        buyer_portfolio = engine.get_portfolio(buyer_account.id)
        buyer_usd = buyer_portfolio.get_balance("USD")
        buyer_btc = buyer_portfolio.get_balance("BTC")

        assert buyer_usd.free == FixedPoint("100000") - expected_initial_locked
        assert buyer_usd.locked == expected_remaining_locked
        assert buyer_usd.total == FixedPoint("100000") - expected_fill_value_with_fee
        assert expected_initial_locked - buyer_usd.locked == expected_fill_value_with_fee
        assert buyer_btc.free == sell_quantity
        assert buyer_btc.locked == FixedPoint("0")
        assert buyer_portfolio.open_orders_count == 1

        buyer_position = buyer_portfolio.get_position(btc_usd_market.symbol)
        assert buyer_position is not None
        assert buyer_position.side == PositionSide.LONG
        assert buyer_position.size == sell_quantity
        assert buyer_position.entry_price == test_price

        seller_portfolio = engine.get_portfolio(seller_account.id)
        seller_btc = seller_portfolio.get_balance("BTC")
        seller_usd = seller_portfolio.get_balance("USD")

        assert seller_btc.free == FixedPoint("10") - sell_quantity
        assert seller_btc.locked == FixedPoint("0")
        assert seller_usd.free == (sell_quantity * test_price) - expected_fill_fee
        assert seller_usd.locked == FixedPoint("0")
        assert seller_portfolio.open_orders_count == 0

        seller_position = seller_portfolio.get_position(btc_usd_market.symbol)
        assert seller_position is not None
        assert seller_position.side == PositionSide.SHORT
        assert seller_position.size == sell_quantity
        assert seller_position.entry_price == test_price

    def test_balance_updates_after_fill(
        self,
        engine: TradingEngine,
        buyer_account: Account,
        seller_account: Account,
        btc_usd_market: Market,
        test_quantity: FixedPoint,
        test_price: FixedPoint,
    ):
        """Verify buyer USD decreased, BTC increased; seller BTC decreased, USD increased."""
        # Submit both orders
        buy_order = create_buy_limit_order(buyer_account, btc_usd_market, test_quantity, test_price)
        engine.submit_order(buy_order)
        sell_order = create_sell_limit_order(
            seller_account, btc_usd_market, test_quantity, test_price
        )
        engine.submit_order(sell_order)

        buyer_portfolio = engine.get_portfolio(buyer_account.id)
        seller_portfolio = engine.get_portfolio(seller_account.id)

        # Buyer: spent 50000 + 50 fee = 50050 USD, received 1.0 BTC
        # Initial: 100000 USD free, 0 BTC
        # After: 100000 - 50050 = 49950 USD free, 1.0 BTC free
        expected_fee = test_quantity * test_price * btc_usd_market.taker_fee  # 50.0
        expected_usd_spent = test_quantity * test_price + expected_fee  # 50050.0

        assert buyer_portfolio.get_balance("USD").free == FixedPoint("100000") - expected_usd_spent
        assert buyer_portfolio.get_balance("USD").locked == FixedPoint("0")
        assert buyer_portfolio.get_balance("BTC").free == test_quantity
        assert buyer_portfolio.get_balance("BTC").locked == FixedPoint("0")

        # Seller: spent 1.0 BTC, received 50000 - 50 fee = 49950 USD
        # Initial: 10 BTC free, 0 USD
        # After: 9 BTC free, 49950 USD free
        expected_usd_received = test_quantity * test_price - expected_fee  # 49950.0

        assert seller_portfolio.get_balance("BTC").free == FixedPoint("10") - test_quantity
        assert seller_portfolio.get_balance("BTC").locked == FixedPoint("0")
        assert seller_portfolio.get_balance("USD").free == expected_usd_received
        assert seller_portfolio.get_balance("USD").locked == FixedPoint("0")

    def test_position_updates_after_fill(
        self,
        engine: TradingEngine,
        buyer_account: Account,
        seller_account: Account,
        btc_usd_market: Market,
        test_quantity: FixedPoint,
        test_price: FixedPoint,
    ):
        """Verify buyer LONG 1.0 @ 50000, seller SHORT 1.0 @ 50000."""
        # Submit both orders
        buy_order = create_buy_limit_order(buyer_account, btc_usd_market, test_quantity, test_price)
        engine.submit_order(buy_order)
        sell_order = create_sell_limit_order(
            seller_account, btc_usd_market, test_quantity, test_price
        )
        engine.submit_order(sell_order)

        buyer_portfolio = engine.get_portfolio(buyer_account.id)
        seller_portfolio = engine.get_portfolio(seller_account.id)

        # Buyer position: LONG 1.0 @ 50000
        buyer_pos = buyer_portfolio.get_position(btc_usd_market.symbol)
        assert buyer_pos is not None
        assert buyer_pos.side == PositionSide.LONG
        assert buyer_pos.size == test_quantity
        assert buyer_pos.entry_price == test_price
        assert buyer_pos.mark_price == test_price  # mark = last trade price

        # Seller position: SHORT 1.0 @ 50000
        seller_pos = seller_portfolio.get_position(btc_usd_market.symbol)
        assert seller_pos is not None
        assert seller_pos.side == PositionSide.SHORT
        assert seller_pos.size == test_quantity
        assert seller_pos.entry_price == test_price
        assert seller_pos.mark_price == test_price

    def test_realized_unrealized_pnl_after_fill(
        self,
        engine: TradingEngine,
        buyer_account: Account,
        seller_account: Account,
        btc_usd_market: Market,
        test_quantity: FixedPoint,
        test_price: FixedPoint,
    ):
        """Verify unrealized P&L = 0 (mark=entry), realized P&L = 0."""
        # Submit both orders
        buy_order = create_buy_limit_order(buyer_account, btc_usd_market, test_quantity, test_price)
        engine.submit_order(buy_order)
        sell_order = create_sell_limit_order(
            seller_account, btc_usd_market, test_quantity, test_price
        )
        engine.submit_order(sell_order)

        buyer_portfolio = engine.get_portfolio(buyer_account.id)
        seller_portfolio = engine.get_portfolio(seller_account.id)

        # Unrealized P&L should be 0 because mark price = entry price
        assert buyer_portfolio.get_total_unrealized_pnl() == FixedPoint("0")
        assert seller_portfolio.get_total_unrealized_pnl() == FixedPoint("0")

        # Realized P&L should be 0 (no position closed yet)
        assert buyer_portfolio.get_total_realized_pnl() == FixedPoint("0")
        assert seller_portfolio.get_total_realized_pnl() == FixedPoint("0")

    def test_trade_history_recorded(
        self,
        engine: TradingEngine,
        buyer_account: Account,
        seller_account: Account,
        btc_usd_market: Market,
        test_quantity: FixedPoint,
        test_price: FixedPoint,
    ):
        """Verify trade history has 2 trades (maker + taker per fill)."""
        # Submit both orders
        buy_order = create_buy_limit_order(buyer_account, btc_usd_market, test_quantity, test_price)
        buy_result = engine.submit_order(buy_order)
        sell_order = create_sell_limit_order(
            seller_account, btc_usd_market, test_quantity, test_price
        )
        engine.submit_order(sell_order)

        buyer_portfolio = engine.get_portfolio(buyer_account.id)
        seller_portfolio = engine.get_portfolio(seller_account.id)

        # Buyer should have 1 trade (the maker BUY trade)
        buyer_trades = buyer_portfolio.trade_history
        assert len(buyer_trades) == 1
        assert buyer_trades[0].side == OrderSide.BUY
        assert buyer_trades[0].quantity == test_quantity
        assert buyer_trades[0].price == test_price
        assert buyer_trades[0].is_maker is True
        assert buyer_trades[0].order_id == buy_result.order.id

        # Seller should have 1 trade (the taker SELL trade)
        seller_trades = seller_portfolio.trade_history
        assert len(seller_trades) == 1
        assert seller_trades[0].side == OrderSide.SELL
        assert seller_trades[0].quantity == test_quantity
        assert seller_trades[0].price == test_price
        assert seller_trades[0].is_maker is False
        assert seller_trades[0].order_id == sell_order.id

    def test_final_order_statuses_filled(
        self,
        engine: TradingEngine,
        buyer_account: Account,
        seller_account: Account,
        btc_usd_market: Market,
        test_quantity: FixedPoint,
        test_price: FixedPoint,
    ):
        """Verify both orders show FILLED status."""
        # Submit both orders
        buy_order = create_buy_limit_order(buyer_account, btc_usd_market, test_quantity, test_price)
        buy_result = engine.submit_order(buy_order)
        sell_order = create_sell_limit_order(
            seller_account, btc_usd_market, test_quantity, test_price
        )
        sell_result = engine.submit_order(sell_order)

        # SELL order result shows FILLED
        assert sell_result.order.status == OrderStatus.FILLED
        assert sell_result.order.filled_quantity == test_quantity
        assert sell_result.order.remaining_quantity == FixedPoint("0")

        # BUY order status can be checked via its trades in portfolio
        buyer_portfolio = engine.get_portfolio(buyer_account.id)
        buy_trades = buyer_portfolio.get_trades_for_order(buy_result.order.id)
        assert len(buy_trades) == 1
        assert buy_trades[0].quantity == test_quantity  # fully filled

        # Order book is empty - both orders removed
        orderbook = engine.get_order_book("BTC-USD")
        assert len(orderbook) == 0

    def test_final_order_book_state_empty(
        self,
        engine: TradingEngine,
        buyer_account: Account,
        seller_account: Account,
        btc_usd_market: Market,
        test_quantity: FixedPoint,
        test_price: FixedPoint,
    ):
        """Verify order book empty after both orders fully matched."""
        # Submit both orders
        buy_order = create_buy_limit_order(buyer_account, btc_usd_market, test_quantity, test_price)
        engine.submit_order(buy_order)
        sell_order = create_sell_limit_order(
            seller_account, btc_usd_market, test_quantity, test_price
        )
        engine.submit_order(sell_order)

        orderbook = engine.get_order_book("BTC-USD")
        assert orderbook.best_bid is None
        assert orderbook.best_ask is None
        assert orderbook.spread is None
        assert orderbook.mid_price is None
        assert len(orderbook) == 0


# ============================================================================
# MAIN - Allow running directly
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
