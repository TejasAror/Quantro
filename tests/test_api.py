from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from quantro_api import create_app
from quantro_api.auth import AuthenticatedUser, SupabaseAuthError
from quantro_api.market_data import DemoMarketDataProvider, MarketDataService, MarketDataStatus
from quantro_api.persistence import _ensure_application_user, engine_from_json, engine_to_json
from quantro_api.service import EngineService


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def create_client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=create_app(EngineService())),
        base_url="http://testserver",
    )


def create_demo_market_client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(
            app=create_app(
                EngineService(),
                market_data_service_override=MarketDataService(
                    DemoMarketDataProvider(["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
                ),
            )
        ),
        base_url="http://testserver",
    )


class ResyncingDemoMarketDataProvider(DemoMarketDataProvider):
    async def get_orderbook_snapshot(self, symbol: str, depth: int):
        book = await super().get_orderbook_snapshot(symbol, depth)
        return book.__class__(
            symbol=book.symbol,
            bids=book.bids,
            asks=book.asks,
            sequence=book.sequence,
            exchange_timestamp=book.exchange_timestamp,
            received_timestamp=book.received_timestamp,
            spread=book.spread,
            mid_price=book.mid_price,
            status=MarketDataStatus.RESYNCING,
        )


def create_resyncing_market_client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(
            app=create_app(
                EngineService(),
                market_data_service_override=MarketDataService(
                    ResyncingDemoMarketDataProvider(["BTC-USDT-SWAP"])
                ),
            )
        ),
        base_url="http://testserver",
    )


class FakeAuthClient:
    user_a = UUID("00000000-0000-0000-0000-00000000000a")
    user_b = UUID("00000000-0000-0000-0000-00000000000b")
    credentials = {
        "a@example.com": ("user-a", user_a),
        "b@example.com": ("user-b", user_b),
    }
    signed_out_tokens = set()

    async def user_from_token(self, access_token: str) -> AuthenticatedUser:
        if access_token in self.signed_out_tokens:
            raise SupabaseAuthError("Invalid or expired bearer token")
        for email, (token, user_id) in self.credentials.items():
            if access_token == token:
                return AuthenticatedUser(id=user_id, email=email)
        raise SupabaseAuthError("Invalid token")

    async def signup(self, email: str, password: str) -> dict:
        if email == "duplicate@example.com":
            raise SupabaseAuthError("User already registered")
        if "@" not in email or len(password) < 6:
            raise SupabaseAuthError("Invalid signup credentials")
        if email == "confirm@example.com":
            return {
                "user": {
                    "id": str(UUID("00000000-0000-0000-0000-00000000000c")),
                    "email": email,
                },
                "session": None,
            }
        return self._session_for(email)

    async def login(self, email: str, password: str) -> dict:
        if email == "missing@example.com" or password == "wrong-password":
            raise SupabaseAuthError("Invalid email or password")
        return self._session_for(email)

    async def logout(self, access_token: str) -> None:
        if access_token not in {token for token, _user_id in self.credentials.values()}:
            raise SupabaseAuthError("Invalid or expired bearer token")
        self.signed_out_tokens.add(access_token)

    def _session_for(self, email: str) -> dict:
        token, user_id = self.credentials[email]
        return {
            "access_token": token,
            "refresh_token": f"refresh-{token}",
            "token_type": "bearer",
            "expires_in": 3600,
            "user": {"id": str(user_id), "email": email},
        }


class BrokenAuthClient:
    async def user_from_token(self, access_token: str) -> AuthenticatedUser:
        raise RuntimeError("auth transport failed")

    async def signup(self, email: str, password: str) -> dict:
        raise RuntimeError("auth transport failed")

    async def login(self, email: str, password: str) -> dict:
        raise RuntimeError("auth transport failed")

    async def logout(self, access_token: str) -> None:
        raise RuntimeError("auth transport failed")


class RestartableStore:
    def __init__(self) -> None:
        self.accounts_by_user = {}
        self.state = None


class AuthenticatedTestService(EngineService):
    def __init__(self, store: RestartableStore | None = None) -> None:
        self._store = store or RestartableStore()
        super().__init__()
        if self._store.state is not None:
            self._engine, self._orders, self._trades_by_account = engine_from_json(
                self._store.state
            )

    def get_or_create_user_account(self, auth_user_id, email=None, name=None):
        if auth_user_id in self._store.accounts_by_user:
            account_id = self._store.accounts_by_user[auth_user_id]
            return self.get_portfolio(account_id).account
        account = self.create_account(
            SimpleNamespace(
                name=name or email or "test",
                initial_balances={"USD": "100000", "USDT": "1000000", "BTC": "10"},
            )
        )
        self._store.accounts_by_user[auth_user_id] = account.id
        self._persist_state()
        return account

    def create_account(self, request):
        account = super().create_account(request)
        self._persist_state()
        return account

    def deposit(self, account_id, request):
        portfolio = super().deposit(account_id, request)
        self._persist_state()
        return portfolio

    def submit_order(self, request):
        result = super().submit_order(request)
        self._persist_state()
        return result

    def cancel_order(self, order_id):
        order = super().cancel_order(order_id)
        self._persist_state()
        return order

    def _persist_state(self) -> None:
        self._store.state = engine_to_json(
            self._engine,
            self._orders,
            self._trades_by_account,
        )


