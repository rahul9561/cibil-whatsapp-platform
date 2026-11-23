import { describe, expect, it, vi } from "vitest";
import { ApiError, api, buildUrl } from "../api/client";

describe("api client", () => {
  it("builds query strings, skipping empty values and repeating lists", () => {
    expect(buildUrl("report-requests", { unpaid: "true", public_ref: "", status: ["a", "b"], x: null })).toBe(
      "/api/v1/report-requests?unpaid=true&status=a&status=b",
    );
  });

  it("sends the CSRF token on writes only", async () => {
    document.cookie = "csrftoken=tok123";
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await api("x");
    await api("y", { method: "POST", body: { a: 1 } });
    expect(fetchMock.mock.calls[0][1].headers["X-CSRFToken"]).toBeUndefined();
    expect(fetchMock.mock.calls[1][1].headers["X-CSRFToken"]).toBe("tok123");
    expect(fetchMock.mock.calls[1][1].credentials).toBe("same-origin");
  });

  it("turns error responses into readable messages", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ amount_paise: ["Too big."] }), { status: 400 })),
    );
    await expect(api("p", { method: "POST", body: {} })).rejects.toThrow("amount_paise: Too big.");
    const err = new ApiError(422, { detail: "Nope", code: "refund_self_approval" });
    expect(err.message).toBe("Nope");
    expect(err.code).toBe("refund_self_approval");
    expect(new ApiError(403, null).message).toMatch(/permission/);
  });

  it("handles 204 No Content", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 204 })),
    );
    expect(await api("reminders/1", { method: "DELETE" })).toBeNull();
  });
});
