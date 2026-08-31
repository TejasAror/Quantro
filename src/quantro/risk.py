"""Pre-trade risk checks and risk management."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from .fixedpoint import FP_ZERO, FixedPoint
from .models import (
    Account,
    Market,
    Order,
    OrderSide,
    OrderType,
    Position,
)

if TYPE_CHECKING:
    pass


class RiskCheckResult(Enum):
    """Result of a risk check."""

    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class RiskCheck:
    """Individual risk check result."""

    name: str
    result: RiskCheckResult
    message: str = ""
    limit: FixedPoint | None = None
    current: FixedPoint | None = None


@dataclass(frozen=True, slots=True)
class RiskReport:
    """Complete risk check report."""

    checks: tuple[RiskCheck, ...]
    overall: RiskCheckResult

    @property
    def passed(self) -> bool:
        return self.overall == RiskCheckResult.PASSED

    @property
    def failed_checks(self) -> list[RiskCheck]:
        return [c for c in self.checks if c.result == RiskCheckResult.FAILED]

    @property
    def warnings(self) -> list[RiskCheck]:
        return [c for c in self.checks if c.result == RiskCheckResult.WARNING]


class RiskEngine:
    """
    Pre-trade risk engine.

    Performs deterministic risk checks before order placement.
    All checks use FixedPoint for precise arithmetic.
    """

    def __init__(
        self,
        max_position_size: FixedPoint | None = None,
        max_order_size: FixedPoint | None = None,
        max_exposure: FixedPoint | None = None,
        max_leverage: FixedPoint = FixedPoint("10"),
        allow_short: bool = True,
        max_open_orders: int = 1000,
    ) -> None:
        self._max_position_size = max_position_size or FixedPoint("1000000")  # 1M base units
        self._max_order_size = max_order_size or FixedPoint("100000")  # 100K base units
        self._max_exposure = max_exposure or FixedPoint("10000000")  # 10M quote units
        self._max_leverage = max_leverage
        self._allow_short = allow_short
        self._max_open_orders = max_open_orders

    @property
    def max_position_size(self) -> FixedPoint:
        return self._max_position_size

    @property
    def max_order_size(self) -> FixedPoint:
        return self._max_order_size

    @property
    def max_exposure(self) -> FixedPoint:
        return self._max_exposure

    @property
    def max_leverage(self) -> FixedPoint:
        return self._max_leverage

    @property
    def allow_short(self) -> bool:
        return self._allow_short

    @property
    def max_open_orders(self) -> int:
        return self._max_open_orders

    def check_order(
        self,
        order: Order,
        account: Account,
        market: Market,
        positions: dict[str, Position] | None = None,
        open_orders_count: int = 0,
        mark_prices: dict[str, FixedPoint] | None = None,
    ) -> RiskReport:
        """
        Run all pre-trade risk checks for an order.

        Args:
            order: Order to check
            account: Account placing the order
            market: Market for the order
            positions: Current positions (symbol -> Position)
            open_orders_count: Number of currently open orders for account
            mark_prices: Current mark prices for exposure calculation

        Returns:
            RiskReport with all check results
        """
        checks: list[RiskCheck] = []

        # 1. Order size limits
        checks.append(self._check_order_size(order, market))

        # 2. Available balance
        checks.append(self._check_available_balance(order, account, market))

        # 3. Position limits
        if positions is not None:
            checks.append(self._check_position_limits(order, account, market, positions))

        # 4. Exposure limits
        if mark_prices is not None and positions is not None:
            checks.append(
                self._check_exposure_limits(order, account, market, positions, mark_prices)
            )

        # 5. Leverage limits
        if positions is not None and mark_prices is not None:
            checks.append(
                self._check_leverage_limits(order, account, market, positions, mark_prices)
            )

        # 6. Short selling permission
        if not self._allow_short:
            checks.append(self._check_short_selling(order))

        # 7. Max open orders
        checks.append(self._check_max_open_orders(open_orders_count))

        # 8. Market-specific limits
        checks.append(self._check_market_limits(order, market))

        # 9. Order validity
        checks.append(self._check_order_validity(order, market))

        # Determine overall result
        failed = any(c.result == RiskCheckResult.FAILED for c in checks)
        warning = any(c.result == RiskCheckResult.WARNING for c in checks)

        if failed:
            overall = RiskCheckResult.FAILED
        elif warning:
            overall = RiskCheckResult.WARNING
        else:
            overall = RiskCheckResult.PASSED

        return RiskReport(checks=tuple(checks), overall=overall)

    def _check_order_size(self, order: Order, market: Market) -> RiskCheck:
        """Check order size against limits."""
        max_allowed = min(self._max_order_size, market.max_order_size)
        min_allowed = market.min_order_size

        if order.quantity > max_allowed:
            return RiskCheck(
                name="max_order_size",
                result=RiskCheckResult.FAILED,
                message=f"Order quantity {order.quantity} exceeds max {max_allowed}",
                limit=max_allowed,
                current=order.quantity,
            )

        if order.quantity < min_allowed:
            return RiskCheck(
                name="min_order_size",
                result=RiskCheckResult.FAILED,
                message=f"Order quantity {order.quantity} below min {min_allowed}",
                limit=min_allowed,
                current=order.quantity,
            )

        # Warning at 80% of max
        if order.quantity > max_allowed * FixedPoint("0.8"):
            return RiskCheck(
                name="max_order_size_warning",
                result=RiskCheckResult.WARNING,
                message=f"Order quantity {order.quantity} approaching max {max_allowed}",
                limit=max_allowed,
                current=order.quantity,
            )

        return RiskCheck(name="order_size", result=RiskCheckResult.PASSED)

    def _check_available_balance(self, order: Order, account: Account, market: Market) -> RiskCheck:
        """Check if account has sufficient balance for order."""
        if order.side == OrderSide.BUY:
            # Need quote asset (e.g., USD) for buy orders
            required_asset = market.quote_asset
            # Estimate required funds: quantity * price + fees
            price = (
                order.price if order.price > FP_ZERO else (market.tick_size * FixedPoint("10000"))
            )  # Fallback
            required = order.quantity * price
            # Add estimated fees (taker fee)
            required = required + (required * market.taker_fee)
        else:
            # Need base asset (e.g., BTC) for sell orders
            required_asset = market.base_asset
            required = order.quantity

        balance = account.get_balance(required_asset)

        if not balance.can_cover(required):
            return RiskCheck(
                name="available_balance",
                result=RiskCheckResult.FAILED,
                message=f"Insufficient {required_asset} balance: {balance.free} < {required}",
                limit=balance.free,
                current=required,
            )

        # Warning at 80% of free balance
        if required > balance.free * FixedPoint("0.8"):
            return RiskCheck(
                name="available_balance_warning",
                result=RiskCheckResult.WARNING,
                message=f"Order uses {required} of {balance.free} free {required_asset}",
                limit=balance.free,
                current=required,
            )

        return RiskCheck(name="available_balance", result=RiskCheckResult.PASSED)

    def _check_position_limits(
        self,
        order: Order,
        account: Account,
        market: Market,
        positions: dict[str, Position],
    ) -> RiskCheck:
        """Check position size limits."""
        position = positions.get(market.symbol)

        if position is None or position.is_flat:
            # New position
            new_size = order.quantity
        elif order.side == OrderSide.BUY:
            if position.is_long:
                new_size = position.size + order.quantity
            else:
                # Reducing short
                new_size = (
                    position.size - order.quantity
                    if position.size > order.quantity
                    else order.quantity - position.size
                )
        else:  # SELL
            if position.is_short:
                new_size = position.size + order.quantity
            else:
                # Reducing long
                new_size = (
                    position.size - order.quantity
                    if position.size > order.quantity
                    else order.quantity - position.size
                )

        if new_size > self._max_position_size:
            return RiskCheck(
                name="max_position_size",
                result=RiskCheckResult.FAILED,
                message=f"Position size {new_size} would exceed max {self._max_position_size}",
                limit=self._max_position_size,
                current=new_size,
            )

        return RiskCheck(name="position_size", result=RiskCheckResult.PASSED)

    def _check_exposure_limits(
        self,
        order: Order,
        account: Account,
        market: Market,
        positions: dict[str, Position],
        mark_prices: dict[str, FixedPoint],
    ) -> RiskCheck:
        """Check total portfolio exposure limits."""
        # Calculate current exposure
        current_exposure = FP_ZERO
        for symbol, pos in positions.items():
            if not pos.is_flat and symbol in mark_prices:
                current_exposure += pos.size * mark_prices[symbol]

        # Add this order's exposure
        price = order.price if order.price > FP_ZERO else mark_prices.get(market.symbol, FP_ZERO)
        if price > FP_ZERO:
            order_exposure = order.quantity * price
            new_exposure = current_exposure + order_exposure

            if new_exposure > self._max_exposure:
                return RiskCheck(
                    name="max_exposure",
                    result=RiskCheckResult.FAILED,
                    message=f"Total exposure {new_exposure} would exceed max {self._max_exposure}",
                    limit=self._max_exposure,
                    current=new_exposure,
                )

        return RiskCheck(name="exposure", result=RiskCheckResult.PASSED)

    def _check_leverage_limits(
        self,
        order: Order,
        account: Account,
        market: Market,
        positions: dict[str, Position],
        mark_prices: dict[str, FixedPoint],
    ) -> RiskCheck:
        """Check leverage limits."""
        # Convert market-symbol-keyed mark_prices to asset-symbol-keyed for equity calc
        # mark_prices uses market symbols (e.g., "BTC-USD"), but Account.total_value
        # expects asset symbols (e.g., "BTC", "USD")
        asset_prices: dict[str, FixedPoint] = {}
        for sym, price in mark_prices.items():
            if "-" in sym:
                base, quote = sym.split("-", 1)
                asset_prices[base] = price
                asset_prices[quote] = FixedPoint("1.0")  # Quote currency = 1.0
        # Ensure this market's assets are covered
        asset_prices.setdefault(market.quote_asset, FixedPoint("1.0"))
        asset_prices.setdefault(
            market.base_asset, mark_prices.get(market.symbol, FixedPoint("1.0"))
        )

        # Calculate account equity using asset-symbol-keyed prices
        equity = account.total_value(asset_prices)
        if equity <= FP_ZERO:
            return RiskCheck(
                name="leverage",
                result=RiskCheckResult.WARNING,
                message="Account equity is zero or negative",
            )

        # Calculate current leveraged exposure
        leveraged_exposure = FP_ZERO
        for symbol, pos in positions.items():
            if not pos.is_flat and symbol in mark_prices:
                leveraged_exposure += pos.size * mark_prices[symbol] * pos.leverage

        # Add order leverage
        price = order.price if order.price > FP_ZERO else mark_prices.get(market.symbol, FP_ZERO)
        if price > FP_ZERO:
            order_leverage = order.quantity * price * FixedPoint("1")  # Assume 1x for new order
            new_leverage = (leveraged_exposure + order_leverage) / equity

            if new_leverage > self._max_leverage:
                return RiskCheck(
                    name="max_leverage",
                    result=RiskCheckResult.FAILED,
                    message=f"Leverage {new_leverage} would exceed max {self._max_leverage}",
                    limit=self._max_leverage,
                    current=new_leverage,
                )

        return RiskCheck(name="leverage", result=RiskCheckResult.PASSED)

    def _check_short_selling(self, order: Order) -> RiskCheck:
        """Check if short selling is allowed."""
        if order.side == OrderSide.SELL:
            # For spot markets, selling requires holding the asset
            # This is checked in available_balance
            return RiskCheck(name="short_selling", result=RiskCheckResult.PASSED)
        return RiskCheck(name="short_selling", result=RiskCheckResult.PASSED)

    def _check_max_open_orders(self, open_orders_count: int) -> RiskCheck:
        """Check max open orders limit."""
        if open_orders_count >= self._max_open_orders:
            return RiskCheck(
                name="max_open_orders",
                result=RiskCheckResult.FAILED,
                message=f"Max open orders {self._max_open_orders} reached",
                limit=FixedPoint(self._max_open_orders),
                current=FixedPoint(open_orders_count),
            )
        return RiskCheck(name="max_open_orders", result=RiskCheckResult.PASSED)

    def _check_market_limits(self, order: Order, market: Market) -> RiskCheck:
        """Check market-specific limits."""
        if not market.is_active:
            return RiskCheck(
                name="market_active",
                result=RiskCheckResult.FAILED,
                message=f"Market {market.symbol} is not active",
            )

        if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            if not market.validate_price(order.price):
                return RiskCheck(
                    name="price_precision",
                    result=RiskCheckResult.FAILED,
                    message=f"Price {order.price} invalid for market tick size {market.tick_size}",
                )

        if not market.validate_quantity(order.quantity):
            return RiskCheck(
                name="quantity_precision",
                result=RiskCheckResult.FAILED,
                message=f"Quantity {order.quantity} invalid for market lot size {market.lot_size}",
            )

        return RiskCheck(name="market_limits", result=RiskCheckResult.PASSED)

    def _check_order_validity(self, order: Order, market: Market) -> RiskCheck:
        """Check order validity."""
        if order.quantity <= FP_ZERO:
            return RiskCheck(
                name="quantity_positive",
                result=RiskCheckResult.FAILED,
                message="Order quantity must be positive",
            )

        if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and order.price <= FP_ZERO:
            return RiskCheck(
                name="price_positive",
                result=RiskCheckResult.FAILED,
                message="Limit price must be positive",
            )

        return RiskCheck(name="order_validity", result=RiskCheckResult.PASSED)


def create_default_risk_engine() -> RiskEngine:
    """Create a risk engine with sensible defaults."""
    return RiskEngine()


__all__ = [
    "RiskCheckResult",
    "RiskCheck",
    "RiskReport",
    "RiskEngine",
    "create_default_risk_engine",
]
