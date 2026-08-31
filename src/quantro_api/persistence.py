"""Database persistence for the process-local Quantro engine."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from uuid import UUID

from quantro import (
    Account,
    Balance,
    FixedPoint,
    Market,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
    TimeInForce,
    Trade,
    TradingEngine,
)
from quantro.orderbook import OrderBook, OrderBookSide, PriceLevel
from quantro.portfolio import Portfolio, PortfolioManager

from .db import Database
from .schemas import AccountCreate
from .service import EngineService


class AccountProvisioningError(RuntimeError):
    """Raised when an authenticated user cannot be mapped to a Quantro account."""


def _dt(value: datetime) -> str:
    return value.isoformat()


def _parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _fp(value: FixedPoint) -> str:
    return str(value)


def _referenced_auth_user_table(cur) -> tuple[str, str, str] | None:  # type: ignore[no-untyped-def]
    cur.execute(
        """
        select target_ns.nspname as referenced_schema,
               target.relname as referenced_table,
               target_att.attname as referenced_column
        from pg_constraint constraint_info
        join pg_class source on source.oid = constraint_info.conrelid
        join pg_namespace source_ns on source_ns.oid = source.relnamespace
        join pg_class target on target.oid = constraint_info.confrelid
        join pg_namespace target_ns on target_ns.oid = target.relnamespace
        join pg_attribute source_att
          on source_att.attrelid = source.oid
         and source_att.attnum = constraint_info.conkey[1]
        join pg_attribute target_att
          on target_att.attrelid = target.oid
         and target_att.attnum = constraint_info.confkey[1]
        where constraint_info.contype = 'f'
          and source_ns.nspname = current_schema()
          and source.relname = 'quantro_users'
          and source_att.attname = 'auth_user_id'
        limit 1
        """
    )
    row = cur.fetchone()
    if row is None:
        return None
    return row["referenced_schema"], row["referenced_table"], row["referenced_column"]


def _table_columns(cur, schema_name: str, table_name: str) -> set[str]:  # type: ignore[no-untyped-def]
    cur.execute(
        """
        select column_name
        from information_schema.columns
        where table_schema = %s and table_name = %s
        """,
        (schema_name, table_name),
    )
    return {row["column_name"] for row in cur.fetchall()}


def _ensure_application_user(
    cur,  # type: ignore[no-untyped-def]
    auth_user_id: UUID,
    email: str | None,
) -> None:
    from psycopg import sql

    referenced = _referenced_auth_user_table(cur)
    if referenced is None:
        return

    schema_name, table_name, id_column = referenced
    parent_table = sql.Identifier(schema_name, table_name)
    parent_id_column = sql.Identifier(id_column)
    if schema_name == "auth":
        for attempt in range(3):
            cur.execute(
                sql.SQL("select 1 from {} where {} = %s").format(
                    parent_table,
                    parent_id_column,
                ),
                (auth_user_id,),
            )
            if cur.fetchone() is not None:
                return
            if attempt < 2:
                time.sleep(0.2 * (attempt + 1))
        raise AccountProvisioningError(
            "Supabase signup returned a user id that is not present in auth.users; "
            "Quantro account provisioning stopped before quantro_users insert"
        )

    if schema_name != "public":
        raise AccountProvisioningError(
            f"Unsupported quantro_users auth_user_id reference target: {schema_name}.{table_name}"
        )

    if table_name != "users":
        raise AccountProvisioningError(
            f"Unsupported quantro_users auth_user_id reference target: public.{table_name}"
        )

    columns = _table_columns(cur, schema_name, table_name)
    if id_column not in columns:
        raise AccountProvisioningError(
            f"Application users table is missing referenced id column: {id_column}"
        )

    insert_columns = [id_column]
    values: list[object] = [auth_user_id]
    placeholders = [sql.Placeholder()]
    updates = []

    if "email" in columns:
        insert_columns.append("email")
        values.append(email)
        placeholders.append(sql.Placeholder())
        updates.append(sql.SQL("email = excluded.email"))
    if "created_at" in columns:
        insert_columns.append("created_at")
        placeholders.append(sql.SQL("now()"))
    if "updated_at" in columns:
        insert_columns.append("updated_at")
        placeholders.append(sql.SQL("now()"))
        updates.append(sql.SQL("updated_at = now()"))

    conflict = sql.SQL("do nothing")
    if updates:
        conflict = sql.SQL("do update set {}").format(sql.SQL(", ").join(updates))

    cur.execute(
        sql.SQL("insert into {} ({}) values ({}) on conflict ({}) {}").format(
            parent_table,
            sql.SQL(", ").join(sql.Identifier(column) for column in insert_columns),
            sql.SQL(", ").join(placeholders),
            parent_id_column,
            conflict,
        ),
        values,
    )


def _market_to_json(market: Market) -> dict:
    return {
        "symbol": market.symbol,
        "base_asset": market.base_asset,
        "quote_asset": market.quote_asset,
        "venue": market.venue,
        "price_precision": market.price_precision,
        "quantity_precision": market.quantity_precision,
        "min_order_size": _fp(market.min_order_size),
        "max_order_size": _fp(market.max_order_size),
        "tick_size": _fp(market.tick_size),
        "lot_size": _fp(market.lot_size),
        "maker_fee": _fp(market.maker_fee),
        "taker_fee": _fp(market.taker_fee),
        "is_active": market.is_active,
        "metadata": market.metadata,
    }


def _market_from_json(data: dict) -> Market:
    return Market(
        symbol=data["symbol"],
        base_asset=data["base_asset"],
        quote_asset=data["quote_asset"],
        venue=data["venue"],
        price_precision=data["price_precision"],
        quantity_precision=data["quantity_precision"],
        min_order_size=FixedPoint(data["min_order_size"]),
        max_order_size=FixedPoint(data["max_order_size"]),
        tick_size=FixedPoint(data["tick_size"]),
        lot_size=FixedPoint(data["lot_size"]),
        maker_fee=FixedPoint(data["maker_fee"]),
        taker_fee=FixedPoint(data["taker_fee"]),
        is_active=data["is_active"],
        metadata=data.get("metadata", {}),
    )


def _balance_to_json(balance: Balance) -> dict:
    return {"asset": balance.asset, "free": _fp(balance.free), "locked": _fp(balance.locked)}


def _balance_from_json(data: dict) -> Balance:
    return Balance(
        asset=data["asset"],
        free=FixedPoint(data["free"]),
        locked=FixedPoint(data["locked"]),
    )


def _account_to_json(account: Account) -> dict:
    return {
        "id": str(account.id),
        "name": account.name,
        "balances": [_balance_to_json(balance) for balance in account.balances.values()],
        "created_at": _dt(account.created_at),
        "updated_at": _dt(account.updated_at),
        "metadata": account.metadata,
    }


def _account_from_json(data: dict) -> Account:
    return Account(
        id=UUID(data["id"]),
        name=data["name"],
        balances={item["asset"]: _balance_from_json(item) for item in data.get("balances", [])},
        created_at=_parse_dt(data["created_at"]) or datetime.now(UTC).replace(tzinfo=None),
        updated_at=_parse_dt(data["updated_at"]) or datetime.now(UTC).replace(tzinfo=None),
        metadata=data.get("metadata", {}),
    )


def _order_to_json(order: Order) -> dict:
    return {
        "id": str(order.id),
        "account_id": str(order.account_id),
        "symbol": order.symbol,
        "side": order.side.value,
        "order_type": order.order_type.value,
        "status": order.status.value,
        "time_in_force": order.time_in_force.value,
        "quantity": _fp(order.quantity),
        "price": _fp(order.price),
        "stop_price": _fp(order.stop_price),
        "filled_quantity": _fp(order.filled_quantity),
        "average_fill_price": _fp(order.average_fill_price),
        "total_fees": _fp(order.total_fees),
        "client_order_id": order.client_order_id,
        "created_at": _dt(order.created_at),
        "updated_at": _dt(order.updated_at),
        "expires_at": _dt(order.expires_at) if order.expires_at else None,
        "metadata": order.metadata,
    }


def _order_from_json(data: dict, markets: dict[str, Market]) -> Order:
    return Order(
        id=UUID(data["id"]),
        account_id=UUID(data["account_id"]),
        market=markets[data["symbol"]],
        symbol=data["symbol"],
        side=OrderSide(data["side"]),
        order_type=OrderType(data["order_type"]),
        status=OrderStatus(data["status"]),
        time_in_force=TimeInForce(data["time_in_force"]),
        quantity=FixedPoint(data["quantity"]),
        price=FixedPoint(data["price"]),
        stop_price=FixedPoint(data["stop_price"]),
        filled_quantity=FixedPoint(data["filled_quantity"]),
        average_fill_price=FixedPoint(data["average_fill_price"]),
        total_fees=FixedPoint(data["total_fees"]),
        client_order_id=data.get("client_order_id", ""),
        created_at=_parse_dt(data["created_at"]) or datetime.now(UTC).replace(tzinfo=None),
        updated_at=_parse_dt(data["updated_at"]) or datetime.now(UTC).replace(tzinfo=None),
        expires_at=_parse_dt(data.get("expires_at")),
        metadata=data.get("metadata", {}),
    )


def _trade_to_json(trade: Trade) -> dict:
    return {
        "id": str(trade.id),
        "order_id": str(trade.order_id),
        "account_id": str(trade.account_id),
        "symbol": trade.symbol,
        "side": trade.side.value,
        "quantity": _fp(trade.quantity),
        "price": _fp(trade.price),
        "fee": _fp(trade.fee),
        "fee_asset": trade.fee_asset,
        "timestamp": _dt(trade.timestamp),
        "is_maker": trade.is_maker,
        "metadata": trade.metadata,
    }


def _trade_from_json(data: dict, markets: dict[str, Market]) -> Trade:
    return Trade(
        id=UUID(data["id"]),
        order_id=UUID(data["order_id"]),
        account_id=UUID(data["account_id"]),
        market=markets[data["symbol"]],
        symbol=data["symbol"],
        side=OrderSide(data["side"]),
        quantity=FixedPoint(data["quantity"]),
        price=FixedPoint(data["price"]),
        fee=FixedPoint(data["fee"]),
        fee_asset=data["fee_asset"],
        timestamp=_parse_dt(data["timestamp"]) or datetime.now(UTC).replace(tzinfo=None),
        is_maker=data["is_maker"],
        metadata=data.get("metadata", {}),
    )


def _position_to_json(position: Position) -> dict:
    return {
        "id": str(position.id),
        "account_id": str(position.account_id),
        "symbol": position.symbol,
        "side": position.side.value,
        "size": _fp(position.size),
        "entry_price": _fp(position.entry_price),
        "mark_price": _fp(position.mark_price),
        "unrealized_pnl": _fp(position.unrealized_pnl),
        "realized_pnl": _fp(position.realized_pnl),
        "leverage": _fp(position.leverage),
        "liquidation_price": _fp(position.liquidation_price),
        "opened_at": _dt(position.opened_at),
        "updated_at": _dt(position.updated_at),
        "metadata": position.metadata,
    }


def _position_from_json(data: dict, markets: dict[str, Market]) -> Position:
    return Position(
        id=UUID(data["id"]),
        account_id=UUID(data["account_id"]),
        market=markets.get(data["symbol"]),
        symbol=data["symbol"],
        side=PositionSide(data["side"]),
        size=FixedPoint(data["size"]),
        entry_price=FixedPoint(data["entry_price"]),
        mark_price=FixedPoint(data["mark_price"]),
        unrealized_pnl=FixedPoint(data["unrealized_pnl"]),
        realized_pnl=FixedPoint(data["realized_pnl"]),
        leverage=FixedPoint(data["leverage"]),
        liquidation_price=FixedPoint(data["liquidation_price"]),
        opened_at=_parse_dt(data["opened_at"]) or datetime.now(UTC).replace(tzinfo=None),
        updated_at=_parse_dt(data["updated_at"]) or datetime.now(UTC).replace(tzinfo=None),
        metadata=data.get("metadata", {}),
    )


def _price_level_to_json(level: PriceLevel) -> dict:
    return {
        "price": _fp(level.price),
        "orders": [_order_to_json(order) for order in level.orders],
    }


def _price_level_from_json(data: dict, markets: dict[str, Market]) -> PriceLevel:
    orders = tuple(_order_from_json(order, markets) for order in data.get("orders", []))
    return PriceLevel(
        price=FixedPoint(data["price"]),
        orders=orders,
        total_quantity=sum((order.remaining_quantity for order in orders), FixedPoint("0")),
    )


def engine_to_json(
    engine: TradingEngine,
    orders: dict[UUID, Order],
    trades_by_account: dict[UUID, list[Trade]],
) -> dict:
    snapshot = engine.snapshot()
    markets = [_market_to_json(book.market) for book in snapshot.order_books.values()]
    order_books = []
    for symbol, book in snapshot.order_books.items():
        bids, asks = book.get_full_depth()
        order_books.append(
            {
                "symbol": symbol,
                "sequence": book.sequence,
                "last_trade_price": _fp(book.last_trade_price),
                "last_trade_quantity": _fp(book.last_trade_quantity),
                "bids": [_price_level_to_json(level) for level in bids],
                "asks": [_price_level_to_json(level) for level in asks],
            }
        )
    portfolios = []
    for portfolio in snapshot.portfolios.values():
        portfolios.append(
            {
                "account": _account_to_json(portfolio.account),
                "positions": [
                    _position_to_json(position) for position in portfolio.positions.values()
                ],
                "trade_history": [_trade_to_json(trade) for trade in portfolio.trade_history],
                "open_orders_count": portfolio.open_orders_count,
                "sequence": portfolio.sequence,
                "total_realized_pnl": _fp(portfolio.get_total_realized_pnl()),
            }
        )
    return {
        "version": 1,
        "sequence": snapshot.sequence,
        "markets": markets,
        "order_books": order_books,
        "portfolios": portfolios,
        "orders": [_order_to_json(order) for order in orders.values()],
        "trades_by_account": {
            str(account_id): [_trade_to_json(trade) for trade in trades]
            for account_id, trades in trades_by_account.items()
        },
    }


def engine_from_json(
    data: dict,
) -> tuple[TradingEngine, dict[UUID, Order], dict[UUID, list[Trade]]]:
    markets = {}
    for item in data.get("markets", []):
        market = _market_from_json(item)
        markets[market.symbol] = market
    portfolio_manager = PortfolioManager()
    for item in data.get("portfolios", []):
        account = _account_from_json(item["account"])
        positions = {
            position["symbol"]: _position_from_json(position, markets)
            for position in item.get("positions", [])
        }
        trade_history = [
            _trade_from_json(trade, markets) for trade in item.get("trade_history", [])
        ]
        portfolio = Portfolio(account, initial_positions=positions, trade_history=trade_history)
        portfolio._open_orders_count = item.get("open_orders_count", 0)
        portfolio._sequence = item.get("sequence", 0)
        portfolio._total_realized_pnl = FixedPoint(item.get("total_realized_pnl", "0"))
        portfolio_manager._portfolios[account.id] = portfolio

    engine = TradingEngine(portfolio_manager=portfolio_manager)
    engine._markets = markets
    engine._order_books = {}
    for item in data.get("order_books", []):
        bids = [_price_level_from_json(level, markets) for level in item.get("bids", [])]
        asks = [_price_level_from_json(level, markets) for level in item.get("asks", [])]
        book = OrderBook(markets[item["symbol"]], initial_bids=bids, initial_asks=asks)
        book._sequence = item.get("sequence", 0)
        book._last_trade_price = FixedPoint(item.get("last_trade_price", "0"))
        book._last_trade_quantity = FixedPoint(item.get("last_trade_quantity", "0"))
        for side, levels in ((OrderBookSide.BID, bids), (OrderBookSide.ASK, asks)):
            for level in levels:
                for order in level.orders:
                    book._order_index[order.id] = (side, level.price)
        engine._order_books[item["symbol"]] = book
    engine._sequence = data.get("sequence", 0)

    orders = {UUID(item["id"]): _order_from_json(item, markets) for item in data.get("orders", [])}
    trades_by_account = {
        UUID(account_id): [_trade_from_json(trade, markets) for trade in trades]
        for account_id, trades in data.get("trades_by_account", {}).items()
    }
    return engine, orders, trades_by_account


class PersistentEngineService(EngineService):
    def __init__(
        self,
        db: Database,
        sandbox_initial_balances: dict[str, str] | None = None,
    ) -> None:
        self._db = db
        self._sandbox_initial_balances = sandbox_initial_balances or {
            "USD": "100000",
            "USDT": "1000000",
            "BTC": "10",
            "ETH": "100",
        }
        super().__init__()
        self._load_state()

    def _load_state(self) -> None:
        with self._db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select state from quantro_engine_state where id = true")
                row = cur.fetchone()
        if row is None:
            self._persist_state()
            return
        self._engine, self._orders, self._trades_by_account = engine_from_json(row["state"])
        self._seed_default_markets()
        self._reseed_paper_demo_accounts()
        self._persist_state()

    def get_or_create_user_account(
        self,
        auth_user_id: UUID,
        email: str | None = None,
        name: str | None = None,
    ) -> Account:
        with self._lock:
            account_id = self._lookup_account_id(auth_user_id)
            if account_id is not None:
                self._ensure_paper_demo_account(account_id)
                portfolio = self.get_portfolio(account_id)
                if portfolio is not None:
                    return portfolio.account
                restored = self._restore_mapped_account(account_id, email, name)
                if restored is not None:
                    self._ensure_paper_demo_account(account_id)
                    portfolio = self.get_portfolio(account_id)
                    if portfolio is not None:
                        self._persist_state()
                        return portfolio.account

            self._ensure_user_reference(auth_user_id, email)
            account_name = name or (email.split("@", 1)[0] if email else "Quantro Account")
            account = self.create_account(
                AccountCreate(
                    name=account_name,
                    initial_balances=self._sandbox_initial_balances,
                )
            )
            self._link_user_account(auth_user_id, account, email)
            return account

    def account_id_for_user(self, auth_user_id: UUID) -> UUID | None:
        return self._lookup_account_id(auth_user_id)

    def _lookup_account_id(self, auth_user_id: UUID) -> UUID | None:
        with self._db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select account_id from quantro_user_accounts where auth_user_id = %s",
                    (auth_user_id,),
                )
                row = cur.fetchone()
        return None if row is None else row["account_id"]

    def _ensure_user_reference(self, auth_user_id: UUID, email: str | None) -> None:
        with self._db.connect() as conn:
            with conn.cursor() as cur:
                _ensure_application_user(cur, auth_user_id, email)
            conn.commit()

    def _link_user_account(self, auth_user_id: UUID, account: Account, email: str | None) -> None:
        from psycopg.types.json import Jsonb

        with self._db.connect() as conn:
            with conn.cursor() as cur:
                _ensure_application_user(cur, auth_user_id, email)
                cur.execute(
                    """
                    insert into quantro_users (auth_user_id, email, updated_at)
                    values (%s, %s, now())
                    on conflict (auth_user_id) do update
                    set email = excluded.email, updated_at = now()
                    """,
                    (auth_user_id, email),
                )
                cur.execute(
                    """
                    insert into quantro_accounts (id, name, metadata, created_at, updated_at)
                    values (%s, %s, %s, %s, %s)
                    on conflict (id) do nothing
                    """,
                    (
                        account.id,
                        account.name,
                        Jsonb(account.metadata),
                        account.created_at,
                        account.updated_at,
                    ),
                )
                cur.execute(
                    """
                    insert into quantro_user_accounts (auth_user_id, account_id)
                    values (%s, %s)
                    on conflict (auth_user_id) do nothing
                    """,
                    (auth_user_id, account.id),
                )
            conn.commit()

    def _after_mutation(self) -> None:
        self._persist_state()

    def _reseed_paper_demo_accounts(self) -> bool:
        changed = False
        for account_id in self._engine.snapshot().portfolios:
            changed = self._ensure_paper_demo_account(account_id, persist=False) or changed
        return changed

    def _ensure_paper_demo_account(self, account_id: UUID, persist: bool = True) -> bool:
        changed = self.ensure_paper_virtual_balances(
            account_id,
            {"USDT": self._sandbox_initial_balances.get("USDT", "0")},
        )
        if changed and persist:
            self._persist_state()
        return changed

    def get_portfolio(self, account_id: UUID):  # type: ignore[no-untyped-def]
        self._ensure_paper_demo_account(account_id)
        return super().get_portfolio(account_id)

    def _restore_mapped_account(
        self,
        account_id: UUID,
        email: str | None,
        name: str | None,
    ) -> Account | None:
        with self._db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select id, name, metadata, created_at, updated_at
                    from quantro_accounts
                    where id = %s
                    """,
                    (account_id,),
                )
                account_row = cur.fetchone()
                cur.execute(
                    """
                    select asset, free, locked
                    from quantro_balances
                    where account_id = %s
                    """,
                    (account_id,),
                )
                balance_rows = cur.fetchall() or []

        account_name = (
            account_row["name"]
            if account_row is not None
            else name or (email.split("@", 1)[0] if email else "Quantro Account")
        )
        metadata = dict(account_row["metadata"] or {}) if account_row is not None else {}
        balances = {
            row["asset"]: Balance(
                asset=row["asset"],
                free=FixedPoint(str(row["free"])),
                locked=FixedPoint(str(row["locked"])),
            )
            for row in balance_rows
        }
        account = Account(
            id=account_id,
            name=account_name,
            balances=balances,
            created_at=account_row["created_at"] if account_row is not None else datetime.now(UTC).replace(tzinfo=None),
            updated_at=account_row["updated_at"] if account_row is not None else datetime.now(UTC).replace(tzinfo=None),
            metadata=metadata,
        )
        self._engine._portfolio_manager._portfolios[account_id] = Portfolio(account)  # type: ignore[attr-defined]
        return account

    def create_account(self, request):  # type: ignore[no-untyped-def]
        account = super().create_account(request)
        self._persist_state()
        return account

    def deposit(self, account_id, request):  # type: ignore[no-untyped-def]
        portfolio = super().deposit(account_id, request)
        self._persist_state()
        return portfolio

    def submit_order(self, request):  # type: ignore[no-untyped-def]
        result = super().submit_order(request)
        self._persist_state()
        return result

    def cancel_order(self, order_id):  # type: ignore[no-untyped-def]
        order = super().cancel_order(order_id)
        if order is not None:
            self._persist_state()
        return order

    def cancel_all_orders(self, symbol, account_id):  # type: ignore[no-untyped-def]
        orders = super().cancel_all_orders(symbol, account_id)
        if orders:
            self._persist_state()
        return orders

    def update_mark_price(self, symbol: str, price: str) -> None:
        super().update_mark_price(symbol, price)
        self._persist_state()

    def _persist_state(self) -> None:
        from psycopg.types.json import Jsonb

        state = engine_to_json(self._engine, self._orders, self._trades_by_account)
        snapshot = self._engine.snapshot()
        with self._db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("delete from quantro_trades")
                cur.execute("delete from quantro_orders")
                cur.execute("delete from quantro_positions")
                cur.execute("delete from quantro_balances")
                cur.execute("delete from quantro_pnl_state")

                for portfolio in snapshot.portfolios.values():
                    account = portfolio.account
                    cur.execute(
                        """
                        insert into quantro_accounts (id, name, metadata, created_at, updated_at)
                        values (%s, %s, %s, %s, %s)
                        on conflict (id) do update
                        set name = excluded.name,
                            metadata = excluded.metadata,
                            updated_at = excluded.updated_at
                        """,
                        (
                            account.id,
                            account.name,
                            Jsonb(account.metadata),
                            account.created_at,
                            account.updated_at,
                        ),
                    )
                    for balance in account.balances.values():
                        cur.execute(
                            """
                            insert into quantro_balances (account_id, asset, free, locked)
                            values (%s, %s, %s, %s)
                            """,
                            (account.id, balance.asset, str(balance.free), str(balance.locked)),
                        )
                    for position in portfolio.positions.values():
                        cur.execute(
                            """
                            insert into quantro_positions (
                                id, account_id, symbol, side, size, entry_price, mark_price,
                                unrealized_pnl, realized_pnl, leverage, liquidation_price,
                                metadata, opened_at, updated_at
                            )
                            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                position.id,
                                position.account_id,
                                position.symbol,
                                position.side.value,
                                str(position.size),
                                str(position.entry_price),
                                str(position.mark_price),
                                str(position.unrealized_pnl),
                                str(position.realized_pnl),
                                str(position.leverage),
                                str(position.liquidation_price),
                                Jsonb(position.metadata),
                                position.opened_at,
                                position.updated_at,
                            ),
                        )
                    cur.execute(
                        """
                        insert into quantro_pnl_state (
                            account_id, total_unrealized_pnl, total_realized_pnl, sequence
                        )
                        values (%s, %s, %s, %s)
                        """,
                        (
                            account.id,
                            str(portfolio.get_total_unrealized_pnl()),
                            str(portfolio.get_total_realized_pnl()),
                            portfolio.sequence,
                        ),
                    )

                for order in self._orders.values():
                    cur.execute(
                        """
                        insert into quantro_orders (
                            id, account_id, symbol, side, order_type, status, time_in_force,
                            quantity, price, stop_price, filled_quantity, remaining_quantity,
                            average_fill_price, total_fees, client_order_id, metadata,
                            created_at, updated_at, expires_at
                        )
                        values (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            order.id,
                            order.account_id,
                            order.symbol,
                            order.side.value,
                            order.order_type.value,
                            order.status.value,
                            order.time_in_force.value,
                            str(order.quantity),
                            str(order.price),
                            str(order.stop_price),
                            str(order.filled_quantity),
                            str(order.remaining_quantity),
                            str(order.average_fill_price),
                            str(order.total_fees),
                            order.client_order_id,
                            Jsonb(order.metadata),
                            order.created_at,
                            order.updated_at,
                            order.expires_at,
                        ),
                    )

                seen_trade_ids = set()
                for trades in self._trades_by_account.values():
                    for trade in trades:
                        if trade.id in seen_trade_ids:
                            continue
                        seen_trade_ids.add(trade.id)
                        cur.execute(
                            """
                            insert into quantro_trades (
                                id, order_id, account_id, symbol, side, quantity, price,
                                notional, fee, fee_asset, is_maker, metadata, executed_at
                            )
                            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                trade.id,
                                trade.order_id,
                                trade.account_id,
                                trade.symbol,
                                trade.side.value,
                                str(trade.quantity),
                                str(trade.price),
                                str(trade.notional),
                                str(trade.fee),
                                trade.fee_asset,
                                trade.is_maker,
                                Jsonb(trade.metadata),
                                trade.timestamp,
                            ),
                        )

                cur.execute(
                    """
                    insert into quantro_engine_state (id, state, updated_at)
                    values (true, %s, now())
                    on conflict (id) do update
                    set state = excluded.state, updated_at = now()
                    """,
                    (Jsonb(state),),
                )
            conn.commit()
