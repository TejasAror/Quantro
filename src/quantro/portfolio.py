"""Portfolio accounting for balances, positions, trade history, and P&L."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from .fixedpoint import FP_ZERO, FixedPoint
from .models import (
    Account,
    Balance,
    Order,
    OrderSide,
    OrderStatus,
    Position,
    PositionSide,
    Trade,
)

if TYPE_CHECKING:
    from typing import Self
else:
    Self = "Portfolio"


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """Immutable snapshot of portfolio state at a point in time."""

    timestamp: datetime
    account_id: UUID
    balances: dict[str, Balance]
    positions: dict[str, Position]
    total_value: FixedPoint
    total_unrealized_pnl: FixedPoint
    total_realized_pnl: FixedPoint
    open_orders_count: int
    trade_count: int


class Portfolio:
    """
    Portfolio accounting with consistent state updates.

    Tracks:
    - Asset balances (free/locked)
    - Positions with entry prices and P&L
    - Trade history
    - Realized and unrealized P&L
    - Consistent state updates on every fill
    """

    def __init__(
        self,
        account: Account,
        initial_positions: dict[str, Position] | None = None,
        trade_history: list[Trade] | None = None,
    ) -> None:
        self._account = account
        self._positions: dict[str, Position] = initial_positions or {}
        self._trade_history: list[Trade] = trade_history or []
        self._trade_index: dict[UUID, Trade] = {t.id: t for t in self._trade_history}
        self._open_orders_count: int = 0
        self._sequence: int = 0
        # Initialize total realized P&L from initial positions
        self._total_realized_pnl: FixedPoint = sum(
            (p.realized_pnl for p in self._positions.values()), FP_ZERO
        )

    @property
    def account(self) -> Account:
        return self._account

    @property
    def account_id(self) -> UUID:
        return self._account.id

    @property
    def positions(self) -> dict[str, Position]:
        return dict(self._positions)

    @property
    def trade_history(self) -> list[Trade]:
        return list(self._trade_history)

    @property
    def open_orders_count(self) -> int:
        return self._open_orders_count

    @property
    def sequence(self) -> int:
        return self._sequence

    def get_position(self, symbol: str) -> Position | None:
        """Get position for symbol."""
        return self._positions.get(symbol)

    def get_balance(self, asset: str) -> Balance:
        """Get balance for asset."""
        return self._account.get_balance(asset)

    def get_balances(self) -> dict[str, Balance]:
        """Get all balances."""
        return dict(self._account.balances)

    def get_total_unrealized_pnl(self) -> FixedPoint:
        """Total unrealized P&L across all positions."""
        return sum((p.unrealized_pnl for p in self._positions.values()), FP_ZERO)

    def get_total_realized_pnl(self) -> FixedPoint:
        """Total realized P&L across all positions."""
        return self._total_realized_pnl

    def get_total_value(self, mark_prices: dict[str, FixedPoint]) -> FixedPoint:
        """Total portfolio value at mark prices."""
        # Account balances value
        total = self._account.total_value(mark_prices)

        # Add position notional values. Positions are already reflected in balances
        # when trades settle, but open positions still need their notional included.
        for symbol, pos in self._positions.items():
            if not pos.is_flat and symbol in mark_prices:
                total += pos.size * mark_prices[symbol]

        return total

    def get_position_notional(self, symbol: str, mark_price: FixedPoint) -> FixedPoint:
        """Get notional value of a position."""
        pos = self._positions.get(symbol)
        if pos is None or pos.is_flat:
            return FP_ZERO
        return pos.size * mark_price

    def increment_open_orders(self) -> Self:
        """Increment open orders count."""
        new_portfolio = self._copy()
        new_portfolio._open_orders_count += 1
        new_portfolio._sequence += 1
        return new_portfolio

    def decrement_open_orders(self) -> Self:
        """Decrement open orders count."""
        new_portfolio = self._copy()
        new_portfolio._open_orders_count = max(0, new_portfolio._open_orders_count - 1)
        new_portfolio._sequence += 1
        return new_portfolio

    def _copy(self) -> Self:
        """Create a mutable copy for internal updates."""
        new_portfolio = Portfolio(
            account=self._account,
            initial_positions=dict(self._positions),
            trade_history=list(self._trade_history),
        )
        new_portfolio._open_orders_count = self._open_orders_count
        new_portfolio._sequence = self._sequence
        new_portfolio._total_realized_pnl = self._total_realized_pnl
        return new_portfolio

    def apply_trade(
        self,
        trade: Trade,
        mark_prices: dict[str, FixedPoint],
    ) -> Self:
        """
        Apply a trade to the portfolio, updating all state consistently.

        This is the core method that ensures atomic updates to:
        - Balances (free/locked)
        - Positions (size, entry price, P&L)
        - Trade history
        - Account state

        Returns new Portfolio instance with updated state.
        """
        new_portfolio = self._copy()
        new_portfolio._trade_history.append(trade)
        new_portfolio._trade_index[trade.id] = trade

        market = trade.market
        if market is None:
            raise ValueError("Trade must have market")

        base_asset = market.base_asset
        quote_asset = market.quote_asset

        # 1. Update balances based on trade side
        if trade.side == OrderSide.BUY:
            # Bought base asset, spent quote asset
            base_received = trade.quantity
            quote_spent = trade.notional + trade.fee

            # Update base asset balance (add free)
            base_balance = new_portfolio._account.get_balance(base_asset)
            new_base_balance = base_balance.add_free(base_received)
            new_portfolio._account = new_portfolio._account.set_balance(new_base_balance)

            # Update quote asset balance (subtract from free - trade settlement)
            quote_balance = new_portfolio._account.get_balance(quote_asset)
            final_quote = quote_balance.subtract_free(quote_spent)
            new_portfolio._account = new_portfolio._account.set_balance(final_quote)

        else:  # SELL
            # Sold base asset, received quote asset
            base_sold = trade.quantity
            quote_received = trade.notional - trade.fee

            # Update base asset balance (subtract from free - trade settlement)
            base_balance = new_portfolio._account.get_balance(base_asset)
            final_base = base_balance.subtract_free(base_sold)
            new_portfolio._account = new_portfolio._account.set_balance(final_base)

            # Update quote asset balance (add free)
            quote_balance = new_portfolio._account.get_balance(quote_asset)
            new_quote_balance = quote_balance.add_free(quote_received)
            new_portfolio._account = new_portfolio._account.set_balance(new_quote_balance)

        # 2. Update position
        symbol = market.symbol
        current_pos = new_portfolio._positions.get(symbol)

        if current_pos is None or current_pos.is_flat:
            # Opening new position
            if trade.side == OrderSide.BUY:
                new_side = PositionSide.LONG
            else:
                new_side = PositionSide.SHORT

            new_pos = Position(
                id=uuid4(),
                account_id=new_portfolio.account_id,
                market=market,
                symbol=symbol,
                side=new_side,
                size=trade.quantity,
                entry_price=trade.price,
                mark_price=trade.price,
                unrealized_pnl=FP_ZERO,
                realized_pnl=FP_ZERO,
                leverage=FP_ZERO,  # Will be calculated
                opened_at=trade.timestamp,
                updated_at=trade.timestamp,
            )
            new_portfolio._positions[symbol] = new_pos
        else:
            # Updating existing position
            if (current_pos.is_long and trade.side == OrderSide.BUY) or (
                current_pos.is_short and trade.side == OrderSide.SELL
            ):
                # Adding to position
                new_pos = current_pos.add_size(trade.quantity, trade.price)
            else:
                # Reducing position (opposite side trade)
                if trade.quantity >= current_pos.size:
                    # Trade quantity exceeds or equals position - close and potentially flip
                    # First, close the current position
                    new_pos, realized = current_pos.reduce_size(current_pos.size, trade.price)
                    new_realized_pnl = current_pos.realized_pnl + realized

                    # Update portfolio's tracked realized P&L
                    new_portfolio._total_realized_pnl += realized

                    remaining_qty = trade.quantity - current_pos.size
                    if remaining_qty > FP_ZERO:
                        # Flip to opposite side with remaining quantity
                        if current_pos.is_long:
                            new_side = PositionSide.SHORT
                        else:
                            new_side = PositionSide.LONG

                        new_pos = Position(
                            id=uuid4(),
                            account_id=new_portfolio.account_id,
                            market=market,
                            symbol=symbol,
                            side=new_side,
                            size=remaining_qty,
                            entry_price=trade.price,
                            mark_price=trade.price,
                            unrealized_pnl=FP_ZERO,
                            realized_pnl=new_realized_pnl,
                            leverage=FP_ZERO,
                            opened_at=trade.timestamp,
                            updated_at=trade.timestamp,
                        )
                    else:
                        # Position fully closed, no flip
                        new_pos = Position(
                            id=current_pos.id,
                            account_id=current_pos.account_id,
                            market=current_pos.market,
                            symbol=current_pos.symbol,
                            side=PositionSide.FLAT,
                            size=FP_ZERO,
                            entry_price=FP_ZERO,
                            mark_price=trade.price,
                            unrealized_pnl=FP_ZERO,
                            realized_pnl=new_realized_pnl,
                            leverage=current_pos.leverage,
                            liquidation_price=current_pos.liquidation_price,
                            opened_at=current_pos.opened_at,
                            updated_at=trade.timestamp,
                            metadata=current_pos.metadata,
                        )
                else:
                    # Normal reduction (trade quantity < position size)
                    new_pos, realized = current_pos.reduce_size(trade.quantity, trade.price)
                    # Update portfolio's tracked realized P&L
                    new_portfolio._total_realized_pnl += realized

            # Update mark price for unrealized P&L
            mark_price = mark_prices.get(symbol, trade.price)
            new_pos = new_pos.update_mark_price(mark_price)

            if new_pos.is_flat:
                new_portfolio._positions.pop(symbol, None)
            else:
                new_portfolio._positions[symbol] = new_pos

        # 3. Update all positions' mark prices
        for sym, pos in new_portfolio._positions.items():
            if sym in mark_prices:
                new_portfolio._positions[sym] = pos.update_mark_price(mark_prices[sym])

        new_portfolio._sequence += 1
        return new_portfolio

    def apply_order_fill(
        self,
        order: Order,
        fill_qty: FixedPoint,
        fill_price: FixedPoint,
        fee: FixedPoint,
        mark_prices: dict[str, FixedPoint],
    ) -> tuple[Self, Trade]:
        """
        Apply an order fill to the portfolio.

        Creates a Trade from the fill and applies it atomically.
        """
        market = order.market
        if market is None:
            raise ValueError("Order must have market")

        trade = Trade(
            order_id=order.id,
            account_id=order.account_id,
            market=market,
            symbol=market.symbol,
            side=order.side,
            quantity=fill_qty,
            price=fill_price,
            fee=fee,
            fee_asset=market.quote_asset,
            timestamp=datetime.now(UTC).replace(tzinfo=None),
            is_maker=order.status == OrderStatus.OPEN,  # Simplified
        )

        new_portfolio = self.apply_trade(trade, mark_prices)
        return new_portfolio, trade

    def lock_funds_for_order(self, order: Order) -> Self:
        """
        Lock funds for a new order.

        For BUY: lock quote asset (price * quantity + estimated fees)
        For SELL: lock base asset (quantity)
        """
        market = order.market
        if market is None:
            raise ValueError("Order must have market")

        new_portfolio = self._copy()

        if order.side == OrderSide.BUY:
            # Lock quote asset
            price = (
                order.price if order.price > FP_ZERO else (market.tick_size * FixedPoint("10000"))
            )
            required = order.quantity * price
            # Add estimated fees
            required = required + (required * market.taker_fee)

            quote_balance = new_portfolio._account.get_balance(market.quote_asset)
            locked_quote = quote_balance.lock(required)
            new_portfolio._account = new_portfolio._account.set_balance(locked_quote)

        else:  # SELL
            # Lock base asset
            base_balance = new_portfolio._account.get_balance(market.base_asset)
            locked_base = base_balance.lock(order.quantity)
            new_portfolio._account = new_portfolio._account.set_balance(locked_base)

        new_portfolio._open_orders_count += 1
        new_portfolio._sequence += 1
        return new_portfolio

    def unlock_funds_from_order(self, order: Order, filled_qty: FixedPoint = FP_ZERO) -> Self:
        """
        Unlock funds when order is cancelled, partially filled, or fully filled.

        For partially/fully filled orders, unlock the filled portion for settlement.
        For cancelled orders, unlock the remaining (unfilled) portion.
        """
        market = order.market
        if market is None:
            raise ValueError("Order must have market")

        remaining = order.remaining_quantity
        new_portfolio = self._copy()

        if filled_qty > FP_ZERO:
            # Order was filled (partially or fully) - unlock the filled portion for settlement
            qty_to_unlock = filled_qty
            # Only decrement open_orders_count if order is fully filled
            if remaining == FP_ZERO:
                new_portfolio._open_orders_count = max(0, new_portfolio._open_orders_count - 1)
        else:
            # Order cancelled - unlock the remaining (unfilled) portion
            qty_to_unlock = remaining
            new_portfolio._open_orders_count = max(0, new_portfolio._open_orders_count - 1)

        if order.side == OrderSide.BUY:
            price = (
                order.price if order.price > FP_ZERO else (market.tick_size * FixedPoint("10000"))
            )
            # Unlock quote for the filled quantity
            unlock_value = qty_to_unlock * price
            unlock_value = unlock_value + (unlock_value * market.taker_fee)

            quote_balance = new_portfolio._account.get_balance(market.quote_asset)
            unlocked_quote = quote_balance.unlock(unlock_value)
            new_portfolio._account = new_portfolio._account.set_balance(unlocked_quote)

        else:  # SELL
            # Unlock base for the filled quantity
            base_balance = new_portfolio._account.get_balance(market.base_asset)
            unlocked_base = base_balance.unlock(qty_to_unlock)
            new_portfolio._account = new_portfolio._account.set_balance(unlocked_base)

        new_portfolio._sequence += 1
        return new_portfolio

    def update_mark_prices(self, mark_prices: dict[str, FixedPoint]) -> Self:
        """Update all positions with new mark prices."""
        new_portfolio = self._copy()

        for symbol, price in mark_prices.items():
            pos = new_portfolio._positions.get(symbol)
            if pos is not None and not pos.is_flat:
                new_portfolio._positions[symbol] = pos.update_mark_price(price)

        new_portfolio._sequence += 1
        return new_portfolio

    def deposit(self, asset: str, amount: FixedPoint) -> Self:
        """Deposit funds to account."""
        if amount <= FP_ZERO:
            raise ValueError("Deposit amount must be positive")

        new_portfolio = self._copy()
        balance = new_portfolio._account.get_balance(asset)
        new_balance = balance.add_free(amount)
        new_portfolio._account = new_portfolio._account.set_balance(new_balance)
        new_portfolio._sequence += 1
        return new_portfolio

    def withdraw(self, asset: str, amount: FixedPoint) -> Self:
        """Withdraw funds from account (must be free)."""
        if amount <= FP_ZERO:
            raise ValueError("Withdrawal amount must be positive")

        new_portfolio = self._copy()
        balance = new_portfolio._account.get_balance(asset)
        new_balance = balance.subtract_free(amount)
        new_portfolio._account = new_portfolio._account.set_balance(new_balance)
        new_portfolio._sequence += 1
        return new_portfolio

    def get_trades_for_order(self, order_id: UUID) -> list[Trade]:
        """Get all trades for a specific order."""
        return [t for t in self._trade_history if t.order_id == order_id]

    def get_trades_for_symbol(self, symbol: str) -> list[Trade]:
        """Get all trades for a symbol."""
        return [t for t in self._trade_history if t.symbol == symbol]

    def snapshot(self, mark_prices: dict[str, FixedPoint]) -> PortfolioSnapshot:
        """Create immutable snapshot of current portfolio state."""
        return PortfolioSnapshot(
            timestamp=datetime.now(UTC).replace(tzinfo=None),
            account_id=self._account.id,
            balances=dict(self._account.balances),
            positions=dict(self._positions),
            total_value=self.get_total_value(mark_prices),
            total_unrealized_pnl=self.get_total_unrealized_pnl(),
            total_realized_pnl=self.get_total_realized_pnl(),
            open_orders_count=self._open_orders_count,
            trade_count=len(self._trade_history),
        )

    def __len__(self) -> int:
        return len(self._trade_history)


class PortfolioManager:
    """
    Manages multiple portfolios (one per account).

    Provides unified interface for multi-account trading systems.
    """

    def __init__(self) -> None:
        self._portfolios: dict[UUID, Portfolio] = {}

    def create_portfolio(self, account: Account) -> Portfolio:
        """Create portfolio for account."""
        portfolio = Portfolio(account)
        self._portfolios[account.id] = portfolio
        return portfolio

    def get_portfolio(self, account_id: UUID) -> Portfolio | None:
        """Get portfolio for account."""
        return self._portfolios.get(account_id)

    def get_or_create_portfolio(self, account: Account) -> Portfolio:
        """Get existing portfolio or create new one."""
        if account.id not in self._portfolios:
            return self.create_portfolio(account)
        return self._portfolios[account.id]

    def remove_portfolio(self, account_id: UUID) -> bool:
        """Remove portfolio."""
        if account_id in self._portfolios:
            del self._portfolios[account_id]
            return True
        return False

    def apply_trade_to_portfolio(
        self,
        trade: Trade,
        mark_prices: dict[str, FixedPoint],
    ) -> Portfolio:
        """Apply trade to the appropriate portfolio."""
        portfolio = self._portfolios.get(trade.account_id)
        if portfolio is None:
            raise ValueError(f"No portfolio for account {trade.account_id}")
        return portfolio.apply_trade(trade, mark_prices)

    def all_portfolios(self) -> list[Portfolio]:
        """Get all portfolios."""
        return list(self._portfolios.values())

    def total_equity(self, mark_prices: dict[str, FixedPoint]) -> FixedPoint:
        """Total equity across all portfolios."""
        return sum(p.get_total_value(mark_prices) for p in self._portfolios.values())


def create_portfolio(account: Account) -> Portfolio:
    """Factory function to create portfolio."""
    return Portfolio(account)


__all__ = [
    "Portfolio",
    "PortfolioManager",
    "PortfolioSnapshot",
    "create_portfolio",
]
