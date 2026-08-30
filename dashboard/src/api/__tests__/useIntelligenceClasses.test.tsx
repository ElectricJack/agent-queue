import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { useIntelligenceClasses } from "../hooks";

const api = vi.hoisted(() => ({ listIntelligenceClasses: vi.fn() }));
vi.mock("../client", () => api);
const clients: QueryClient[] = [];

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => vi.clearAllMocks());
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); });

describe("useIntelligenceClasses", () => {
  it("returns effective mappings and edit revisions through the SDK", async () => {
    const body = {
      success: true,
      classes: [{ id: "fast-off", name: "Fast", description: "", revision: "revision-1",
        mapping: { anthropic: { model: "sonnet", thinking: "off", extra: true }, codex: null } }],
    };
    api.listIntelligenceClasses.mockResolvedValue({ data: body });
    const { result } = renderHook(() => useIntelligenceClasses(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(body);
    expect(api.listIntelligenceClasses).toHaveBeenCalledWith({ body: {}, throwOnError: true });
  });

  it("surfaces the SDK request error", async () => {
    api.listIntelligenceClasses.mockRejectedValue(new Error("API 500: boom"));
    const { result } = renderHook(() => useIntelligenceClasses(), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe("API 500: boom");
  });
});
