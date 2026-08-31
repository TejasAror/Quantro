"""FastAPI application for the Quantro sandbox REST API."""
from __future__ import annotations
import logging
import time

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    Security,
    WebSocket,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from quantro import Account

from .auth import SupabaseAuthClient, SupabaseAuthError, bearer_token_from_request
from .config import MarketDataSettings, SupabaseSettings
from .market_data import (
    DemoMarketDataProvider,
    MarketDataService,
    MarketDataStatus,
    MarketDataUnavailableError,
    OKXMarketDataProvider,
    OKXMarketDataStreamManager,
    create_okx_market_data_service,
)
from .persistence import AccountProvisioningError
from .schemas import (
    AccountCreate,
    AccountResponse,
    AuthLoginRequest,
    AuthSessionResponse,
    AuthSignupRequest,
    BalancesResponse,
    CandlesResponse,
    DepositRequest,
    MarketResponse,
    MarketTickerResponse,
    OrderBookResponse,
    OrderCreate,
    OrderResponse,
    OrderResultResponse,
    OrdersResponse,
    PnlResponse,
    PositionsResponse,
    PublicTradesResponse,
    TradesResponse,
)
from .serializers import (
    account_response,
    balance_response,
    candles_response,
    external_market_response,
    external_order_book_response,
    market_response,
    market_ticker_response,
    order_book_response,
    order_response,
    position_response,
    public_trades_response,
    risk_report_response,
    trade_response,
)
from .service import EngineService, create_service

logger = logging.getLogger(__name__)


def build_market_data_service(settings: MarketDataSettings | None) -> MarketDataService:
    if settings is None or settings.provider in {"", "none", "disabled"}:
        return MarketDataService(None)
    if settings.provider in {"demo", "local"}:
        return MarketDataService(DemoMarketDataProvider(list(settings.symbols)))
    if settings.provider != "okx":
        return MarketDataService(None)
    return create_okx_market_data_service(
        symbols=list(settings.symbols),
        rest_base_url=settings.okx_rest_base_url,
        ws_public_url=settings.okx_ws_public_url,
    )


