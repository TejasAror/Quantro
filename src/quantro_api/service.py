"""In-memory application service wrapping the Quantro trading engine."""

from __future__ import annotations

from threading import RLock
from uuid import UUID

from quantro import (
    FP_ZERO,
    Account,
    Balance,
    FixedPoint,
    Market,
    Order,
    OrderResult,
    OrderSide,
    OrderType,
    Portfolio,
    Trade,
    TradingEngine,
    create_engine,
)

from .schemas import AccountCreate, DepositRequest, MarketCreate, OrderCreate


class EngineService:
    """Thread-safe owner for the process-local trading engine instance."""

    def __init__(self, engine: TradingEngine | None = None) -> None:
        self._engine = engine or create_engine()
        self._lock = RLock()
        self._orders: dict[UUID, Order] = {}
        self._trades_by_account: dict[UUID, list[Trade]] = {}
        self._seed_default_markets()

    @property
    def engine(self) -> TradingEngine:
        return self._engine

    def _seed_default_markets(self) -> None:
        default_markets = [
            Market(
                symbol="BTC-USD",
                base_asset="BTC",
                quote_asset="USD",
                venue="SANDBOX",
                price_precision=2,
                quantity_precision=8,
                min_order_size=FixedPoint("0.0001"),
                max_order_size=FixedPoint("1000"),
                tick_size=FixedPoint("0.01"),
                lot_size=FixedPoint("0.0001"),
                maker_fee=FixedPoint("0.001"),
                taker_fee=FixedPoint("0.001"),
                metadata={
                    "execution_supported": True,
                    "execution_mode": "paper",
                    "real_funds": False,
                    "venue_routing": "disabled",
                },
            ),
            Market(
                symbol="BTC-USDT-SWAP",
                base_asset="BTC",
                quote_asset="USDT",
                venue="PAPER",
                price_precision=1,
                quantity_precision=4,
                min_order_size=FixedPoint("0.0001"),
                max_order_size=FixedPoint("1000"),
                tick_size=FixedPoint("0.1"),
                lot_size=FixedPoint("0.0001"),
                maker_fee=FixedPoint("0.0005"),
                taker_fee=FixedPoint("0.0005"),
                metadata={
                    "product_type": "perpetual",
                    "instrument_type": "perpetual",
                    "settle_asset": "USDT",
                    "execution_supported": True,
                    "execution_mode": "paper",
                    "real_funds": False,
                    "venue_routing": "disabled",
                    "market_data_source": "okx_public_read_only",
                },
            ),
            Market(
                symbol="ETH-USDT-SWAP",
                base_asset="ETH",
                quote_asset="USDT",
                venue="PAPER",
                price_precision=2,
                quantity_precision=3,
                min_order_size=FixedPoint("0.001"),
                max_order_size=FixedPoint("10000"),
                tick_size=FixedPoint("0.01"),
                lot_size=FixedPoint("0.001"),
                maker_fee=FixedPoint("0.0005"),
                taker_fee=FixedPoint("0.0005"),
                metadata={
                    "product_type": "perpetual",
                    "instrument_type": "perpetual",
                    "settle_asset": "USDT",
                    "execution_supported": True,
                    "execution_mode": "paper",
                    "real_funds": False,
                    "venue_routing": "disabled",
                    "market_data_source": "okx_public_read_only",
                },
            ),
        ]
        for market in default_markets:
            existing = self._engine.get_market(market.symbol)
            if existing is None:
                self._engine = self._engine.add_market(market)
                continue

            merged_metadata = {**existing.metadata, **market.metadata}
            if existing == market and existing.metadata == merged_metadata:
                continue

            upgraded = Market(
                symbol=market.symbol,
                base_asset=market.base_asset,
                quote_asset=market.quote_asset,
                venue=market.venue,
                price_precision=market.price_precision,
                quantity_precision=market.quantity_precision,
                min_order_size=market.min_order_size,
                max_order_size=market.max_order_size,
                tick_size=market.tick_size,
                lot_size=market.lot_size,
                maker_fee=market.maker_fee,
                taker_fee=market.taker_fee,
                is_active=market.is_active,
                metadata=merged_metadata,
            )
            self._engine._markets[market.symbol] = upgraded  # type: ignore[attr-defined]
            order_book = self._engine._order_books.get(market.symbol)  # type: ignore[attr-defined]
            if order_book is not None:
                order_book._market = upgraded  # type: ignore[attr-defined]

    def list_markets(self) -> list[Market]:
        snapshot = self._engine.snapshot()
        return [order_book.market for order_book in snapshot.order_books.values()]

    def get_market(self, symbol: str) -> Market | None:
        return self._engine.get_market(symbol)

    def add_market(self, request: MarketCreate) -> Market:
        market = Market(
            symbol=request.symbol,
            base_asset=request.base_asset,
            quote_asset=request.quote_asset,
            venue=request.venue,
            price_precision=request.price_precision,
            quantity_precision=request.quantity_precision,
            min_order_size=FixedPoint(request.min_order_size),
            max_order_size=FixedPoint(request.max_order_size),
            tick_size=FixedPoint(request.tick_size),
            lot_size=FixedPoint(request.lot_size),
            maker_fee=FixedPoint(request.maker_fee),
            taker_fee=FixedPoint(request.taker_fee),
            is_active=request.is_active,
            metadata=request.metadata,
        )
        with self._lock:
            self._engine = self._engine.add_market(market)
        return market

    def create_account(self, request: AccountCreate) -> Account:
        balances = {asset: FixedPoint(amount) for asset, amount in request.initial_balances.items()}
        with self._lock:
            self._engine, account = self._engine.create_account(request.name, balances)
        return account

    def deposit(self, account_id: UUID, request: DepositRequest) -> Portfolio | None:
        with self._lock:
            self._engine = self._engine.deposit(
                account_id=account_id,
                asset=request.asset,
                amount=FixedPoint(request.amount),
            )
            return self._engine.get_portfolio(account_id)

    def submit_order(self, request: OrderCreate) -> OrderResult:
        with self._lock:
            if request.account_id is None:
                raise ValueError("account_id is required for paper execution")
            market = self._engine.get_market(request.symbol)
            if market is None:
                raise ValueError(f"Market {request.symbol} not found")
            execution_mode = market.metadata.get("execution_mode")
            if execution_mode != "paper":
                raise ValueError("Only Quantro paper execution is enabled")

            order = Order(
                account_id=request.account_id,
                market=market,
                symbol=market.symbol,
                side=request.side,
                order_type=request.order_type,
                time_in_force=request.time_in_force,
                quantity=FixedPoint(request.quantity),
                price=FixedPoint(request.price),
                stop_price=FixedPoint(request.stop_price),
                client_order_id=request.client_order_id,
                metadata={
                    **request.metadata,
                    "execution_mode": "paper",
                    "real_funds": False,
                    "venue_routing": "disabled",
                },
            )
            if (
                market.metadata.get("product_type") == "perpetual"
                and order.order_type == OrderType.MARKET
            ):
                return self._submit_paper_market_order(order)
            result = self._engine.submit_order(order)
            if result.accepted:
                self._orders[order.id] = order
                self._index_order_result(result)
            return result

    def _submit_paper_market_order(self, order: Order) -> OrderResult:
        market = order.market
        if market is None:
            raise ValueError("Order must have market")
        portfolio = self._engine.get_portfolio(order.account_id)
        if portfolio is None:
            raise ValueError(f"No portfolio for account {order.account_id}")
        if order.price <= FP_ZERO:
            raise ValueError("Paper market orders require a backend-derived reference price")

        mark_prices = self._engine._get_mark_prices()  # type: ignore[attr-defined]
        mark_prices[market.symbol] = order.price
        risk_report = self._engine._risk_engine.check_order(  # type: ignore[attr-defined]
            order=order,
            account=portfolio.account,
            market=market,
            positions=dict(portfolio.positions),
            open_orders_count=portfolio.open_orders_count,
            mark_prices=mark_prices,
        )
        if not risk_report.passed:
            return OrderResult(
                order=order.reject("Risk check failed"),
                risk_report=risk_report,
                trades=(),
                accepted=False,
                reject_reason="Risk check failed",
            )

        notional = order.quantity * order.price
        fee = (notional * market.taker_fee).clamp(FP_ZERO, notional)
        filled = order.fill(order.quantity, order.price, fee)
        trade = Trade(
            order_id=filled.id,
            account_id=filled.account_id,
            market=market,
            symbol=market.symbol,
            side=OrderSide(order.side),
            quantity=filled.quantity,
            price=order.price,
            fee=fee,
            fee_asset=market.quote_asset,
            is_maker=False,
            metadata={
                "execution_mode": "paper",
                "real_funds": False,
                "venue_routing": "disabled",
                "fill_source": "backend_public_market_data_reference",
            },
        )
        self._engine._portfolio_manager._portfolios[order.account_id] = portfolio.apply_trade(  # type: ignore[attr-defined]
            trade,
            mark_prices,
        )
        self._engine = self._engine.update_mark_price(market.symbol, order.price)
        self._orders[filled.id] = filled
        self._trades_by_account.setdefault(filled.account_id, []).append(trade)
        return OrderResult(order=filled, risk_report=risk_report, trades=(trade,), accepted=True)

    def _index_order_result(self, result: OrderResult) -> None:
        for trade in result.trades:
            self._trades_by_account.setdefault(trade.account_id, []).append(trade)
            existing_order = self._orders.get(trade.order_id)
            if existing_order is not None and trade.order_id != result.order.id:
                self._orders[trade.order_id] = existing_order.fill(
                    trade.quantity,
                    trade.price,
                    trade.fee,
                )

        self._orders[result.order.id] = result.order

    def get_order(self, order_id: UUID) -> Order | None:
        return self._orders.get(order_id)

    def list_account_orders(self, account_id: UUID) -> list[Order]:
        return [order for order in self._orders.values() if order.account_id == account_id]

    def list_account_trades(self, account_id: UUID) -> list[Trade]:
        return list(self._trades_by_account.get(account_id, ()))

    def cancel_order(self, order_id: UUID) -> Order | None:
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                return None

            cancelled = self._engine.cancel_order(order_id, order.symbol)
            if cancelled is None:
                raise ValueError(f"Cannot cancel order in status {order.status.value}")
            if cancelled is not None:
                self._orders[order_id] = cancelled
            return cancelled

    def cancel_all_orders(self, symbol: str, account_id: UUID) -> list[Order]:
        with self._lock:
            cancelled = self._engine.cancel_all_orders(account_id, symbol)
            for order in cancelled:
                self._orders[order.id] = order
            return cancelled

    def update_mark_price(self, symbol: str, price: str) -> None:
        with self._lock:
            self._engine = self._engine.update_mark_price(symbol, FixedPoint(price))

    def ensure_paper_virtual_balances(
        self,
        account_id: UUID,
        target_balances: dict[str, str],
    ) -> bool:
        """Initialize missing virtual balances for uninitialized paper swap accounts."""
        with self._lock:
            portfolio = self._engine.get_portfolio(account_id)
            if portfolio is None or not self._has_paper_swap_markets():
                return False
            if (
                portfolio.account.metadata.get("real_funds") is True
                or portfolio.account.metadata.get("execution_mode") not in {None, "paper"}
            ):
                return False
            account_has_activity = (
                bool(portfolio.positions)
                or bool(self._trades_by_account.get(account_id))
                or any(order.account_id == account_id for order in self._orders.values())
            )

            next_portfolio = portfolio
            changed = False
            for asset, amount in target_balances.items():
                target = FixedPoint(amount)
                if target <= FP_ZERO:
                    continue
                if asset in next_portfolio.account.balances:
                    current = next_portfolio.account.balances[asset]
                else:
                    current = next_portfolio.account.get_balance(asset)
                if current.free > FP_ZERO or current.locked > FP_ZERO or account_has_activity:
                    continue
                current = next_portfolio.account.get_balance(asset)
                next_account = next_portfolio.account.set_balance(
                    Balance(asset=asset, free=target, locked=current.locked)
                )
                next_portfolio = next_portfolio._copy()  # type: ignore[attr-defined]
                next_portfolio._account = next_account  # type: ignore[attr-defined]
                next_portfolio._sequence += 1  # type: ignore[attr-defined]
                changed = True

            if changed:
                self._engine._portfolio_manager._portfolios[account_id] = next_portfolio  # type: ignore[attr-defined]
            return changed

    def _has_paper_swap_markets(self) -> bool:
        return any(
            market.quote_asset == "USDT"
            and market.metadata.get("product_type") == "perpetual"
            and market.metadata.get("execution_mode") == "paper"
            and market.metadata.get("real_funds") is False
            and market.metadata.get("venue_routing") == "disabled"
            for market in self.list_markets()
        )

    def get_portfolio(self, account_id: UUID) -> Portfolio | None:
        return self._engine.get_portfolio(account_id)

    def get_total_pnl(self, account_id: UUID) -> FixedPoint | None:
        portfolio = self.get_portfolio(account_id)
        if portfolio is None:
            return None
        return portfolio.get_total_unrealized_pnl() + portfolio.get_total_realized_pnl()

    def zero(self) -> FixedPoint:
        return FP_ZERO


def create_service() -> EngineService:
    from .config import SupabaseSettings
    from .db import Database
    from .persistence import PersistentEngineService

    settings = SupabaseSettings.from_env()
    if settings is None:
        return EngineService()

    db = Database(settings.database_url)
    if settings.auto_migrate:
        db.migrate()
    return PersistentEngineService(db, settings.sandbox_initial_balances)
