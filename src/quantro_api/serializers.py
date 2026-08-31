"""Serialization helpers for frozen Quantro core objects."""

from __future__ import annotations

from quantro import (
    Account,
    Balance,
    Market,
    Order,
    OrderBook,
    Portfolio,
    Position,
    PriceLevel,
    RiskCheck,
    RiskReport,
    Trade,
)

from .schemas import (
    AccountResponse,
    BalanceResponse,
    CandleResponse,
    CandlesResponse,
    MarketResponse,
    MarketTickerResponse,
    OrderBookResponse,
    OrderResponse,
    PortfolioResponse,
    PositionResponse,
    PriceLevelResponse,
    PublicTradeResponse,
    PublicTradesResponse,
    RiskCheckResponse,
    RiskReportResponse,
    TradeResponse,
)


def fixed(value: object | None) -> str | None:
    return None if value is None else str(value)


def balance_response(balance: Balance) -> BalanceResponse:
    return BalanceResponse(
        asset=balance.asset,
        free=str(balance.free),
        locked=str(balance.locked),
        total=str(balance.total),
    )


def market_response(market: Market) -> MarketResponse:
    return MarketResponse(
        symbol=market.symbol,
        base_asset=market.base_asset,
        quote_asset=market.quote_asset,
        venue=market.venue,
        price_precision=market.price_precision,
        quantity_precision=market.quantity_precision,
        min_order_size=str(market.min_order_size),
        max_order_size=str(market.max_order_size),
        tick_size=str(market.tick_size),
        lot_size=str(market.lot_size),
        maker_fee=str(market.maker_fee),
        taker_fee=str(market.taker_fee),
        is_active=market.is_active,
        metadata=market.metadata,
    )


def account_response(account: Account) -> AccountResponse:
    return AccountResponse(
        id=account.id,
        name=account.name,
        balances={asset: balance_response(balance) for asset, balance in account.balances.items()},
        created_at=account.created_at,
        updated_at=account.updated_at,
        metadata=account.metadata,
    )


def position_response(position: Position) -> PositionResponse:
    return PositionResponse(
        id=position.id,
        account_id=position.account_id,
        symbol=position.symbol,
        side=position.side.value,
        size=str(position.size),
        entry_price=str(position.entry_price),
        mark_price=str(position.mark_price),
        unrealized_pnl=str(position.unrealized_pnl),
        realized_pnl=str(position.realized_pnl),
        leverage=str(position.leverage),
        liquidation_price=str(position.liquidation_price),
        opened_at=position.opened_at,
        updated_at=position.updated_at,
        metadata=position.metadata,
    )


def portfolio_response(portfolio: Portfolio) -> PortfolioResponse:
    return PortfolioResponse(
        account=account_response(portfolio.account),
        positions={
            symbol: position_response(position) for symbol, position in portfolio.positions.items()
        },
        open_orders_count=portfolio.open_orders_count,
        sequence=portfolio.sequence,
        total_unrealized_pnl=str(portfolio.get_total_unrealized_pnl()),
        total_realized_pnl=str(portfolio.get_total_realized_pnl()),
        trade_count=len(portfolio.trade_history),
    )


def order_response(order: Order) -> OrderResponse:
    return OrderResponse(
        id=order.id,
        account_id=order.account_id,
        symbol=order.symbol,
        side=order.side.value,
        order_type=order.order_type.value,
        status=order.status.value,
        time_in_force=order.time_in_force.value,
        quantity=str(order.quantity),
        price=str(order.price),
        stop_price=str(order.stop_price),
        filled_quantity=str(order.filled_quantity),
        remaining_quantity=str(order.remaining_quantity),
        average_fill_price=str(order.average_fill_price),
        total_fees=str(order.total_fees),
        client_order_id=order.client_order_id,
        created_at=order.created_at,
        updated_at=order.updated_at,
        expires_at=order.expires_at,
        metadata=order.metadata,
    )


def trade_response(trade: Trade) -> TradeResponse:
    return TradeResponse(
        id=trade.id,
        order_id=trade.order_id,
        account_id=trade.account_id,
        symbol=trade.symbol,
        side=trade.side.value,
        quantity=str(trade.quantity),
        price=str(trade.price),
        notional=str(trade.notional),
        fee=str(trade.fee),
        fee_asset=trade.fee_asset,
        timestamp=trade.timestamp,
        is_maker=trade.is_maker,
        metadata=trade.metadata,
    )


def risk_check_response(check: RiskCheck) -> RiskCheckResponse:
    return RiskCheckResponse(
        name=check.name,
        result=check.result.value,
        message=check.message,
        limit=fixed(check.limit),
        current=fixed(check.current),
    )


