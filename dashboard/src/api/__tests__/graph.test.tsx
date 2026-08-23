import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { useProjectGraphs } from "../graph";

const mockGet = vi.fn();
vi.mock("@aq/ts-client", async () => {
  const actual = await vi.importActual<typeof import("@aq/ts-client")>("@aq/ts-client");
  return {
    ...actual,
    getProjectGraphApiProjectsProjectIdGraphGet: (...args: unknown[]) => mockGet(...args),
  };
});

function makeWrapper() {
  // No retry override here: these tests assert the hook's OWN retry config,
  // so the client must not mask it.
  const qc = new QueryClient();
  return function wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

const emptyGraph = { tasks: [], edges: [], gates: [], agents: [] };

beforeEach(() => {
  mockGet.mockReset();
});

describe("useProjectGraphs", () => {
  // Regression: a project id left in the persisted selection that 404s used to
  // hold `isLoading` true through React Query's default 3 retries (1s+2s+4s),
  // stalling the whole Command Center canvas at "Loading…" for ~7s on every
  // visit — even though every other selected project had already resolved.
  it("does not stay loading because one project failed", async () => {
    mockGet.mockImplementation(({ path }: { path: { project_id: string } }) => {
      if (path.project_id === "gone") return Promise.reject(new Error("404"));
      return Promise.resolve({ data: { ...emptyGraph, tasks: [{ id: "t1" }] } });
    });

    const { result } = renderHook(() => useProjectGraphs(["live", "gone"]), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.isLoading).toBe(false), { timeout: 3000 });
    // The healthy project's data is still merged in.
    expect(result.current.data.tasks.map((t) => t.id)).toEqual(["t1"]);
  });

  // Regression: `merged` was rebuilt as a fresh object literal on every call,
  // so GraphCanvas's useMemo on the dagre layout never hit cache and re-ran
  // the layout on every render (~78ms at 500 nodes).
  it("keeps the merged graph referentially stable across re-renders", async () => {
    mockGet.mockResolvedValue({ data: { ...emptyGraph, tasks: [{ id: "t1" }] } });

    const { result, rerender } = renderHook(() => useProjectGraphs(["live"]), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.data.tasks.length).toBe(1));
    const first = result.current.data;
    rerender();
    expect(result.current.data).toBe(first);
  });
});
