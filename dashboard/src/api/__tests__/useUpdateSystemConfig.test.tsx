import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook } from "@testing-library/react";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useUpdateSystemConfig } from "../hooks";

const api = vi.hoisted(() => {
  vi.resetModules();
  return { updateConfig: vi.fn() };
});
vi.mock("../client", () => api);

const clients: QueryClient[] = [];

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  vi.clearAllMocks();
  api.updateConfig.mockResolvedValue({ data: { validation_errors: [] } });
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});
afterAll(() => {
  vi.doUnmock("../client");
  vi.resetModules();
});

describe("useUpdateSystemConfig", () => {
  it("invalidates cached onboarding roots after saving project roots", async () => {
    const { result } = renderHook(() => useUpdateSystemConfig(), { wrapper });
    const client = clients[0]!;
    client.setQueryData(["project-roots"], [{ id: "previous" }]);

    await result.current.mutateAsync({
      section: "project_roots",
      data: [{ id: "local", label: "Local", path: "/work/local" }],
    });

    expect(api.updateConfig).toHaveBeenCalledWith({
      body: {
        section: "project_roots",
        data: [{ id: "local", label: "Local", path: "/work/local" }],
      },
      throwOnError: true,
    });
    expect(client.getQueryState(["project-roots"])?.isInvalidated).toBe(true);
  });

  it("does not invalidate onboarding roots for an unrelated config save", async () => {
    const { result } = renderHook(() => useUpdateSystemConfig(), { wrapper });
    const client = clients[0]!;
    client.setQueryData(["project-roots"], [{ id: "previous" }]);

    await result.current.mutateAsync({ section: "logging", data: { level: "DEBUG" } });

    expect(client.getQueryState(["project-roots"])?.isInvalidated).toBe(false);
  });
});