def risk_report_response(report: RiskReport) -> RiskReportResponse:
    return RiskReportResponse(
        overall=report.overall.value,
        passed=report.passed,
        checks=[risk_check_response(check) for check in report.checks],
    )


def price_level_response(level: PriceLevel) -> PriceLevelResponse:
    return PriceLevelResponse(
        price=str(level.price),
        total_quantity=str(level.total_quantity),
        order_count=len(level.orders),
        orders=[order_response(order) for order in level.orders],
    )


def order_book_response(order_book: OrderBook, depth: int = 10) -> OrderBookResponse:
    return OrderBookResponse(
        symbol=order_book.symbol,
        sequence=order_book.sequence,
        best_bid=fixed(order_book.best_bid),
        best_ask=fixed(order_book.best_ask),
        spread=fixed(order_book.spread),
        mid_price=fixed(order_book.mid_price),
        last_trade_price=str(order_book.last_trade_price),
        last_trade_quantity=str(order_book.last_trade_quantity),
        bids=[price_level_response(level) for level in order_book.get_bid_depth(depth)],
        asks=[price_level_response(level) for level in order_book.get_ask_depth(depth)],
        venue=order_book.market.venue,
    )


def external_market_response(instrument: object) -> MarketResponse:
    return MarketResponse(
        symbol=instrument.symbol,
        base_asset=instrument.base_asset,
        quote_asset=instrument.quote_asset,
        venue=instrument.venue,
        price_precision=instrument.price_precision,
        quantity_precision=instrument.quantity_precision,
        min_order_size=instrument.min_size,
        max_order_size="0",
        tick_size=instrument.tick_size,
        lot_size=instrument.lot_size,
        maker_fee="0",
        taker_fee="0",
        is_active=instrument.status.lower() in {"live", "trading"},
        metadata={
            **instrument.metadata,
            "instrument_type": instrument.instrument_type,
            "venue_symbol": instrument.venue_symbol,
            "funding_interval": instrument.funding_interval,
            "exchange_timestamp": instrument.exchange_timestamp.isoformat()
            if instrument.exchange_timestamp
            else None,
        },
    )


def external_order_book_response(book: object) -> OrderBookResponse:
    bids = [
        PriceLevelResponse(
            price=level.price,
            total_quantity=level.total_quantity,
            order_count=0,
            orders=[],
        )
        for level in book.bids
    ]
    asks = [
        PriceLevelResponse(
            price=level.price,
            total_quantity=level.total_quantity,
            order_count=0,
            orders=[],
        )
        for level in book.asks
    ]
    return OrderBookResponse(
        symbol=book.symbol,
        sequence=book.sequence,
        best_bid=bids[0].price if bids else None,
        best_ask=asks[0].price if asks else None,
        spread=book.spread,
        mid_price=book.mid_price,
        last_trade_price="0",
        last_trade_quantity="0",
        bids=bids,
        asks=asks,
        exchange_timestamp=book.exchange_timestamp,
        received_timestamp=book.received_timestamp,
        status=str(book.status),
        venue="OKX",
    )


def market_ticker_response(ticker: object) -> MarketTickerResponse:
    return MarketTickerResponse(
        symbol=ticker.symbol,
        last_price=ticker.last_price,
        bid_price=ticker.bid_price,
        ask_price=ticker.ask_price,
        high_24h=ticker.high_24h,
        low_24h=ticker.low_24h,
        volume_24h=ticker.volume_24h,
        change_24h=ticker.change_24h,
        mark_price=ticker.mark_price,
        index_price=ticker.index_price,
        funding_rate=ticker.funding_rate,
        open_interest=ticker.open_interest,
        exchange_timestamp=ticker.exchange_timestamp,
        received_timestamp=ticker.received_timestamp,
        status=str(ticker.status),
    )


def public_trade_response(trade: object) -> PublicTradeResponse:
    return PublicTradeResponse(
        id=trade.id,
        symbol=trade.symbol,
        price=trade.price,
        quantity=trade.quantity,
        side=trade.side,
        exchange_timestamp=trade.exchange_timestamp,
        received_timestamp=trade.received_timestamp,
    )


def public_trades_response(symbol: str, trades: list[object]) -> PublicTradesResponse:
    return PublicTradesResponse(
        symbol=symbol,
        trades=[public_trade_response(trade) for trade in trades],
    )


def candles_response(symbol: str, interval: str, candles: list[object]) -> CandlesResponse:
    return CandlesResponse(
        symbol=symbol,
        interval=interval,
        candles=[
            CandleResponse(
                symbol=candle.symbol,
                interval=candle.interval,
                timestamp=candle.timestamp,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                is_closed=candle.is_closed,
            )
            for candle in candles
        ],
    )
