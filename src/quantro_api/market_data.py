"""Read-only external market data providers for Quantro."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
from typing import Any, Protocol, cast

import httpx

logger = logging.getLogger(__name__)

OKX_REST_BASE_URL = "https://www.okx.com"
OKX_WS_PUBLIC_URL = "wss://ws.okx.com:8443/ws/v5/public"
OKX_WS_BUSINESS_URL = "wss://ws.okx.com:8443/ws/v5/business"
OKX_INTERVALS = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "4h": "4H",
    "1d": "1D",
}


class MarketDataStatus(StrEnum):
    SYNCING = "syncing"
    SYNCED = "synced"
    STALE = "stale"
    RESYNCING = "resyncing"
    DISCONNECTED = "disconnected"
    UNAVAILABLE = "unavailable"


class MarketDataUnavailableError(RuntimeError):
    """Raised when the external market-data provider cannot supply real data."""


class SequenceGapError(RuntimeError):
    """Raised when a local order book receives a non-contiguous delta."""


class MarketDataIntegrityError(RuntimeError):
    """Raised when normalized market data violates execution-safety invariants."""


@dataclass(frozen=True, slots=True)
class RawMarketDataEvent:
    provider: str
    venue: str
    channel: str
    symbol: str
    event_type: str
    sequence_id: int | None
    previous_sequence_id: int | None
    exchange_timestamp: datetime | None
    received_timestamp: datetime
    raw_payload: dict[str, Any] | list[Any]


class RawEventJournal:
    """Bounded in-process raw market-data event journal."""

    def __init__(self, max_events: int = 5000) -> None:
        self._events: deque[RawMarketDataEvent] = deque(maxlen=max_events)

    def append(self, event: RawMarketDataEvent) -> None:
        self._events.append(event)

    def recent(self, limit: int = 100) -> list[RawMarketDataEvent]:
        return list(self._events)[-limit:]


@dataclass(frozen=True, slots=True)
class NormalizedInstrument:
    symbol: str
    venue: str
    venue_symbol: str
    base_asset: str
    quote_asset: str
    instrument_type: str
    contract_multiplier: str | None
    tick_size: str
    lot_size: str
    min_size: str
    price_precision: int
    quantity_precision: int
    funding_interval: str | None
    status: str
    exchange_timestamp: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MarketDataLevel:
    price: str
    total_quantity: str


@dataclass(frozen=True, slots=True)
class NormalizedOrderBook:
    symbol: str
    bids: list[MarketDataLevel]
    asks: list[MarketDataLevel]
    sequence: int | None
    exchange_timestamp: datetime | None
    received_timestamp: datetime
    spread: str | None
    mid_price: str | None
    status: MarketDataStatus


@dataclass(frozen=True, slots=True)
class NormalizedTicker:
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
    status: MarketDataStatus = MarketDataStatus.SYNCED


@dataclass(frozen=True, slots=True)
class CachedSupplementalTickerField:
    value: str
    received_timestamp: datetime


@dataclass(frozen=True, slots=True)
class NormalizedTrade:
    id: str
    symbol: str
    price: str
    quantity: str
    side: str
    exchange_timestamp: datetime | None
    received_timestamp: datetime


@dataclass(frozen=True, slots=True)
class NormalizedCandle:
    symbol: str
    interval: str
    timestamp: datetime
    open: str
    high: str
    low: str
    close: str
    volume: str
    is_closed: bool


class MarketDataProvider(Protocol):
    venue: str

    def supported_symbols(self) -> set[str]: ...

    async def get_instruments(self) -> list[NormalizedInstrument]: ...

    async def get_ticker(self, symbol: str) -> NormalizedTicker: ...

    async def get_orderbook_snapshot(self, symbol: str, depth: int) -> NormalizedOrderBook: ...

    async def get_recent_trades(self, symbol: str, limit: int) -> list[NormalizedTrade]: ...

    async def get_candles(
        self,
        symbol: str,
        interval: str,
        limit: int,
    ) -> list[NormalizedCandle]: ...

    def subscribe(self, symbols: list[str]) -> AsyncIterator[RawMarketDataEvent]: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_exchange_time(value: str | int | None) -> datetime | None:
    if value in (None, ""):
        return None
    return datetime.fromtimestamp(int(value) / 1000, UTC)


def decimal_or_none(value: str | None) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def decimal_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def precision_from_step(value: str) -> int:
    step = Decimal(value)
    exponent = step.normalize().as_tuple().exponent
    return max(abs(int(exponent)), 0)


def okx_api_error(payload: dict[str, Any]) -> bool:
    return payload.get("code") not in (None, "0")


class LocalOrderBookBuilder:
    """Local L2 book with OKX seqId/prevSeqId continuity validation."""

    def __init__(self, symbol: str, stale_after_seconds: float = 10.0) -> None:
        self.symbol = symbol
        self.status = MarketDataStatus.SYNCING
        self.last_sequence: int | None = None
        self.last_exchange_timestamp: datetime | None = None
        self.last_received_timestamp: datetime | None = None
        self.resync_count = 0
        self.sequence_gap_count = 0
        self._bids: dict[str, Decimal] = {}
        self._asks: dict[str, Decimal] = {}
        self._stale_after_seconds = stale_after_seconds

    def apply_snapshot(
        self,
        bids: list[list[str]],
        asks: list[list[str]],
        sequence: int | None,
        exchange_timestamp: datetime | None,
        received_timestamp: datetime | None = None,
    ) -> None:
        self._bids = self._levels_from_rows(bids)
        self._asks = self._levels_from_rows(asks)
        self.last_sequence = sequence
        self.last_exchange_timestamp = exchange_timestamp
        self.last_received_timestamp = received_timestamp or utc_now()
        self.status = MarketDataStatus.SYNCED
        self._validate_book_integrity()

    def apply_delta(
        self,
        bids: list[list[str]],
        asks: list[list[str]],
        sequence: int | None,
        previous_sequence: int | None,
        exchange_timestamp: datetime | None,
        received_timestamp: datetime | None = None,
    ) -> None:
        if sequence is None:
            raise SequenceGapError("Missing sequence id")
        if self.last_sequence is None:
            self.status = MarketDataStatus.RESYNCING
            raise SequenceGapError("Delta received before snapshot")
        if sequence <= self.last_sequence:
            return
        if previous_sequence is not None and previous_sequence != self.last_sequence:
            self.status = MarketDataStatus.RESYNCING
            self.sequence_gap_count += 1
            raise SequenceGapError(
                f"Order book sequence gap for {self.symbol}: expected prevSeqId "
                f"{self.last_sequence}, got {previous_sequence}",
            )

        self._apply_levels(self._bids, bids)
        self._apply_levels(self._asks, asks)
        self.last_sequence = sequence
        self.last_exchange_timestamp = exchange_timestamp
        self.last_received_timestamp = received_timestamp or utc_now()
        self.status = MarketDataStatus.SYNCED
        self._validate_book_integrity()

    def mark_disconnected(self) -> None:
        self.status = MarketDataStatus.DISCONNECTED

    def mark_resyncing(self) -> None:
        self.status = MarketDataStatus.RESYNCING
        self.resync_count += 1

    def snapshot(self, depth: int) -> NormalizedOrderBook:
        status = self._fresh_status()
        bids = [
            MarketDataLevel(price=price, total_quantity=decimal_str(quantity) or "0")
            for price, quantity in sorted(
                self._bids.items(),
                key=lambda item: Decimal(item[0]),
                reverse=True,
            )[:depth]
        ]
        asks = [
            MarketDataLevel(price=price, total_quantity=decimal_str(quantity) or "0")
            for price, quantity in sorted(
                self._asks.items(),
                key=lambda item: Decimal(item[0]),
            )[:depth]
        ]
        best_bid = decimal_or_none(bids[0].price) if bids else None
        best_ask = decimal_or_none(asks[0].price) if asks else None
        spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
        mid = (
            (best_bid + best_ask) / Decimal("2")
            if best_bid is not None and best_ask is not None
            else None
        )
        return NormalizedOrderBook(
            symbol=self.symbol,
            bids=bids,
            asks=asks,
            sequence=self.last_sequence,
            exchange_timestamp=self.last_exchange_timestamp,
            received_timestamp=self.last_received_timestamp or utc_now(),
            spread=decimal_str(spread),
            mid_price=decimal_str(mid),
            status=status,
        )

    def can_publish_to_simulator(self) -> bool:
        return self._fresh_status() == MarketDataStatus.SYNCED and not self._is_crossed()

    def state_hash(self, depth: int = 100) -> str:
        snapshot = self.snapshot(depth)
        payload = {
            "symbol": snapshot.symbol,
            "sequence": snapshot.sequence,
            "bids": [(level.price, level.total_quantity) for level in snapshot.bids],
            "asks": [(level.price, level.total_quantity) for level in snapshot.asks],
        }
        return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _fresh_status(self) -> MarketDataStatus:
        if self.status != MarketDataStatus.SYNCED or self.last_received_timestamp is None:
            return self.status
        age = (utc_now() - self.last_received_timestamp).total_seconds()
        if age > self._stale_after_seconds:
            return MarketDataStatus.STALE
        return MarketDataStatus.SYNCED

    def _validate_book_integrity(self) -> None:
        if self._is_crossed():
            self.status = MarketDataStatus.RESYNCING
            raise MarketDataIntegrityError(f"Crossed order book for {self.symbol}")

    def _is_crossed(self) -> bool:
        if not self._bids or not self._asks:
            return False
        best_bid = max(Decimal(price) for price in self._bids)
        best_ask = min(Decimal(price) for price in self._asks)
        return best_bid >= best_ask

    @staticmethod
    def _levels_from_rows(rows: list[list[str]]) -> dict[str, Decimal]:
        levels: dict[str, Decimal] = {}
        for row in rows:
            if len(row) < 2:
                raise MarketDataIntegrityError("Malformed order-book level")
            price = Decimal(row[0])
            quantity = Decimal(row[1])
            if price <= 0 or quantity < 0:
                raise MarketDataIntegrityError("Invalid order-book price or quantity")
            if quantity > 0:
                levels[row[0]] = quantity
        return levels

    @staticmethod
    def _apply_levels(side: dict[str, Decimal], rows: list[list[str]]) -> None:
        for row in rows:
            if len(row) < 2:
                raise MarketDataIntegrityError("Malformed order-book level")
            price = Decimal(row[0])
            quantity = Decimal(row[1])
            if price <= 0 or quantity < 0:
                raise MarketDataIntegrityError("Invalid order-book price or quantity")
            if quantity <= 0:
                side.pop(row[0], None)
            else:
                side[row[0]] = quantity


class SimulatorMarketDataGate:
    """Expose only validated canonical book state to simulators.

    Frontend snapshot consumers may coalesce on their own cursor. Simulation advances only from
    a fresh, sequence-valid, non-crossed canonical state.
    """

    def __init__(self, builder: LocalOrderBookBuilder) -> None:
        self.builder = builder
        self.simulator_event_cursor: int | None = None
        self.frontend_snapshot_cursor: int | None = None
        self.blocked_count = 0

    def frontend_snapshot(self, depth: int) -> NormalizedOrderBook:
        snapshot = self.builder.snapshot(depth)
        self.frontend_snapshot_cursor = snapshot.sequence
        return snapshot

    def canonical_state_for_simulator(self, depth: int) -> NormalizedOrderBook:
        snapshot = self.builder.snapshot(depth)
        if not self.builder.can_publish_to_simulator():
            self.blocked_count += 1
            raise MarketDataUnavailableError("Market data is not healthy for simulator consumption")
        self.simulator_event_cursor = snapshot.sequence
        return snapshot


class OKXMarketDataProvider:
    """OKX public/read-only market-data adapter."""

    venue = "OKX"
    PUBLIC_WS_CHANNELS = ("tickers", "books", "trades")
    BUSINESS_WS_CHANNELS = ("candle1m",)
    WS_IDLE_TIMEOUT_SECONDS = 25.0

    def __init__(
        self,
        symbols: list[str],
        rest_base_url: str = OKX_REST_BASE_URL,
        ws_public_url: str = OKX_WS_PUBLIC_URL,
        ws_business_url: str | None = None,
        timeout: float = 10.0,
        journal: RawEventJournal | None = None,
    ) -> None:
        self._symbols = set(symbols)
        self._rest_base_url = rest_base_url.rstrip("/")
        self._ws_public_url = ws_public_url
        self._ws_business_url = ws_business_url or self._business_url_from_public_url(ws_public_url)
        self._timeout = timeout
        self._journal = journal or RawEventJournal()
        self._http_client = httpx.AsyncClient(timeout=self._timeout)
        self._supplemental_cache: dict[tuple[str, str], CachedSupplementalTickerField] = {}
        self._supplemental_poll_after_seconds = 5.0
        self._supplemental_stale_after_seconds = 15.0
        self._supplemental_refresh_semaphore = asyncio.Semaphore(4)
        self._supplemental_refresh_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._instruments_cache: list[NormalizedInstrument] | None = None
        self._instruments_cache_timestamp: datetime | None = None
        self._instruments_cache_ttl_seconds = 60.0

    @property
    def journal(self) -> RawEventJournal:
        return self._journal

    def supported_symbols(self) -> set[str]:
        return set(self._symbols)

    async def get_instruments(self) -> list[NormalizedInstrument]:
        now = utc_now()

        if (
            self._instruments_cache is not None
            and self._instruments_cache_timestamp is not None
            and (now - self._instruments_cache_timestamp).total_seconds()
            < self._instruments_cache_ttl_seconds
        ):
            return list(self._instruments_cache)

        payload = await self._get(
            "/api/v5/public/instruments",
            {"instType": "SWAP"},
        )
        rows = [
            row
            for row in payload.get("data", [])
            if row.get("instId") in self._symbols
        ]
        instruments = [self._instrument_from_okx(row) for row in rows]

        self._instruments_cache = instruments
        self._instruments_cache_timestamp = now

        return list(instruments)

    async def get_ticker(self, symbol: str) -> NormalizedTicker:
        self._ensure_symbol(symbol)
        received = utc_now()
        payload = await self._get("/api/v5/market/ticker", {"instId": symbol})
        row = self._first_data(payload, f"ticker unavailable for {symbol}")
        self._record("tickers", symbol, "snapshot", row, received)
        last = decimal_or_none(row.get("last"))
        open_24h = decimal_or_none(row.get("open24h"))
        change_24h = (
            decimal_str(((last - open_24h) / open_24h) * Decimal("100"))
            if last is not None and open_24h not in (None, Decimal("0"))
            else None
        )
        mark_price = await self._get_supplemental_field(symbol, "mark_price", "/api/v5/public/mark-price", {"instType": "SWAP", "instId": symbol}, "markPx", received)
        index_price = await self._get_supplemental_field(symbol, "index_price", "/api/v5/market/index-tickers", {"instId": self._index_symbol(symbol)}, "idxPx", received)
        funding_rate = await self._get_supplemental_field(symbol, "funding_rate", "/api/v5/public/funding-rate", {"instId": symbol}, "fundingRate", received)
        open_interest = await self._get_supplemental_field(symbol, "open_interest", "/api/v5/public/open-interest", {"instType": "SWAP", "instId": symbol}, "oi", received)
        return NormalizedTicker(
            symbol=symbol,
            last_price=row.get("last") or None,
            bid_price=row.get("bidPx") or None,
            ask_price=row.get("askPx") or None,
            high_24h=row.get("high24h") or None,
            low_24h=row.get("low24h") or None,
            volume_24h=row.get("vol24h") or None,
            change_24h=change_24h,
            mark_price=mark_price,
            index_price=index_price,
            funding_rate=funding_rate,
            open_interest=open_interest,
            exchange_timestamp=parse_exchange_time(row.get("ts")),
            received_timestamp=received,
        )

    async def get_orderbook_snapshot(self, symbol: str, depth: int) -> NormalizedOrderBook:
        self._ensure_symbol(symbol)
        received = utc_now()
        payload = await self._get("/api/v5/market/books", {"instId": symbol, "sz": str(depth)})
        row = self._first_data(payload, f"order book unavailable for {symbol}")
        self._record("books", symbol, "snapshot", row, received)
        builder = LocalOrderBookBuilder(symbol)
        builder.apply_snapshot(
            bids=row.get("bids", []),
            asks=row.get("asks", []),
            sequence=int(row["seqId"]) if row.get("seqId") not in (None, "") else None,
            exchange_timestamp=parse_exchange_time(row.get("ts")),
            received_timestamp=received,
        )
        return builder.snapshot(depth)

    async def get_recent_trades(self, symbol: str, limit: int) -> list[NormalizedTrade]:
        self._ensure_symbol(symbol)
        received = utc_now()
        payload = await self._get("/api/v5/market/trades", {"instId": symbol, "limit": str(limit)})
        rows = payload.get("data", [])
        for row in rows:
            self._record("trades", symbol, "snapshot", row, received)
        return [
            NormalizedTrade(
                id=str(row.get("tradeId") or f"{symbol}-{row.get('ts')}-{index}"),
                symbol=symbol,
                price=str(row["px"]),
                quantity=str(row["sz"]),
                side=str(row.get("side") or "unknown").lower(),
                exchange_timestamp=parse_exchange_time(row.get("ts")),
                received_timestamp=received,
            )
            for index, row in enumerate(rows[:limit])
            if row.get("px") is not None and row.get("sz") is not None
        ]

    async def get_candles(
        self,
        symbol: str,
        interval: str,
        limit: int,
    ) -> list[NormalizedCandle]:
        self._ensure_symbol(symbol)
        okx_interval = OKX_INTERVALS.get(interval.lower())
        if okx_interval is None:
            raise ValueError("Unsupported candle interval")
        payload = await self._get(
            "/api/v5/market/candles",
            {"instId": symbol, "bar": okx_interval, "limit": str(limit)},
        )
        rows = payload.get("data", [])
        received = utc_now()
        for row in rows:
            self._record("candles", symbol, "snapshot", {"row": row}, received)
        candles = [
            candle
            for row in rows[:limit]
            if (candle := self._candle_from_okx_row(symbol, interval, row, received)) is not None
        ]
        return sorted(candles, key=lambda candle: candle.timestamp)

    async def subscribe(self, symbols: list[str]) -> AsyncIterator[RawMarketDataEvent]:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - uvicorn[standard] supplies this in app envs
            raise MarketDataUnavailableError("WebSocket client dependency is unavailable") from exc

        queue: asyncio.Queue[RawMarketDataEvent] = asyncio.Queue()
        tasks = [
            asyncio.create_task(
                self._subscribe_connection(
                    websockets,
                    self._ws_public_url,
                    self._subscription_args(symbols, self.PUBLIC_WS_CHANNELS),
                    queue,
                ),
                name="okx-public-market-data",
            ),
            asyncio.create_task(
                self._subscribe_connection(
                    websockets,
                    self._ws_business_url,
                    self._subscription_args(symbols, self.BUSINESS_WS_CHANNELS),
                    queue,
                ),
                name="okx-business-market-data",
            ),
        ]
        get_task = asyncio.create_task(queue.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    [get_task, *tasks],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if get_task in done:
                    event = get_task.result()
                    get_task = asyncio.create_task(queue.get())
                    yield event
                for task in tasks:
                    if task in done:
                        task_exception = task.exception()
                        if task_exception is not None:
                            raise task_exception
                        raise ConnectionError("OKX WebSocket stream ended")
        finally:
            get_task.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(get_task, *tasks, return_exceptions=True)

    async def _subscribe_connection(
        self,
        websockets: Any,
        url: str,
        args: list[dict[str, str]],
        queue: asyncio.Queue[RawMarketDataEvent],
    ) -> None:
        async with websockets.connect(url, ping_interval=None) as ws:
            await ws.send(json.dumps({"op": "subscribe", "args": args}))
            while True:
                message = await self._recv_okx_message(ws)
                if message == "pong":
                    continue
                event_payloads = self._events_from_ws_message(message)
                for event in event_payloads:
                    self._journal.append(event)
                    await queue.put(event)

    async def _recv_okx_message(self, ws: Any) -> str:
        try:
            return str(await asyncio.wait_for(ws.recv(), timeout=self.WS_IDLE_TIMEOUT_SECONDS))
        except TimeoutError:
            await ws.send("ping")
            pong = str(await asyncio.wait_for(ws.recv(), timeout=self.WS_IDLE_TIMEOUT_SECONDS))
            if pong != "pong":
                raise ConnectionError("OKX WebSocket heartbeat failed")
            return pong

    def _events_from_ws_message(self, message: str) -> list[RawMarketDataEvent]:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("Ignoring malformed OKX WebSocket message")
            return []
        if not isinstance(payload, dict):
            return []
        if payload.get("event") == "notice" and payload.get("code") == "64008":
            raise ConnectionError("OKX WebSocket service upgrade notice")
        if payload.get("event") == "error":
            raise MarketDataUnavailableError(
                str(payload.get("msg") or "OKX WebSocket subscription failed")
            )
        arg = payload.get("arg", {})
        if not isinstance(arg, dict):
            return []
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        channel = str(arg.get("channel") or "unknown")
        symbol = str(arg.get("instId") or "")
        event_type = str(payload.get("action") or "update")
        events = []
        for row in data:
            if not isinstance(row, (dict, list)):
                continue
            received = utc_now()
            events.append(self._event_from_row(channel, symbol, event_type, row, received))
        return events

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self._rest_base_url}{path}"
        try:
            response = await self._http_client.get(url, params=params)
            response.raise_for_status()
            payload = cast(dict[str, Any], response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataUnavailableError("OKX market data unavailable") from exc

        if okx_api_error(payload):
            message = payload.get("msg") or "OKX market data request failed"
            raise MarketDataUnavailableError(str(message))
        return payload

    def _ensure_symbol(self, symbol: str) -> None:
        if symbol not in self._symbols:
            raise KeyError(symbol)

    def _record(
        self,
        channel: str,
        symbol: str,
        event_type: str,
        row: dict[str, Any],
        received: datetime,
    ) -> None:
        self._journal.append(self._event_from_row(channel, symbol, event_type, row, received))

    def _event_from_row(
        self,
        channel: str,
        symbol: str,
        event_type: str,
        row: dict[str, Any] | list[Any],
        received: datetime,
    ) -> RawMarketDataEvent:
        if isinstance(row, dict):
            sequence_id = int(row["seqId"]) if row.get("seqId") not in (None, "") else None
            previous_sequence_id = (
                int(row["prevSeqId"]) if row.get("prevSeqId") not in (None, "") else None
            )
            exchange_timestamp = parse_exchange_time(row.get("ts"))
        else:
            sequence_id = None
            previous_sequence_id = None
            exchange_timestamp = parse_exchange_time(row[0] if row else None)
        return RawMarketDataEvent(
            provider="okx",
            venue=self.venue,
            channel=channel,
            symbol=symbol,
            event_type=event_type,
            sequence_id=sequence_id,
            previous_sequence_id=previous_sequence_id,
            exchange_timestamp=exchange_timestamp,
            received_timestamp=received,
            raw_payload=row,
        )

    def _fresh_cached_supplemental(
        self,
        symbol: str,
        field: str,
        received: datetime,
    ) -> str | None:
        cached = self._supplemental_cache.get((symbol, field))
        if cached is None:
            return None
        age = (received - cached.received_timestamp).total_seconds()
        if age <= self._supplemental_poll_after_seconds:
            return cached.value
        return None

    def _usable_cached_supplemental(
        self,
        symbol: str,
        field: str,
        received: datetime,
    ) -> str | None:
        cached = self._supplemental_cache.get((symbol, field))
        if cached is None:
            return None
        age = (received - cached.received_timestamp).total_seconds()
        if age <= self._supplemental_stale_after_seconds:
            return cached.value
        return None

    def _get_cached_supplemental(self, symbol: str, field: str, received: datetime) -> str | None:
        fresh = self._fresh_cached_supplemental(symbol, field, received)
        if fresh is not None:
            return fresh
        return self._usable_cached_supplemental(symbol, field, received)

    async def _get_supplemental_field(
        self,
        symbol: str,
        field: str,
        path: str,
        params: dict[str, str],
        value_key: str,
        received: datetime,
    ) -> str | None:
        cached_value = self._get_cached_supplemental(symbol, field, received)
        if cached_value is not None:
            if self._fresh_cached_supplemental(symbol, field, received) is None:
                self._schedule_supplemental_refresh(symbol, received)
            return cached_value
        try:
            async with asyncio.timeout(2.0):
                payload = await self._get(path, params)
                value = self._first_data(payload, "").get(value_key)
                if value is not None:
                    self._supplemental_cache[(symbol, field)] = CachedSupplementalTickerField(
                        value=str(value),
                        received_timestamp=utc_now(),
                    )
                    return str(value)
        except (MarketDataUnavailableError, KeyError, TimeoutError):
            pass
        return None

    def _schedule_supplemental_refresh(self, symbol: str, received: datetime) -> None:
        now = utc_now()
        for field, path, params, value_key in (
            ("mark_price", "/api/v5/public/mark-price", {"instType": "SWAP", "instId": symbol}, "markPx"),
            ("index_price", "/api/v5/market/index-tickers", {"instId": self._index_symbol(symbol)}, "idxPx"),
            ("funding_rate", "/api/v5/public/funding-rate", {"instId": symbol}, "fundingRate"),
            ("open_interest", "/api/v5/public/open-interest", {"instType": "SWAP", "instId": symbol}, "oi"),
        ):
            cached = self._supplemental_cache.get((symbol, field))
            age = (now - cached.received_timestamp).total_seconds() if cached else float("inf")
            if age > self._supplemental_poll_after_seconds:
                key = (symbol, field)
                if key not in self._supplemental_refresh_tasks or self._supplemental_refresh_tasks[key].done():
                    self._supplemental_refresh_tasks[key] = asyncio.create_task(
                        self._refresh_supplemental_field(symbol, field, path, params, value_key),
                        name=f"supplemental-refresh-{symbol}-{field}",
                    )

    async def _refresh_supplemental_field(
        self,
        symbol: str,
        field: str,
        path: str,
        params: dict[str, str],
        value_key: str,
    ) -> None:
        async with self._supplemental_refresh_semaphore:
            try:
                payload = await self._get(path, params)
                value = self._first_data(payload, "").get(value_key)
                if value is not None:
                    self._supplemental_cache[(symbol, field)] = CachedSupplementalTickerField(
                        value=str(value),
                        received_timestamp=utc_now(),
                    )
            except (MarketDataUnavailableError, KeyError):
                pass

    @staticmethod
    def _index_symbol(symbol: str) -> str:
        return symbol.removesuffix("-SWAP")

    @staticmethod
    def _first_data(payload: dict[str, Any], message: str) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise MarketDataUnavailableError(message or "OKX response did not include data")
        row = data[0]
        if not isinstance(row, dict):
            raise MarketDataUnavailableError(message or "OKX response was malformed")
        return row

    @staticmethod
    def _subscription_args(symbols: list[str], channels: tuple[str, ...]) -> list[dict[str, str]]:
        return [
            {"channel": channel, "instId": symbol}
            for symbol in symbols
            for channel in channels
        ]

    @staticmethod
    def _business_url_from_public_url(ws_public_url: str) -> str:
        if ws_public_url.endswith("/public"):
            return f"{ws_public_url.removesuffix('/public')}/business"
        return OKX_WS_BUSINESS_URL

    @staticmethod
    def _candle_from_okx_row(
        symbol: str,
        interval: str,
        row: Any,
        received: datetime,
    ) -> NormalizedCandle | None:
        if not isinstance(row, list) or len(row) < 9:
            return None
        return NormalizedCandle(
            symbol=symbol,
            interval=interval.lower(),
            timestamp=parse_exchange_time(row[0]) or received,
            open=str(row[1]),
            high=str(row[2]),
            low=str(row[3]),
            close=str(row[4]),
            volume=str(row[5]),
            is_closed=str(row[8]) == "1",
        )

    @staticmethod
    def _instrument_from_okx(row: dict[str, Any]) -> NormalizedInstrument:
        tick_size = str(row.get("tickSz") or "0.00000001")
        lot_size = str(row.get("lotSz") or "0.00000001")
        return NormalizedInstrument(
            symbol=str(row["instId"]),
            venue="OKX",
            venue_symbol=str(row["instId"]),
            base_asset=str(row.get("baseCcy") or row["instId"].split("-")[0]),
            quote_asset=str(row.get("quoteCcy") or row["instId"].split("-")[1]),
            instrument_type="perpetual",
            contract_multiplier=row.get("ctMult"),
            tick_size=tick_size,
            lot_size=lot_size,
            min_size=str(row.get("minSz") or lot_size),
            price_precision=precision_from_step(tick_size),
            quantity_precision=precision_from_step(lot_size),
            funding_interval=None,
            status=str(row.get("state") or "unknown"),
            exchange_timestamp=parse_exchange_time(row.get("listTime")),
            metadata={
                "product_type": "perpetual",
                "venue_symbol": row.get("instId"),
                "settle_asset": row.get("settleCcy"),
                "contract_multiplier": row.get("ctMult"),
                "contract_value_currency": row.get("ctValCcy"),
                "alias": row.get("alias"),
                "source": "okx_public_market_data",
                "execution_supported": False,
            },
        )


class OKXMarketDataStreamManager:
    """Shared OKX public WebSocket ingestion task for hot market state."""

    def __init__(self, provider: OKXMarketDataProvider, symbols: list[str]) -> None:
        self.provider = provider
        self.symbols = symbols
        self.status = MarketDataStatus.DISCONNECTED
        self.reconnect_count = 0
        self.normalization_errors = 0
        self.queue_depth = 0
        self.order_books = {symbol: LocalOrderBookBuilder(symbol) for symbol in symbols}
        self.tickers: dict[str, NormalizedTicker] = {}
        self.trades: dict[str, deque[NormalizedTrade]] = {
            symbol: deque(maxlen=200) for symbol in symbols
        }
        self.candles: dict[tuple[str, str], deque[NormalizedCandle]] = {}
        self._task: asyncio.Task[None] | None = None
        self._subscribers: set[asyncio.Queue[RawMarketDataEvent]] = set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="okx-market-data-stream")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def subscribe(self, max_queue_size: int = 256) -> AsyncIterator[RawMarketDataEvent]:
        queue: asyncio.Queue[RawMarketDataEvent] = asyncio.Queue(maxsize=max_queue_size)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                self.status = MarketDataStatus.SYNCING
                async for event in self.provider.subscribe(self.symbols):
                    self.status = MarketDataStatus.SYNCED
                    self._publish(event)
                    backoff = 1.0
            except asyncio.CancelledError:
                self.status = MarketDataStatus.DISCONNECTED
                raise
            except Exception:
                logger.exception("OKX public market-data stream disconnected")
                self.status = MarketDataStatus.DISCONNECTED
                for builder in self.order_books.values():
                    builder.mark_disconnected()
                self.reconnect_count += 1
                await asyncio.sleep(backoff + random.uniform(0, backoff / 4))
                backoff = min(backoff * 2, 30.0)

    def ingest_event(self, event: RawMarketDataEvent) -> None:
        self.provider.journal.append(event)
        self._publish(event)

    def _publish(self, event: RawMarketDataEvent) -> None:
        self._update_hot_state(event)
        stale_subscribers: list[asyncio.Queue[RawMarketDataEvent]] = []
        for queue in self._subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                stale_subscribers.append(queue)
        for queue in stale_subscribers:
            self._subscribers.discard(queue)
        self.queue_depth = sum(queue.qsize() for queue in self._subscribers)

    def _update_hot_state(self, event: RawMarketDataEvent) -> None:
        if event.symbol not in self.order_books:
            return
        if event.channel == "tickers" and isinstance(event.raw_payload, dict):
            self.tickers[event.symbol] = self._ticker_from_event(event)
            return
        if event.channel == "trades" and isinstance(event.raw_payload, dict):
            trade = self._trade_from_event(event)
            if trade is not None:
                self.trades[event.symbol].appendleft(trade)
            return
        if event.channel.startswith("candle") and isinstance(event.raw_payload, list):
            candle = self._candle_from_event(event)
            if candle is not None:
                key = (event.symbol, event.channel.removeprefix("candle").lower())
                series = self.candles.setdefault(key, deque(maxlen=500))
                existing_index = next(
                    (
                        index
                        for index, item in enumerate(series)
                        if item.timestamp == candle.timestamp
                    ),
                    None,
                )
                if existing_index is None:
                    series.append(candle)
                else:
                    series[existing_index] = candle
            return
        if event.channel != "books":
            return
        builder = self.order_books[event.symbol]
        payload = event.raw_payload
        if not isinstance(payload, dict):
            return
        try:
            if event.event_type == "snapshot":
                builder.apply_snapshot(
                    bids=payload.get("bids", []),
                    asks=payload.get("asks", []),
                    sequence=event.sequence_id,
                    exchange_timestamp=event.exchange_timestamp,
                    received_timestamp=event.received_timestamp,
                )
                return
            builder.apply_delta(
                bids=payload.get("bids", []),
                asks=payload.get("asks", []),
                sequence=event.sequence_id,
                previous_sequence=event.previous_sequence_id,
                exchange_timestamp=event.exchange_timestamp,
                received_timestamp=event.received_timestamp,
            )
        except (InvalidOperation, MarketDataIntegrityError, SequenceGapError):
            logger.warning(
                "OKX order book sequence invalid; resync required",
                extra={"symbol": event.symbol},
            )
            builder.mark_resyncing()
            self.normalization_errors += 1

    def hot_ticker(self, symbol: str, stale_after_seconds: float) -> NormalizedTicker | None:
        ticker = self.tickers.get(symbol)
        if ticker is None:
            return None
        age = (utc_now() - ticker.received_timestamp).total_seconds()
        if age > stale_after_seconds and ticker.status == MarketDataStatus.SYNCED:
            return replace(ticker, status=MarketDataStatus.STALE)
        return ticker

    def hot_orderbook(self, symbol: str, depth: int) -> NormalizedOrderBook | None:
        builder = self.order_books.get(symbol)
        if builder is None:
            return None
        snapshot = builder.snapshot(depth)
        if snapshot.bids or snapshot.asks:
            return snapshot
        return None

    def hot_trades(self, symbol: str, limit: int) -> list[NormalizedTrade]:
        return list(self.trades.get(symbol, []))[:limit]

    def hot_candles(self, symbol: str, interval: str, limit: int) -> list[NormalizedCandle]:
        series = self.candles.get((symbol, interval.lower()))
        if not series:
            return []
        return sorted(series, key=lambda candle: candle.timestamp)[-limit:]

    @staticmethod
    def _ticker_from_event(event: RawMarketDataEvent) -> NormalizedTicker:
        row = cast(dict[str, Any], event.raw_payload)
        last = decimal_or_none(row.get("last"))
        open_24h = decimal_or_none(row.get("open24h"))
        change_24h = (
            decimal_str(((last - open_24h) / open_24h) * Decimal("100"))
            if last is not None and open_24h not in (None, Decimal("0"))
            else None
        )
        return NormalizedTicker(
            symbol=event.symbol,
            last_price=row.get("last") or None,
            bid_price=row.get("bidPx") or None,
            ask_price=row.get("askPx") or None,
            high_24h=row.get("high24h") or None,
            low_24h=row.get("low24h") or None,
            volume_24h=row.get("vol24h") or None,
            change_24h=change_24h,
            mark_price=None,
            index_price=None,
            funding_rate=None,
            open_interest=None,
            exchange_timestamp=event.exchange_timestamp,
            received_timestamp=event.received_timestamp,
            status=MarketDataStatus.SYNCED,
        )

    @staticmethod
    def _trade_from_event(event: RawMarketDataEvent) -> NormalizedTrade | None:
        row = cast(dict[str, Any], event.raw_payload)
        if row.get("px") is None or row.get("sz") is None:
            return None
        return NormalizedTrade(
            id=str(
                row.get("tradeId")
                or f"{event.symbol}-{row.get('ts')}-{event.received_timestamp.timestamp()}"
            ),
            symbol=event.symbol,
            price=str(row["px"]),
            quantity=str(row["sz"]),
            side=str(row.get("side") or "unknown").lower(),
            exchange_timestamp=event.exchange_timestamp,
            received_timestamp=event.received_timestamp,
        )

    @staticmethod
    def _candle_from_event(event: RawMarketDataEvent) -> NormalizedCandle | None:
        return OKXMarketDataProvider._candle_from_okx_row(
            symbol=event.symbol,
            interval=event.channel.removeprefix("candle").lower(),
            row=event.raw_payload,
            received=event.received_timestamp,
        )


class DemoMarketDataProvider:
    """Deterministic no-outbound market data for local paper-trading demos."""

    venue = "DEMO"
    _base_prices = {
        "BTC-USDT-SWAP": Decimal("50000"),
        "ETH-USDT-SWAP": Decimal("2500"),
    }

    def __init__(self, symbols: list[str]) -> None:
        self._symbols = {symbol for symbol in symbols if symbol in self._base_prices}

    def supported_symbols(self) -> set[str]:
        return set(self._symbols)

    async def get_instruments(self) -> list[NormalizedInstrument]:
        return [self._instrument(symbol) for symbol in sorted(self._symbols)]

    async def get_ticker(self, symbol: str) -> NormalizedTicker:
        self._ensure_symbol(symbol)
        now = utc_now()
        last = self._demo_price(symbol, now)
        tick = self._tick(symbol)
        return NormalizedTicker(
            symbol=symbol,
            last_price=decimal_str(last),
            bid_price=decimal_str(last - tick),
            ask_price=decimal_str(last + tick),
            high_24h=decimal_str(last * Decimal("1.018")),
            low_24h=decimal_str(last * Decimal("0.982")),
            volume_24h="12450" if symbol.startswith("BTC") else "84200",
            change_24h="0.84" if symbol.startswith("BTC") else "-0.31",
            mark_price=decimal_str(last),
            index_price=decimal_str(last - tick),
            funding_rate="0.0001",
            open_interest="18500" if symbol.startswith("BTC") else "92600",
            exchange_timestamp=now,
            received_timestamp=now,
            status=MarketDataStatus.SYNCED,
        )

    async def get_orderbook_snapshot(self, symbol: str, depth: int) -> NormalizedOrderBook:
        self._ensure_symbol(symbol)
        now = utc_now()
        mid = self._demo_price(symbol, now)
        tick = self._tick(symbol)
        bids = [
            MarketDataLevel(
                price=decimal_str(mid - (tick * Decimal(index))) or "0",
                total_quantity=decimal_str(self._level_size(symbol, index)) or "0",
            )
            for index in range(1, depth + 1)
        ]
        asks = [
            MarketDataLevel(
                price=decimal_str(mid + (tick * Decimal(index))) or "0",
                total_quantity=decimal_str(self._level_size(symbol, index + 1)) or "0",
            )
            for index in range(1, depth + 1)
        ]
        return NormalizedOrderBook(
            symbol=symbol,
            bids=bids,
            asks=asks,
            sequence=int(now.timestamp()),
            exchange_timestamp=now,
            received_timestamp=now,
            spread=decimal_str(tick * Decimal("2")),
            mid_price=decimal_str(mid),
            status=MarketDataStatus.SYNCED,
        )

    async def get_recent_trades(self, symbol: str, limit: int) -> list[NormalizedTrade]:
        self._ensure_symbol(symbol)
        now = utc_now()
        mid = self._demo_price(symbol, now)
        tick = self._tick(symbol)
        trades: list[NormalizedTrade] = []
        for index in range(limit):
            side = "buy" if index % 2 == 0 else "sell"
            price = mid + tick if side == "buy" else mid - tick
            trades.append(
                NormalizedTrade(
                    id=f"demo-{symbol}-{int(now.timestamp())}-{index}",
                    symbol=symbol,
                    price=decimal_str(price) or "0",
                    quantity=decimal_str(self._level_size(symbol, index + 1) / Decimal("10"))
                    or "0",
                    side=side,
                    exchange_timestamp=now - timedelta(seconds=index * 3),
                    received_timestamp=now,
                )
            )
        return trades

    async def get_candles(
        self,
        symbol: str,
        interval: str,
        limit: int,
    ) -> list[NormalizedCandle]:
        self._ensure_symbol(symbol)
        normalized_interval = interval.lower()
        if normalized_interval not in OKX_INTERVALS:
            raise ValueError("Unsupported candle interval")
        now = utc_now()
        step = self._interval_delta(normalized_interval)
        base = self._demo_price(symbol, now)
        candles = []
        for offset in range(limit):
            timestamp = now - (step * (limit - offset - 1))
            drift = Decimal(offset - limit) * self._tick(symbol) * Decimal("0.8")
            open_price = base + drift
            direction = Decimal("3") if offset % 2 else Decimal("-2")
            close_price = open_price + (self._tick(symbol) * direction)
            high = max(open_price, close_price) + self._tick(symbol) * Decimal("4")
            low = min(open_price, close_price) - self._tick(symbol) * Decimal("4")
            candles.append(
                NormalizedCandle(
                    symbol=symbol,
                    interval=normalized_interval,
                    timestamp=timestamp,
                    open=decimal_str(open_price) or "0",
                    high=decimal_str(high) or "0",
                    low=decimal_str(low) or "0",
                    close=decimal_str(close_price) or "0",
                    volume=decimal_str(Decimal("25") + Decimal(offset % 12)) or "0",
                    is_closed=offset != limit - 1,
                )
            )
        return candles

    async def subscribe(self, symbols: list[str]) -> AsyncIterator[RawMarketDataEvent]:
        while True:
            await asyncio.sleep(1)
            now = utc_now()
            for symbol in symbols:
                if symbol in self._symbols:
                    yield RawMarketDataEvent(
                        provider="demo",
                        venue=self.venue,
                        channel="tickers",
                        symbol=symbol,
                        event_type="snapshot",
                        sequence_id=int(now.timestamp()),
                        previous_sequence_id=None,
                        exchange_timestamp=now,
                        received_timestamp=now,
                        raw_payload={"demo": True, "symbol": symbol},
                    )

    def _ensure_symbol(self, symbol: str) -> None:
        if symbol not in self._symbols:
            raise KeyError(symbol)

    def _instrument(self, symbol: str) -> NormalizedInstrument:
        base, quote, _ = symbol.split("-")
        tick = "0.1" if base == "BTC" else "0.01"
        lot = "0.0001" if base == "BTC" else "0.001"
        return NormalizedInstrument(
            symbol=symbol,
            venue=self.venue,
            venue_symbol=symbol,
            base_asset=base,
            quote_asset=quote,
            instrument_type="perpetual",
            contract_multiplier="1",
            tick_size=tick,
            lot_size=lot,
            min_size=lot,
            price_precision=precision_from_step(tick),
            quantity_precision=precision_from_step(lot),
            funding_interval="8h",
            status="live",
            exchange_timestamp=utc_now(),
            metadata={
                "product_type": "perpetual",
                "source": "local_demo_market_data",
                "execution_supported": False,
            },
        )

    def _demo_price(self, symbol: str, now: datetime) -> Decimal:
        wave = Decimal(now.minute % 10) - Decimal("5")
        return self._base_prices[symbol] + (wave * self._tick(symbol) * Decimal("12"))

    @staticmethod
    def _tick(symbol: str) -> Decimal:
        return Decimal("5") if symbol.startswith("BTC") else Decimal("0.5")

    @staticmethod
    def _level_size(symbol: str, index: int) -> Decimal:
        unit = Decimal("0.08") if symbol.startswith("BTC") else Decimal("1.2")
        return unit + (Decimal(index % 6) * unit / Decimal("3"))

    @staticmethod
    def _interval_delta(interval: str) -> timedelta:
        return {
            "1m": timedelta(minutes=1),
            "5m": timedelta(minutes=5),
            "15m": timedelta(minutes=15),
            "1h": timedelta(hours=1),
            "4h": timedelta(hours=4),
            "1d": timedelta(days=1),
        }[interval]


class MarketDataService:
    def __init__(
        self,
        provider: MarketDataProvider | None,
        stream_manager: OKXMarketDataStreamManager | None = None,
        stale_after_seconds: float = 15.0,
    ) -> None:
        self.provider = provider
        self.stream_manager = stream_manager
        self.stale_after_seconds = stale_after_seconds
        self._ticker_cache: dict[str, NormalizedTicker] = {}
        self._orderbook_cache: dict[str, NormalizedOrderBook] = {}
        self._trades_cache: dict[str, list[NormalizedTrade]] = {}
        self._candles_cache: dict[tuple[str, str], list[NormalizedCandle]] = {}

    def supports(self, symbol: str) -> bool:
        return self.provider is not None and symbol in self.provider.supported_symbols()

    async def list_instruments(self) -> list[NormalizedInstrument]:
        if self.provider is None:
            return []
        return await self.provider.get_instruments()

    async def get_ticker(self, symbol: str) -> NormalizedTicker:
        if not self.supports(symbol):
            raise KeyError(symbol)
        assert self.provider is not None
        try:
            ticker = await self.provider.get_ticker(symbol)
            self._ticker_cache[symbol] = ticker
            return ticker
        except MarketDataUnavailableError:
            fallback_ticker = (
                self.stream_manager.hot_ticker(symbol, self.stale_after_seconds)
                if self.stream_manager
                else None
            )
            if fallback_ticker is not None:
                fallback_ticker = self._with_cached_supplemental_fields(
                    fallback_ticker,
                    self._ticker_cache.get(symbol),
                )
            if fallback_ticker is None:
                fallback_ticker = self._stale_ticker(self._ticker_cache.get(symbol))
            if fallback_ticker is not None:
                return fallback_ticker
            raise

    async def get_orderbook(self, symbol: str, depth: int) -> NormalizedOrderBook:
        if not self.supports(symbol):
            raise KeyError(symbol)
        assert self.provider is not None
        try:
            book = await self.provider.get_orderbook_snapshot(symbol, depth)
            self._orderbook_cache[symbol] = book
            return book
        except MarketDataUnavailableError:
            fallback_book = (
                self.stream_manager.hot_orderbook(symbol, depth) if self.stream_manager else None
            )
            if fallback_book is None:
                fallback_book = self._stale_orderbook(self._orderbook_cache.get(symbol))
            if fallback_book is not None:
                return fallback_book
            raise

    async def get_trades(self, symbol: str, limit: int) -> list[NormalizedTrade]:
        if not self.supports(symbol):
            raise KeyError(symbol)
        assert self.provider is not None
        try:
            trades = await self.provider.get_recent_trades(symbol, limit)
            self._trades_cache[symbol] = trades
            return trades
        except MarketDataUnavailableError:
            trades = self.stream_manager.hot_trades(symbol, limit) if self.stream_manager else []
            if trades:
                return trades
            cached = self._trades_cache.get(symbol)
            if cached:
                return cached[:limit]
            raise

    async def get_candles(self, symbol: str, interval: str, limit: int) -> list[NormalizedCandle]:
        if not self.supports(symbol):
            raise KeyError(symbol)
        assert self.provider is not None
        normalized_interval = interval.lower()
        try:
            candles = await self.provider.get_candles(symbol, normalized_interval, limit)
            if not candles:
                cached = self._candles_cache.get((symbol, normalized_interval))
                if cached:
                    return cached[-limit:]
            self._candles_cache[(symbol, normalized_interval)] = candles
            return candles
        except MarketDataUnavailableError:
            candles = (
                self.stream_manager.hot_candles(symbol, normalized_interval, limit)
                if self.stream_manager
                else []
            )
            if candles:
                return candles
            cached = self._candles_cache.get((symbol, normalized_interval))
            if cached:
                return cached[-limit:]
            raise

    def _stale_ticker(self, ticker: NormalizedTicker | None) -> NormalizedTicker | None:
        if ticker is None:
            return None
        age = (utc_now() - ticker.received_timestamp).total_seconds()
        if age > self.stale_after_seconds and ticker.status == MarketDataStatus.SYNCED:
            return replace(ticker, status=MarketDataStatus.STALE)
        return ticker

    def _with_cached_supplemental_fields(
        self,
        ticker: NormalizedTicker,
        cached: NormalizedTicker | None,
    ) -> NormalizedTicker:
        if cached is None:
            return ticker
        cached_age = (utc_now() - cached.received_timestamp).total_seconds()
        if cached_age > self.stale_after_seconds or cached.status != MarketDataStatus.SYNCED:
            return ticker
        return replace(
            ticker,
            mark_price=ticker.mark_price or cached.mark_price,
            index_price=ticker.index_price or cached.index_price,
            funding_rate=ticker.funding_rate or cached.funding_rate,
            open_interest=ticker.open_interest or cached.open_interest,
        )

    def _stale_orderbook(self, book: NormalizedOrderBook | None) -> NormalizedOrderBook | None:
        if book is None:
            return None
        age = (utc_now() - book.received_timestamp).total_seconds()
        if age > self.stale_after_seconds and book.status == MarketDataStatus.SYNCED:
            return replace(book, status=MarketDataStatus.STALE)
        return book


def create_okx_market_data_service(
    symbols: list[str],
    rest_base_url: str = OKX_REST_BASE_URL,
    ws_public_url: str = OKX_WS_PUBLIC_URL,
) -> MarketDataService:
    provider = OKXMarketDataProvider(
        symbols=symbols,
        rest_base_url=rest_base_url,
        ws_public_url=ws_public_url,
    )
    return MarketDataService(provider)
