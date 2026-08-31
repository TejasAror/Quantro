import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from quantro_api.market_data import (
    LocalOrderBookBuilder,
    MarketDataIntegrityError,
    MarketDataLevel,
    MarketDataService,
    MarketDataStatus,
    MarketDataUnavailableError,
    NormalizedCandle,
    NormalizedOrderBook,
    NormalizedTicker,
    OKXMarketDataProvider,
    OKXMarketDataStreamManager,
    RawEventJournal,
    RawMarketDataEvent,
    SequenceGapError,
    SimulatorMarketDataGate,
    parse_exchange_time,
)


class FlakyMarketDataProvider:
    venue = "TEST"

    def __init__(self, symbols: list[str]) -> None:
        self._symbols = set(symbols)
        self.journal = RawEventJournal()
        self.fail_ticker = False
        self.fail_orderbook = False
        self.fail_candles = False
        now = datetime.now(UTC)
        self.tickers = {
            symbol: NormalizedTicker(
                symbol=symbol,
                last_price="100",
                bid_price="99",
                ask_price="101",
                high_24h="110",
                low_24h="90",
                volume_24h="1000",
                change_24h="1",
                mark_price="100",
                index_price="100",
                funding_rate="0.0001",
                open_interest="100",
                exchange_timestamp=now,
                received_timestamp=now,
                status=MarketDataStatus.SYNCED,
            )
            for symbol in symbols
        }
        self.orderbooks = {
            symbol: NormalizedOrderBook(
                symbol=symbol,
                bids=[],
                asks=[],
                sequence=1,
                exchange_timestamp=now,
                received_timestamp=now,
                spread=None,
                mid_price="100",
                status=MarketDataStatus.SYNCED,
            )
            for symbol in symbols
        }
        self.candles = {
            symbol: [
                NormalizedCandle(
                    symbol=symbol,
                    interval="1h",
                    timestamp=now - timedelta(hours=1),
                    open="100",
                    high="102",
                    low="99",
                    close="101",
                    volume="10",
                    is_closed=True,
                )
            ]
            for symbol in symbols
        }

    def supported_symbols(self) -> set[str]:
        return set(self._symbols)

    async def get_instruments(self):
        return []

    async def get_ticker(self, symbol: str) -> NormalizedTicker:
        if self.fail_ticker:
            raise MarketDataUnavailableError("REST ticker unavailable")
        return self.tickers[symbol]

    async def get_orderbook_snapshot(self, symbol: str, depth: int) -> NormalizedOrderBook:
        _ = depth
        if self.fail_orderbook:
            raise MarketDataUnavailableError("REST book unavailable")
        return self.orderbooks[symbol]

    async def get_recent_trades(self, symbol: str, limit: int):
        _ = symbol, limit
        return []

    async def get_candles(self, symbol: str, interval: str, limit: int) -> list[NormalizedCandle]:
        _ = interval
        if self.fail_candles:
            raise MarketDataUnavailableError("REST candles unavailable")
        return self.candles[symbol][-limit:]

    async def subscribe(self, symbols: list[str]):
        _ = symbols
        if False:
            yield


class ReconnectingMarketDataProvider(FlakyMarketDataProvider):
    def __init__(self, symbols: list[str]) -> None:
        super().__init__(symbols)
        self.subscribe_calls = 0

    async def subscribe(self, symbols: list[str]):
        self.subscribe_calls += 1
        if self.subscribe_calls == 1:
            raise ConnectionError("keepalive ping timeout")
        now = datetime.now(UTC)
        for symbol in symbols:
            yield raw_ticker_event(symbol, price="125" if symbol.startswith("BTC") else "2500")
            if symbol == "BTC-USDT-SWAP":
                yield raw_book_event(
                    event_type="snapshot",
                    sequence=10,
                    previous_sequence=None,
                    bids=[["124.9", "1"]],
                    asks=[["125.1", "1"]],
                )
                yield raw_trade_event(symbol)
                yield raw_candle_event(symbol, "1h", timestamp=now)
        await asyncio.sleep(3600)