def create_app(
    service: EngineService | None = None,
    auth_client_override: SupabaseAuthClient | None = None,
    market_data_service_override: MarketDataService | None = None,
) -> FastAPI:
    engine_service = service or create_service()
    supabase_settings = None if service is not None else SupabaseSettings.from_env()
    market_data_settings = (
        None
        if service is not None or market_data_service_override is not None
        else MarketDataSettings.from_env()
    )
    market_data_service = market_data_service_override or build_market_data_service(
        market_data_settings,
    )
    market_data_stream = (
        OKXMarketDataStreamManager(
            market_data_service.provider,
            list(market_data_settings.symbols),
        )
        if market_data_settings is not None
        and isinstance(market_data_service.provider, OKXMarketDataProvider)
        else None
    )
    market_data_service.stream_manager = market_data_stream
    auth_client = (
        auth_client_override
        if auth_client_override is not None
        else SupabaseAuthClient(supabase_settings)
        if supabase_settings is not None
        else None
    )
    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        if market_data_stream is not None:
            market_data_stream.start()
        try:
            yield
        finally:
            if market_data_stream is not None:
                await market_data_stream.stop()

    app = FastAPI(
        title="Quantro Sandbox API",
        version="0.1.0",
        description="Sandbox REST API around the deterministic Quantro trading engine.",
        lifespan=lifespan,
    )
    bearer_auth = HTTPBearer(
        scheme_name="BearerAuth",
        bearerFormat="JWT",
        auto_error=False,
    )

    @app.exception_handler(HTTPException)
    async def http_error_handler(
        _request: Request,
        exc: HTTPException,
    ) -> JSONResponse:
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            content = exc.detail
        else:
            content = {"error": {"code": "http_error", "message": str(exc.detail)}}
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed",
                },
                "details": exc.errors(),
            },
        )

    async def get_service() -> EngineService:
        return engine_service

    async def get_market_data_service() -> MarketDataService:
        return market_data_service

    def api_error(status_code: int, code: str, message: str) -> HTTPException:
        return HTTPException(
            status_code=status_code,
            detail={"error": {"code": code, "message": message}},
        )

    def auth_enabled() -> bool:
        return auth_client is not None

    def require_owned_account(account_id: UUID, current_account: AccountResponse | None) -> None:
        if current_account is not None and account_id != current_account.id:
            raise api_error(
                status.HTTP_403_FORBIDDEN,
                "account_forbidden",
                "Authenticated users can only access their linked Quantro account",
            )

    def account_id_for_order(
        request: OrderCreate,
        current_account: AccountResponse | None,
    ) -> UUID:
        if current_account is None:
            if request.account_id is None:
                raise api_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "validation_error",
                    "account_id is required when authentication is disabled",
                )
            return request.account_id
        if request.account_id is not None and request.account_id != current_account.id:
            raise api_error(
                status.HTTP_403_FORBIDDEN,
                "account_forbidden",
                "Orders must use the authenticated user's linked Quantro account",
            )
        return current_account.id

    async def paper_market_data_reference(symbol: str, market_data: MarketDataService) -> str:
        if not market_data.supports(symbol):
            raise api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "market_data_unavailable",
                "Healthy backend market data is required for paper swap orders",
            )
        try:
            ticker, book = await market_data.get_ticker(symbol), await market_data.get_orderbook(
                symbol,
                5,
            )
        except (KeyError, MarketDataUnavailableError) as exc:
            raise api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "market_data_unavailable",
                "Healthy backend market data is required for paper swap orders",
            ) from exc

        now = datetime.now(UTC)
        ticker_age = (now - ticker.received_timestamp).total_seconds()
        book_age = (now - book.received_timestamp).total_seconds()
        if (
            str(ticker.status) != MarketDataStatus.SYNCED
            or str(book.status) != MarketDataStatus.SYNCED
            or ticker_age > 15
            or book_age > 15
        ):
            raise api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "market_data_not_healthy",
                "Paper trading is blocked until market data is synced and fresh",
            )
        reference_price = ticker.mark_price or ticker.last_price or book.mid_price
        if reference_price is None:
            raise api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "market_data_unavailable",
                "Paper trading is blocked because no backend reference price is available",
            )
        return reference_price

    def user_from_auth_body(body: dict) -> dict:
        session = body.get("session") or {}
        return body.get("user") or session.get("user") or body

    def access_token_from_auth_body(body: dict) -> str | None:
        session = body.get("session") or body
        token = session.get("access_token")
        return token if isinstance(token, str) and token else None

    def auth_response(body: dict, account: AccountResponse) -> AuthSessionResponse:
        session = body.get("session") or body
        user = user_from_auth_body(body)
        return AuthSessionResponse(
            access_token=session.get("access_token"),
            refresh_token=session.get("refresh_token"),
            token_type=session.get("token_type", "bearer"),
            expires_in=session.get("expires_in"),
            user_id=UUID(user["id"]),
            account=account,
        )

    def get_or_create_authenticated_account(
        svc: EngineService,
        user_id: UUID,
        email: str | None = None,
        name: str | None = None,
    ) -> Account:
        try:
            return cast(
                Account,
                svc.get_or_create_user_account(user_id, email, name),  # type: ignore[attr-defined]
            )
        except AccountProvisioningError as exc:
            raise api_error(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "account_mapping_failed",
                str(exc),
            ) from exc
        except Exception as exc:
            raise api_error(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "account_mapping_failed",
                "Could not provision the authenticated Quantro account",
            ) from exc

    async def get_current_account(
        request: Request,
        _credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
    ) -> AccountResponse | None:
        if not auth_enabled():
            return None
        token = bearer_token_from_request(request)
        if token is None:
            raise api_error(
                status.HTTP_401_UNAUTHORIZED,
                "authentication_required",
                "Bearer token is required",
            )
        try:
            user = await auth_client.user_from_token(token)  # type: ignore[union-attr]
        except SupabaseAuthError as exc:
            raise api_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", str(exc)) from exc
        except Exception as exc:
            raise api_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
                "auth_service_unavailable",
                "Authentication service is unavailable",
            ) from exc

        if not hasattr(engine_service, "get_or_create_user_account"):
            raise api_error(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "auth_not_configured",
                "Authenticated mode requires the persistent Supabase service",
            )

        account = get_or_create_authenticated_account(engine_service, user.id, user.email)
        return account_response(account)

    @app.post(
        "/auth/signup",
        response_model=AuthSessionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def signup(
        request: AuthSignupRequest,
        svc: EngineService = Depends(get_service),
    ) -> AuthSessionResponse:
        if auth_client is None or not hasattr(svc, "get_or_create_user_account"):
            raise api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "auth_not_configured",
                "Supabase Auth is not configured",
            )
        try:
            body = await auth_client.signup(request.email, request.password)
        except SupabaseAuthError as exc:
            raise api_error(status.HTTP_400_BAD_REQUEST, "signup_failed", str(exc)) from exc
        except Exception as exc:
            raise api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "auth_service_unavailable",
                "Authentication service is unavailable",
            ) from exc

        user_body = user_from_auth_body(body)
        if access_token_from_auth_body(body) is None:
            raise api_error(
                status.HTTP_409_CONFLICT,
                "email_confirmation_required",
                "Check your email to confirm your account before signing in.",
            )
        user_id = UUID(user_body["id"])
        account = get_or_create_authenticated_account(
            svc,
            user_id,
            user_body.get("email"),
            request.name,
        )
        return auth_response(body, account_response(account))

    @app.post("/auth/login", response_model=AuthSessionResponse)
    async def login(
        request: AuthLoginRequest,
        svc: EngineService = Depends(get_service),
    ) -> AuthSessionResponse:
        if auth_client is None or not hasattr(svc, "get_or_create_user_account"):
            raise api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "auth_not_configured",
                "Supabase Auth is not configured",
            )
        try:
            body = await auth_client.login(request.email, request.password)
        except SupabaseAuthError as exc:
            raise api_error(status.HTTP_401_UNAUTHORIZED, "login_failed", str(exc)) from exc
        except Exception as exc:
            raise api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "auth_service_unavailable",
                "Authentication service is unavailable",
            ) from exc

        user_body = user_from_auth_body(body)
        user_id = UUID(user_body["id"])
        account = get_or_create_authenticated_account(svc, user_id, user_body.get("email"))
        return auth_response(body, account_response(account))

    @app.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(request: Request) -> Response:
        if auth_client is None:
            raise api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "auth_not_configured",
                "Supabase Auth is not configured",
            )
        token = bearer_token_from_request(request)
        if token is None:
            raise api_error(
                status.HTTP_401_UNAUTHORIZED,
                "authentication_required",
                "Bearer token is required",
            )
        try:
            await auth_client.logout(token)
        except SupabaseAuthError as exc:
            raise api_error(status.HTTP_401_UNAUTHORIZED, "logout_failed", str(exc)) from exc
        except Exception as exc:
            raise api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "auth_service_unavailable",
                "Authentication service is unavailable",
            ) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/me/account", response_model=AccountResponse)
    async def get_my_account(
        current_account: AccountResponse | None = Depends(get_current_account),
    ) -> AccountResponse:
        if current_account is None:
            raise api_error(
                status.HTTP_401_UNAUTHORIZED,
                "authentication_required",
                "Bearer token is required",
            )
        return current_account

    @app.post("/accounts", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
    async def create_account(
        request: AccountCreate,
        svc: EngineService = Depends(get_service),
        current_account: AccountResponse | None = Depends(get_current_account),
    ) -> AccountResponse:
        if current_account is not None:
            return current_account
        try:
            return account_response(svc.create_account(request))
        except ValueError as exc:
            raise api_error(status.HTTP_400_BAD_REQUEST, "invalid_account", str(exc)) from exc

    @app.post("/accounts/{account_id}/deposit", response_model=AccountResponse)
    async def deposit(
        account_id: UUID,
        request: DepositRequest,
        svc: EngineService = Depends(get_service),
        current_account: AccountResponse | None = Depends(get_current_account),
    ) -> AccountResponse:
        require_owned_account(account_id, current_account)
        if svc.get_portfolio(account_id) is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "account_not_found", "Account not found")
        try:
            portfolio = svc.deposit(account_id, request)
        except ValueError as exc:
            raise api_error(status.HTTP_400_BAD_REQUEST, "invalid_deposit", str(exc)) from exc

        if portfolio is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "account_not_found", "Account not found")
        return account_response(portfolio.account)

    @app.get("/markets", response_model=list[MarketResponse])
    async def list_markets(
        svc: EngineService = Depends(get_service),
        market_data: MarketDataService = Depends(get_market_data_service),
    ) -> list[MarketResponse]:
        markets = [market_response(market) for market in svc.list_markets()]

        try:
            external_start = time.perf_counter()
            external = await market_data.list_instruments()
            logger.info(
                "Markets instruments retrieved in %.3f s",
                time.perf_counter() - external_start,
            )
        except MarketDataUnavailableError as exc:
            raise api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "market_data_unavailable",
                str(exc),
            ) from exc
        existing = {market.symbol for market in markets}
        markets.extend(
            external_market_response(instrument)
            for instrument in external
            if instrument.symbol not in existing
        )
        return markets

    @app.get("/markets/{symbol}/orderbook", response_model=OrderBookResponse)
    async def get_order_book(
        symbol: str,
        depth: int = Query(10, ge=1, le=100),
        svc: EngineService = Depends(get_service),
        market_data: MarketDataService = Depends(get_market_data_service),
    ) -> OrderBookResponse:
        if market_data.supports(symbol):
            try:
                return external_order_book_response(await market_data.get_orderbook(symbol, depth))
            except MarketDataUnavailableError as exc:
                raise api_error(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "market_data_unavailable",
                    str(exc),
                ) from exc
        order_book = svc.engine.get_order_book(symbol)
        if order_book is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "market_not_found", "Market not found")
        return order_book_response(order_book, depth)

    @app.get("/markets/{symbol}/ticker", response_model=MarketTickerResponse)
    async def get_market_ticker(
        symbol: str,
        market_data: MarketDataService = Depends(get_market_data_service),
    ) -> MarketTickerResponse:
        try:
            return market_ticker_response(await market_data.get_ticker(symbol))
        except KeyError as exc:
            raise api_error(
                status.HTTP_404_NOT_FOUND,
                "market_not_found",
                "Market not found",
            ) from exc
        except MarketDataUnavailableError as exc:
            raise api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "market_data_unavailable",
                str(exc),
            ) from exc

    @app.get("/markets/{symbol}/trades", response_model=PublicTradesResponse)
    async def get_public_market_trades(
        symbol: str,
        limit: int = Query(50, ge=1, le=100),
        market_data: MarketDataService = Depends(get_market_data_service),
    ) -> PublicTradesResponse:
        try:
            return public_trades_response(
                symbol,
                cast(list[object], await market_data.get_trades(symbol, limit)),
            )
        except KeyError as exc:
            raise api_error(
                status.HTTP_404_NOT_FOUND,
                "market_not_found",
                "Market not found",
            ) from exc
        except MarketDataUnavailableError as exc:
            raise api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "market_data_unavailable",
                str(exc),
            ) from exc

    @app.get("/markets/{symbol}/candles", response_model=CandlesResponse)
    async def get_market_candles(
        symbol: str,
        interval: str = Query("1h"),
        limit: int = Query(300, ge=1, le=500),
        market_data: MarketDataService = Depends(get_market_data_service),
    ) -> CandlesResponse:
        try:
            candles = await market_data.get_candles(symbol, interval, limit)
            return candles_response(symbol, interval, cast(list[object], candles))
        except ValueError as exc:
            raise api_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_interval",
                str(exc),
            ) from exc
        except KeyError as exc:
            raise api_error(
                status.HTTP_404_NOT_FOUND,
                "market_not_found",
                "Market not found",
            ) from exc
        except MarketDataUnavailableError as exc:
            raise api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "market_data_unavailable",
                str(exc),
            ) from exc

    @app.websocket("/ws/market-data")
    async def market_data_websocket(websocket: WebSocket) -> None:
        if market_data_stream is None:
            await websocket.accept()
            await websocket.send_json(
                {
                    "type": "status",
                    "status": "unavailable",
                    "message": "External market-data streaming is not configured.",
                }
            )
            await websocket.close()
            return

        await websocket.accept()
        await websocket.send_json({"type": "status", "status": str(market_data_stream.status)})
        async for event in market_data_stream.subscribe():
            await websocket.send_json(
                {
                    "type": "raw_market_data",
                    "provider": event.provider,
                    "venue": event.venue,
                    "channel": event.channel,
                    "symbol": event.symbol,
                    "event_type": event.event_type,
                    "sequence_id": event.sequence_id,
                    "previous_sequence_id": event.previous_sequence_id,
                    "exchange_timestamp": event.exchange_timestamp.isoformat()
                    if event.exchange_timestamp
                    else None,
                    "received_timestamp": event.received_timestamp.isoformat(),
                    "raw_payload": event.raw_payload,
                }
            )

    @app.post("/orders", response_model=OrderResultResponse, status_code=status.HTTP_201_CREATED)
    async def submit_order(
        request: OrderCreate,
        svc: EngineService = Depends(get_service),
        market_data: MarketDataService = Depends(get_market_data_service),
        current_account: AccountResponse | None = Depends(get_current_account),
    ) -> OrderResultResponse:
        account_id = account_id_for_order(request, current_account)
        request = request.model_copy(update={"account_id": account_id})
        market = svc.get_market(request.symbol)
        if market is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "market_not_found", "Market not found")
        if market.metadata.get("product_type") == "perpetual":
            reference_price = await paper_market_data_reference(request.symbol, market_data)
            metadata = {
                **request.metadata,
                "paper_reference_price": reference_price,
                "market_data_status": "synced",
            }
            price = reference_price if request.order_type.value == "market" else request.price
            request = request.model_copy(update={"metadata": metadata, "price": price})
        if svc.get_portfolio(account_id) is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "account_not_found", "Account not found")

        try:
            result = svc.submit_order(request)
        except ValueError as exc:
            raise api_error(status.HTTP_400_BAD_REQUEST, "invalid_order", str(exc)) from exc

        if not result.accepted:
            failed_checks = result.risk_report.failed_checks
            code = (
                "insufficient_balance"
                if any(check.name == "available_balance" for check in failed_checks)
                else "risk_rejected"
            )
            message = result.reject_reason or "Order rejected"
            raise api_error(status.HTTP_400_BAD_REQUEST, code, message)

        return OrderResultResponse(
            accepted=result.accepted,
            reject_reason=result.reject_reason,
            order=order_response(result.order),
            risk_report=risk_report_response(result.risk_report),
            trades=[trade_response(trade) for trade in result.trades],
        )

    @app.get("/orders/{order_id}", response_model=OrderResponse)
    async def get_order(
        order_id: UUID,
        svc: EngineService = Depends(get_service),
        current_account: AccountResponse | None = Depends(get_current_account),
    ) -> OrderResponse:
        order = svc.get_order(order_id)
        if order is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "order_not_found", "Order not found")
        require_owned_account(order.account_id, current_account)
        return order_response(order)

    @app.delete("/orders/{order_id}", response_model=OrderResponse)
    async def cancel_order(
        order_id: UUID,
        svc: EngineService = Depends(get_service),
        current_account: AccountResponse | None = Depends(get_current_account),
    ) -> OrderResponse:
        order = svc.get_order(order_id)
        if order is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "order_not_found", "Order not found")
        require_owned_account(order.account_id, current_account)
        try:
            cancelled = svc.cancel_order(order_id)
        except ValueError as exc:
            raise api_error(status.HTTP_400_BAD_REQUEST, "invalid_cancellation", str(exc)) from exc
        if cancelled is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "order_not_found", "Order not found")
        return order_response(cancelled)

    @app.get("/accounts/{account_id}/orders", response_model=OrdersResponse)
    async def list_account_orders(
        account_id: UUID,
        svc: EngineService = Depends(get_service),
        current_account: AccountResponse | None = Depends(get_current_account),
    ) -> OrdersResponse:
        require_owned_account(account_id, current_account)
        if svc.get_portfolio(account_id) is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "account_not_found", "Account not found")
        return OrdersResponse(
            orders=[order_response(order) for order in svc.list_account_orders(account_id)]
        )

    @app.get("/accounts/{account_id}/trades", response_model=TradesResponse)
    async def list_account_trades(
        account_id: UUID,
        svc: EngineService = Depends(get_service),
        current_account: AccountResponse | None = Depends(get_current_account),
    ) -> TradesResponse:
        require_owned_account(account_id, current_account)
        if svc.get_portfolio(account_id) is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "account_not_found", "Account not found")
        return TradesResponse(
            trades=[trade_response(trade) for trade in svc.list_account_trades(account_id)]
        )

    @app.get("/accounts/{account_id}/balances", response_model=BalancesResponse)
    async def get_account_balances(
        account_id: UUID,
        svc: EngineService = Depends(get_service),
        current_account: AccountResponse | None = Depends(get_current_account),
    ) -> BalancesResponse:
        require_owned_account(account_id, current_account)
        portfolio = svc.get_portfolio(account_id)
        if portfolio is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "account_not_found", "Account not found")
        return BalancesResponse(
            account_id=account_id,
            balances=[balance_response(balance) for balance in portfolio.get_balances().values()],
        )

    @app.get("/accounts/{account_id}/positions", response_model=PositionsResponse)
    async def get_account_positions(
        account_id: UUID,
        svc: EngineService = Depends(get_service),
        current_account: AccountResponse | None = Depends(get_current_account),
    ) -> PositionsResponse:
        require_owned_account(account_id, current_account)
        portfolio = svc.get_portfolio(account_id)
        if portfolio is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "account_not_found", "Account not found")
        return PositionsResponse(
            account_id=account_id,
            positions=[position_response(position) for position in portfolio.positions.values()],
        )

    @app.get("/accounts/{account_id}/pnl", response_model=PnlResponse)
    async def get_account_pnl(
        account_id: UUID,
        svc: EngineService = Depends(get_service),
        current_account: AccountResponse | None = Depends(get_current_account),
    ) -> PnlResponse:
        require_owned_account(account_id, current_account)
        portfolio = svc.get_portfolio(account_id)
        if portfolio is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "account_not_found", "Account not found")

        unrealized = portfolio.get_total_unrealized_pnl()
        realized = portfolio.get_total_realized_pnl()
        return PnlResponse(
            account_id=account_id,
            total_unrealized_pnl=str(unrealized),
            total_realized_pnl=str(realized),
            total_pnl=str(unrealized + realized),
        )

    return app


class LazyApp:
    def __init__(self) -> None:
        self._app: FastAPI | None = None

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if self._app is None:
            self._app = create_app()
        await self._app(scope, receive, send)


app = LazyApp()
