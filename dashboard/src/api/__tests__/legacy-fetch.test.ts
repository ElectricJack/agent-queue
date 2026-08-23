import { describe, expect, it, vi } from "vitest";
import { apiGet, ApiError } from "../legacy-fetch";

describe("apiGet", () => {
  it("throws ApiError with a status field on non-2xx", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("nope", { status: 404 })));
    await expect(apiGet("/api/x")).rejects.toMatchObject(new ApiError(404, "API 404: nope"));
  });

  it("resolves with parsed JSON on 2xx", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: 200 })),
    );
    await expect(apiGet("/api/x")).resolves.toEqual({ ok: true });
  });
});
