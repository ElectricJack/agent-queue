import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { useProjectGraphs } from "../graph";
import { useMetricsSeries } from "../metrics";
import { prefetchInitialRoute } from "../../routeData";

const mockGet = vi.fn();
const mockMetrics = vi.fn();
vi.mock("@aq/ts-client", async () => {
  const actual = await vi.importActual<typeof import("@aq/ts-client")>("@aq/ts-client");
  return {
    ...actual,
    getProjectGraphApiProjectsProjectIdGraphGet: (...args: unknown[]) => mockGet(...args),
    getMetricsSeriesApiMetricsSeriesGet: (...args: unknown[]) => mockMetrics(...args),
  };
});

function makeWrapper(qc = new QueryClient()) {
  // No retry override here: these tests assert the hook's OWN retry config,
  // so the client must not mask it.
  return function wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

const emptyGraph = { tasks: [], edges: [], gates: [], agents: [] };

beforeEach(() => {
  mockGet.mockReset();
  mockMetrics.mockReset();
});

describe("initial-route reads", () => {
  it("shares an in-flight cold Tasks read with the mounted workspace", async () => {
    let finish!: (response: { data: typeof emptyGraph }) => void;
    mockGet.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const qc = new QueryClient();
    const prefetch = prefetchInitialRoute(qc, "/projects/project%20id/tasks");
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet.mock.calls[0]![0].path.project_id).toBe("project id");
    const { result } = renderHook(() => useProjectGraphs(["project id"]), {
      wrapper: makeWrapper(qc),
    });
    finish({ data: emptyGraph });
    await prefetch;
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(mockGet).toHaveBeenCalledTimes(1);
    qc.clear();
  });

  it("shares the complete one-hour history with the mounted Metrics page", async () => {
    const data = { step: "1s", samples: [], from_ts: 0, to_ts: 3600, truncated: false };
    mockMetrics.mockResolvedValue({ data });
    const qc = new QueryClient();
    await prefetchInitialRoute(qc, "/metrics");
    const { result } = renderHook(() => useMetricsSeries("1h"), { wrapper: makeWrapper(qc) });
    await waitFor(() => expect(result.current.data).toEqual(data));
    expect(mockMetrics).toHaveBeenCalledTimes(1);
    const options = mockMetrics.mock.calls[0]![0];
    expect(options.query.to - options.query.from).toBe(3600);
    expect(options.query.step).toBe("auto");
    qc.clear();
  });

  it("leaves errors on the normal query without rejecting startup", async () => {
    mockGet.mockRejectedValue(new Error("missing project"));
    const qc = new QueryClient();
    await expect(prefetchInitialRoute(qc, "/projects/gone/tasks")).resolves.toBeUndefined();
    expect(qc.getQueryState(["projectGraph", "gone"])?.error?.message).toBe("missing project");
    qc.clear();
  });

  it.each(["/agents", "/projects/p/overview", "/command-center/tasks", "/projects/%/tasks"])(
    "does not start unrelated or malformed reads for %s", async (pathname) => {
      const qc = new QueryClient();
      await prefetchInitialRoute(qc, pathname);
      expect(mockGet).not.toHaveBeenCalled();
      expect(mockMetrics).not.toHaveBeenCalled();
      qc.clear();
    },
  );
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
