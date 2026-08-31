"""Pydantic request and response schemas for the Quantro REST API."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from quantro import OrderSide, OrderType, TimeInForce


class MarketCreate(BaseModel):
    symbol: str
    base_asset: str
    quote_asset: str
    venue: str
    price_precision: int = 8
    quantity_precision: int = 8
    min_order_size: StrictStr = "0"
    max_order_size: StrictStr = "1000000000000000000"
    tick_size: StrictStr = "0.00000001"
    lot_size: StrictStr = "0.00000001"
    maker_fee: StrictStr = "0.001"
    taker_fee: StrictStr = "0.001"
    is_active: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class BalanceResponse(BaseModel):
    asset: str
    free: str
    locked: str
    total: str


class MarketResponse(BaseModel):
    symbol: str
    base_asset: str
    quote_asset: str
    venue: str
    price_precision: int
    quantity_precision: int
    min_order_size: str
    max_order_size: str
    tick_size: str
    lot_size: str
    maker_fee: str
    taker_fee: str
    is_active: bool
    metadata: dict[str, Any]


class AccountCreate(BaseModel):
    name: str
    initial_balances: dict[str, StrictStr] = Field(default_factory=dict)


class AccountResponse(BaseModel):
    id: UUID
    name: str
    balances: dict[str, BalanceResponse]
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any]


class AuthSignupRequest(BaseModel):
    email: str
    password: StrictStr
    name: str | None = None


class AuthLoginRequest(BaseModel):
    email: str
    password: StrictStr


class AuthSessionResponse(BaseModel):
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int | None = None
    user_id: UUID
    account: AccountResponse


class DepositRequest(BaseModel):
    asset: str
    amount: StrictStr


class PositionResponse(BaseModel):
    id: UUID
    account_id: UUID
    symbol: str
    side: str
    size: str
    entry_price: str
    mark_price: str
    unrealized_pnl: str
    realized_pnl: str
    leverage: str
    liquidation_price: str
    opened_at: datetime
    updated_at: datetime
    metadata: dict[str, Any]


class PortfolioResponse(BaseModel):
    account: AccountResponse
    positions: dict[str, PositionResponse]
    open_orders_count: int
    sequence: int
    total_unrealized_pnl: str
    total_realized_pnl: str
    trade_count: int


class OrderCreate(BaseModel):
    account_id: UUID | None = None
    symbol: str
    side: OrderSide
    order_type: OrderType = OrderType.LIMIT
    time_in_force: TimeInForce = TimeInForce.GTC
    quantity: StrictStr
    price: StrictStr = "0"
    stop_price: StrictStr = "0"
    client_order_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(use_enum_values=False)


class OrderResponse(BaseModel):
    id: UUID
    account_id: UUID
    symbol: str
    side: str
    order_type: str
    status: str
    time_in_force: str
    quantity: str
    price: str
    stop_price: str
    filled_quantity: str
    remaining_quantity: str
    average_fill_price: str
    total_fees: str
    client_order_id: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    metadata: dict[str, Any]


class TradeResponse(BaseModel):
    id: UUID
    order_id: UUID
    account_id: UUID
    symbol: str
    side: str
    quantity: str
    price: str
    notional: str
    fee: str
    fee_asset: str
    timestamp: datetime
    is_maker: bool
    metadata: dict[str, Any]


class RiskCheckResponse(BaseModel):
    name: str
    result: str
    message: str
    limit: str | None
    current: str | None


class RiskReportResponse(BaseModel):
    overall: str
    passed: bool
    checks: list[RiskCheckResponse]


class OrderResultResponse(BaseModel):
    accepted: bool
    reject_reason: str | None
    order: OrderResponse
    risk_report: RiskReportResponse
    trades: list[TradeResponse]


class OrdersResponse(BaseModel):
    orders: list[OrderResponse]


class TradesResponse(BaseModel):
    trades: list[TradeResponse]


class BalancesResponse(BaseModel):
    account_id: UUID
    balances: list[BalanceResponse]


class PositionsResponse(BaseModel):
    account_id: UUID
    positions: list[PositionResponse]


class PnlResponse(BaseModel):
    account_id: UUID
    total_unrealized_pnl: str
    total_realized_pnl: str
    total_pnl: str


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class PriceLevelResponse(BaseModel):
    price: str
    total_quantity: str
    order_count: int
    orders: list[OrderResponse]


class OrderBookResponse(BaseModel):
    symbol: str
    sequence: int | None
    best_bid: str | None
    best_ask: str | None
    spread: str | None
    mid_price: str | None
    last_trade_price: str
    last_trade_quantity: str
    bids: list[PriceLevelResponse]
    asks: list[PriceLevelResponse]
    exchange_timestamp: datetime | None = None
    received_timestamp: datetime | None = None
    status: str = "synced"
    venue: str | None = None


class MarketTickerResponse(BaseModel):
    symbol: str
    last_price: str | None
    bid_price: str | None
    ask_price: str | None
    high_24h: str | None
    low_24h: str | None
    volume_24h: str | None
    change_24h: str | None
    mark_price: str | None
    index_price: str | None
    funding_rate: str | None
    open_interest: str | None
    exchange_timestamp: datetime | None
    received_timestamp: datetime
    status: str


class PublicTradeResponse(BaseModel):
    id: str
    symbol: str
    price: str
    quantity: str
    side: str
    exchange_timestamp: datetime | None
    received_timestamp: datetime


class PublicTradesResponse(BaseModel):
    symbol: str
    trades: list[PublicTradeResponse]


class CandleResponse(BaseModel):
    symbol: str
    interval: str
    timestamp: datetime
    open: str
    high: str
    low: str
    close: str
    volume: str
    is_closed: bool


class CandlesResponse(BaseModel):
    symbol: str
    interval: str
    candles: list[CandleResponse]


class EngineSnapshotResponse(BaseModel):
    timestamp: datetime
    sequence: int
    markets: list[MarketResponse]
    portfolio_count: int
    order_book_count: int


class HealthResponse(BaseModel):
    status: str
    service: str
