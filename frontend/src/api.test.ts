import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, QuantroApiError, sessionStore } from "./api";
import type { Account, AuthSession } from "./types";

const account: Account = {
  id: "00000000-0000-0000-0000-00000000000a",
  name: "Test Account",
  balances: {},
  created_at: "2026-08-27T00:00:00Z",
  updated_at: "2026-08-27T00:00:00Z",
  metadata: {},
};

const session: AuthSession = {
  access_token: "real-access-token",
  refresh_token: "real-refresh-token",
  token_type: "bearer",
  expires_in: 3600,
  user_id: "00000000-0000-0000-0000-00000000000a",
  account,
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("Quantro auth API client", () => {
  beforeEach(() => {
    const storage = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => storage.set(key, value),
      removeItem: (key: string) => storage.delete(key),
      clear: () => storage.clear(),
    });
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("stores a real login session and validates account with /me/account", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(jsonResponse(session))
      .mockResolvedValueOnce(jsonResponse(account));

    const login = await api.login("a@example.com", "secure-password");
    sessionStore.set(login);
    const restored = await api.me();

    expect(restored.id).toBe(account.id);
    expect(sessionStore.getToken()).toBe("real-access-token");
    expect(fetchMock.mock.calls[1][1]?.headers).toMatchObject({
      Authorization: "Bearer real-access-token",
    });
  });

  it("surfaces invalid credentials from the backend", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ error: { code: "login_failed", message: "Invalid email or password" } }, 401),
    );

    await expect(api.login("missing@example.com", "wrong-password")).rejects.toMatchObject({
      code: "login_failed",
      status: 401,
    } satisfies Partial<QuantroApiError>);
  });

  it("clears persisted tokens and account state on logout", async () => {
    sessionStore.set(session);
    vi.mocked(fetch).mockResolvedValueOnce(new Response(null, { status: 204 }));

    await api.logout();
    sessionStore.clear();

    expect(sessionStore.getToken()).toBeNull();
    expect(sessionStore.getRefreshToken()).toBeNull();
    expect(sessionStore.getAccount()).toBeNull();
  });

  it("detects expired persisted sessions before account restoration", () => {
    sessionStore.set({ ...session, expires_in: -1 });

    expect(sessionStore.isExpired()).toBe(true);
  });

  it("initializes sandbox accounts with sufficient virtual USDT for paper swaps", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(account));

    await api.createSandboxAccount("Paper Trader");

    const body = JSON.parse(String(vi.mocked(fetch).mock.calls[0][1]?.body));
    expect(body.initial_balances).toMatchObject({
      USD: "100000",
      USDT: "1000000",
      BTC: "10",
      ETH: "100",
    });
  });
});
