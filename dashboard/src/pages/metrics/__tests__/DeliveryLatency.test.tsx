import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import DeliveryLatency from "../DeliveryLatency";
import { deliveryState, deployState, formatDuration } from "../deliveryLatencyFormat";
import type { DeliveryLatencyGeneration, DeliveryLatencyResponse } from "../../../api/client";

const api = vi.hoisted(() => ({
  calls: [] as Array<{ query: Record<string, unknown> }>,
  respond: null as null | (() => Promise<{ data: unknown }>),
  deploys: [] as Array<{ timestamp: number; outcome: string; target?: string }>,
}));

vi.mock("../../../api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("../../../api/client");
  return {
    ...actual,
    getDeliveryLatencyApiMetricsDeliveryLatencyGet: (options: { query: Record<string, unknown> }) => {
      api.calls.push(options);
      return api.respond!();
    },
  };
});
vi.mock("../../../api/hooks", () => ({
  useProjects: () => ({ data: [{ id: "aq", name: "Agent Queue" }, { id: "other", name: "Other" }] }),
  useDeploymentHistory: () => ({ data: api.deploys, isError: false }),
}));
// The shared wait explanation has its own tests; here it only has to be the
// thing a generation opens.
vi.mock("../../../components/TaskWaitPanel", () => ({
  default: ({ taskId, projectId }: { taskId: string; projectId: string }) => (
    <div data-testid="wait-panel">{`${taskId}@${projectId}`}</div>
  ),
}));

const NOW = 1_800_000_000;

function generation(overrides: Partial<DeliveryLatencyGeneration>): DeliveryLatencyGeneration {
  return {
    task_id: "t", generation_id: "c-t", project_id: "aq", status: "sample",
    completed_at: NOW - 3000, anchor_at: NOW - 3600, anchor_kind: "task_created",
    on_dev_at: null, segments: {}, waits: [], ...overrides,
  };
}

function report(overrides: Partial<DeliveryLatencyResponse> = {}): DeliveryLatencyResponse {
  return {
    observed_at: NOW, window: "24h", since: NOW - 86_400, until: NOW,
    percentiles_exclude_pending: true, median_seconds: null, p90_seconds: null,
    sample_count: 0, eligible_count: 0, pending_count: 0, unknown_count: 0,
    historical_unknown_count: 0, left_censored_count: 0, slow_count: 0, stuck_count: 0,
    excluded: [], segments: [], generations: [], ...overrides,
  };
}

const POPULATED = report({
  median_seconds: 600, p90_seconds: 1500, completion_to_dev_median_seconds: 300,
  sample_count: 3, eligible_count: 9, pending_count: 3, unknown_count: 1,
  historical_unknown_count: 1, slow_count: 1, stuck_count: 1, coverage: 3 / 9,
  current_blocker_count: 1, within_target_fraction: 2 / 3,
  target: { target_seconds: 1800, source: "floor" },
  excluded: [{ reason: "delivered_by_other_means", count: 2 }, { reason: "code_free", count: 0 }],
  segments: [
    { segment: "ci_time", median_seconds: 240, generations: 3 },
    { segment: "train_wait", median_seconds: 300, generations: 2 },
  ],
  residual_median_seconds: 60, cache_hit_rate: 0.5, cache_hit_rate_derived: true,
  rebuild_seconds_total: 90,
  generations: [
    generation({ task_id: "stuck", status: "pending", watchdog: "stuck", age_seconds: 7200 }),
    generation({
      task_id: "slow", status: "pending", watchdog: "slow", age_seconds: 3600,
      source_ref: "aq/slow", source_oid: "a".repeat(40), dev_ref: "main", target_oid: "b".repeat(40),
      reason: "source_not_contained", observed_at: NOW - 100,
      waits: [{
        wait_id: "w1", origin: "integration", condition_kind: "ci_run", segment: "ci_time",
        outcome: "pending", created_at: NOW - 500, decision_reason: "batch CI running",
        result_ref: "https://github.com/o/r/actions/runs/7", ci_url: "https://github.com/o/r/actions/runs/7",
      }],
    }),
    generation({ task_id: "waiting", status: "pending", age_seconds: 60 }),
    generation({ task_id: "unseen", status: "unknown" }),
    generation({ task_id: "deployed", on_dev_at: NOW - 2000, approval_to_dev_seconds: 600 }),
    generation({ task_id: "undeployed", on_dev_at: NOW - 200, approval_to_dev_seconds: 900 }),
    generation({ task_id: "rewritten", on_dev_at: NOW - 2500, current_blocker: true }),
    generation({ task_id: "merged", status: "excluded", exclusion: "delivered_by_other_means" }),
  ],
});

