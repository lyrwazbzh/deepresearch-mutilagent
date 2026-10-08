import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "../lib/api/client";
import type { AccessSession } from "../lib/api/types";

const session: AccessSession = {
  access_token: "fresh-token",
  token_type: "Bearer",
  expires_in: 900,
  session_id: "session_1",
  user: { user_id: "user_1", email: "test@example.com" },
  tenant: { tenant_id: "tenant_1", name: "Test" },
  roles: ["admin"],
};

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { "Content-Type": status >= 400 ? "application/problem+json" : "application/json" },
});

describe("ApiClient", () => {
  it("logs in without persisting the access token and clears local auth on logout", async () => {
    let current: AccessSession | null = null;
    let expired = 0;
    const storageWrite = vi.spyOn(Storage.prototype, "setItem");
    const fetchImpl = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (url.endsWith("/auth/login")) {
        const body = init?.body;
        expect(typeof body).toBe("string");
        expect(JSON.parse(typeof body === "string" ? body : "{}")).toEqual({ email: "test@example.com", password: "password", tenant_id: "tenant_1" });
        return Promise.resolve(json(session));
      }
      expect(url).toMatch(/\/auth\/logout$/);
      return Promise.resolve(new Response(null, { status: 204 }));
    });
    const client = new ApiClient({
      getAccessToken: () => current?.access_token ?? null,
      onSession: (value) => { current = value; },
      onSessionExpired: () => { current = null; expired += 1; },
      fetchImpl,
    });
    await client.login("test@example.com", "password", "tenant_1");
    expect(current).toEqual(session);
    expect(storageWrite).not.toHaveBeenCalled();
    await client.logout();
    expect(current).toBeNull();
    expect(expired).toBe(1);
  });

  it("coalesces concurrent 401 refreshes and replays each request once", async () => {
    let token = "expired";
    let refreshCalls = 0;
    let resourceCalls = 0;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (url.endsWith("/auth/refresh")) {
        refreshCalls += 1;
        await new Promise((resolve) => setTimeout(resolve, 5));
        return json(session);
      }
      resourceCalls += 1;
      const authorization = new Headers(init?.headers).get("Authorization");
      return authorization === "Bearer fresh-token"
        ? json({ ok: true })
        : json({ type: "urn:test", title: "Expired", status: 401, code: "unauthenticated", detail: "expired" }, 401);
    });
    const client = new ApiClient({
      getAccessToken: () => token,
      onSession: (value) => { token = value.access_token; },
      onSessionExpired: () => { token = ""; },
      fetchImpl,
    });
    const [one, two] = await Promise.all([
      client.request<{ ok: boolean }>("/api/v1/one"),
      client.request<{ ok: boolean }>("/api/v1/two"),
    ]);
    expect(one.ok).toBe(true);
    expect(two.ok).toBe(true);
    expect(refreshCalls).toBe(1);
    expect(resourceCalls).toBe(4);
  });

  it("surfaces RFC 9457 problem details", async () => {
    const client = new ApiClient({
      getAccessToken: () => "token",
      onSession: vi.fn(),
      onSessionExpired: vi.fn(),
      fetchImpl: vi.fn(() => Promise.resolve(json({ type: "urn:test", title: "Conflict", status: 409, code: "stale", detail: "changed" }, 409))),
    });
    await expect(client.request("/api/v1/test", { retryAuth: false })).rejects.toMatchObject({ problem: { status: 409, code: "stale" } });
  });

  it("does not recurse when refresh itself fails", async () => {
    const expired = vi.fn();
    const fetchImpl = vi.fn(() => Promise.resolve(json({ type: "urn:test", title: "Expired", status: 401, code: "unauthenticated", detail: "expired" }, 401)));
    const client = new ApiClient({ getAccessToken: () => "expired", onSession: vi.fn(), onSessionExpired: expired, fetchImpl });
    await expect(client.request("/api/v1/tasks")).rejects.toMatchObject({ problem: { status: 401 } });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(expired).toHaveBeenCalledOnce();
  });

  it("uses the supplied idempotency key without replacing it", async () => {
    const fetchImpl = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      expect(new Headers(init?.headers).get("Idempotency-Key")).toBe("stable-key");
      return Promise.resolve(json({ task_id: "task_1" }));
    });
    const client = new ApiClient({ getAccessToken: () => "token", onSession: vi.fn(), onSessionExpired: vi.fn(), fetchImpl });
    await client.request("/api/v1/tasks", { method: "POST", body: { query: "x" }, idempotencyKey: "stable-key" });
  });
});