class FailingMappingService(AuthenticatedTestService):
    def get_or_create_user_account(self, auth_user_id, email=None, name=None):
        raise RuntimeError("mapping failed")


def create_authenticated_client(service: AuthenticatedTestService) -> AsyncClient:
    FakeAuthClient.signed_out_tokens = set()
    return AsyncClient(
        transport=ASGITransport(app=create_app(service, auth_client_override=FakeAuthClient())),
        base_url="http://testserver",
    )


async def create_account(client: AsyncClient, name: str) -> str:
    response = await client.post("/accounts", json={"name": name})
    assert response.status_code == 201
    body = response.json()
    assert_account_schema(body)
    return body["id"]


async def deposit(client: AsyncClient, account_id: str, asset: str, amount: str) -> dict:
    response = await client.post(
        f"/accounts/{account_id}/deposit",
        json={"asset": asset, "amount": amount},
    )
    assert response.status_code == 200
    body = response.json()
    assert_account_schema(body)
    return body


async def submit_limit_order(
    client: AsyncClient,
    account_id: str,
    side: str,
    quantity: str = "1.0",
    price: str = "50000.00",
    symbol: str = "BTC-USD",
) -> tuple[int, dict]:
    response = await client.post(
        "/orders",
        json={
            "account_id": account_id,
            "symbol": symbol,
            "side": side,
            "order_type": "limit",
            "quantity": quantity,
            "price": price,
        },
    )
    return response.status_code, response.json()


def balance_by_asset(balances: list[dict], asset: str) -> dict:
    return next(balance for balance in balances if balance["asset"] == asset)


def assert_error(body: dict, code: str) -> None:
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["message"]


def assert_account_schema(body: dict) -> None:
    assert set(body) == {"id", "name", "balances", "created_at", "updated_at", "metadata"}
    assert isinstance(body["balances"], dict)


def assert_balance_schema(body: dict) -> None:
    assert set(body) == {"asset", "free", "locked", "total"}
    assert all(isinstance(body[key], str) for key in ("asset", "free", "locked", "total"))


def assert_order_schema(body: dict) -> None:
    assert set(body) == {
        "id",
        "account_id",
        "symbol",
        "side",
        "order_type",
        "status",
        "time_in_force",
        "quantity",
        "price",
        "stop_price",
        "filled_quantity",
        "remaining_quantity",
        "average_fill_price",
        "total_fees",
        "client_order_id",
        "created_at",
        "updated_at",
        "expires_at",
        "metadata",
    }


def assert_trade_schema(body: dict) -> None:
    assert set(body) == {
        "id",
        "order_id",
        "account_id",
        "symbol",
        "side",
        "quantity",
        "price",
        "notional",
        "fee",
        "fee_asset",
        "timestamp",
        "is_maker",
        "metadata",
    }


def assert_orderbook_empty(body: dict) -> None:
    assert body["best_bid"] is None
    assert body["best_ask"] is None
    assert body["bids"] == []
    assert body["asks"] == []


def test_paper_demo_reseed_tops_up_missing_virtual_usdt_without_funding_flow() -> None:
    service = EngineService()
    for symbol in ("BTC-USDT-SWAP", "ETH-USDT-SWAP"):
        old_market = service.get_market(symbol)
        assert old_market is not None
        service.engine._markets[symbol] = replace(  # type: ignore[attr-defined]
            old_market,
            metadata={
                "product_type": "perpetual",
                "instrument_type": "perpetual",
                "settle_asset": "USDT",
                "execution_supported": True,
                "execution_mode": "paper",
                "market_data_source": "okx_public_read_only",
            },
        )
    service._seed_default_markets()

    account = service.create_account(
        SimpleNamespace(name="old-demo", initial_balances={"USD": "100000"})
    )
    assert account.get_balance("USDT").free == service.zero()

    changed = service.ensure_paper_virtual_balances(account.id, {"USDT": "1000000"})
    portfolio = service.get_portfolio(account.id)

    assert changed is True
    assert portfolio is not None
    for symbol in ("BTC-USDT-SWAP", "ETH-USDT-SWAP"):
        market = service.get_market(symbol)
        assert market is not None
        assert market.metadata["real_funds"] is False
        assert market.metadata["venue_routing"] == "disabled"
    assert str(portfolio.account.get_balance("USDT").free) == "1000000"
    assert portfolio.account.get_balance("USDT").locked == service.zero()


def test_paper_virtual_usdt_initialization_is_one_time_for_uninitialized_accounts() -> None:
    service = EngineService()
    account = service.create_account(
        SimpleNamespace(name="paper-once", initial_balances={"USD": "100000"})
    )

    assert service.ensure_paper_virtual_balances(account.id, {"USDT": "1000000"}) is True
    assert service.ensure_paper_virtual_balances(account.id, {"USDT": "1000000"}) is False
    portfolio = service.get_portfolio(account.id)
    assert portfolio is not None
    assert str(portfolio.account.get_balance("USDT").free) == "1000000"

    service.deposit(account.id, SimpleNamespace(asset="USDT", amount="1"))
    portfolio = service.get_portfolio(account.id)
    assert portfolio is not None
    before = portfolio.account.get_balance("USDT").free

    assert service.ensure_paper_virtual_balances(account.id, {"USDT": "1000000"}) is False
    after = service.get_portfolio(account.id).account.get_balance("USDT").free  # type: ignore[union-attr]
    assert after == before