const lastQuery = () => api.calls[api.calls.length - 1]?.query;

function page(path = "/metrics") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <DeliveryLatency />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  api.calls = [];
  api.respond = async () => ({ data: POPULATED });
  api.deploys = [{ timestamp: NOW - 1000, outcome: "updated", target: "c".repeat(40) }];
});
afterEach(cleanup);

describe("formatting", () => {
  it("renders missing values as no data, never zero", () => {
    expect(formatDuration(null)).toBe("No data");
    expect(formatDuration(undefined)).toBe("No data");
    expect(formatDuration(0)).toBe("0s");
    expect(formatDuration(3725)).toBe("1h 02m");
  });
});

describe("delivery state", () => {
  it("separates on-dev, pending, slow, stuck, unknown and rewritten generations", () => {
    expect(deliveryState(generation({ on_dev_at: NOW }))).toBe("on_dev");
    expect(deliveryState(generation({ current_blocker: true }))).toBe("current_blocker");
    expect(deliveryState(generation({ status: "pending" }))).toBe("pending");
    expect(deliveryState(generation({ status: "pending", watchdog: "slow" }))).toBe("slow");
    expect(deliveryState(generation({ status: "pending", watchdog: "stuck" }))).toBe("stuck");
    expect(deliveryState(generation({ status: "unknown" }))).toBe("unknown");
    expect(deliveryState(generation({ status: "historical_unknown" }))).toBe("historical_unknown");
  });

  it("orders deployment against on-dev by time and says unknown without history", () => {
    const g = generation({ on_dev_at: NOW - 100 });
    expect(deployState(g, [{ timestamp: NOW, outcome: "updated" }])).toBe("deployed_after");
    expect(deployState(g, [{ timestamp: NOW - 200, outcome: "updated" }])).toBe("awaiting");
    expect(deployState(g, [{ timestamp: NOW, outcome: "failed" }])).toBe("unknown");
    expect(deployState(g, undefined)).toBe("unknown");
    expect(deployState(generation({ status: "pending" }), [])).toBeNull();
  });
});

