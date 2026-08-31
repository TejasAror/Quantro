"""End-to-end trading engine integrating all components."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from .fixedpoint import FP_ZERO, FixedPoint
from .models import (
    Account,
    Balance,
    Market,
    Order,
    OrderStatus,
    OrderType,
    TimeInForce,
    Trade,
)
from .orderbook import OrderBook
from .portfolio import Portfolio, PortfolioManager
from .risk import RiskCheckResult, RiskEngine, RiskReport

if TYPE_CHECKING:
    from typing import Self


@dataclass(frozen=True, slots=True)
class OrderResult:
    """Result of order submission."""

    order: Order
    risk_report: RiskReport
    trades: tuple[Trade, ...]
    accepted: bool
    reject_reason: str | None = None


@dataclass(frozen=True, slots=True)
class EngineSnapshot:
    """Complete engine state snapshot for replay."""

    timestamp: datetime
    sequence: int
    portfolios: dict[UUID, Portfolio]
    order_books: dict[str, OrderBook]
    account_balances: dict[UUID, dict[str, Balance]]


class TradingEngine:
    """
    Complete trading engine integrating:
    - Account management
    - Risk validation
    - Order book matching
    - Portfolio accounting
    - Deterministic replay
    """

    def __init__(
        self,
        risk_engine: RiskEngine | None = None,
        portfolio_manager: PortfolioManager | None = None,
    ) -> None:
        self._risk_engine = risk_engine or RiskEngine()
        self._portfolio_manager = portfolio_manager or PortfolioManager()
        self._order_books: dict[str, OrderBook] = {}
        self._markets: dict[str, Market] = {}
        self._sequence: int = 0
        self._event_log: list[dict] = []

    def add_market(self, market: Market) -> Self:
        """Add a market and create its order book."""
        if market.symbol in self._markets:
            raise ValueError(f"Market {market.symbol} already exists")

        new_engine = self._copy()
        new_engine._markets[market.symbol] = market
        new_engine._order_books[market.symbol] = OrderBook(market)
        new_engine._sequence += 1
        return new_engine

    def create_account(
        self, name: str, initial_balances: dict[str, FixedPoint] | None = None
    ) -> tuple[Self, Account]:
        """Create a new account with optional initial balances."""
        account = Account(name=name)

        if initial_balances:
            for asset, amount in initial_balances.items():
                if amount > FP_ZERO:
                    account = account.set_balance(Balance(asset=asset, free=amount))

        new_engine = self._copy()
        new_engine._portfolio_manager.create_portfolio(account)
        new_engine._sequence += 1
        return new_engine, account

    def deposit(self, account_id: UUID, asset: str, amount: FixedPoint) -> Self:
        """Deposit funds to account."""
        portfolio = self._portfolio_manager.get_portfolio(account_id)
        if portfolio is None:
            raise ValueError(f"No portfolio for account {account_id}")

        new_engine = self._copy()
        new_portfolio = portfolio.deposit(asset, amount)
        new_engine._portfolio_manager._portfolios[account_id] = new_portfolio
        new_engine._sequence += 1
        return new_engine

    def submit_order(self, order: Order) -> OrderResult:
        """
        Submit an order through the complete pipeline:
        1. Risk validation
        2. Lock funds
        3. Order book matching
        4. Trade execution
        5. Portfolio updates
        """
        # Get market
        market = self._markets.get(order.symbol)
        if market is None:
            reject_order = order.reject(f"Market {order.symbol} not found")
            return OrderResult(
                order=reject_order,
                risk_report=RiskReport(checks=(), overall=RiskCheckResult.FAILED),
                trades=(),
                accepted=False,
                reject_reason=f"Market {order.symbol} not found",
            )

        # Get portfolio
        portfolio = self._portfolio_manager.get_portfolio(order.account_id)
        if portfolio is None:
            reject_order = order.reject(f"No portfolio for account {order.account_id}")
            return OrderResult(
                order=reject_order,
                risk_report=RiskReport(checks=(), overall=RiskCheckResult.FAILED),
                trades=(),
                accepted=False,
                reject_reason=f"No portfolio for account {order.account_id}",
            )

        # 1. Risk validation
        positions = dict(portfolio.positions)
        open_orders = portfolio.open_orders_count
        mark_prices = self._get_mark_prices()

        risk_report = self._risk_engine.check_order(
            order=order,
            account=portfolio.account,
            market=market,
            positions=positions,
            open_orders_count=open_orders,
            mark_prices=mark_prices,
        )

        if not risk_report.passed:
            reject_order = order.reject(
                "Risk check failed: " + "; ".join(c.message for c in risk_report.failed_checks)
            )
            return OrderResult(
                order=reject_order,
                risk_report=risk_report,
                trades=(),
                accepted=False,
                reject_reason="Risk check failed",
            )

        # 2. Lock funds for order
        portfolio = portfolio.lock_funds_for_order(order)

        # 3. Order book matching
        order_book = self._order_books[market.symbol]

        if order.order_type == OrderType.MARKET:
            trades, updated_order = order_book.execute_market_order(order)
        elif order.order_type == OrderType.LIMIT:
            trades, updated_order = order_book.execute_limit_order(order)
        else:
            reject_order = order.reject(f"Unsupported order type: {order.order_type}")
            return OrderResult(
                order=reject_order,
                risk_report=risk_report,
                trades=(),
                accepted=False,
                reject_reason=f"Unsupported order type: {order.order_type}",
            )

        # 4. Portfolio updates for each trade
        # First unlock funds for the filled quantity for the submitter
        filled_qty = order.quantity - updated_order.remaining_quantity
        if filled_qty > FP_ZERO:
            portfolio = portfolio.unlock_funds_from_order(order, filled_qty)

        # Also unlock funds for maker orders that were filled
        # Track maker orders and their filled quantities from trades
        maker_fills: dict[UUID, FixedPoint] = {}
        for trade in trades:
            if trade.is_maker:
                # Accumulate fills per maker order (in case multiple fills at different prices)
                existing = maker_fills.get(trade.order_id, FP_ZERO)
                maker_fills[trade.order_id] = existing + trade.quantity

        for maker_order_id, maker_filled_qty in maker_fills.items():
            # Get the maker's portfolio and unlock their locked funds for the filled quantity
            # We need to find the maker order to know its side/market for unlock calculation
            # The order may have been removed from the book if fully filled, so we use trade info
            # Find the first trade for this maker to get the order details
            maker_trade = next(t for t in trades if t.is_maker and t.order_id == maker_order_id)
            maker_portfolio = self._portfolio_manager.get_portfolio(maker_trade.account_id)
            if maker_portfolio is not None and maker_filled_qty > FP_ZERO:
                # Try to get the maker order from the order book to determine remaining quantity
                # If fully filled, the order was removed from the book (remaining = 0)
                # If partially filled, the order is still on the book with
                # updated remaining_quantity.
                order_book = self._order_books[maker_trade.symbol]
                maker_order_on_book = order_book.get_order(maker_order_id)

                if maker_order_on_book is not None:
                    # Order is partially filled and still on book
                    original_quantity = maker_order_on_book.quantity
                    filled_quantity = maker_order_on_book.filled_quantity
                    order_status = maker_order_on_book.status
                else:
                    # Order was fully filled and removed from book
                    # The original quantity equals the filled quantity (since fully filled)
                    original_quantity = maker_filled_qty
                    filled_quantity = maker_filled_qty
                    order_status = OrderStatus.FILLED

                # Create a minimal order-like object for unlock_funds_from_order
                # We need the original order's side and market to calculate unlock amount
                temp_order = Order(
                    id=maker_order_id,
                    account_id=maker_trade.account_id,
                    market=maker_trade.market,
                    symbol=maker_trade.symbol,
                    side=maker_trade.side,
                    order_type=OrderType.LIMIT,
                    status=order_status,
                    time_in_force=TimeInForce.GTC,
                    quantity=original_quantity,
                    price=maker_trade.price,
                    filled_quantity=filled_quantity,
                )
                maker_portfolio = maker_portfolio.unlock_funds_from_order(
                    temp_order, maker_filled_qty
                )
                self._portfolio_manager._portfolios[maker_trade.account_id] = maker_portfolio

        # Apply each trade to the correct portfolio (taker vs maker)
        for trade in trades:
            trade_portfolio = self._portfolio_manager.get_portfolio(trade.account_id)
            if trade_portfolio is None:
                raise ValueError(f"No portfolio for trade account {trade.account_id}")
            trade_portfolio = trade_portfolio.apply_trade(trade, mark_prices)
            self._portfolio_manager._portfolios[trade.account_id] = trade_portfolio

        # 5. Handle remaining unfilled quantity
        if updated_order.remaining_quantity > FP_ZERO:
            if (
                updated_order.status == OrderStatus.OPEN
                or updated_order.status == OrderStatus.PARTIALLY_FILLED
            ):
                # Funds for remaining quantity stay locked (already handled above)
                pass
            else:
                # Cancelled/rejected - unlock remaining funds
                portfolio = portfolio.unlock_funds_from_order(updated_order, filled_qty)
        else:
            # Fully filled - unlock any remaining (should be 0, already handled above)
            pass

        # Ensure submitter's portfolio is up-to-date in manager
        # If there were trades, the apply_trade loop already updated it
        # If no trades (resting order), the portfolio variable has the locked funds
        if not trades:
            self._portfolio_manager._portfolios[order.account_id] = portfolio

        # Update mark prices from last trade
        if trades:
            last_trade = trades[-1]
            mark_prices[market.symbol] = last_trade.price

        self._sequence += 1
        self._log_event(
            "order_submitted",
            {
                "order_id": str(order.id),
                "account_id": str(order.account_id),
                "symbol": order.symbol,
                "side": order.side.value,
                "quantity": str(order.quantity),
                "price": str(order.price) if order.price > FP_ZERO else "MARKET",
                "trades": len(trades),
                "accepted": True,
            },
        )

        return OrderResult(
            order=updated_order,
            risk_report=risk_report,
            trades=tuple(trades),
            accepted=True,
        )

    def cancel_order(self, order_id: UUID, symbol: str) -> Order | None:
        """Cancel an order by ID."""
        order_book = self._order_books.get(symbol)
        if order_book is None:
            return None

        cancelled = order_book.cancel_order(order_id)
        if cancelled:
            # Unlock funds
            portfolio = self._portfolio_manager.get_portfolio(cancelled.account_id)
            if portfolio is not None:
                portfolio = portfolio.unlock_funds_from_order(cancelled)
                self._portfolio_manager._portfolios[cancelled.account_id] = portfolio

            self._sequence += 1
            self._log_event(
                "order_cancelled",
                {
                    "order_id": str(order_id),
                    "symbol": symbol,
                },
            )

        return cancelled

    def cancel_all_orders(self, account_id: UUID, symbol: str) -> list[Order]:
        """Cancel all orders for an account on a symbol."""
        order_book = self._order_books.get(symbol)
        if order_book is None:
            return []

        cancelled = order_book.cancel_all_orders(account_id)

        # Unlock funds for each cancelled order
        for order in cancelled:
            portfolio = self._portfolio_manager.get_portfolio(account_id)
            if portfolio is not None:
                portfolio = portfolio.unlock_funds_from_order(order)
                self._portfolio_manager._portfolios[account_id] = portfolio

        self._sequence += len(cancelled)
        return cancelled

    def update_mark_price(self, symbol: str, price: FixedPoint) -> Self:
        """Update mark price for a symbol."""
        new_engine = self._copy()

        # Update order book last trade price
        if symbol in new_engine._order_books:
            # OrderBook doesn't have direct mark price setter, but we can track it
            pass

        # Update all portfolios
        for portfolio in new_engine._portfolio_manager._portfolios.values():
            new_engine._portfolio_manager._portfolios[portfolio.account_id] = (
                portfolio.update_mark_prices({symbol: price})
            )

        new_engine._sequence += 1
        return new_engine

    def _get_mark_prices(self) -> dict[str, FixedPoint]:
        """Get current mark prices from order books."""
        prices = {}
        for symbol, book in self._order_books.items():
            if book.last_trade_price > FP_ZERO:
                prices[symbol] = book.last_trade_price
            elif book.mid_price is not None:
                prices[symbol] = book.mid_price
            elif book.best_bid is not None:
                # Fallback to best bid if no mid price (one-sided book)
                prices[symbol] = book.best_bid
            elif book.best_ask is not None:
                # Fallback to best ask if no mid price (one-sided book)
                prices[symbol] = book.best_ask
        return prices

    def get_portfolio(self, account_id: UUID) -> Portfolio | None:
        """Get portfolio for account."""
        return self._portfolio_manager.get_portfolio(account_id)

    def get_order_book(self, symbol: str) -> OrderBook | None:
        """Get order book for symbol."""
        return self._order_books.get(symbol)

    def get_market(self, symbol: str) -> Market | None:
        """Get market for symbol."""
        return self._markets.get(symbol)

    def _copy(self) -> Self:
        """Create mutable copy for internal updates."""
        new_engine = TradingEngine(
            risk_engine=self._risk_engine,
            portfolio_manager=self._portfolio_manager,
        )
        new_engine._markets = dict(self._markets)
        new_engine._order_books = dict(self._order_books)
        new_engine._sequence = self._sequence
        new_engine._event_log = list(self._event_log)
        return new_engine

    def _log_event(self, event_type: str, data: dict) -> None:
        """Log event for replay."""
        self._event_log.append(
            {
                "sequence": self._sequence,
                "timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                "type": event_type,
                "data": data,
            }
        )

    def snapshot(self) -> EngineSnapshot:
        """Create complete engine snapshot."""
        return EngineSnapshot(
            timestamp=datetime.now(UTC).replace(tzinfo=None),
            sequence=self._sequence,
            portfolios=dict(self._portfolio_manager._portfolios),
            order_books=dict(self._order_books),
            account_balances={
                pid: dict(p.account.balances)
                for pid, p in self._portfolio_manager._portfolios.items()
            },
        )

    def replay_events(self, events: list[dict]) -> Self:
        """Replay events from log (deterministic replay)."""
        # This would re-apply all events in order
        # For now, return current state
        return self


def create_engine(
    risk_engine: RiskEngine | None = None,
) -> TradingEngine:
    """Factory function to create trading engine."""
    return TradingEngine(risk_engine=risk_engine)


__all__ = [
    "TradingEngine",
    "OrderResult",
    "EngineSnapshot",
    "create_engine",
]