@pytest.mark.anyio
async def test_two_account_buy_sell_http_lifecycle_shared_orderbook() -> None:
    async with create_client() as client:
        buyer_id = await create_account(client, "buyer")
        seller_id = await create_account(client, "seller")

        buyer_account = await deposit(client, buyer_id, "USD", "100000")
        seller_account = await deposit(client, seller_id, "BTC", "10")
        assert buyer_account["balances"]["USD"]["free"] == "100000"
        assert seller_account["balances"]["BTC"]["free"] == "10"

        markets_response = await client.get("/markets")
        assert markets_response.status_code == 200
        markets = markets_response.json()
        assert {market["symbol"] for market in markets} >= {
            "BTC-USD",
            "BTC-USDT-SWAP",
            "ETH-USDT-SWAP",
        }
        assert markets[0]["symbol"] == "BTC-USD"
        assert markets[0]["tick_size"] == "0.01"

        initial_book_response = await client.get("/markets/BTC-USD/orderbook")
        assert initial_book_response.status_code == 200
        initial_book = initial_book_response.json()
        assert initial_book["symbol"] == "BTC-USD"
        assert_orderbook_empty(initial_book)

        buy_status, buy_result = await submit_limit_order(client, buyer_id, "buy")
        assert buy_status == 201
        assert buy_result["accepted"] is True
        assert buy_result["trades"] == []
        buy_order = buy_result["order"]
        assert_order_schema(buy_order)
        assert buy_order["status"] == "open"
        assert buy_order["filled_quantity"] == "0"
        assert buy_order["remaining_quantity"] == "1"

        resting_book = (await client.get("/markets/BTC-USD/orderbook")).json()
        assert resting_book["best_bid"] == "50000"
        assert resting_book["best_ask"] is None
        assert resting_book["bids"][0]["total_quantity"] == "1"
        assert resting_book["bids"][0]["orders"][0]["id"] == buy_order["id"]

        sell_status, sell_result = await submit_limit_order(client, seller_id, "sell")
        assert sell_status == 201
        assert sell_result["accepted"] is True
        sell_order = sell_result["order"]
        assert_order_schema(sell_order)
        assert sell_order["status"] == "filled"
        assert sell_order["filled_quantity"] == "1"
        assert sell_order["remaining_quantity"] == "0"
        assert len(sell_result["trades"]) == 2
        for trade in sell_result["trades"]:
            assert_trade_schema(trade)
            assert trade["quantity"] == "1"
            assert trade["price"] == "50000"
            assert trade["notional"] == "50000"

        buyer_order_response = await client.get(f"/orders/{buy_order['id']}")
        seller_order_response = await client.get(f"/orders/{sell_order['id']}")
        assert buyer_order_response.status_code == 200
        assert seller_order_response.status_code == 200
        retrieved_buy_order = buyer_order_response.json()
        retrieved_sell_order = seller_order_response.json()
        assert retrieved_buy_order["status"] == "filled"
        assert retrieved_buy_order["filled_quantity"] == "1"
        assert retrieved_buy_order["remaining_quantity"] == "0"
        assert retrieved_sell_order["status"] == "filled"

        buyer_orders = (await client.get(f"/accounts/{buyer_id}/orders")).json()["orders"]
        seller_orders = (await client.get(f"/accounts/{seller_id}/orders")).json()["orders"]
        assert [order["id"] for order in buyer_orders] == [buy_order["id"]]
        assert [order["id"] for order in seller_orders] == [sell_order["id"]]
        assert buyer_orders[0]["status"] == "filled"
        assert seller_orders[0]["status"] == "filled"

        buyer_trades = (await client.get(f"/accounts/{buyer_id}/trades")).json()["trades"]
        seller_trades = (await client.get(f"/accounts/{seller_id}/trades")).json()["trades"]
        assert len(buyer_trades) == 1
        assert len(seller_trades) == 1
        assert buyer_trades[0]["order_id"] == buy_order["id"]
        assert seller_trades[0]["order_id"] == sell_order["id"]
        assert buyer_trades[0]["is_maker"] is True
        assert seller_trades[0]["is_maker"] is False

        buyer_balances_response = await client.get(f"/accounts/{buyer_id}/balances")
        seller_balances_response = await client.get(f"/accounts/{seller_id}/balances")
        assert buyer_balances_response.status_code == 200
        assert seller_balances_response.status_code == 200
        buyer_balances = buyer_balances_response.json()["balances"]
        seller_balances = seller_balances_response.json()["balances"]
        for balance in buyer_balances + seller_balances:
            assert_balance_schema(balance)

        buyer_usd = balance_by_asset(buyer_balances, "USD")
        buyer_btc = balance_by_asset(buyer_balances, "BTC")
        seller_usd = balance_by_asset(seller_balances, "USD")
        seller_btc = balance_by_asset(seller_balances, "BTC")
        assert buyer_usd["free"] == "49950"
        assert buyer_usd["locked"] == "0"
        assert buyer_btc["free"] == "1"
        assert seller_usd["free"] == "49950"
        assert seller_btc["free"] == "9"
        assert seller_btc["locked"] == "0"

        buyer_positions_response = await client.get(f"/accounts/{buyer_id}/positions")
        seller_positions_response = await client.get(f"/accounts/{seller_id}/positions")
        assert buyer_positions_response.status_code == 200
        assert seller_positions_response.status_code == 200
        buyer_positions = buyer_positions_response.json()["positions"]
        seller_positions = seller_positions_response.json()["positions"]
        assert buyer_positions[0]["symbol"] == "BTC-USD"
        assert buyer_positions[0]["side"] == "long"
        assert buyer_positions[0]["size"] == "1"
        assert seller_positions[0]["symbol"] == "BTC-USD"
        assert seller_positions[0]["side"] == "short"
        assert seller_positions[0]["size"] == "1"

        buyer_pnl_response = await client.get(f"/accounts/{buyer_id}/pnl")
        seller_pnl_response = await client.get(f"/accounts/{seller_id}/pnl")
        assert buyer_pnl_response.status_code == 200
        assert seller_pnl_response.status_code == 200
        assert buyer_pnl_response.json() == {
            "account_id": buyer_id,
            "total_unrealized_pnl": "0",
            "total_realized_pnl": "0",
            "total_pnl": "0",
        }
        assert seller_pnl_response.json() == {
            "account_id": seller_id,
            "total_unrealized_pnl": "0",
            "total_realized_pnl": "0",
            "total_pnl": "0",
        }

        final_book_response = await client.get("/markets/BTC-USD/orderbook")
        assert final_book_response.status_code == 200
        final_book = final_book_response.json()
        assert final_book["last_trade_price"] == "50000"
        assert final_book["last_trade_quantity"] == "1"
        assert_orderbook_empty(final_book)


