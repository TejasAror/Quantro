"""Price-time priority order book implementation."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING
from uuid import UUID

from .fixedpoint import FP_ZERO, FixedPoint
from .models import Market, Order, OrderSide, OrderStatus, OrderType, TimeInForce, Trade

if TYPE_CHECKING:
    from typing import Self


class OrderBookSide(Enum):
    """Order book side."""

    BID = "bid"
    ASK = "ask"


@dataclass(frozen=True, slots=True)
class PriceLevel:
    """Single price level with FIFO queue of orders."""

    price: FixedPoint
    orders: tuple[Order, ...] = field(default_factory=tuple)
    total_quantity: FixedPoint = field(default_factory=lambda: FP_ZERO)

    def add_order(self, order: Order) -> Self:
        """Add order to the end of queue (FIFO)."""
        new_orders = self.orders + (order,)
        return PriceLevel(
            price=self.price,
            orders=new_orders,
            total_quantity=self.total_quantity + order.remaining_quantity,
        )

    def remove_order(self, order_id: UUID) -> tuple[Self, Order | None]:
        """Remove order by ID, return new level and removed order."""
        for i, order in enumerate(self.orders):
            if order.id == order_id:
                new_orders = self.orders[:i] + self.orders[i + 1 :]
                return PriceLevel(
                    price=self.price,
                    orders=new_orders,
                    total_quantity=self.total_quantity - order.remaining_quantity,
                ), order
        return self, None

    def update_order(self, order_id: UUID, new_order: Order) -> Self:
        """Update order in place (for fills)."""
        for i, order in enumerate(self.orders):
            if order.id == order_id:
                new_orders = self.orders[:i] + (new_order,) + self.orders[i + 1 :]
                return PriceLevel(
                    price=self.price,
                    orders=new_orders,
                    total_quantity=self.total_quantity
                    - order.remaining_quantity
                    + new_order.remaining_quantity,
                )
        return self

    def is_empty(self) -> bool:
        return len(self.orders) == 0

    def __iter__(self) -> Iterator[Order]:
        return iter(self.orders)


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    """Immutable snapshot of order book state."""

    symbol: str
    timestamp: datetime
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    last_trade_price: FixedPoint = field(default_factory=lambda: FP_ZERO)
    last_trade_quantity: FixedPoint = field(default_factory=lambda: FP_ZERO)
    sequence: int = 0


class OrderBook:
    """
    Deterministic price-time priority order book.

    Features:
    - Bid/ask sides with multiple price levels
    - FIFO queues at each price level
    - Market and limit order support
    - Full and partial fills
    - Order cancellation
    - Market depth access
    - Deterministic execution (no randomness)
    """

    def __init__(
        self,
        market: Market,
        initial_bids: list[PriceLevel] | None = None,
        initial_asks: list[PriceLevel] | None = None,
    ) -> None:
        self._market = market
        self._bids: dict[FixedPoint, PriceLevel] = {}
        self._asks: dict[FixedPoint, PriceLevel] = {}
        self._bid_prices: list[FixedPoint] = []  # Sorted descending
        self._ask_prices: list[FixedPoint] = []  # Sorted ascending
        self._order_index: dict[UUID, tuple[OrderBookSide, FixedPoint]] = {}
        self._sequence: int = 0
        self._last_trade_price: FixedPoint = FP_ZERO
        self._last_trade_quantity: FixedPoint = FP_ZERO

        if initial_bids:
            for level in initial_bids:
                self._add_price_level(OrderBookSide.BID, level)
        if initial_asks:
            for level in initial_asks:
                self._add_price_level(OrderBookSide.ASK, level)

    @property
    def market(self) -> Market:
        return self._market

    @property
    def symbol(self) -> str:
        return self._market.symbol

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def last_trade_price(self) -> FixedPoint:
        return self._last_trade_price

    @property
    def last_trade_quantity(self) -> FixedPoint:
        return self._last_trade_quantity

    @property
    def best_bid(self) -> FixedPoint | None:
        return self._bid_prices[0] if self._bid_prices else None

    @property
    def best_ask(self) -> FixedPoint | None:
        return self._ask_prices[0] if self._ask_prices else None

    @property
    def spread(self) -> FixedPoint | None:
        bid = self.best_bid
        ask = self.best_ask
        if bid is not None and ask is not None:
            return ask - bid
        return None

    @property
    def mid_price(self) -> FixedPoint | None:
        bid = self.best_bid
        ask = self.best_ask
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        return None

    def _add_price_level(self, side: OrderBookSide, level: PriceLevel) -> None:
        if side == OrderBookSide.BID:
            self._bids[level.price] = level
            self._insert_sorted_desc(self._bid_prices, level.price)
        else:
            self._asks[level.price] = level
            self._insert_sorted_asc(self._ask_prices, level.price)

    def _remove_price_level(self, side: OrderBookSide, price: FixedPoint) -> None:
        if side == OrderBookSide.BID:
            self._bids.pop(price, None)
            self._bid_prices = [p for p in self._bid_prices if p != price]
        else:
            self._asks.pop(price, None)
            self._ask_prices = [p for p in self._ask_prices if p != price]

    @staticmethod
    def _insert_sorted_desc(arr: list[FixedPoint], value: FixedPoint) -> None:
        """Insert value into descending sorted list."""
        left, right = 0, len(arr)
        while left < right:
            mid = (left + right) // 2
            if arr[mid] > value:
                left = mid + 1
            else:
                right = mid
        arr.insert(left, value)

    @staticmethod
    def _insert_sorted_asc(arr: list[FixedPoint], value: FixedPoint) -> None:
        """Insert value into ascending sorted list."""
        left, right = 0, len(arr)
        while left < right:
            mid = (left + right) // 2
            if arr[mid] < value:
                left = mid + 1
            else:
                right = mid
        arr.insert(left, value)

    def get_price_level(self, side: OrderBookSide, price: FixedPoint) -> PriceLevel | None:
        """Get price level for side and price."""
        if side == OrderBookSide.BID:
            return self._bids.get(price)
        return self._asks.get(price)

    def get_orders_at_price(self, side: OrderBookSide, price: FixedPoint) -> tuple[Order, ...]:
        """Get all orders at a specific price level."""
        level = self.get_price_level(side, price)
        return level.orders if level else ()

    def get_order(self, order_id: UUID) -> Order | None:
        """Get order by ID from any side/price."""
        if order_id not in self._order_index:
            return None
        side, price = self._order_index[order_id]
        level = self.get_price_level(side, price)
        if not level:
            return None
        for order in level.orders:
            if order.id == order_id:
                return order
        return None

    def add_order(self, order: Order) -> Order:
        """
        Add order to book.

        Returns updated order with status OPEN.
        """
        if not order.is_active:
            raise ValueError(f"Cannot add order in status {order.status}")

        side = OrderBookSide.BID if order.side == OrderSide.BUY else OrderBookSide.ASK
        price = order.price

        if order.order_type == OrderType.MARKET:
            # Market orders don't rest on book, they execute immediately
            # This method shouldn't be called for market orders
            raise ValueError("Use execute_market_order for market orders")

        # Validate price
        if not self._market.validate_price(price):
            raise ValueError(f"Invalid price {price} for market {self._market.symbol}")

        # Add to price level
        level = self._bids.get(price) if side == OrderBookSide.BID else self._asks.get(price)

        if level is None:
            level = PriceLevel(
                price=price, orders=(order,), total_quantity=order.remaining_quantity
            )
            self._add_price_level(side, level)
        else:
            new_level = level.add_order(order)
            if side == OrderBookSide.BID:
                self._bids[price] = new_level
            else:
                self._asks[price] = new_level

        self._order_index[order.id] = (side, price)
        self._sequence += 1

        # If order was PENDING, transition to OPEN. If already PARTIALLY_FILLED, keep as is.
        if order.status == OrderStatus.PENDING:
            return order.to_open()
        return order

    def cancel_order(self, order_id: UUID) -> Order | None:
        """Cancel order by ID. Returns cancelled order or None if not found."""
        if order_id not in self._order_index:
            return None

        side, price = self._order_index[order_id]
        level = self.get_price_level(side, price)

        if not level:
            self._order_index.pop(order_id, None)
            return None

        new_level, removed_order = level.remove_order(order_id)

        if removed_order is None:
            self._order_index.pop(order_id, None)
            return None

        if new_level.is_empty():
            self._remove_price_level(side, price)
        else:
            if side == OrderBookSide.BID:
                self._bids[price] = new_level
            else:
                self._asks[price] = new_level

        self._order_index.pop(order_id, None)
        self._sequence += 1

        return removed_order.cancel()

    def cancel_all_orders(self, account_id: UUID) -> list[Order]:
        """Cancel all orders for an account."""
        cancelled = []
        order_ids = list(self._order_index.keys())

        for order_id in order_ids:
            order = self.get_order(order_id)
            if order and order.account_id == account_id:
                cancelled_order = self.cancel_order(order_id)
                if cancelled_order:
                    cancelled.append(cancelled_order)

        return cancelled

    def execute_market_order(self, order: Order) -> tuple[list[Trade], Order]:
        """
        Execute market order against book.

        Returns tuple of (trades executed, updated order).
        """
        if order.order_type != OrderType.MARKET:
            raise ValueError("Order must be MARKET type")
        if not order.is_active:
            raise ValueError(f"Cannot execute order in status {order.status}")

        side = OrderBookSide.BID if order.side == OrderSide.BUY else OrderBookSide.ASK
        opposite_side = OrderBookSide.ASK if side == OrderBookSide.BID else OrderBookSide.BID

        trades: list[Trade] = []
        remaining_qty = order.remaining_quantity
        updated_order = order

        # Get price levels from best to worst for the taker
        prices = self._ask_prices if side == OrderBookSide.BID else self._bid_prices

        # For FOK, first check if fully fillable
        if order.time_in_force == TimeInForce.FOK:
            total_available = FP_ZERO
            for price in prices:
                level = self.get_price_level(opposite_side, price)
                if level:
                    for resting_order in level.orders:
                        if resting_order.is_active:
                            total_available += resting_order.remaining_quantity
            if total_available < order.remaining_quantity:
                return [], order.reject("FOK order not fully fillable")

        for price in prices:
            if remaining_qty <= FP_ZERO:
                break

            level = self.get_price_level(opposite_side, price)
            if not level:
                continue

            # Execute against orders at this price level (FIFO)
            for resting_order in level.orders:
                if remaining_qty <= FP_ZERO:
                    break
                if not resting_order.is_active:
                    continue

                fill_qty = min(remaining_qty, resting_order.remaining_quantity)

                # Calculate fee (taker pays taker fee, maker pays maker fee)
                taker_fee = self._calculate_fee(fill_qty, price, self._market.taker_fee)
                maker_fee = self._calculate_fee(fill_qty, price, self._market.maker_fee)

                # Create trades
                taker_trade = Trade(
                    order_id=updated_order.id,
                    account_id=updated_order.account_id,
                    market=self._market,
                    symbol=self._market.symbol,
                    side=updated_order.side,
                    quantity=fill_qty,
                    price=price,
                    fee=taker_fee,
                    fee_asset=self._market.quote_asset,
                    is_maker=False,
                )

                maker_trade = Trade(
                    order_id=resting_order.id,
                    account_id=resting_order.account_id,
                    market=self._market,
                    symbol=self._market.symbol,
                    side=resting_order.side,
                    quantity=fill_qty,
                    price=price,
                    fee=maker_fee,
                    fee_asset=self._market.quote_asset,
                    is_maker=True,
                )

                trades.append(taker_trade)
                trades.append(maker_trade)

                # Update orders
                updated_taker = updated_order.fill(fill_qty, price, taker_fee)
                updated_maker = resting_order.fill(fill_qty, price, maker_fee)

                updated_order = updated_taker
                remaining_qty = updated_order.remaining_quantity

                # Update resting order in book
                new_level = level.update_order(resting_order.id, updated_maker)
                if opposite_side == OrderBookSide.BID:
                    self._bids[price] = new_level
                else:
                    self._asks[price] = new_level

                # Remove filled orders
                if updated_maker.is_complete:
                    new_level_after_remove, _ = new_level.remove_order(resting_order.id)
                    if new_level_after_remove.is_empty():
                        self._remove_price_level(opposite_side, price)
                    else:
                        if opposite_side == OrderBookSide.BID:
                            self._bids[price] = new_level_after_remove
                        else:
                            self._asks[price] = new_level_after_remove
                    self._order_index.pop(resting_order.id, None)

                self._last_trade_price = price
                self._last_trade_quantity = fill_qty
                self._sequence += 1

        # Handle remaining quantity based on time in force
        if remaining_qty > FP_ZERO:
            if updated_order.time_in_force == TimeInForce.IOC:
                # Immediate or Cancel - cancel remaining
                if updated_order.filled_quantity > FP_ZERO:
                    updated_order = updated_order.cancel()
                else:
                    updated_order = updated_order.reject("No liquidity for IOC order")
            elif updated_order.time_in_force == TimeInForce.FOK:
                # Should have been caught above, but just in case
                updated_order = updated_order.reject("FOK order not fully fillable")
            # GTC orders would remain on book (but market orders don't rest)

        return trades, updated_order

    def execute_limit_order(self, order: Order) -> tuple[list[Trade], Order]:
        """
        Execute limit order against book.

        Adds remaining to book if not fully filled.
        """
        if order.order_type != OrderType.LIMIT:
            raise ValueError("Order must be LIMIT type")
        if not order.is_active:
            raise ValueError(f"Cannot execute order in status {order.status}")

        side = OrderBookSide.BID if order.side == OrderSide.BUY else OrderBookSide.ASK
        opposite_side = OrderBookSide.ASK if side == OrderBookSide.BID else OrderBookSide.BID

        trades: list[Trade] = []
        remaining_qty = order.remaining_quantity

        # Get prices we can match against
        prices = self._ask_prices if side == OrderBookSide.BID else self._bid_prices

        # For FOK, first check if fully fillable
        if order.time_in_force == TimeInForce.FOK:
            total_available = FP_ZERO
            for price in prices:
                # Check price limit
                if side == OrderBookSide.BID and price > order.price:
                    break  # Ask price too high for buy limit
                if side == OrderBookSide.ASK and price < order.price:
                    break  # Bid price too low for sell limit
                level = self.get_price_level(opposite_side, price)
                if level:
                    for resting_order in level.orders:
                        if resting_order.is_active:
                            total_available += resting_order.remaining_quantity
            if total_available < order.remaining_quantity:
                return [], order.reject("FOK order not fully fillable")

        for price in prices:
            if remaining_qty <= FP_ZERO:
                break

            # Check price limit
            if side == OrderBookSide.BID and price > order.price:
                break  # Ask price too high for buy limit
            if side == OrderBookSide.ASK and price < order.price:
                break  # Bid price too low for sell limit

            level = self.get_price_level(opposite_side, price)
            if not level:
                continue

            for resting_order in level.orders:
                if remaining_qty <= FP_ZERO:
                    break
                if not resting_order.is_active:
                    continue

                fill_qty = min(remaining_qty, resting_order.remaining_quantity)

                taker_fee = self._calculate_fee(fill_qty, price, self._market.taker_fee)
                maker_fee = self._calculate_fee(fill_qty, price, self._market.maker_fee)

                taker_trade = Trade(
                    order_id=order.id,
                    account_id=order.account_id,
                    market=self._market,
                    symbol=self._market.symbol,
                    side=order.side,
                    quantity=fill_qty,
                    price=price,
                    fee=taker_fee,
                    fee_asset=self._market.quote_asset,
                    is_maker=False,
                )

                maker_trade = Trade(
                    order_id=resting_order.id,
                    account_id=resting_order.account_id,
                    market=self._market,
                    symbol=self._market.symbol,
                    side=resting_order.side,
                    quantity=fill_qty,
                    price=price,
                    fee=maker_fee,
                    fee_asset=self._market.quote_asset,
                    is_maker=True,
                )

                trades.append(taker_trade)
                trades.append(maker_trade)

                updated_taker = order.fill(fill_qty, price, taker_fee)
                updated_maker = resting_order.fill(fill_qty, price, maker_fee)

                order = updated_taker
                remaining_qty = order.remaining_quantity

                new_level = level.update_order(resting_order.id, updated_maker)
                if opposite_side == OrderBookSide.BID:
                    self._bids[price] = new_level
                else:
                    self._asks[price] = new_level

                if updated_maker.is_complete:
                    new_level_after_remove, _ = new_level.remove_order(resting_order.id)
                    if new_level_after_remove.is_empty():
                        self._remove_price_level(opposite_side, price)
                    else:
                        if opposite_side == OrderBookSide.BID:
                            self._bids[price] = new_level_after_remove
                        else:
                            self._asks[price] = new_level_after_remove
                    self._order_index.pop(resting_order.id, None)

                self._last_trade_price = price
                self._last_trade_quantity = fill_qty
                self._sequence += 1
        # Handle remaining
        if remaining_qty > FP_ZERO:
            if order.time_in_force == TimeInForce.IOC:
                if order.filled_quantity > FP_ZERO:
                    order = order.cancel()
                else:
                    order = order.reject("No liquidity for IOC order")
            elif order.time_in_force == TimeInForce.FOK:
                order = order.reject("FOK order not fully fillable")
            else:
                # GTC or GTD - add remaining to book
                order = self.add_order(order)
        else:
            self._sequence += 1

        return trades, order

    def execute_order(self, order: Order) -> tuple[list[Trade], Order]:
        """Execute order (market or limit)."""
        if order.order_type == OrderType.MARKET:
            return self.execute_market_order(order)
        elif order.order_type == OrderType.LIMIT:
            return self.execute_limit_order(order)
        else:
            raise ValueError(f"Unsupported order type: {order.order_type}")

    def _calculate_fee(
        self, quantity: FixedPoint, price: FixedPoint, fee_rate: FixedPoint
    ) -> FixedPoint:
        """Calculate fee for trade."""
        notional = quantity * price
        return (notional * fee_rate).clamp(FP_ZERO, notional)

    def get_depth(self, side: OrderBookSide, levels: int = 10) -> list[PriceLevel]:
        """Get market depth for side up to N levels."""
        if side == OrderBookSide.BID:
            prices = self._bid_prices[:levels]
            return [self._bids[p] for p in prices if p in self._bids]
        else:
            prices = self._ask_prices[:levels]
            return [self._asks[p] for p in prices if p in self._asks]

    def get_full_depth(self) -> tuple[list[PriceLevel], list[PriceLevel]]:
        """Get full order book depth (bids, asks)."""
        bids = [self._bids[p] for p in self._bid_prices if p in self._bids]
        asks = [self._asks[p] for p in self._ask_prices if p in self._asks]
        return bids, asks

    def get_bid_depth(self, levels: int = 10) -> list[PriceLevel]:
        return self.get_depth(OrderBookSide.BID, levels)

    def get_ask_depth(self, levels: int = 10) -> list[PriceLevel]:
        return self.get_depth(OrderBookSide.ASK, levels)

    def snapshot(self) -> OrderBookSnapshot:
        """Create immutable snapshot of current state."""
        bids, asks = self.get_full_depth()
        return OrderBookSnapshot(
            symbol=self._market.symbol,
            timestamp=datetime.now(UTC).replace(tzinfo=None),
            bids=tuple(bids),
            asks=tuple(asks),
            last_trade_price=self._last_trade_price,
            last_trade_quantity=self._last_trade_quantity,
            sequence=self._sequence,
        )

    def __len__(self) -> int:
        return len(self._order_index)

    def __contains__(self, order_id: UUID) -> bool:
        return order_id in self._order_index


__all__ = [
    "OrderBook",
    "OrderBookSide",
    "PriceLevel",
    "OrderBookSnapshot",
]
