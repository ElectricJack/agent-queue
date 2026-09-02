import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Metrics from "../Metrics";
import { toAlignedData, type Series } from "../chartData";
import { breakdownKeys, buildCharts, pick } from "../series";
import { __dispatchEventForTests } from "../../../ws/useEventStream";
import type { MetricsSample, MetricsSeriesResponse } from "../../../api/metrics";

// ``useEventStream`` opens its singleton socket at import time; stub the
// constructor before that module is evaluated.
vi.hoisted(() => {
  vi.stubGlobal(
    "WebSocket",
    class {
      static OPEN = 1;
      static CONNECTING = 0;
      readyState = 0;
      close() {}
    },
  );
});

// uPlot draws to a canvas, which jsdom does not implement.  The wrapper's
// own contract (sample -> aligned arrays) is exercised directly below; the
// page tests only need to know a chart was asked to render, and with which
// series.
const plots = vi.hoisted(() => ({ calls: [] as Array<{ title: string; series: Series[]; count: number }> }));
vi.mock("../TimeSeriesChart", () => ({
  default: ({ title, series, samples }: { title: string; series: Series[]; samples: unknown[] }) => {
    plots.calls.push({ title, series, count: samples.length });
    return (
      <div data-testid={`chart-${title}`} data-points={samples.length}>
        {title}
      </div>
    );
  },
}));

const api = vi.hoisted(() => ({ calls: [] as unknown[], response: null as MetricsSeriesResponse | null }));
vi.mock("../../../api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("../../../api/client");
  return {
    ...actual,
    getMetricsSeriesApiMetricsSeriesGet: async (options: unknown) => {
      api.calls.push(options);
      return { data: api.response };
    },
  };
});

function sample(ts: number, overrides: Partial<MetricsSample> = {}): MetricsSample {
  return {
    ts,
    agents: { total: 3, by_state: { running: 3 }, by_harness: { claude: 2, codex: 1 }, by_profile: {}, by_lifecycle: {} },
    tasks: { READY: 4, IN_PROGRESS: 3, ASSIGNED: 0, PAUSED: 1, BLOCKED: 2, WAITING_INPUT: 0, other: 0, total: 10 },
    subagents: { total: 5, native: 3, aq: 2, complete: true, by_session: {} },
    tokens: { input_per_min: 1200, output_per_min: 300, total_per_min: 1500, unattributed_per_min: 0, by_model: {} },
    slots: { used: 6, total: 8, cap: 8 },
    machine: { load1: 4.5, load5: 4, load15: 3.5, cpu_count: 24, mem_total_mb: 32000, mem_free_mb: 8000, mem_available_mb: 12000 },
    daemon: { uptime_seconds: 600, restarts: 1 },
    stall: { nudges_per_hour: 2, kills_per_hour: 0 },
    throughput: { completions_per_hour: 7, prs_per_hour: 5 },
    merges_per_hour: 4,
    sampler: { collect_ms: 1.2 },
    ...overrides,
  } as MetricsSample;
}

function page() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return (
    <QueryClientProvider client={queryClient}>
      <Metrics />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  plots.calls = [];
  api.calls = [];
  api.response = {
    step: "1s",
    from_ts: 1000,
    to_ts: 1002,
    truncated: false,
    samples: [sample(1000), sample(1001), sample(1002)],
  };
});

afterAll(() => vi.unstubAllGlobals());
afterEach(cleanup);