@pytest.mark.anyio
async def test_error_cases_return_structured_json_and_have_no_side_effects() -> None:
    async with create_client() as client:
        missing_account_id = str(uuid4())
        missing_order_id = str(uuid4())

        missing_account = await client.get(f"/accounts/{missing_account_id}/balances")
        assert missing_account.status_code == 404
        assert_error(missing_account.json(), "account_not_found")

        missing_order = await client.get(f"/orders/{missing_order_id}")
        assert missing_order.status_code == 404
        assert_error(missing_order.json(), "order_not_found")

        missing_market_book = await client.get("/markets/ETH-USD/orderbook")
        assert missing_market_book.status_code == 404
        assert_error(missing_market_book.json(), "market_not_found")

        buyer_id = await create_account(client, "buyer")

        missing_order_account_status, missing_order_account = await submit_limit_order(
            client,
            missing_account_id,
            "buy",
        )
        assert missing_order_account_status == 404
        assert_error(missing_order_account, "account_not_found")

        missing_order_market_status, missing_order_market = await submit_limit_order(
            client,
            buyer_id,
            "buy",
            symbol="ETH-USD",
        )
        assert missing_order_market_status == 404
        assert_error(missing_order_market, "market_not_found")

        invalid_status, invalid_order = await submit_limit_order(
            client,
            buyer_id,
            "buy",
            quantity="-1",
        )
        assert invalid_status == 400
        assert_error(invalid_order, "invalid_order")

        numeric_financial_value = await client.post(
            f"/accounts/{buyer_id}/deposit",
            json={"asset": "USD", "amount": 1.25},
        )
        assert numeric_financial_value.status_code == 422
        assert_error(numeric_financial_value.json(), "validation_error")

        await deposit(client, buyer_id, "USD", "100")

        insufficient_status, insufficient = await submit_limit_order(
            client,
            buyer_id,
            "buy",
            quantity="1.0",
            price="50000.00",
        )
        assert insufficient_status == 400
        assert_error(insufficient, "insufficient_balance")

        risk_status, risk_rejection = await submit_limit_order(
            client,
            buyer_id,
            "buy",
            quantity="1001.0",
            price="0.01",
        )
        assert risk_status == 400
        assert_error(risk_rejection, "risk_rejected")

        orderbook = (await client.get("/markets/BTC-USD/orderbook")).json()
        assert_orderbook_empty(orderbook)

        orders = (await client.get(f"/accounts/{buyer_id}/orders")).json()["orders"]
        trades = (await client.get(f"/accounts/{buyer_id}/trades")).json()["trades"]
        positions = (await client.get(f"/accounts/{buyer_id}/positions")).json()["positions"]
        balances = (await client.get(f"/accounts/{buyer_id}/balances")).json()["balances"]
        usd = balance_by_asset(balances, "USD")
        assert orders == []
        assert trades == []
        assert positions == []
        assert usd["free"] == "100"
        assert usd["locked"] == "0"

        invalid_cancel = await client.delete(f"/orders/{missing_order_id}")
        assert invalid_cancel.status_code == 404
        assert_error(invalid_cancel.json(), "order_not_found")


