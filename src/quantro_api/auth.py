"""Supabase Auth integration for API requests."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import httpx
from fastapi import Request

from .config import SupabaseSettings


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    id: UUID
    email: str | None = None


class SupabaseAuthError(ValueError):
    """Raised when Supabase rejects credentials or tokens."""


class SupabaseAuthClient:
    def __init__(self, settings: SupabaseSettings) -> None:
        self._settings = settings

    def _headers(self, bearer: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self._settings.supabase_anon_key,
            "Content-Type": "application/json",
        }
        if bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"
        return headers

    async def signup(self, email: str, password: str) -> dict:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self._settings.supabase_url}/auth/v1/signup",
                headers=self._headers(),
                json={"email": email, "password": password},
            )
        if response.status_code >= 400:
            raise SupabaseAuthError(_auth_error_message(response, "Signup failed"))
        return response.json()

    async def login(self, email: str, password: str) -> dict:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self._settings.supabase_url}/auth/v1/token?grant_type=password",
                headers=self._headers(),
                json={"email": email, "password": password},
            )
        if response.status_code >= 400:
            raise SupabaseAuthError(_auth_error_message(response, "Invalid email or password"))
        return response.json()

    async def logout(self, access_token: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self._settings.supabase_url}/auth/v1/logout",
                headers=self._headers(access_token),
            )
        if response.status_code >= 400:
            raise SupabaseAuthError(_auth_error_message(response, "Logout failed"))

    async def user_from_token(self, access_token: str) -> AuthenticatedUser:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{self._settings.supabase_url}/auth/v1/user",
                headers=self._headers(access_token),
            )
        if response.status_code >= 400:
            raise SupabaseAuthError("Invalid or expired bearer token")

        body = response.json()
        return AuthenticatedUser(id=UUID(body["id"]), email=body.get("email"))


def _auth_error_message(response: httpx.Response, fallback: str) -> str:
    try:
        body = response.json()
    except ValueError:
        return fallback
    for key in ("msg", "message", "error_description", "error"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


def bearer_token_from_request(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token
