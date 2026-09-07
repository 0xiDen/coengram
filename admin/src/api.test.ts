import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { canAccessPage, visibleNavItems } from "./App";
import { getStoredCsrf, login, logout, storeCsrf } from "./api";

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    ...init
  });
}

describe("admin api auth helpers", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("stores csrf from login and sends it on logout", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(
          {
            session_id: "session-a",
            operator_id: "operator-a",
            roles: ["operator_admin"],
            csrf_token: "csrf-a",
            absolute_expires_at: "2026-09-07T10:00:00Z",
            idle_expires_at: "2026-09-07T09:00:00Z"
          },
          { status: 201 }
        )
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    const session = await login("op1.secret");
    await logout(getStoredCsrf());

    expect(session.operator_id).toBe("operator-a");
    expect(getStoredCsrf()).toBeNull();
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/admin/session",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        body: JSON.stringify({ access_token: "op1.secret" })
      })
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/admin/session",
      expect.objectContaining({
        method: "DELETE",
        headers: expect.any(Headers)
      })
    );
    const logoutHeaders = fetchMock.mock.calls[1][1].headers as Headers;
    expect(logoutHeaders.get("X-CoEngram-CSRF")).toBe("csrf-a");
  });

  it("clears stored csrf on explicit reset", () => {
    storeCsrf("csrf-a");
    storeCsrf(null);

    expect(getStoredCsrf()).toBeNull();
  });
});

describe("admin route guards", () => {
  it("keeps privileged pages behind operator roles", () => {
    expect(canAccessPage("dashboard", [])).toBe(true);
    expect(canAccessPage("operators", ["token_admin"])).toBe(false);
    expect(canAccessPage("tokens", ["tenant_support"])).toBe(true);
    expect(canAccessPage("knowledge", ["identity_admin"])).toBe(false);
  });

  it("filters navigation to pages the current operator can use", () => {
    const labels = visibleNavItems(["knowledge_admin"]).map((item) => item.label);

    expect(labels).toContain("Dashboard");
    expect(labels).toContain("Knowledge");
    expect(labels).toContain("Memory");
    expect(labels).not.toContain("Operators");
  });
});