@pytest.mark.anyio
async def test_valid_open_order_cancellation_and_invalid_filled_order_cancellation() -> None:
    async with create_client() as client:
        cancel_account_id = await create_account(client, "cancel-buyer")
        await deposit(client, cancel_account_id, "USD", "100000")

        open_status, open_result = await submit_limit_order(client, cancel_account_id, "buy")
        assert open_status == 201
        open_order_id = open_result["order"]["id"]

        cancel_response = await client.delete(f"/orders/{open_order_id}")
        assert cancel_response.status_code == 200
        cancelled = cancel_response.json()
        assert cancelled["status"] == "cancelled"
        assert cancelled["remaining_quantity"] == "1"

        cancel_balances = (await client.get(f"/accounts/{cancel_account_id}/balances")).json()
        cancel_usd = balance_by_asset(cancel_balances["balances"], "USD")
        assert cancel_usd["free"] == "100000"
        assert cancel_usd["locked"] == "0"
        assert_orderbook_empty((await client.get("/markets/BTC-USD/orderbook")).json())

        buyer_id = await create_account(client, "buyer")
        seller_id = await create_account(client, "seller")
        await deposit(client, buyer_id, "USD", "100000")
        await deposit(client, seller_id, "BTC", "10")
        buy_status, buy_result = await submit_limit_order(client, buyer_id, "buy")
        sell_status, sell_result = await submit_limit_order(client, seller_id, "sell")
        assert buy_status == 201
        assert sell_status == 201
        assert buy_result["order"]["status"] == "open"
        assert sell_result["order"]["status"] == "filled"

        invalid_cancel = await client.delete(f"/orders/{sell_result['order']['id']}")
        assert invalid_cancel.status_code == 400
        assert_error(invalid_cancel.json(), "invalid_cancellation")


@pytest.mark.anyio
async def test_authenticated_sandbox_journey_persists_across_restart() -> None:
    store = RestartableStore()
    async with create_authenticated_client(AuthenticatedTestService(store)) as client:
        signup_a = await client.post(
            "/auth/signup",
            json={"email": "a@example.com", "password": "secret-a", "name": "User A"},
        )
        signup_b = await client.post(
            "/auth/signup",
            json={"email": "b@example.com", "password": "secret-b", "name": "User B"},
        )
        assert signup_a.status_code == 201
        assert signup_b.status_code == 201

        body_a = signup_a.json()
        body_b = signup_b.json()
        account_a = body_a["account"]
        account_b = body_b["account"]
        headers_a = {"Authorization": f"Bearer {body_a['access_token']}"}
        headers_b = {"Authorization": f"Bearer {body_b['access_token']}"}

        assert account_a["id"] != account_b["id"]
        assert account_a["balances"]["USD"]["free"] == "100000"
        assert account_a["balances"]["USDT"]["free"] == "1000000"
        assert account_a["balances"]["BTC"]["free"] == "10"
        assert account_b["balances"]["USD"]["free"] == "100000"
        assert account_b["balances"]["USDT"]["free"] == "1000000"
        assert account_b["balances"]["BTC"]["free"] == "10"

        repeated_signup_a = await client.post(
            "/auth/signup",
            json={"email": "a@example.com", "password": "secret-a", "name": "User A"},
        )
        assert repeated_signup_a.status_code == 201
        assert repeated_signup_a.json()["account"]["id"] == account_a["id"]

        login_a = await client.post(
            "/auth/login",
            json={"email": "a@example.com", "password": "secret-a"},
        )
        assert login_a.status_code == 200
        assert login_a.json()["account"]["id"] == account_a["id"]
        assert (await client.get("/me/account", headers=headers_a)).json()["id"] == account_a["id"]

        markets = await client.get("/markets")
        assert markets.status_code == 200
        assert markets.json()[0]["symbol"] == "BTC-USD"
        assert (await client.get("/markets/BTC-USD/orderbook")).status_code == 200

        forbidden_balance = await client.get(
            f"/accounts/{account_b['id']}/balances",
            headers=headers_a,
        )
        assert forbidden_balance.status_code == 403
        assert_error(forbidden_balance.json(), "account_forbidden")

        forbidden_order = await client.post(
            "/orders",
            json={
                "account_id": account_b["id"],
                "symbol": "BTC-USD",
                "side": "buy",
                "order_type": "limit",
                "quantity": "1.0",
                "price": "50000.00",
            },
            headers=headers_a,
        )
        assert forbidden_order.status_code == 403
        assert_error(forbidden_order.json(), "account_forbidden")

        buy = await client.post(
            "/orders",
            json={
                "symbol": "BTC-USD",
                "side": "buy",
                "order_type": "limit",
                "quantity": "1.0",
                "price": "50000.00",
            },
            headers=headers_a,
        )
        sell = await client.post(
            "/orders",
            json={
                "symbol": "BTC-USD",
                "side": "sell",
                "order_type": "limit",
                "quantity": "1.0",
                "price": "50000.00",
            },
            headers=headers_b,
        )
        assert buy.status_code == 201
        assert sell.status_code == 201
        buy_order = buy.json()["order"]
        sell_order = sell.json()["order"]
        assert buy_order["account_id"] == account_a["id"]
        assert sell_order["account_id"] == account_b["id"]
        assert sell_order["status"] == "filled"

        pre_restart_a = await account_state(client, account_a["id"], headers_a)
        pre_restart_b = await account_state(client, account_b["id"], headers_b)
        assert_state_after_match(pre_restart_a, account_a["id"], buy_order["id"], "buy")
        assert_state_after_match(pre_restart_b, account_b["id"], sell_order["id"], "sell")

    async with create_authenticated_client(AuthenticatedTestService(store)) as restarted:
        headers_a = {"Authorization": "Bearer user-a"}
        headers_b = {"Authorization": "Bearer user-b"}
        restored_a = (await restarted.get("/me/account", headers=headers_a)).json()
        restored_b = (await restarted.get("/me/account", headers=headers_b)).json()
        assert restored_a["id"] == account_a["id"]
        assert restored_b["id"] == account_b["id"]

        post_restart_a = await account_state(restarted, account_a["id"], headers_a)
        post_restart_b = await account_state(restarted, account_b["id"], headers_b)
        assert post_restart_a == pre_restart_a
        assert post_restart_b == pre_restart_b

        restored_book = (await restarted.get("/markets/BTC-USD/orderbook")).json()
        assert restored_book["last_trade_price"] == "50000"
        assert restored_book["last_trade_quantity"] == "1"
        assert_orderbook_empty(restored_book)