class FakeOKXMarketDataProvider(OKXMarketDataProvider):
    def __init__(self, symbols: list[str]) -> None:
        super().__init__(symbols, rest_base_url="https://okx.test")
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.fail_paths: set[str] = set()
        self.values = {
            "BTC-USDT-SWAP": {
                "last": "77420.1",
                "bidPx": "77420",
                "askPx": "77420.2",
                "high24h": "79000",
                "low24h": "76000",
                "vol24h": "12345",
                "open24h": "77000",
                "markPx": "77424.8",
                "idxPx": "77418.6",
                "fundingRate": "0.0000688337233400",
                "oi": "2904693.29000000702",
            },
            "ETH-USDT-SWAP": {
                "last": "4420.1",
                "bidPx": "4420",
                "askPx": "4420.2",
                "high24h": "4500",
                "low24h": "4300",
                "vol24h": "23456",
                "open24h": "4400",
                "markPx": "4421.3",
                "idxPx": "4419.7",
                "fundingRate": "0.000031",
                "oi": "3812345.67",
            },
        }

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        self.calls.append((path, dict(params)))
        if path in self.fail_paths:
            raise MarketDataUnavailableError(f"{path} failed")
        symbol = self._symbol_for(path, params)
        value = self.values[symbol]
        if path == "/api/v5/market/ticker":
            return {
                "code": "0",
                "data": [
                    {
                        "instId": symbol,
                        "last": value["last"],
                        "bidPx": value["bidPx"],
                        "askPx": value["askPx"],
                        "high24h": value["high24h"],
                        "low24h": value["low24h"],
                        "vol24h": value["vol24h"],
                        "open24h": value["open24h"],
                        "ts": "1700000000000",
                    }
                ],
            }
        if path == "/api/v5/public/mark-price":
            return {"code": "0", "data": [{"instId": symbol, "markPx": value["markPx"]}]}
        if path == "/api/v5/market/index-tickers":
            return {"code": "0", "data": [{"instId": params["instId"], "idxPx": value["idxPx"]}]}
        if path == "/api/v5/public/funding-rate":
            return {"code": "0", "data": [{"instId": symbol, "fundingRate": value["fundingRate"]}]}
        if path == "/api/v5/public/open-interest":
            return {"code": "0", "data": [{"instId": symbol, "oi": value["oi"]}]}
        raise AssertionError(f"unexpected path {path}")

    @staticmethod
    def _symbol_for(path: str, params: dict[str, str]) -> str:
        if path == "/api/v5/market/index-tickers":
            return f"{params['instId']}-SWAP"
        return params["instId"]


def raw_book_event(
    *,
    event_type: str,
    sequence: int | None,
    previous_sequence: int | None,
    bids: list[list[str]] | None = None,
    asks: list[list[str]] | None = None,
) -> RawMarketDataEvent:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "seqId": str(sequence) if sequence is not None else "",
        "prevSeqId": str(previous_sequence) if previous_sequence is not None else "",
        "bids": bids or [],
        "asks": asks or [],
        "ts": str(int(now.timestamp() * 1000)),
    }
    return RawMarketDataEvent(
        provider="okx",
        venue="OKX",
        channel="books",
        symbol="BTC-USDT-SWAP",
        event_type=event_type,
        sequence_id=sequence,
        previous_sequence_id=previous_sequence,
        exchange_timestamp=now,
        received_timestamp=now,
        raw_payload=payload,
    )


def raw_ticker_event(symbol: str, *, price: str = "120") -> RawMarketDataEvent:
    now = datetime.now(UTC)
    row = {
        "instId": symbol,
        "last": price,
        "bidPx": str(Decimal(price) - Decimal("0.1")),
        "askPx": str(Decimal(price) + Decimal("0.1")),
        "high24h": str(Decimal(price) + Decimal("10")),
        "low24h": str(Decimal(price) - Decimal("10")),
        "vol24h": "1000",
        "open24h": str(Decimal(price) - Decimal("1")),
        "ts": str(int(now.timestamp() * 1000)),
    }
    return RawMarketDataEvent(
        provider="okx",
        venue="OKX",
        channel="tickers",
        symbol=symbol,
        event_type="snapshot",
        sequence_id=None,
        previous_sequence_id=None,
        exchange_timestamp=now,
        received_timestamp=now,
        raw_payload=row,
    )


def raw_trade_event(symbol: str, *, price: str = "125") -> RawMarketDataEvent:
    now = datetime.now(UTC)
    return RawMarketDataEvent(
        provider="okx",
        venue="OKX",
        channel="trades",
        symbol=symbol,
        event_type="snapshot",
        sequence_id=None,
        previous_sequence_id=None,
        exchange_timestamp=now,
        received_timestamp=now,
        raw_payload={
            "tradeId": f"{symbol}-{int(now.timestamp())}",
            "px": price,
            "sz": "0.1",
            "side": "buy",
            "ts": str(int(now.timestamp() * 1000)),
        },
    )


def raw_candle_event(
    symbol: str,
    interval: str = "1m",
    *,
    timestamp: datetime | None = None,
) -> RawMarketDataEvent:
    now = timestamp or datetime.now(UTC)
    row = [
        str(int(now.timestamp() * 1000)),
        "100",
        "102",
        "99",
        "101",
        "12",
        "0",
        "0",
        "1",
    ]
    return RawMarketDataEvent(
        provider="okx",
        venue="OKX",
        channel=f"candle{interval}",
        symbol=symbol,
        event_type="snapshot",
        sequence_id=None,
        previous_sequence_id=None,
        exchange_timestamp=now,
        received_timestamp=now,
        raw_payload=row,
    )


def assert_simulator_blocked(builder: LocalOrderBookBuilder) -> None:
    gate = SimulatorMarketDataGate(builder)
    with pytest.raises(MarketDataUnavailableError):
        gate.canonical_state_for_simulator(5)
    assert gate.blocked_count == 1
    assert gate.simulator_event_cursor is None


def assert_not_crossed(builder: LocalOrderBookBuilder) -> None:
    snapshot = builder.snapshot(10)
    if snapshot.bids and snapshot.asks:
        assert float(snapshot.bids[0].price) < float(snapshot.asks[0].price)