describe("DeliveryLatency panel", () => {
  it("shows no data rather than zero for an empty cohort", async () => {
    api.respond = async () => ({ data: report() });
    render(page());
    expect(await screen.findByTestId("delivery-no-data")).toBeTruthy();
    expect(within(screen.getByTestId("delivery-median")).getByText("No data")).toBeTruthy();
    expect(within(screen.getByTestId("delivery-p90")).getByText("No data")).toBeTruthy();
    expect(screen.getByText("No completed generations in this cohort.")).toBeTruthy();
  });

  it("puts the pending bias beside the delivered-only percentiles", async () => {
    render(page());
    const median = await screen.findByTestId("delivery-median");
    expect(within(median).getByText("10m 00s")).toBeTruthy();
    expect(screen.getByTestId("delivery-bias").textContent).toMatch(
      /Percentiles exclude 3 pending and 2 unknown\s*—\s*sample 3 of 9 eligible\s*\(33% coverage\)/);
    expect(screen.getByTestId("delivery-watchdog").textContent).toContain("1 / 1");
    expect(screen.getByTestId("delivery-excluded").textContent).toContain("2 delivered by other means");
    expect(screen.getByTestId("delivery-excluded").textContent).not.toContain("code-free");
    expect(screen.getByText("Segments overlap and do not sum to the total.")).toBeTruthy();
    expect(screen.getByTestId("delivery-cache").textContent).toContain("cache hit 50% (derived)");
  });

  it("distinguishes on-dev, deploy, pending, slow, stuck, unknown and rewritten rows", async () => {
    render(page());
    await screen.findByTestId("generation-stuck");
    const state = (id: string) => screen.getByTestId(`generation-${id}`).getAttribute("data-state");
    expect(state("stuck")).toBe("stuck");
    expect(state("slow")).toBe("slow");
    expect(state("waiting")).toBe("pending");
    expect(state("unseen")).toBe("unknown");
    expect(state("deployed")).toBe("on_dev");
    expect(state("rewritten")).toBe("current_blocker");
    expect(state("merged")).toBe("excluded");
    expect(within(screen.getByTestId("generation-deployed")).getByText(/daemon deployed after on-dev/)).toBeTruthy();
    expect(within(screen.getByTestId("generation-undeployed")).getByText("no daemon deploy since on-dev")).toBeTruthy();
    expect(within(screen.getByTestId("generation-rewritten")).getByText("timing kept")).toBeTruthy();
    expect(within(screen.getByTestId("generation-waiting")).queryByText(/deploy/)).toBeNull();
    expect(screen.getByTestId("delivery-last-deploy").textContent).toContain("separate from on-dev");
  });

  it("drills into a generation: refs, Git reason, exact wait reason and CI run", async () => {
    render(page());
    const row = await screen.findByTestId("generation-slow");
    fireEvent.click(within(row).getByRole("button", { name: "Details" }));
    expect(screen.getByTestId("generation-refs").textContent).toMatch(
      /aq\/slow@aaaaaaaaa → main@bbbbbbbbb · Git reason source_not_contained/);
    expect(screen.getByText("reason: batch CI running")).toBeTruthy();
    expect(screen.getByRole("link", { name: /CI run/ }).getAttribute("href"))
      .toBe("https://github.com/o/r/actions/runs/7");
    expect(screen.getByTestId("wait-panel").textContent).toBe("slow@aq");
    expect(screen.getByRole("link", { name: "Task explain" }).getAttribute("href")).toBe("/tasks/slow?tab=explain");
    expect(screen.getByRole("link", { name: "Project wait graph" }).getAttribute("href")).toBe("/waiting?project=aq");
  });

  it("says a rewritten dev ref keeps the timing and blocks current delivery", async () => {
    render(page());
    const row = await screen.findByTestId("generation-rewritten");
    fireEvent.click(within(row).getByRole("button", { name: "Details" }));
    expect(screen.getByText(/Timing kept from the first sighting; dev no longer contains this generation/)).toBeTruthy();
  });

  it("filters by project and switches to the 7-day cohort", async () => {
    render(page());
    await screen.findByTestId("generation-slow");
    expect(api.calls[0]?.query).toEqual({ window: "24h" });
    fireEvent.change(screen.getByLabelText("Delivery project"), { target: { value: "aq" } });
    await waitFor(() => expect(lastQuery()).toEqual({ window: "24h", project_id: "aq" }));
    fireEvent.click(screen.getByRole("button", { name: "7d" }));
    await waitFor(() => expect(lastQuery()).toEqual({ window: "7d", project_id: "aq" }));
  });

  it("narrows the drill-down to one task from the explain link", async () => {
    render(page("/metrics?task=slow"));
    expect(await screen.findByText(/Showing slow only/)).toBeTruthy();
    expect(screen.queryByTestId("generation-stuck")).toBeNull();
    expect(screen.getByTestId("wait-panel")).toBeTruthy();
  });

  it("labels a report stale when its refresh fails and keeps the last numbers", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    queryClient.setQueryData(["metrics", "delivery-latency", "24h", "fleet"], POPULATED);
    api.respond = async () => { throw new Error("API 503: down"); };
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter><DeliveryLatency /></MemoryRouter>
      </QueryClientProvider>,
    );
    expect((await screen.findByTestId("delivery-stale", undefined, { timeout: 4000 })).textContent)
      .toContain("refresh failed");
    expect(within(screen.getByTestId("delivery-median")).getByText("10m 00s")).toBeTruthy();
  });

  it("reports a load failure without inventing numbers", async () => {
    api.respond = async () => { throw new Error("API 500: boom"); };
    render(page());
    expect((await screen.findByRole("alert", undefined, { timeout: 4000 })).textContent).toContain("boom");
    expect(screen.queryByTestId("delivery-median")).toBeNull();
  });
});
