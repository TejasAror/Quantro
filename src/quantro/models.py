"""Core domain models for the Quantro trading engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from .fixedpoint import FP_ONE, FP_ZERO, FixedPoint

if TYPE_CHECKING:
    from typing import Self
else:
    Self = object


def utc_now_naive() -> datetime:
    """Return the current time as a naive UTC datetime (matches datetime.utcnow())."""
    return datetime.now(UTC).replace(tzinfo=None)



class OrderSide(Enum):
    """Order side: BUY or SELL."""

    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    """Order type."""

    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(Enum):
    """Order lifecycle status."""

    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TimeInForce(Enum):
    """Time-in-force for orders."""

    GTC = "gtc"  # Good Till Cancelled
    IOC = "ioc"  # Immediate Or Cancel
    FOK = "fok"  # Fill Or Kill
    GTD = "gtd"  # Good Till Date


class PositionSide(Enum):
    """Position side."""

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(frozen=True, slots=True)
class Market:
    """
    Market definition.

    Represents a trading pair/venue with its specifications.
    """

    symbol: str
    base_asset: str
    quote_asset: str
    venue: str
    price_precision: int = 8
    quantity_precision: int = 8
    min_order_size: FixedPoint = field(default_factory=lambda: FP_ZERO)
    max_order_size: FixedPoint = field(default_factory=lambda: FixedPoint(10**18))
    tick_size: FixedPoint = field(default_factory=lambda: FixedPoint("0.00000001"))
    lot_size: FixedPoint = field(default_factory=lambda: FixedPoint("0.00000001"))
    maker_fee: FixedPoint = field(default_factory=lambda: FixedPoint("0.001"))  # 0.1%
    taker_fee: FixedPoint = field(default_factory=lambda: FixedPoint("0.001"))  # 0.1%
    is_active: bool = True
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("Symbol cannot be empty")
        if not self.base_asset:
            raise ValueError("Base asset cannot be empty")
        if not self.quote_asset:
            raise ValueError("Quote asset cannot be empty")
        if not self.venue:
            raise ValueError("Venue cannot be empty")
        if self.price_precision < 0 or self.price_precision > 18:
            raise ValueError("Price precision must be between 0 and 18")
        if self.quantity_precision < 0 or self.quantity_precision > 18:
            raise ValueError("Quantity precision must be between 0 and 18")
        if self.min_order_size < FP_ZERO:
            raise ValueError("Min order size cannot be negative")
        if self.max_order_size < self.min_order_size:
            raise ValueError("Max order size must be >= min order size")
        if self.tick_size <= FP_ZERO:
            raise ValueError("Tick size must be positive")
        if self.lot_size <= FP_ZERO:
            raise ValueError("Lot size must be positive")
        if self.maker_fee < FP_ZERO or self.maker_fee > FP_ONE:
            raise ValueError("Maker fee must be between 0 and 1")
        if self.taker_fee < FP_ZERO or self.taker_fee > FP_ONE:
            raise ValueError("Taker fee must be between 0 and 1")

    def round_price(self, price: FixedPoint) -> FixedPoint:
        """Round price to tick size."""
        ticks = price // self.tick_size
        return ticks * self.tick_size

    def round_quantity(self, quantity: FixedPoint) -> FixedPoint:
        """Round quantity to lot size."""
        lots = quantity // self.lot_size
        return lots * self.lot_size

    def validate_price(self, price: FixedPoint) -> bool:
        """Validate price against tick size and precision."""
        return price >= self.tick_size and price % self.tick_size == FP_ZERO

    def validate_quantity(self, quantity: FixedPoint) -> bool:
        """Validate quantity against lot size and limits."""
        return (
            quantity >= self.min_order_size
            and quantity <= self.max_order_size
            and quantity % self.lot_size == FP_ZERO
        )


@dataclass(frozen=True, slots=True)
class Balance:
    """
    Account balance for a specific asset.

    All values use FixedPoint for deterministic arithmetic.
    """

    asset: str
    free: FixedPoint = field(default_factory=lambda: FP_ZERO)
    locked: FixedPoint = field(default_factory=lambda: FP_ZERO)

    def __post_init__(self) -> None:
        if not self.asset:
            raise ValueError("Asset cannot be empty")
        if self.free < FP_ZERO:
            raise ValueError("Free balance cannot be negative")
        if self.locked < FP_ZERO:
            raise ValueError("Locked balance cannot be negative")

    @property
    def total(self) -> FixedPoint:
        """Total balance (free + locked)."""
        return self.free + self.locked

    def can_cover(self, amount: FixedPoint) -> bool:
        """Check if free balance can cover amount."""
        return self.free >= amount

    def lock(self, amount: FixedPoint) -> Self:
        """Lock amount from free balance."""
        if not self.can_cover(amount):
            raise ValueError(f"Insufficient free balance: {self.free} < {amount}")
        return Balance(asset=self.asset, free=self.free - amount, locked=self.locked + amount)

    def unlock(self, amount: FixedPoint) -> Self:
        """Unlock amount back to free balance."""
        if self.locked < amount:
            raise ValueError(f"Insufficient locked balance: {self.locked} < {amount}")
        return Balance(asset=self.asset, free=self.free + amount, locked=self.locked - amount)

    def add_free(self, amount: FixedPoint) -> Self:
        """Add to free balance."""
        if amount < FP_ZERO:
            raise ValueError("Cannot add negative amount")
        return Balance(asset=self.asset, free=self.free + amount, locked=self.locked)

    def subtract_free(self, amount: FixedPoint) -> Self:
        """Subtract from free balance."""
        if not self.can_cover(amount):
            raise ValueError(f"Insufficient free balance: {self.free} < {amount}")
        return Balance(asset=self.asset, free=self.free - amount, locked=self.locked)


@dataclass(frozen=True, slots=True)
class Account:
    """
    Trading account with multiple asset balances.

    Immutable - all modifications return new instances.
    """

    id: UUID = field(default_factory=uuid4)
    name: str = ""
    balances: dict[str, Balance] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now_naive)
    updated_at: datetime = field(default_factory=utc_now_naive)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Account name cannot be empty")

    def get_balance(self, asset: str) -> Balance:
        """Get balance for asset, returns zero balance if not found."""
        return self.balances.get(asset, Balance(asset=asset))

    def set_balance(self, balance: Balance) -> Self:
        """Set balance for an asset."""
        new_balances = dict(self.balances)
        new_balances[balance.asset] = balance
        return Account(
            id=self.id,
            name=self.name,
            balances=new_balances,
            created_at=self.created_at,
            updated_at=datetime.now(UTC).replace(tzinfo=None),
            metadata=self.metadata,
        )

    def add_balance(self, balance: Balance) -> Self:
        """Add to existing balance or create new."""
        existing = self.get_balance(balance.asset)
        new_balance = Balance(
            asset=balance.asset,
            free=existing.free + balance.free,
            locked=existing.locked + balance.locked,
        )
        return self.set_balance(new_balance)

    def total_value(self, prices: dict[str, FixedPoint]) -> FixedPoint:
        """Calculate total portfolio value in quote currency."""
        total = FP_ZERO
        for asset, balance in self.balances.items():
            if asset in prices:
                total += balance.total * prices[asset]
        return total


@dataclass(frozen=True, slots=True)
class Position:
    """
    Trading position.

    Tracks size, entry price, unrealized/realized PnL.
    """

    id: UUID = field(default_factory=uuid4)
    account_id: UUID = field(default_factory=uuid4)
    market: Market | None = None
    symbol: str = ""
    side: PositionSide = PositionSide.FLAT
    size: FixedPoint = field(default_factory=lambda: FP_ZERO)
    entry_price: FixedPoint = field(default_factory=lambda: FP_ZERO)
    mark_price: FixedPoint = field(default_factory=lambda: FP_ZERO)
    unrealized_pnl: FixedPoint = field(default_factory=lambda: FP_ZERO)
    realized_pnl: FixedPoint = field(default_factory=lambda: FP_ZERO)
    leverage: FixedPoint = field(default_factory=lambda: FP_ONE)
    liquidation_price: FixedPoint = field(default_factory=lambda: FP_ZERO)
    opened_at: datetime = field(default_factory=utc_now_naive)
    updated_at: datetime = field(default_factory=utc_now_naive)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.symbol and self.market is not None:
            object.__setattr__(self, "symbol", self.market.symbol)
        if not self.symbol:
            raise ValueError("Symbol cannot be empty")
        if self.size < FP_ZERO:
            raise ValueError("Position size cannot be negative")
        if self.side == PositionSide.FLAT and self.size > FP_ZERO:
            raise ValueError("Flat position must have zero size")
        if self.side != PositionSide.FLAT and self.size == FP_ZERO:
            raise ValueError("Non-flat position must have positive size")

    @property
    def notional(self) -> FixedPoint:
        """Position notional value at mark price."""
        return self.size * self.mark_price

    @property
    def is_long(self) -> bool:
        return self.side == PositionSide.LONG

    @property
    def is_short(self) -> bool:
        return self.side == PositionSide.SHORT

    @property
    def is_flat(self) -> bool:
        return self.side == PositionSide.FLAT

    def update_mark_price(self, price: FixedPoint) -> Self:
        """Update mark price and recalculate unrealized PnL."""
        if price < FP_ZERO:
            raise ValueError("Mark price cannot be negative")

        if self.is_long:
            unrealized = (price - self.entry_price) * self.size
        elif self.is_short:
            unrealized = (self.entry_price - price) * self.size
        else:
            unrealized = FP_ZERO

        return Position(
            id=self.id,
            account_id=self.account_id,
            market=self.market,
            symbol=self.symbol,
            side=self.side,
            size=self.size,
            entry_price=self.entry_price,
            mark_price=price,
            unrealized_pnl=unrealized,
            realized_pnl=self.realized_pnl,
            leverage=self.leverage,
            liquidation_price=self.liquidation_price,
            opened_at=self.opened_at,
            updated_at=datetime.now(UTC).replace(tzinfo=None),
            metadata=self.metadata,
        )

    def add_size(self, size: FixedPoint, price: FixedPoint) -> Self:
        """Add to position (increase or open)."""
        if size <= FP_ZERO:
            raise ValueError("Size must be positive")
        if price <= FP_ZERO:
            raise ValueError("Price must be positive")

        if self.is_flat:
            # Opening new position
            new_side = PositionSide.LONG  # Will be determined by order side
            new_entry = price
            new_size = size
        elif self.is_long:
            # Adding to long
            new_side = PositionSide.LONG
            new_size = self.size + size
            # Weighted average entry price
            new_entry = ((self.entry_price * self.size) + (price * size)) / new_size
        elif self.is_short:
            # Adding to short
            new_side = PositionSide.SHORT
            new_size = self.size + size
            new_entry = ((self.entry_price * self.size) + (price * size)) / new_size
        else:
            raise ValueError("Invalid position state")

        return Position(
            id=self.id,
            account_id=self.account_id,
            market=self.market,
            symbol=self.symbol,
            side=new_side,
            size=new_size,
            entry_price=new_entry,
            mark_price=self.mark_price,
            unrealized_pnl=self.unrealized_pnl,
            realized_pnl=self.realized_pnl,
            leverage=self.leverage,
            liquidation_price=self.liquidation_price,
            opened_at=self.opened_at,
            updated_at=datetime.now(UTC).replace(tzinfo=None),
            metadata=self.metadata,
        )

    def reduce_size(self, size: FixedPoint, price: FixedPoint) -> tuple[Self, FixedPoint]:
        """
        Reduce position size.

        Returns new position and realized PnL from the reduction.
        """
        if size <= FP_ZERO:
            raise ValueError("Size must be positive")
        if size > self.size:
            raise ValueError(f"Cannot reduce by {size}, position size is {self.size}")
        if price <= FP_ZERO:
            raise ValueError("Price must be positive")

        new_size = self.size - size

        # Calculate realized PnL
        if self.is_long:
            realized = (price - self.entry_price) * size
        elif self.is_short:
            realized = (self.entry_price - price) * size
        else:
            realized = FP_ZERO

        new_realized = self.realized_pnl + realized

        if new_size == FP_ZERO:
            # Position closed
            new_side = PositionSide.FLAT
            new_entry = FP_ZERO
        else:
            new_side = self.side
            new_entry = self.entry_price

        new_position = Position(
            id=self.id,
            account_id=self.account_id,
            market=self.market,
            symbol=self.symbol,
            side=new_side,
            size=new_size,
            entry_price=new_entry,
            mark_price=price,
            unrealized_pnl=self.unrealized_pnl,
            realized_pnl=new_realized,
            leverage=self.leverage,
            liquidation_price=self.liquidation_price,
            opened_at=self.opened_at,
            updated_at=datetime.now(UTC).replace(tzinfo=None),
            metadata=self.metadata,
        )

        return new_position, realized


@dataclass(frozen=True, slots=True)
class Order:
    """
    Trading order.

    Immutable - all state changes return new instances.
    """

    id: UUID = field(default_factory=uuid4)
    account_id: UUID = field(default_factory=uuid4)
    market: Market | None = None
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    order_type: OrderType = OrderType.LIMIT
    status: OrderStatus = OrderStatus.PENDING
    time_in_force: TimeInForce = TimeInForce.GTC

    # Order parameters
    quantity: FixedPoint = field(default_factory=lambda: FP_ZERO)
    price: FixedPoint = field(default_factory=lambda: FP_ZERO)
    stop_price: FixedPoint = field(default_factory=lambda: FP_ZERO)

    # Execution tracking
    filled_quantity: FixedPoint = field(default_factory=lambda: FP_ZERO)
    average_fill_price: FixedPoint = field(default_factory=lambda: FP_ZERO)
    total_fees: FixedPoint = field(default_factory=lambda: FP_ZERO)

    # Metadata
    client_order_id: str = ""
    created_at: datetime = field(default_factory=utc_now_naive)
    updated_at: datetime = field(default_factory=utc_now_naive)
    expires_at: datetime | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.symbol and self.market is not None:
            object.__setattr__(self, "symbol", self.market.symbol)
        if not self.symbol:
            raise ValueError("Symbol cannot be empty")
        if self.quantity <= FP_ZERO:
            raise ValueError("Quantity must be positive")
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and self.price <= FP_ZERO:
            raise ValueError("Limit price must be positive for limit orders")
        if self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and self.stop_price <= FP_ZERO:
            raise ValueError("Stop price must be positive for stop orders")
        if self.filled_quantity < FP_ZERO:
            raise ValueError("Filled quantity cannot be negative")
        if self.filled_quantity > self.quantity:
            raise ValueError("Filled quantity cannot exceed order quantity")
        if self.total_fees < FP_ZERO:
            raise ValueError("Total fees cannot be negative")

    @property
    def remaining_quantity(self) -> FixedPoint:
        """Quantity remaining to be filled."""
        return self.quantity - self.filled_quantity

    @property
    def is_active(self) -> bool:
        """Check if order is still active."""
        return self.status in (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)

    @property
    def is_complete(self) -> bool:
        """Check if order is in terminal state."""
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )

    @property
    def notional(self) -> FixedPoint:
        """Order notional value."""
        if self.order_type == OrderType.MARKET:
            # For market orders, use a reference price or 0
            return self.quantity * self.price if self.price > FP_ZERO else FP_ZERO
        return self.quantity * self.price

    def fill(self, fill_qty: FixedPoint, fill_price: FixedPoint, fee: FixedPoint) -> Self:
        """Apply a fill to the order."""
        if fill_qty <= FP_ZERO:
            raise ValueError("Fill quantity must be positive")
        if fill_price <= FP_ZERO:
            raise ValueError("Fill price must be positive")
        if fee < FP_ZERO:
            raise ValueError("Fee cannot be negative")
        if self.filled_quantity + fill_qty > self.quantity:
            raise ValueError("Fill would exceed order quantity")

        new_filled = self.filled_quantity + fill_qty

        # Calculate new average fill price (VWAP)
        if self.filled_quantity == FP_ZERO:
            new_avg_price = fill_price
        else:
            new_avg_price = (
                (self.average_fill_price * self.filled_quantity) + (fill_price * fill_qty)
            ) / new_filled

        # Determine new status
        if new_filled >= self.quantity:
            new_status = OrderStatus.FILLED
        else:
            new_status = OrderStatus.PARTIALLY_FILLED

        return Order(
            id=self.id,
            account_id=self.account_id,
            market=self.market,
            symbol=self.symbol,
            side=self.side,
            order_type=self.order_type,
            status=new_status,
            time_in_force=self.time_in_force,
            quantity=self.quantity,
            price=self.price,
            stop_price=self.stop_price,
            filled_quantity=new_filled,
            average_fill_price=new_avg_price,
            total_fees=self.total_fees + fee,
            client_order_id=self.client_order_id,
            created_at=self.created_at,
            updated_at=datetime.now(UTC).replace(tzinfo=None),
            expires_at=self.expires_at,
            metadata=self.metadata,
        )

    def cancel(self) -> Self:
        """Cancel the order."""
        if not self.is_active:
            raise ValueError(f"Cannot cancel order in status {self.status}")
        return Order(
            id=self.id,
            account_id=self.account_id,
            market=self.market,
            symbol=self.symbol,
            side=self.side,
            order_type=self.order_type,
            status=OrderStatus.CANCELLED,
            time_in_force=self.time_in_force,
            quantity=self.quantity,
            price=self.price,
            stop_price=self.stop_price,
            filled_quantity=self.filled_quantity,
            average_fill_price=self.average_fill_price,
            total_fees=self.total_fees,
            client_order_id=self.client_order_id,
            created_at=self.created_at,
            updated_at=datetime.now(UTC).replace(tzinfo=None),
            expires_at=self.expires_at,
            metadata=self.metadata,
        )

    def reject(self, reason: str = "") -> Self:
        """Reject the order."""
        meta = dict(self.metadata)
        if reason:
            meta["reject_reason"] = reason
        return Order(
            id=self.id,
            account_id=self.account_id,
            market=self.market,
            symbol=self.symbol,
            side=self.side,
            order_type=self.order_type,
            status=OrderStatus.REJECTED,
            time_in_force=self.time_in_force,
            quantity=self.quantity,
            price=self.price,
            stop_price=self.stop_price,
            filled_quantity=self.filled_quantity,
            average_fill_price=self.average_fill_price,
            total_fees=self.total_fees,
            client_order_id=self.client_order_id,
            created_at=self.created_at,
            updated_at=datetime.now(UTC).replace(tzinfo=None),
            expires_at=self.expires_at,
            metadata=meta,
        )

    def expire(self) -> Self:
        """Mark order as expired."""
        return Order(
            id=self.id,
            account_id=self.account_id,
            market=self.market,
            symbol=self.symbol,
            side=self.side,
            order_type=self.order_type,
            status=OrderStatus.EXPIRED,
            time_in_force=self.time_in_force,
            quantity=self.quantity,
            price=self.price,
            stop_price=self.stop_price,
            filled_quantity=self.filled_quantity,
            average_fill_price=self.average_fill_price,
            total_fees=self.total_fees,
            client_order_id=self.client_order_id,
            created_at=self.created_at,
            updated_at=datetime.now(UTC).replace(tzinfo=None),
            expires_at=self.expires_at,
            metadata=self.metadata,
        )

    def to_open(self) -> Self:
        """Transition from pending to open."""
        if self.status != OrderStatus.PENDING:
            raise ValueError(f"Can only transition from PENDING, current: {self.status}")
        return Order(
            id=self.id,
            account_id=self.account_id,
            market=self.market,
            symbol=self.symbol,
            side=self.side,
            order_type=self.order_type,
            status=OrderStatus.OPEN,
            time_in_force=self.time_in_force,
            quantity=self.quantity,
            price=self.price,
            stop_price=self.stop_price,
            filled_quantity=self.filled_quantity,
            average_fill_price=self.average_fill_price,
            total_fees=self.total_fees,
            client_order_id=self.client_order_id,
            created_at=self.created_at,
            updated_at=datetime.now(UTC).replace(tzinfo=None),
            expires_at=self.expires_at,
            metadata=self.metadata,
        )


@dataclass(frozen=True, slots=True)
class Trade:
    """
    Executed trade (fill).

    Represents a single execution/fill event.
    """

    id: UUID = field(default_factory=uuid4)
    order_id: UUID = field(default_factory=uuid4)
    account_id: UUID = field(default_factory=uuid4)
    market: Market | None = None
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    quantity: FixedPoint = field(default_factory=lambda: FP_ZERO)
    price: FixedPoint = field(default_factory=lambda: FP_ZERO)
    fee: FixedPoint = field(default_factory=lambda: FP_ZERO)
    fee_asset: str = ""
    timestamp: datetime = field(default_factory=utc_now_naive)
    is_maker: bool = False
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.symbol and self.market is not None:
            object.__setattr__(self, "symbol", self.market.symbol)
        if not self.symbol:
            raise ValueError("Symbol cannot be empty")
        if self.quantity <= FP_ZERO:
            raise ValueError("Trade quantity must be positive")
        if self.price <= FP_ZERO:
            raise ValueError("Trade price must be positive")
        if self.fee < FP_ZERO:
            raise ValueError("Fee cannot be negative")
        if not self.fee_asset:
            # Default fee asset to quote
            if self.market:
                object.__setattr__(self, "fee_asset", self.market.quote_asset)

    @property
    def notional(self) -> FixedPoint:
        """Trade notional value."""
        return self.quantity * self.price

    @property
    def fee_rate(self) -> FixedPoint:
        """Fee rate as fraction of notional."""
        if self.notional == FP_ZERO:
            return FP_ZERO
        return self.fee / self.notional