@pytest.mark.anyio
async def test_five_authenticated_paper_users_have_isolated_accounts_and_state() -> None:
    original_credentials = dict(FakeAuthClient.credentials)
    FakeAuthClient.credentials = {
        f"user-{index}@example.com": (
            f"user-{index}",
            UUID(f"00000000-0000-0000-0000-00000000000{index}"),
        )
        for index in range(1, 6)
    }
    try:
        async with create_authenticated_client(AuthenticatedTestService()) as client:
            sessions = []
            for index in range(1, 6):
                response = await client.post(
                    "/auth/signup",
                    json={
                        "email": f"user-{index}@example.com",
                        "password": f"secret-{index}",
                        "name": f"User {index}",
                    },
                )
                assert response.status_code == 201
                sessions.append(response.json())

            account_ids = [session["account"]["id"] for session in sessions]
            assert len(set(account_ids)) == 5
            for session in sessions:
                balances = session["account"]["balances"]
                assert balances["USDT"]["free"] == "1000000"
                assert balances["USD"]["free"] == "100000"
                assert balances["BTC"]["free"] == "10"

            headers = [
                {"Authorization": f"Bearer {session['access_token']}"} for session in sessions
            ]
            forbidden = await client.get(f"/accounts/{account_ids[1]}/balances", headers=headers[0])
            assert forbidden.status_code == 403
            assert_error(forbidden.json(), "account_forbidden")

            buy = await client.post(
                "/orders",
                json={
                    "symbol": "BTC-USD",
                    "side": "buy",
                    "order_type": "limit",
                    "quantity": "1.0",
                    "price": "50000.00",
                },
                headers=headers[0],
            )
            sell = await client.post(
                "/orders",
                json={
                    "symbol": "BTC-USD",
                    "side": "sell",
                    "order_type": "limit",
                    "quantity": "1.0",
                    "price": "50000.00",
                },
                headers=headers[1],
            )
            assert buy.status_code == 201
            assert sell.status_code == 201

            state_a = await account_state(client, account_ids[0], headers[0])
            state_b = await account_state(client, account_ids[1], headers[1])
            assert_state_after_match(state_a, account_ids[0], buy.json()["order"]["id"], "buy")
            assert_state_after_match(state_b, account_ids[1], sell.json()["order"]["id"], "sell")

            for index in range(2, 5):
                state = await account_state(client, account_ids[index], headers[index])
                balances = state["balances"]["balances"]
                assert balance_by_asset(balances, "USDT")["free"] == "1000000"
                assert balance_by_asset(balances, "USD")["free"] == "100000"
                assert balance_by_asset(balances, "BTC")["free"] == "10"
                assert state["orders"]["orders"] == []
                assert state["trades"]["trades"] == []
                assert state["positions"]["positions"] == []
    finally:
        FakeAuthClient.credentials = original_credentials


