import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Metrics from "../Metrics";
import { toAlignedData, type Series } from "../chartData";
import { breakdownKeys, buildCharts, pick } from "../series";
import { __dispatchEventForTests } from "../../../ws/useEventStream";
import type { MetricsSample, MetricsSeriesResponse } from "../../../api/metrics";
import SustainedLagBanner, { sustainedLag } from "../SustainedLagBanner";
import { BOUNDS_MS, type Hist } from "../histogram";

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
    // The page renders the provider cards too; stub their fetch so this test
    // does not reach the network for a surface it is not about.
    getProviderUsageApiProvidersUsageGet: async () => ({
      data: { now: 1000, snapshots: [], series: {} },
    }),
    getProviderAvailabilityApiProvidersAvailabilityGet: async () => ({
      data: { success: true, mode: "enforce", now: 1000, providers: [] },
    }),
  };
});

function sample(ts: number, overrides: Partial<MetricsSample> = {}): MetricsSample {
  return {
    ts,
    agents: { total: 3, by_state: { running: 3 }, by_harness: { claude: 2, codex: 1 }, by_profile: {}, by_lifecycle: {} },
    tasks: { READY: 4, IN_PROGRESS: 3, ASSIGNED: 0, PAUSED: 1, BLOCKED: 2, WAITING_INPUT: 0, other: 0, total: 10 },
    subagents: { total: 5, active: 5, native: 3, aq: 2, spawned_per_hour: 12, complete: true, by_session: {} },
    tokens: { input_per_min: 1200, output_per_min: 300, cache_read_per_min: 90000, cache_write_per_min: 200, total_per_min: 91700, unattributed_per_min: 0, input_per_min_1m: 0, output_per_min_1m: 0, total_per_min_1m: 0, window_seconds: 300, by_model: {} },
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
    // The total includes cache, which on a warm agent is most of it.
    expect(screen.getByText("91,700")).toBeInTheDocument(); // tokens/min
    expect(screen.getByText("Sub-agents / hr")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument(); // sub-agents started/hr
    expect(screen.getByText("of 8 cap")).toBeInTheDocument();
    expect(screen.getByText("4.50")).toBeInTheDocument(); // load1
  });

  it("says the open sub-agent count is a floor when a session had no hooks", async () => {
    api.response = {
      ...api.response!,
      samples: [sample(1000, {
        subagents: { total: 2, active: 2, native: 1, aq: 1, spawned_per_hour: 9, complete: false, by_session: {} },
      } as Partial<MetricsSample>)],
    };
    render(page());
    await screen.findByTestId("chart-Running agents");
    expect(screen.getByText("2+ open — hooks missing")).toBeInTheDocument();
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

function hist(value: number, count = 1): Hist {
  const counts = new Array(BOUNDS_MS.length + 1).fill(0);
  const index = BOUNDS_MS.findIndex((bound) => value <= bound);
  counts[index < 0 ? BOUNDS_MS.length : index] = count;
  return { kind: "hist", counts, count, sum: value * count, max: value };
}

function perfSample(ts: number, drift: number, pool = 1): MetricsSample {
  return sample(ts, {
    perf: {
      enabled: true,
      loop: { drift: hist(drift), probe_interval_ms: 100 },
      db: {
        pool_wait: hist(pool), query: hist(1),
        counters: { kind: "sum", slow_queries: 2 }, pool: { checked_out: 3 },
      },
      host: {
        psi: { cpu: { some_avg10: 20 }, io: null, memory: null },
        test_slots: { used: 1, total: 4, waiting: 2, orphaned: 0 },
        ungated: { unattributed: 5 },
      },
      api: { all: hist(20), routes: {}, streams: {}, errors: { kind: "sum" } },
      relay: { available: true, http: hist(30) },
    },
  });
}

function lagRows(length = 130): MetricsSample[] {
  return Array.from({ length }, (_, i) => perfSample(1000 + i, 900, 400));
}

describe("sustained loop lag", () => {
  it("requires 120 nonempty loop histograms and p95 strictly above 500 ms", () => {
    expect(sustainedLag([])).toBeNull();
    expect(sustainedLag(lagRows(119))).toBeNull();
    expect(sustainedLag(lagRows(120))?.samples).toBe(120);
    expect(sustainedLag(Array.from({ length: 130 }, (_, i) => perfSample(1000 + i, 5))))
      .toBeNull();
    // Uniform observations in (200, 500] produce a p95 below the warning threshold.
    expect(sustainedLag(Array.from({ length: 130 }, (_, i) => perfSample(1000 + i, 500))))
      .toBeNull();
    const boundary = hist(500, 95);
    boundary.counts[9] = 5;
    boundary.count = 100;
    boundary.sum += 900 * 5;
    boundary.max = 900;
    const atThreshold = lagRows();
    for (const row of atThreshold) row.perf!.loop!.drift = boundary;
    expect(sustainedLag(atThreshold)).toBeNull(); // merged p95 is exactly 500
    const rows = lagRows();
    for (const row of rows.slice(0, 11)) row.perf!.loop!.drift = hist(0, 0);
    expect(sustainedLag(rows)).toBeNull();
  });

  it("uses only the last 180 seconds relative to the newest sample", () => {
    const verdict = sustainedLag(lagRows(400))!;
    expect(verdict.since).toBe(1219);
    expect(verdict.samples).toBe(181);
    expect(verdict.p95).toBeCloseTo(975);
    expect(sustainedLag(lagRows(), 60)).toBeNull();
  });

  it("ignores old high lag after probes are switched off", () => {
    expect(sustainedLag([...lagRows(), sample(1130, { perf: { enabled: false } })]))
      .toBeNull();
  });

  it("ranks pool, query, latest available PSI and the slowest qualified route", () => {
    const rows = lagRows();
    rows[0]!.perf!.db!.query = hist(400, 1000);
    rows[0]!.perf!.host!.psi = {
      memory: { some_avg10: 5 }, io: { some_avg10: 20 }, cpu: { some_avg10: 20 },
    };
    for (const row of rows.slice(1)) row.perf!.host!.psi = { cpu: null, io: null, memory: null };
    rows[0]!.perf!.api!.routes = {
      "GET /api/tasks/{task_id}": { latency: hist(400, 10) },
      "GET /api/agents": { latency: hist(900, 5) },
      "GET /api/rare": { latency: hist(9000, 9) },
    };
    rows[1]!.perf!.api!.routes = { "GET /api/agents": { latency: hist(900, 5) } };
    expect(sustainedLag(rows)?.candidates).toEqual([
      "db_pool_wait", "db_query", "host_memory_pressure", "host_io_pressure",
      "host_cpu_pressure", "api_route:GET /api/agents",
    ]);
  });

  it("uses synchronous Python as a fallback and respects latest lower pressure", () => {
    const rows = Array.from({ length: 130 }, (_, i) => perfSample(1000 + i, 900));
    rows[rows.length - 1]!.perf!.host!.psi = { cpu: { some_avg10: 0 } };
    expect(sustainedLag(rows)?.candidates).toEqual(["synchronous_python"]);
  });

  it("renders the banner and five charts using stored histogram buckets", async () => {
    api.response = { ...api.response!, samples: lagRows(), to_ts: 1130 };
    render(page());
    const banner = await screen.findByRole("status", { name: "Event-loop lag" });
    expect(banner).toHaveTextContent("975 ms sustained since 00:16:40Z");
    expect(banner).toHaveTextContent("candidate causes, not a diagnosis");
    expect(banner).toHaveTextContent("db_pool_wait");
    const titles = plots.calls.map((call) => call.title);
    for (const title of [
      "Event-loop lag", "API latency", "Database", "Host pressure", "Test slots and ungated load",
    ]) expect(titles).toContain(title);
  });

  it("renders no banner for a short window", () => {
    render(<SustainedLagBanner samples={lagRows(30)} />);
    expect(screen.queryByRole("status", { name: "Event-loop lag" })).not.toBeInTheDocument();
  });
});

describe("performance chart readings", () => {
  it("derives latencies from buckets and reads the matching gauges and counters", () => {
    const row = perfSample(1000, 900, 400);
    const charts = buildCharts([row]);
    const machineIndex = charts.findIndex((chart) => chart.id === "load");
    const perf = charts.slice(machineIndex + 1, machineIndex + 6);
    expect(perf.map((chart) => chart.title)).toEqual([
      "Event-loop lag", "API latency", "Database", "Host pressure", "Test slots and ungated load",
    ]);
    const readings = perf.map((chart) => chart.series.map((series) => series.value(row)));
    expect(readings).toEqual([
      [975, 900], [19.5, 15, 48.5], [485, 0.95, 3, 2],
      [20, null, null], [1, 4, 2, 0, 5],
    ]);
    for (const chart of perf) {
      for (const series of chart.series) expect(series.value(sample(2))).toBeNull();
    }
  });

  it("keeps empty histograms and disabled probes as chart gaps", () => {
    const row = perfSample(1000, 900, 400);
    row.perf!.loop!.drift = hist(0, 0);
    const charts = buildCharts([row]);
    const loop = charts.find((chart) => chart.title === "Event-loop lag")!;
    expect(loop.series.map((series) => series.value(row))).toEqual([null, null]);
    row.perf!.enabled = false;
    const start = charts.findIndex((chart) => chart.title === "Event-loop lag");
    for (const chart of charts.slice(start, start + 5)) {
      for (const series of chart.series) expect(series.value(row)).toBeNull();
    }
  });
});