describe("pure sample readers", () => {
  it("reads a nested value and reports a missing hop as no reading, not zero", () => {
    expect(pick(sample(1), "machine.load1")).toBe(4.5);
    expect(pick(sample(1), "machine.nonexistent")).toBeNull();
    expect(pick(sample(1), "nothing.at.all")).toBeNull();
    // A null in the payload (platform cannot supply it) is not a zero either.
    expect(pick({ machine: { load1: null } }, "machine.load1")).toBeNull();
  });

  it("takes the union of breakdown keys across the window", () => {
    const rows = [
      { agents: { by_harness: { claude: 2 } } },
      { agents: { by_harness: { codex: 1 } } },
    ];
    // A harness that only ran at the start of the range still gets a line.
    expect(breakdownKeys(rows, "agents.by_harness")).toEqual(["claude", "codex"]);
  });

  it("turns samples into uPlot's parallel arrays, with gaps as null", () => {
    const series: Series[] = [
      { key: "a", label: "a", color: "#fff", value: (s) => pick(s, "agents.total") },
      { key: "b", label: "b", color: "#000", value: (s) => pick(s, "machine.load1") },
    ];
    const rows = [sample(10), { ts: 11 } as unknown as MetricsSample];
    const [xs, a, b] = toAlignedData(rows as unknown as Array<Record<string, unknown>>, series);
    expect(xs).toEqual([10, 11]);
    expect(a).toEqual([3, null]);
    expect(b).toEqual([4.5, null]);
  });

  it("builds a harness line per harness seen", () => {
    const charts = buildCharts([sample(1)]);
    const agents = charts.find((chart) => chart.id === "agents");
    expect(agents?.series.map((s) => s.label)).toEqual(
      expect.arrayContaining(["total", "claude", "codex"]),
    );
  });
});

describe("Metrics page", () => {
  it("renders every series from the fetched history", async () => {
    render(page());
    await screen.findByTestId("chart-Running agents");
    expect(screen.getByTestId("chart-Tokens per minute")).toBeInTheDocument();
    expect(screen.getByTestId("chart-Tasks by status")).toBeInTheDocument();
    expect(screen.getByTestId("chart-Machine load")).toBeInTheDocument();
    expect(screen.getByTestId("chart-Running agents")).toHaveAttribute("data-points", "3");
  });

  it("shows the now-row from the newest sample", async () => {
    render(page());
    // The tiles render immediately with "—"; wait for the history to land.
    await screen.findByTestId("chart-Running agents");
    expect(screen.getByText("Agents now")).toBeInTheDocument();
    expect(screen.getByText("1,500")).toBeInTheDocument(); // tokens/min
    expect(screen.getByText("of 8 cap")).toBeInTheDocument();
    expect(screen.getByText("4.50")).toBeInTheDocument(); // load1
  });

  it("says the sub-agent count is a floor when a session had no hooks", async () => {
    api.response = {
      ...api.response!,
      samples: [sample(1000, {
        subagents: { total: 2, native: 1, aq: 1, complete: false, by_session: {} },
      } as Partial<MetricsSample>)],
    };
    render(page());
    await screen.findByTestId("chart-Running agents");
    expect(screen.getByText("at least — hooks missing")).toBeInTheDocument();
  });

  it("appends a live tick without refetching history", async () => {
    render(page());
    await screen.findByTestId("chart-Running agents");
    expect(api.calls).toHaveLength(1);

    act(() => {
      __dispatchEventForTests({
        _event_type: "metrics.tick",
        event_type: "metrics.tick",
        ...sample(1003, { agents: { total: 9, by_state: {}, by_harness: {}, by_profile: {}, by_lifecycle: {} } }),
      } as never);
    });

    await waitFor(() =>
      expect(screen.getByTestId("chart-Running agents")).toHaveAttribute("data-points", "4"),
    );
    // The whole point of the WS tick: no second request at 1 Hz.
    expect(api.calls).toHaveLength(1);
  });

  it("ignores a tick that predates the fetched window", async () => {
    render(page());
    await screen.findByTestId("chart-Running agents");
    act(() => {
      __dispatchEventForTests({
        _event_type: "metrics.tick",
        event_type: "metrics.tick",
        ...sample(999),
      } as never);
    });
    await waitFor(() =>
      expect(screen.getByTestId("chart-Running agents")).toHaveAttribute("data-points", "3"),
    );
  });

  it("refetches with a new window when the range changes", async () => {
    render(page());
    await screen.findByTestId("chart-Running agents");
    const first = api.calls[0] as { query: { from: number; to: number } };

    fireEvent.click(screen.getByRole("button", { name: "24h" }));

    await waitFor(() => expect(api.calls).toHaveLength(2));
    const second = api.calls[1] as { query: { from: number; to: number } };
    expect(second.query.to - second.query.from).toBeCloseTo(86_400, 0);
    expect(first.query.to - first.query.from).toBeCloseTo(3_600, 0);
  });

  it("names the resolution it is actually showing", async () => {
    api.response = { ...api.response!, step: "1m" };
    render(page());
    expect(await screen.findByText(/1-minute averages/)).toBeInTheDocument();
  });
});
