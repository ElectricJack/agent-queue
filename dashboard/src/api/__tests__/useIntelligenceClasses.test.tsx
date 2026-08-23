import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { useIntelligenceClasses } from "../hooks";
import * as legacyFetchModule from "../legacy-fetch";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("useIntelligenceClasses", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("fetches and returns the full response shape", async () => {
    const body = {
      success: true,
      classes: [
        { id: "fast-off", name: "Fast", description: "", mapping: { anthropic: { model: "haiku" } } },
      ],
    };
    vi.spyOn(legacyFetchModule, "legacyFetch").mockResolvedValue({
      ok: true,
      json: async () => body,
      text: async () => "",
    } as Response);

    const { result } = renderHook(() => useIntelligenceClasses(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(body);
  });

  it("throws on a non-ok response", async () => {
    vi.spyOn(legacyFetchModule, "legacyFetch").mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({}),
      text: async () => "boom",
    } as Response);

    const { result } = renderHook(() => useIntelligenceClasses(), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});