def test_okx_ticker_normalization_from_public_payload() -> None:
    provider = OKXMarketDataProvider(["BTC-USDT-SWAP"])
    row = {
        "instId": "BTC-USDT-SWAP",
        "last": "64000.1",
        "bidPx": "64000",
        "askPx": "64000.2",
        "high24h": "65000",
        "low24h": "62000",
        "vol24h": "12345",
        "open24h": "63200",
        "ts": "1700000000000",
    }

    event = provider._event_from_row("tickers", "BTC-USDT-SWAP", "snapshot", row, datetime.now(UTC))

    assert event.provider == "okx"
    assert event.venue == "OKX"
    assert event.symbol == "BTC-USDT-SWAP"
    assert event.sequence_id is None
    assert event.exchange_timestamp == datetime.fromtimestamp(1700000000, UTC)


def test_okx_instrument_normalization_preserves_perp_semantics() -> None:
    instrument = OKXMarketDataProvider._instrument_from_okx(
        {
            "instId": "ETH-USDT-SWAP",
            "baseCcy": "ETH",
            "quoteCcy": "USDT",
            "settleCcy": "USDT",
            "instType": "SWAP",
            "ctMult": "1",
            "tickSz": "0.01",
            "lotSz": "0.01",
            "minSz": "0.01",
            "state": "live",
            "listTime": "1700000000000",
        }
    )

    assert instrument.symbol == "ETH-USDT-SWAP"
    assert instrument.venue_symbol == "ETH-USDT-SWAP"
    assert instrument.instrument_type == "perpetual"
    assert instrument.metadata["settle_asset"] == "USDT"
    assert instrument.metadata["execution_supported"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("symbol", ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
async def test_okx_ticker_aggregates_perpetual_supplemental_fields(symbol: str) -> None:
    provider = FakeOKXMarketDataProvider(["BTC-USDT-SWAP", "ETH-USDT-SWAP"])

    ticker = await provider.get_ticker(symbol)

    expected = provider.values[symbol]
    assert ticker.symbol == symbol
    assert ticker.last_price == expected["last"]
    assert ticker.bid_price == expected["bidPx"]
    assert ticker.ask_price == expected["askPx"]
    assert ticker.high_24h == expected["high24h"]
    assert ticker.low_24h == expected["low24h"]
    assert ticker.volume_24h == expected["vol24h"]
    assert ticker.mark_price == expected["markPx"]
    assert ticker.index_price == expected["idxPx"]
    assert ticker.funding_rate == expected["fundingRate"]
    assert ticker.open_interest == expected["oi"]
    assert ticker.exchange_timestamp == datetime.fromtimestamp(1700000000, UTC)
    assert ticker.status == MarketDataStatus.SYNCED
    assert ("/api/v5/public/mark-price", {"instType": "SWAP", "instId": symbol}) in provider.calls
    assert (
        "/api/v5/market/index-tickers",
        {"instId": symbol.removesuffix("-SWAP")},
    ) in provider.calls
    assert ("/api/v5/public/funding-rate", {"instId": symbol}) in provider.calls
    assert (
        "/api/v5/public/open-interest",
        {"instType": "SWAP", "instId": symbol},
    ) in provider.calls


@pytest.mark.anyio
async def test_okx_ticker_partial_supplemental_failure_preserves_cached_valid_field() -> None:
    provider = FakeOKXMarketDataProvider(["BTC-USDT-SWAP"])
    first = await provider.get_ticker("BTC-USDT-SWAP")
    old_enough_to_repoll = datetime.now(UTC) - timedelta(seconds=6)
    provider._supplemental_cache[("BTC-USDT-SWAP", "funding_rate")] = replace(
        provider._supplemental_cache[("BTC-USDT-SWAP", "funding_rate")],
        received_timestamp=old_enough_to_repoll,
    )
    provider.fail_paths.add("/api/v5/public/funding-rate")

    second = await provider.get_ticker("BTC-USDT-SWAP")

    assert first.funding_rate == "0.0000688337233400"
    assert second.funding_rate == first.funding_rate
    assert second.mark_price == first.mark_price
    assert second.index_price == first.index_price
    assert second.open_interest == first.open_interest
    assert second.status == MarketDataStatus.SYNCED


@pytest.mark.anyio
async def test_okx_ticker_remains_responsive_when_one_uncached_supplemental_endpoint_fails() -> (
    None
):
    provider = FakeOKXMarketDataProvider(["BTC-USDT-SWAP"])
    provider.fail_paths.add("/api/v5/public/mark-price")

    ticker = await provider.get_ticker("BTC-USDT-SWAP")

    assert ticker.last_price == "77420.1"
    assert ticker.mark_price is None
    assert ticker.index_price == "77418.6"
    assert ticker.funding_rate == "0.0000688337233400"
    assert ticker.open_interest == "2904693.29000000702"
    assert ticker.status == MarketDataStatus.SYNCED


@pytest.mark.anyio
async def test_okx_ticker_supplemental_cache_bounds_rest_polling() -> None:
    provider = FakeOKXMarketDataProvider(["BTC-USDT-SWAP"])

    await provider.get_ticker("BTC-USDT-SWAP")
    await provider.get_ticker("BTC-USDT-SWAP")

    paths = [path for path, _params in provider.calls]
    assert paths.count("/api/v5/market/ticker") == 2
    assert paths.count("/api/v5/public/mark-price") == 1
    assert paths.count("/api/v5/market/index-tickers") == 1
    assert paths.count("/api/v5/public/funding-rate") == 1
    assert paths.count("/api/v5/public/open-interest") == 1


@pytest.mark.anyio
async def test_candle_cache_serves_last_valid_history_when_rest_returns_empty() -> None:
    provider = FlakyMarketDataProvider(["BTC-USDT-SWAP"])
    service = MarketDataService(provider)
    first = await service.get_candles("BTC-USDT-SWAP", "1h", 5)
    provider.candles["BTC-USDT-SWAP"] = []

    fallback = await service.get_candles("BTC-USDT-SWAP", "1h", 5)

    assert fallback == first


@pytest.mark.anyio
async def test_candle_cache_covers_supported_symbols_and_intervals() -> None:
    provider = FlakyMarketDataProvider(["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    service = MarketDataService(provider)

    for symbol in ("BTC-USDT-SWAP", "ETH-USDT-SWAP"):
        for interval in ("1m", "5m", "15m", "1h", "4h", "1d"):
            candle = replace(provider.candles[symbol][0], symbol=symbol, interval=interval)
            provider.candles[symbol] = [candle]
            first = await service.get_candles(symbol, interval, 240)
            provider.fail_candles = True
            fallback = await service.get_candles(symbol, interval, 240)
            provider.fail_candles = False

            assert fallback == first
            assert fallback[0].symbol == symbol
            assert fallback[0].interval == interval


@pytest.mark.anyio
async def test_market_data_stream_reconnects_after_timeout_and_repopulates_hot_state() -> None:
    provider = ReconnectingMarketDataProvider(["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    manager = OKXMarketDataStreamManager(provider, ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])

    manager.start()
    try:
        for _ in range(50):
            if manager.hot_candles("BTC-USDT-SWAP", "1h", 1):
                break
            await asyncio.sleep(0.05)

        assert provider.subscribe_calls == 2
        assert manager.reconnect_count == 1
        assert manager.status == MarketDataStatus.SYNCED
        assert manager.hot_ticker("BTC-USDT-SWAP", 15) is not None
        assert manager.hot_ticker("ETH-USDT-SWAP", 15) is not None
        assert manager.hot_orderbook("BTC-USDT-SWAP", 5) is not None
        assert manager.hot_trades("BTC-USDT-SWAP", 5)
        assert manager.hot_candles("BTC-USDT-SWAP", "1h", 1)
        assert manager.queue_depth == 0
    finally:
        await manager.stop()


def test_local_order_book_applies_snapshot_and_delta() -> None:
    builder = LocalOrderBookBuilder("BTC-USDT-SWAP")
    now = datetime.now(UTC)

    builder.apply_snapshot(
        bids=[["64000", "2"], ["63990", "1"]],
        asks=[["64010", "3"], ["64020", "1"]],
        sequence=100,
        exchange_timestamp=now,
        received_timestamp=now,
    )
    builder.apply_delta(
        bids=[["64005", "0.5"], ["63990", "0"]],
        asks=[["64010", "2"]],
        sequence=101,
        previous_sequence=100,
        exchange_timestamp=now,
        received_timestamp=now,
    )

    snapshot = builder.snapshot(5)
    assert snapshot.status == MarketDataStatus.SYNCED
    assert snapshot.sequence == 101
    assert snapshot.bids[0].price == "64005"
    assert snapshot.bids[1].price == "64000"
    assert snapshot.asks[0].total_quantity == "2"
    assert snapshot.spread == "5"
    assert snapshot.mid_price == "64007.5"


def test_local_order_book_ignores_duplicate_delta() -> None:
    builder = LocalOrderBookBuilder("BTC-USDT-SWAP")
    now = datetime.now(UTC)
    builder.apply_snapshot([["1", "1"]], [["2", "1"]], 10, now, now)
    builder.apply_delta([["1.5", "1"]], [], 10, 9, now, now)

    snapshot = builder.snapshot(5)
    assert snapshot.sequence == 10
    assert [level.price for level in snapshot.bids] == ["1"]


def test_local_order_book_detects_sequence_gap_and_stops_publishing_synced_state() -> None:
    builder = LocalOrderBookBuilder("BTC-USDT-SWAP")
    now = datetime.now(UTC)
    builder.apply_snapshot([["1", "1"]], [["2", "1"]], 10, now, now)

    with pytest.raises(SequenceGapError):
        builder.apply_delta([["1.5", "1"]], [], 12, 11, now, now)

    assert builder.snapshot(5).status == MarketDataStatus.RESYNCING
    assert builder.sequence_gap_count == 1


def test_local_order_book_can_resync_after_gap() -> None:
    builder = LocalOrderBookBuilder("BTC-USDT-SWAP")
    now = datetime.now(UTC)
    builder.apply_snapshot([["1", "1"]], [["2", "1"]], 10, now, now)
    with pytest.raises(SequenceGapError):
        builder.apply_delta([], [], 15, 14, now, now)

    builder.mark_resyncing()
    builder.apply_snapshot([["3", "2"]], [["4", "2"]], 20, now, now)

    snapshot = builder.snapshot(5)
    assert snapshot.status == MarketDataStatus.SYNCED
    assert snapshot.sequence == 20
    assert snapshot.bids[0].price == "3"
    assert builder.resync_count == 1


def test_local_order_book_marks_stale_without_fabricating_values() -> None:
    builder = LocalOrderBookBuilder("BTC-USDT-SWAP", stale_after_seconds=1)
    old = datetime.now(UTC) - timedelta(seconds=5)
    builder.apply_snapshot([["1", "1"]], [["2", "1"]], 10, old, old)

    snapshot = builder.snapshot(5)

    assert snapshot.status == MarketDataStatus.STALE
    assert snapshot.bids[0].price == "1"
    assert_simulator_blocked(builder)


def test_simulator_gate_keeps_cursor_separate_from_frontend_snapshot_cursor() -> None:
    builder = LocalOrderBookBuilder("BTC-USDT-SWAP")
    now = datetime.now(UTC)
    gate = SimulatorMarketDataGate(builder)
    builder.apply_snapshot([["100", "1"]], [["101", "1"]], 50, now, now)

    frontend_snapshot = gate.frontend_snapshot(1)
    simulator_snapshot = gate.canonical_state_for_simulator(1)

    assert frontend_snapshot.sequence == 50
    assert simulator_snapshot.sequence == 50
    assert gate.frontend_snapshot_cursor == 50
    assert gate.simulator_event_cursor == 50

    builder.mark_resyncing()
    stale_frontend = gate.frontend_snapshot(1)
    assert stale_frontend.status == MarketDataStatus.RESYNCING
    with pytest.raises(MarketDataUnavailableError):
        gate.canonical_state_for_simulator(1)
    assert gate.simulator_event_cursor == 50


def test_invalid_quantities_and_crossed_books_are_not_healthy_simulation_state() -> None:
    now = datetime.now(UTC)
    builder = LocalOrderBookBuilder("BTC-USDT-SWAP")
    with pytest.raises(MarketDataIntegrityError):
        builder.apply_snapshot([["100", "-1"]], [["101", "1"]], 1, now, now)
    assert_simulator_blocked(builder)

    builder = LocalOrderBookBuilder("BTC-USDT-SWAP")
    with pytest.raises(MarketDataIntegrityError):
        builder.apply_snapshot([["102", "1"]], [["101", "1"]], 1, now, now)
    assert_simulator_blocked(builder)


def test_malformed_book_payload_marks_manager_unhealthy_without_crashing() -> None:
    journal = RawEventJournal()
    provider = OKXMarketDataProvider(["BTC-USDT-SWAP"], journal=journal)
    manager = OKXMarketDataStreamManager(provider, ["BTC-USDT-SWAP"])
    event = raw_book_event(
        event_type="snapshot",
        sequence=1,
        previous_sequence=None,
        bids=[["100"]],
        asks=[["101", "1"]],
    )

    manager.ingest_event(event)
    builder = manager.order_books["BTC-USDT-SWAP"]

    assert builder.snapshot(5).status == MarketDataStatus.RESYNCING
    assert manager.normalization_errors == 1
    assert journal.recent(1)[0] == event
    assert_simulator_blocked(builder)


def test_disconnect_marks_market_unhealthy_and_blocks_simulator() -> None:
    builder = LocalOrderBookBuilder("BTC-USDT-SWAP")
    now = datetime.now(UTC)
    builder.apply_snapshot([["100", "1"]], [["101", "1"]], 1, now, now)
    builder.mark_disconnected()

    assert builder.snapshot(5).status == MarketDataStatus.DISCONNECTED
    assert_simulator_blocked(builder)


def test_dropped_l2_message_triggers_recovery_before_simulator_resumes() -> None:
    builder = LocalOrderBookBuilder("BTC-USDT-SWAP")
    now = datetime.now(UTC)
    gate = SimulatorMarketDataGate(builder)
    builder.apply_snapshot([["100", "1"]], [["101", "1"]], 10, now, now)
    assert gate.canonical_state_for_simulator(5).sequence == 10

    with pytest.raises(SequenceGapError):
        builder.apply_delta([["100.5", "1"]], [], 12, 11, now, now)
    with pytest.raises(MarketDataUnavailableError):
        gate.canonical_state_for_simulator(5)

    builder.mark_resyncing()
    builder.apply_snapshot([["100.5", "1"]], [["101.5", "1"]], 20, now, now)
    recovered = gate.canonical_state_for_simulator(5)

    assert recovered.status == MarketDataStatus.SYNCED
    assert gate.simulator_event_cursor == 20
    assert_not_crossed(builder)


def test_out_of_order_event_cannot_silently_corrupt_state() -> None:
    builder = LocalOrderBookBuilder("BTC-USDT-SWAP")
    now = datetime.now(UTC)
    builder.apply_snapshot([["100", "1"]], [["101", "1"]], 10, now, now)
    before = builder.state_hash()

    with pytest.raises(SequenceGapError):
        builder.apply_delta([["99", "5"]], [], 12, 9, now, now)

    assert builder.snapshot(5).status == MarketDataStatus.RESYNCING
    assert builder.state_hash() == before
    assert_simulator_blocked(builder)


def test_snapshot_delta_race_requires_snapshot_before_canonical_publication() -> None:
    builder = LocalOrderBookBuilder("BTC-USDT-SWAP")
    now = datetime.now(UTC)

    with pytest.raises(SequenceGapError):
        builder.apply_delta([["100", "1"]], [], 1, 0, now, now)

    assert builder.snapshot(5).status == MarketDataStatus.RESYNCING
    assert_simulator_blocked(builder)
    builder.apply_snapshot([["100", "1"]], [["101", "1"]], 2, now, now)
    assert SimulatorMarketDataGate(builder).canonical_state_for_simulator(5).sequence == 2


def test_replay_produces_same_state_hash_and_duplicate_events_are_harmless() -> None:
    now = datetime.now(UTC)
    events = [
        ("snapshot", 100, None, [["100", "1"], ["99", "2"]], [["101", "1"]]),
        ("update", 101, 100, [["100.5", "1"]], []),
        ("update", 101, 100, [["100.5", "1"]], []),
        ("update", 102, 101, [], [["101", "0"], ["101.5", "3"]]),
    ]

    hashes = []
    for _ in range(2):
        builder = LocalOrderBookBuilder("BTC-USDT-SWAP")
        for event_type, seq, prev, bids, asks in events:
            if event_type == "snapshot":
                builder.apply_snapshot(bids, asks, seq, now, now)
            else:
                builder.apply_delta(bids, asks, seq, prev, now, now)
        hashes.append(builder.state_hash())
        assert builder.snapshot(5).status == MarketDataStatus.SYNCED
        assert_not_crossed(builder)

    assert hashes[0] == hashes[1]


def test_reconnect_does_not_create_duplicate_consumers_or_duplicate_simulator_cursor() -> None:
    journal = RawEventJournal()
    provider = OKXMarketDataProvider(["BTC-USDT-SWAP"], journal=journal)
    manager = OKXMarketDataStreamManager(provider, ["BTC-USDT-SWAP"])
    manager.ingest_event(
        raw_book_event(
            event_type="snapshot",
            sequence=1,
            previous_sequence=None,
            bids=[["100", "1"]],
            asks=[["101", "1"]],
        )
    )
    manager.ingest_event(
        raw_book_event(
            event_type="update",
            sequence=2,
            previous_sequence=1,
            bids=[["100.5", "1"]],
        )
    )

    builder = manager.order_books["BTC-USDT-SWAP"]
    gate = SimulatorMarketDataGate(builder)
    first = gate.canonical_state_for_simulator(5)
    second = gate.canonical_state_for_simulator(5)

    assert first.sequence == 2
    assert second.sequence == 2
    assert gate.simulator_event_cursor == 2
    assert len(manager._subscribers) == 0
    assert len(journal.recent(10)) == 2


def test_high_volume_reconnect_replay_preserves_canonical_state_without_duplicate_fills() -> None:
    journal = RawEventJournal(max_events=2000)
    provider = OKXMarketDataProvider(["BTC-USDT-SWAP"], journal=journal)
    manager = OKXMarketDataStreamManager(provider, ["BTC-USDT-SWAP"])
    manager.ingest_event(
        raw_book_event(
            event_type="snapshot",
            sequence=1000,
            previous_sequence=None,
            bids=[["100", "1"]],
            asks=[["101", "1"]],
        )
    )
    for sequence in range(1001, 1101):
        manager.ingest_event(
            raw_book_event(
                event_type="update",
                sequence=sequence,
                previous_sequence=sequence - 1,
                bids=[[str(100 + (sequence - 1000) / 1000), "1"]],
            )
        )

    builder = manager.order_books["BTC-USDT-SWAP"]
    before_reconnect_hash = builder.state_hash()
    manager.status = MarketDataStatus.DISCONNECTED
    builder.mark_disconnected()
    assert_simulator_blocked(builder)

    manager.ingest_event(
        raw_book_event(
            event_type="snapshot",
            sequence=2000,
            previous_sequence=None,
            bids=[["100.2", "2"]],
            asks=[["101.2", "2"]],
        )
    )
    for sequence in range(2001, 2051):
        manager.ingest_event(
            raw_book_event(
                event_type="update",
                sequence=sequence,
                previous_sequence=sequence - 1,
                asks=[[str(101.2 + (sequence - 2000) / 1000), "2"]],
            )
        )

    recovered = SimulatorMarketDataGate(builder).canonical_state_for_simulator(5)

    assert recovered.sequence == 2050
    assert builder.state_hash() != before_reconnect_hash
    assert len(journal.recent(2000)) == 152
    assert len(manager._subscribers) == 0
    assert_not_crossed(builder)


def test_okx_candle_stream_array_rows_do_not_disconnect_subscription_parser() -> None:
    provider = OKXMarketDataProvider(["BTC-USDT-SWAP"])
    now = datetime.now(UTC)
    event = provider._event_from_row(
        "candle1m",
        "BTC-USDT-SWAP",
        "snapshot",
        [str(int(now.timestamp() * 1000)), "100", "102", "99", "101", "12", "0", "0", "1"],
        now,
    )

    assert event.channel == "candle1m"
    assert event.sequence_id is None
    assert event.previous_sequence_id is None
    assert event.exchange_timestamp == parse_exchange_time(str(int(now.timestamp() * 1000)))
    assert event.raw_payload[1] == "100"


class FakeOKXWebSocket:
    def __init__(self, url: str, messages: list[str]) -> None:
        self.url = url
        self.messages = messages
        self.sent: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        _ = exc_type, exc, traceback

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class FakeOKXWebSocketsModule:
    def __init__(self, by_url: dict[str, list[str]]) -> None:
        self.by_url = by_url
        self.connections: list[FakeOKXWebSocket] = []

    def connect(self, url: str, *, ping_interval=None) -> FakeOKXWebSocket:
        assert ping_interval is None
        connection = FakeOKXWebSocket(url, self.by_url[url])
        self.connections.append(connection)
        return connection


@pytest.mark.anyio
async def test_okx_subscribe_uses_public_and_business_websocket_channel_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OKXMarketDataProvider(
        ["BTC-USDT-SWAP"],
        ws_public_url="wss://example.test/ws/v5/public",
        ws_business_url="wss://example.test/ws/v5/business",
    )
    fake_websockets = FakeOKXWebSocketsModule(
        {
            "wss://example.test/ws/v5/public": [
                json.dumps(
                    {
                        "arg": {"channel": "tickers", "instId": "BTC-USDT-SWAP"},
                        "data": [{"last": "100", "open24h": "99", "ts": "1700000000000"}],
                    }
                )
            ],
            "wss://example.test/ws/v5/business": [
                json.dumps(
                    {
                        "arg": {"channel": "candle1m", "instId": "BTC-USDT-SWAP"},
                        "data": [
                            [
                                "1700000000000",
                                "100",
                                "101",
                                "99",
                                "100.5",
                                "12",
                                "1200",
                                "1200",
                                "0",
                            ]
                        ],
                    }
                )
            ],
        }
    )
    monkeypatch.setattr("websockets.connect", fake_websockets.connect)

    stream = provider.subscribe(["BTC-USDT-SWAP"])
    try:
        events = [await anext(stream), await anext(stream)]
    finally:
        await stream.aclose()

    sent_by_url = {
        connection.url: json.loads(connection.sent[0])["args"]
        for connection in fake_websockets.connections
    }

    assert sent_by_url["wss://example.test/ws/v5/public"] == [
        {"channel": "tickers", "instId": "BTC-USDT-SWAP"},
        {"channel": "books", "instId": "BTC-USDT-SWAP"},
        {"channel": "trades", "instId": "BTC-USDT-SWAP"},
    ]
    assert sent_by_url["wss://example.test/ws/v5/business"] == [
        {"channel": "candle1m", "instId": "BTC-USDT-SWAP"},
    ]
    assert {event.channel for event in events} == {"tickers", "candle1m"}


@pytest.mark.anyio
async def test_okx_websocket_keepalive_uses_text_ping_pong() -> None:
    provider = OKXMarketDataProvider(["BTC-USDT-SWAP"])
    provider.WS_IDLE_TIMEOUT_SECONDS = 0.01

    class SilentUntilPingWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._pong_pending = False

        async def send(self, message: str) -> None:
            self.sent.append(message)
            if message == "ping":
                self._pong_pending = True

        async def recv(self) -> str:
            if self._pong_pending:
                self._pong_pending = False
                return "pong"
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

    ws = SilentUntilPingWebSocket()

    assert await provider._recv_okx_message(ws) == "pong"
    assert ws.sent == ["ping"]


@pytest.mark.anyio
async def test_stream_reconnect_repopulates_hot_ticker_fallback_for_btc_and_eth() -> None:
    provider = FlakyMarketDataProvider(["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    manager = OKXMarketDataStreamManager(provider, ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    service = MarketDataService(provider, stream_manager=manager)

    for symbol in ("BTC-USDT-SWAP", "ETH-USDT-SWAP"):
        assert (await service.get_ticker(symbol)).status == MarketDataStatus.SYNCED

    manager.status = MarketDataStatus.DISCONNECTED
    provider.fail_ticker = True
    with pytest.raises(MarketDataUnavailableError):
        await MarketDataService(provider).get_ticker("BTC-USDT-SWAP")

    manager.status = MarketDataStatus.SYNCING
    manager.ingest_event(raw_ticker_event("BTC-USDT-SWAP", price="77625.4"))
    manager.ingest_event(raw_ticker_event("ETH-USDT-SWAP", price="4500.1"))
    manager.status = MarketDataStatus.SYNCED

    btc = await service.get_ticker("BTC-USDT-SWAP")
    eth = await service.get_ticker("ETH-USDT-SWAP")

    assert btc.last_price == "77625.4"
    assert btc.bid_price == "77625.3"
    assert btc.ask_price == "77625.5"
    assert btc.status == MarketDataStatus.SYNCED
    assert eth.last_price == "4500.1"
    assert eth.status == MarketDataStatus.SYNCED


@pytest.mark.anyio
async def test_stream_ticker_fallback_preserves_fresh_cached_perpetual_fields() -> None:
    provider = FlakyMarketDataProvider(["BTC-USDT-SWAP"])
    manager = OKXMarketDataStreamManager(provider, ["BTC-USDT-SWAP"])
    service = MarketDataService(provider, stream_manager=manager, stale_after_seconds=15)

    cached = await service.get_ticker("BTC-USDT-SWAP")
    provider.fail_ticker = True
    manager.ingest_event(raw_ticker_event("BTC-USDT-SWAP", price="121"))

    fallback = await service.get_ticker("BTC-USDT-SWAP")

    assert fallback.last_price == "121"
    assert fallback.mark_price == cached.mark_price
    assert fallback.index_price == cached.index_price
    assert fallback.funding_rate == cached.funding_rate
    assert fallback.open_interest == cached.open_interest
    assert fallback.status == MarketDataStatus.SYNCED


@pytest.mark.anyio
async def test_stream_ticker_fallback_does_not_merge_stale_cached_perpetual_fields() -> None:
    provider = FlakyMarketDataProvider(["BTC-USDT-SWAP"])
    manager = OKXMarketDataStreamManager(provider, ["BTC-USDT-SWAP"])
    service = MarketDataService(provider, stream_manager=manager, stale_after_seconds=1)
    old = datetime.now(UTC) - timedelta(seconds=5)
    provider.tickers["BTC-USDT-SWAP"] = replace(
        provider.tickers["BTC-USDT-SWAP"],
        received_timestamp=old,
        exchange_timestamp=old,
    )

    await service.get_ticker("BTC-USDT-SWAP")
    provider.fail_ticker = True
    manager.ingest_event(raw_ticker_event("BTC-USDT-SWAP", price="121"))

    fallback = await service.get_ticker("BTC-USDT-SWAP")

    assert fallback.last_price == "121"
    assert fallback.mark_price is None
    assert fallback.index_price is None
    assert fallback.funding_rate is None
    assert fallback.open_interest is None
    assert fallback.status == MarketDataStatus.SYNCED


@pytest.mark.anyio
async def test_candle_history_uses_rest_cache_and_recovers_after_rest_returns() -> None:
    provider = FlakyMarketDataProvider(["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    manager = OKXMarketDataStreamManager(provider, ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    service = MarketDataService(provider, stream_manager=manager)

    first = await service.get_candles("BTC-USDT-SWAP", "1h", 5)
    provider.fail_candles = True

    fallback = await service.get_candles("BTC-USDT-SWAP", "1h", 5)
    assert fallback == first

    manager.ingest_event(raw_candle_event("BTC-USDT-SWAP", "1m"))
    one_minute = await service.get_candles("BTC-USDT-SWAP", "1m", 5)
    assert len(one_minute) == 1
    assert one_minute[0].interval == "1m"
    assert one_minute[0].close == "101"

    provider.fail_candles = False
    provider.candles["BTC-USDT-SWAP"].append(
        NormalizedCandle(
            symbol="BTC-USDT-SWAP",
            interval="1h",
            timestamp=datetime.now(UTC),
            open="101",
            high="103",
            low="100",
            close="102",
            volume="11",
            is_closed=False,
        )
    )

    recovered = await service.get_candles("BTC-USDT-SWAP", "1h", 5)
    assert len(recovered) == 2
    assert recovered[-1].close == "102"


@pytest.mark.anyio
async def test_stale_cached_ticker_and_orderbook_remain_rejected_for_execution() -> None:
    provider = FlakyMarketDataProvider(["BTC-USDT-SWAP"])
    service = MarketDataService(provider, stale_after_seconds=1)
    old = datetime.now(UTC) - timedelta(seconds=5)
    provider.tickers["BTC-USDT-SWAP"] = replace(
        provider.tickers["BTC-USDT-SWAP"],
        received_timestamp=old,
        exchange_timestamp=old,
    )
    provider.orderbooks["BTC-USDT-SWAP"] = replace(
        provider.orderbooks["BTC-USDT-SWAP"],
        received_timestamp=old,
        exchange_timestamp=old,
        bids=[MarketDataLevel("99", "1")],
        asks=[MarketDataLevel("101", "1")],
        spread="2",
    )

    await service.get_ticker("BTC-USDT-SWAP")
    await service.get_orderbook("BTC-USDT-SWAP", 5)
    provider.fail_ticker = True
    provider.fail_orderbook = True

    ticker = await service.get_ticker("BTC-USDT-SWAP")
    book = await service.get_orderbook("BTC-USDT-SWAP", 5)

    assert ticker.status == MarketDataStatus.STALE
    assert book.status == MarketDataStatus.STALE
    with pytest.raises(MarketDataUnavailableError):
        if ticker.status != MarketDataStatus.SYNCED or book.status != MarketDataStatus.SYNCED:
            raise MarketDataUnavailableError(
                "Paper trading is blocked until market data is synced and fresh"
            )
