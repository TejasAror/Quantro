"""Quantro - Deterministic Trading Engine."""

from .engine import (
    EngineSnapshot,
    OrderResult,
    TradingEngine,
    create_engine,
)
from .fixedpoint import FP_ONE, FP_ZERO, FixedPoint, scale_from_decimal, scale_to_decimal
from .models import (
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
from .orderbook import (
    OrderBook,
    OrderBookSide,
    OrderBookSnapshot,
    PriceLevel,
)
from .portfolio import (
    Portfolio,
    PortfolioManager,
    PortfolioSnapshot,
    create_portfolio,
)
from .risk import (
    RiskCheck,
    RiskCheckResult,
    RiskEngine,
    RiskReport,
    create_default_risk_engine,
)

__version__ = "0.1.0"

__all__ = [
    # Fixed-point
    "FixedPoint",
    "FP_ZERO",
    "FP_ONE",
    "scale_from_decimal",
    "scale_to_decimal",
    # Models
    "Market",
    "Account",
    "Balance",
    "Position",
    "PositionSide",
    "Order",
    "OrderSide",
    "OrderType",
    "OrderStatus",
    "TimeInForce",
    "Trade",
    # Order Book
    "OrderBook",
    "OrderBookSide",
    "PriceLevel",
    "OrderBookSnapshot",
    # Risk
    "RiskCheckResult",
    "RiskCheck",
    "RiskReport",
    "RiskEngine",
    "create_default_risk_engine",
    # Portfolio
    "Portfolio",
    "PortfolioManager",
    "PortfolioSnapshot",
    "create_portfolio",
    # Engine
    "TradingEngine",
    "OrderResult",
    "EngineSnapshot",
    "create_engine",
]
