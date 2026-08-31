"""Runtime configuration for Supabase-backed deployments."""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency is optional for in-memory tests
    load_dotenv = None


if load_dotenv is not None:
    load_dotenv()


@dataclass(frozen=True, slots=True)
class SupabaseSettings:
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str
    database_url: str
    auto_migrate: bool = True
    sandbox_initial_balances: dict[str, str] | None = None

    @classmethod
    def from_env(cls) -> SupabaseSettings | None:
        supabase_url = os.getenv("SUPABASE_URL")
        supabase_anon_key = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_PUBLISHABLE_KEY")
        supabase_service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv(
            "SUPABASE_SECRET_KEY"
        )
        database_url = os.getenv("DATABASE_URL")

        if not all((supabase_url, supabase_anon_key, supabase_service_role_key, database_url)):
            return None

        return cls(
            supabase_url=supabase_url.rstrip("/"),
            supabase_anon_key=supabase_anon_key,
            supabase_service_role_key=supabase_service_role_key,
            database_url=database_url,
            auto_migrate=os.getenv("QUANTRO_AUTO_MIGRATE", "1") != "0",
            sandbox_initial_balances=parse_sandbox_initial_balances(
                os.getenv(
                    "QUANTRO_SANDBOX_INITIAL_BALANCES",
                    "USD=100000,USDT=1000000,BTC=10,ETH=100",
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class MarketDataSettings:
    provider: str
    symbols: tuple[str, ...]
    okx_rest_base_url: str
    okx_ws_public_url: str

    @classmethod
    def from_env(cls) -> MarketDataSettings:
        symbols = tuple(
            symbol.strip().upper()
            for symbol in os.getenv(
                "QUANTRO_MARKET_DATA_SYMBOLS",
                "BTC-USDT-SWAP,ETH-USDT-SWAP",
            ).split(",")
            if symbol.strip()
        )
        return cls(
            provider=os.getenv("QUANTRO_MARKET_DATA_PROVIDER", "okx").strip().lower(),
            symbols=symbols,
            okx_rest_base_url=os.getenv("OKX_REST_BASE_URL", "https://www.okx.com").rstrip("/"),
            okx_ws_public_url=os.getenv(
                "OKX_WS_PUBLIC_URL",
                "wss://ws.okx.com:8443/ws/v5/public",
            ),
        )


def parse_sandbox_initial_balances(raw: str) -> dict[str, str]:
    balances = {}
    for item in raw.split(","):
        if not item.strip():
            continue
        asset, separator, amount = item.partition("=")
        if not separator or not asset.strip() or not amount.strip():
            raise ValueError("Sandbox balances must use ASSET=AMOUNT comma-separated pairs")
        balances[asset.strip().upper()] = amount.strip()
    return balances
