import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { useAllOpenGates } from "../hooks";

const api = vi.hoisted(() => {
  vi.resetModules();
  return { listProjects: vi.fn(), gateList: vi.fn() };
});
vi.mock("../client", () => api);
const clients: QueryClient[] = [];

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const project = (id: string) => ({ id, name: id, status: "ACTIVE" });
const gate = (id: string, project_id: string) => ({
  id, project_id, gate_type: "review", title: id, status: "open",
});

beforeEach(() => vi.clearAllMocks());
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); });
afterAll(() => {
  vi.doUnmock("../client");
  vi.resetModules();
});

describe("useAllOpenGates", () => {
  it("reads every project's open gates in one request, not one per project", async () => {
    api.listProjects.mockResolvedValue({
      data: { projects: ["a", "b", "c", "d"].map(project) },
    });
    api.gateList.mockResolvedValue({
      data: { success: true, gates: [gate("g1", "a"), gate("g2", "c")] },
    });
    const { result } = renderHook(() => useAllOpenGates(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(api.gateList).toHaveBeenCalledTimes(1);
    expect(api.gateList).toHaveBeenCalledWith({ body: { status: "open" }, throwOnError: true });
    expect(result.current.data?.map((g) => g.id)).toEqual(["g1", "g2"]);
  });

  it("keeps to the projects the dashboard lists", async () => {
    // A gate left behind by a project the daemon no longer lists stayed out of
    // the per-project fan-out; the unscoped read must not surface it either.
    api.listProjects.mockResolvedValue({ data: { projects: [project("a")] } });
    api.gateList.mockResolvedValue({
      data: { success: true, gates: [gate("g1", "a"), gate("orphan", "gone")] },
    });
    const { result } = renderHook(() => useAllOpenGates(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.map((g) => g.id)).toEqual(["g1"]);
  });

  it("waits for the project list before asking", async () => {
    api.listProjects.mockResolvedValue({ data: { projects: [] } });
    const { result } = renderHook(() => useAllOpenGates(), { wrapper });
    await waitFor(() => expect(api.listProjects).toHaveBeenCalled());
    expect(result.current.fetchStatus).toBe("idle");
    expect(api.gateList).not.toHaveBeenCalled();
  });
});