@pytest.mark.anyio
async def test_authenticated_signup_login_error_and_logout_paths() -> None:
    service = AuthenticatedTestService()
    async with create_authenticated_client(service) as client:
        duplicate = await client.post(
            "/auth/signup",
            json={"email": "duplicate@example.com", "password": "secret-a"},
        )
        assert duplicate.status_code == 400
        assert_error(duplicate.json(), "signup_failed")

        invalid_signup = await client.post(
            "/auth/signup",
            json={"email": "invalid-email", "password": "short"},
        )
        assert invalid_signup.status_code == 400
        assert_error(invalid_signup.json(), "signup_failed")

        confirmation_required = await client.post(
            "/auth/signup",
            json={"email": "confirm@example.com", "password": "secret-c"},
        )
        assert confirmation_required.status_code == 409
        assert_error(confirmation_required.json(), "email_confirmation_required")
        assert UUID("00000000-0000-0000-0000-00000000000c") not in service._store.accounts_by_user

        invalid_login = await client.post(
            "/auth/login",
            json={"email": "missing@example.com", "password": "wrong-password"},
        )
        assert invalid_login.status_code == 401
        assert_error(invalid_login.json(), "login_failed")

        login = await client.post(
            "/auth/login",
            json={"email": "a@example.com", "password": "secret-a"},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        protected_without_token = await client.get("/me/account")
        assert protected_without_token.status_code == 401
        assert_error(protected_without_token.json(), "authentication_required")

        protected_with_invalid_token = await client.get(
            "/me/account",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert protected_with_invalid_token.status_code == 401
        assert_error(protected_with_invalid_token.json(), "invalid_token")

        protected_with_valid_token = await client.get("/me/account", headers=headers)
        assert protected_with_valid_token.status_code == 200
        assert protected_with_valid_token.json()["id"] == login.json()["account"]["id"]

        logout_without_token = await client.post("/auth/logout")
        assert logout_without_token.status_code == 401
        assert_error(logout_without_token.json(), "authentication_required")

        logout_invalid_token = await client.post(
            "/auth/logout",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert logout_invalid_token.status_code == 401
        assert_error(logout_invalid_token.json(), "logout_failed")

        logout = await client.post("/auth/logout", headers=headers)
        assert logout.status_code == 204

        protected_after_logout = await client.get("/me/account", headers=headers)
        assert protected_after_logout.status_code == 401
        assert_error(protected_after_logout.json(), "invalid_token")


@pytest.mark.anyio
async def test_authenticated_account_mapping_failure_returns_clean_error() -> None:
    async with create_authenticated_client(FailingMappingService()) as client:
        signup = await client.post(
            "/auth/signup",
            json={"email": "a@example.com", "password": "secret-a", "name": "User A"},
        )
        assert signup.status_code == 500
        assert_error(signup.json(), "account_mapping_failed")

        login = await client.post(
            "/auth/login",
            json={"email": "a@example.com", "password": "secret-a"},
        )
        assert login.status_code == 500
        assert_error(login.json(), "account_mapping_failed")

        me = await client.get("/me/account", headers={"Authorization": "Bearer user-a"})
        assert me.status_code == 500
        assert_error(me.json(), "account_mapping_failed")


@pytest.mark.anyio
async def test_unexpected_auth_failure_returns_clean_error() -> None:
    app = create_app(AuthenticatedTestService(), auth_client_override=BrokenAuthClient())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        signup = await client.post(
            "/auth/signup",
            json={"email": "a@example.com", "password": "secret-a", "name": "User A"},
        )
        assert signup.status_code == 503
        assert_error(signup.json(), "auth_service_unavailable")

        login = await client.post(
            "/auth/login",
            json={"email": "a@example.com", "password": "secret-a"},
        )
        assert login.status_code == 503
        assert_error(login.json(), "auth_service_unavailable")

        me = await client.get("/me/account", headers={"Authorization": "Bearer user-a"})
        assert me.status_code == 503
        assert_error(me.json(), "auth_service_unavailable")

        logout = await client.post("/auth/logout", headers={"Authorization": "Bearer user-a"})
        assert logout.status_code == 503
        assert_error(logout.json(), "auth_service_unavailable")


def test_application_user_row_is_upserted_for_public_users_fk() -> None:
    user_id = UUID("00000000-0000-0000-0000-00000000000a")
    cursor = FakeCursor(
        fk_row={
            "referenced_schema": "public",
            "referenced_table": "users",
            "referenced_column": "id",
        },
        columns={"id", "email", "created_at", "updated_at"},
    )

    _ensure_application_user(cursor, user_id, "a@example.com")

    assert len(cursor.executions) == 3
    assert cursor.executions[2][1] == [user_id, "a@example.com"]


def test_application_user_row_is_not_inserted_for_supabase_auth_fk() -> None:
    user_id = UUID("00000000-0000-0000-0000-00000000000a")
    cursor = FakeCursor(
        fk_row={
            "referenced_schema": "auth",
            "referenced_table": "users",
            "referenced_column": "id",
        },
        columns={"id", "email", "created_at", "updated_at"},
        parent_exists=True,
    )

    _ensure_application_user(cursor, user_id, "a@example.com")

    assert len(cursor.executions) == 2


def test_missing_supabase_auth_user_stops_before_quantro_user_insert() -> None:
    user_id = UUID("00000000-0000-0000-0000-00000000000a")
    cursor = FakeCursor(
        fk_row={
            "referenced_schema": "auth",
            "referenced_table": "users",
            "referenced_column": "id",
        },
        columns={"id", "email", "created_at", "updated_at"},
        parent_exists=False,
    )

    with pytest.raises(RuntimeError, match="not present in auth.users"):
        _ensure_application_user(cursor, user_id, "a@example.com")


class FakeCursor:
    def __init__(
        self,
        fk_row: dict,
        columns: set[str],
        parent_exists: bool = False,
    ) -> None:
        self._fk_row = fk_row
        self._columns = columns
        self._parent_exists = parent_exists
        self.executions = []
        self._result = None

    def execute(self, query, params=None) -> None:
        self.executions.append((query, params))
        text = str(query)
        if "pg_constraint" in text:
            self._result = self._fk_row
        elif "select 1" in text:
            self._result = {"exists": 1} if self._parent_exists else None
        elif "information_schema.columns" in text:
            self._result = [{"column_name": column} for column in self._columns]
        else:
            self._result = None

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._result


async def account_state(client: AsyncClient, account_id: str, headers: dict[str, str]) -> dict:
    balances = await client.get(f"/accounts/{account_id}/balances", headers=headers)
    orders = await client.get(f"/accounts/{account_id}/orders", headers=headers)
    trades = await client.get(f"/accounts/{account_id}/trades", headers=headers)
    positions = await client.get(f"/accounts/{account_id}/positions", headers=headers)
    pnl = await client.get(f"/accounts/{account_id}/pnl", headers=headers)
    assert balances.status_code == 200
    assert orders.status_code == 200
    assert trades.status_code == 200
    assert positions.status_code == 200
    assert pnl.status_code == 200
    return {
        "balances": balances.json(),
        "orders": orders.json(),
        "trades": trades.json(),
        "positions": positions.json(),
        "pnl": pnl.json(),
    }


def assert_state_after_match(state: dict, account_id: str, order_id: str, side: str) -> None:
    balances = state["balances"]["balances"]
    usd = balance_by_asset(balances, "USD")
    btc = balance_by_asset(balances, "BTC")
    if side == "buy":
        assert usd["free"] == "49950"
        assert btc["free"] == "11"
        expected_position = "long"
    else:
        assert usd["free"] == "149950"
        assert btc["free"] == "9"
        expected_position = "short"

    assert state["balances"]["account_id"] == account_id
    assert usd["locked"] == "0"
    assert btc["locked"] == "0"
    assert [order["id"] for order in state["orders"]["orders"]] == [order_id]
    assert len(state["trades"]["trades"]) == 1
    assert state["trades"]["trades"][0]["order_id"] == order_id
    assert len(state["positions"]["positions"]) == 1
    assert state["positions"]["positions"][0]["side"] == expected_position
    assert state["positions"]["positions"][0]["size"] == "1"
    assert state["pnl"] == {
        "account_id": account_id,
        "total_unrealized_pnl": "0",
        "total_realized_pnl": "0",
        "total_pnl": "0",
    }


@pytest.mark.anyio
async def test_paper_swap_market_order_updates_positions_without_live_routing() -> None:
    async with create_demo_market_client() as client:
        response = await client.post(
            "/accounts",
            json={
                "name": "paper-swap",
                "initial_balances": {"USDT": "1000000"},
            },
        )
        assert response.status_code == 201
        account_id = response.json()["id"]
        assert response.json()["balances"]["USDT"]["free"] == "1000000"
        markets = (await client.get("/markets")).json()
        swap_markets = {
            market["symbol"]: market
            for market in markets
            if market["symbol"] in {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}
        }
        assert set(swap_markets) == {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}
        for market in swap_markets.values():
            assert market["quote_asset"] == "USDT"
            assert market["metadata"]["execution_mode"] == "paper"
            assert market["metadata"]["real_funds"] is False
            assert market["metadata"]["venue_routing"] == "disabled"

        fills = {}
        for symbol, quantity in (
            ("ETH-USDT-SWAP", "1.000"),
            ("BTC-USDT-SWAP", "0.0100"),
        ):
            response = await client.post(
                "/orders",
                json={
                    "account_id": account_id,
                    "symbol": symbol,
                    "side": "buy",
                    "order_type": "market",
                    "time_in_force": "ioc",
                    "quantity": quantity,
                    "price": "0",
                    "metadata": {"source": "quantro_terminal"},
                },
            )

            assert response.status_code == 201
            body = response.json()
            assert body["accepted"] is True
            assert body["order"]["status"] == "filled"
            assert body["order"]["metadata"]["execution_mode"] == "paper"
            assert body["order"]["metadata"]["real_funds"] is False
            assert body["order"]["metadata"]["venue_routing"] == "disabled"
            assert body["trades"][0]["metadata"]["fill_source"] == (
                "backend_public_market_data_reference"
            )
            assert body["trades"][0]["fee_asset"] == "USDT"
            fills[symbol] = body["order"]["average_fill_price"]

        positions = (await client.get(f"/accounts/{account_id}/positions")).json()["positions"]
        pnl = (await client.get(f"/accounts/{account_id}/pnl")).json()
        balances = (await client.get(f"/accounts/{account_id}/balances")).json()["balances"]

        positions_by_symbol = {position["symbol"]: position for position in positions}
        assert set(positions_by_symbol) == {"ETH-USDT-SWAP", "BTC-USDT-SWAP"}
        assert positions_by_symbol["ETH-USDT-SWAP"]["side"] == "long"
        assert positions_by_symbol["ETH-USDT-SWAP"]["entry_price"] == fills["ETH-USDT-SWAP"]
        assert positions_by_symbol["BTC-USDT-SWAP"]["side"] == "long"
        assert positions_by_symbol["BTC-USDT-SWAP"]["entry_price"] == fills["BTC-USDT-SWAP"]
        assert pnl["total_pnl"] == "0"
        assert float(balance_by_asset(balances, "USDT")["free"]) < 1000000


@pytest.mark.anyio
async def test_unhealthy_market_data_blocks_paper_swap_order_fill() -> None:
    async with create_resyncing_market_client() as client:
        account_id = await create_account(client, "paper-swap-blocked")
        await deposit(client, account_id, "USDT", "100000")

        response = await client.post(
            "/orders",
            json={
                "account_id": account_id,
                "symbol": "BTC-USDT-SWAP",
                "side": "buy",
                "order_type": "market",
                "time_in_force": "ioc",
                "quantity": "0.0100",
                "price": "50000",
            },
        )

        assert response.status_code == 503
        assert_error(response.json(), "market_data_not_healthy")
        assert (await client.get(f"/accounts/{account_id}/trades")).json()["trades"] == []
        assert (await client.get(f"/accounts/{account_id}/positions")).json()["positions"] == []
