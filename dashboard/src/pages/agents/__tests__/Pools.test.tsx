import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import LeftRail from "../../../shell/LeftRail";
import AgentWorkspace from "../AgentWorkspace";
import type { FlockAgent } from "../../../api/agents";
import type { PoolStatusRow, SessionSummary } from "../../../api/hooks";
import { boundsOf, scaleRequest, validateBounds } from "../PoolScaleFields";
import { poolEntries, poolProfileIds, isPoolAgent, formatIdle } from "../pools";
import { parseAgentSelection, poolSelectionKey, selectionAddress } from "../useAgentSelection";
import { TerminalMock, FitAddonMock, TerminalSocketMock } from "../../../testUtils/terminal";

vi.mock("@xterm/xterm", async () => ({ Terminal: (await import("../../../testUtils/terminal")).TerminalMock }));
vi.mock("@xterm/addon-fit", async () => ({ FitAddon: (await import("../../../testUtils/terminal")).FitAddonMock }));

const api = vi.hoisted(() => ({
  listAgents: vi.fn(), listProjects: vi.fn(), listProfiles: vi.fn(), listIntelligenceClasses: vi.fn(),
  getAgent: vi.fn(), editAgent: vi.fn(), createAgent: vi.fn(), deleteAgent: vi.fn(),
  sessionInput: vi.fn(), startAgentTerminal: vi.fn(),
  poolStatus: vi.fn(), poolScale: vi.fn(), sessionList: vi.fn(),
}));
vi.mock("../../../api/client", () => api);

/** Mirrors one `pool_status` row (src/api/models/task.py PoolStatusRow). */
function pool(over: Partial<PoolStatusRow> = {}): PoolStatusRow {
  return {
    project_id: "agent-queue", profile_id: "worker-standard",
    min_active: 1, max_active: 4, desired: 3,
    running_idle: 1, running_busy: 2, starting: 0, draining: 0, ready: 5,
    quarantined_until: null, ...over,
  };
}

/** Mirrors one `aq session list` row with lifecycle "pool". */
function instance(suffix: string, over: Partial<SessionSummary> = {}): SessionSummary {
  const name = "p-worker-standard--agent-queue--" + suffix;
  return {
    id: name, name, project_id: "agent-queue", profile_id: "worker-standard",
    lifecycle: "pool", state: "running", provider: "tmux", harness: "claude",
    model: "claude-opus-5", intelligence_class: "standard-high", task_id: null,
    work_dir: "/w/" + suffix, started_at: 100, last_activity: 100,
    idle_seconds: 42, stalled: false, restarts: 0, ...over,
  };
}

function agent(id: string, name: string, profileId: string): FlockAgent {
  return {
    id, name, profile_id: profileId, role: "worker", enabled: true, state: "idle",
    provider: "anthropic", harness: "claude", model: "claude-sonnet-4-6",
    intelligence_class: "standard-high", current_task_id: null, current_task_title: null,
    current_project_id: null, session_id: "session-" + id, session_state: "running",
    session_provider: "tmux", project_id: null, workspace_id: null,
    active_subagent_count: 0, subagent_count_complete: true,
    aq_subagent_count: 0, native_subagent_count: 0,
    settings: { name, profile_id: profileId, harness: null, model: null, intelligence_class: null, enabled: true },
  };
}

// The rail row for a pool needs two queries (pool_status + session_list) to
// land; the 1s default is tight when the whole suite runs in parallel.
const SLOW = { timeout: 5_000 };

const clients: QueryClient[] = [];

function renderAgents(initial = "/agents") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initial]}>
        <LeftRail />
        <Routes><Route path="/agents" element={<AgentWorkspace />} /><Route path="*" element={null} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  TerminalSocketMock.instances = [];
  TerminalMock.instances = [];
  FitAddonMock.instances = [];
  vi.stubGlobal("WebSocket", TerminalSocketMock);
  vi.stubGlobal("EventSource", vi.fn());
  api.listAgents.mockResolvedValue({ data: { agents: [
    agent("fixed", "Builder", "implementer"),
    agent("pooled", "worker-standard-9f2a", "worker-standard"),
  ], count: 2 } });
  api.listProjects.mockResolvedValue({ data: { projects: [] } });
  api.listProfiles.mockResolvedValue({ data: { profiles: [{ id: "implementer", name: "Implementer" }, { id: "worker-standard", name: "Worker standard" }] } });
  api.listIntelligenceClasses.mockResolvedValue({ data: { classes: [] } });
  api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool()] } });
  api.sessionList.mockResolvedValue({ data: { success: true, sessions: [instance("aaa"), instance("bbb", { task_id: "quick-torrent-39", started_at: 200 })], count: 2 } });
  api.poolScale.mockResolvedValue({ data: { success: true, project_id: "agent-queue", profile_id: "worker-standard", min_active: 2, max_active: 6, terminated: [] } });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  clients.splice(0).forEach((client) => client.clear());
});

describe("pool derivation", () => {
  it("joins pool_status rows to their live sessions, oldest instance first", () => {
    const entries = poolEntries([pool()], [instance("bbb", { started_at: 200 }), instance("aaa", { started_at: 100 })]);
    expect(entries).toHaveLength(1);
    expect(entries[0]!.key).toBe("pool:agent-queue:worker-standard");
    expect(entries[0]!.instances.map((row) => row.id)).toEqual([
      "p-worker-standard--agent-queue--aaa",
      "p-worker-standard--agent-queue--bbb",
    ]);
  });

  it("ignores sessions belonging to another project or profile", () => {
    const entries = poolEntries([pool()], [
      instance("aaa"),
      instance("other", { project_id: "elsewhere" }),
      instance("push", { profile_id: "implementer" }),
    ]);
    expect(entries[0]!.instances.map((row) => row.id)).toEqual(["p-worker-standard--agent-queue--aaa"]);
  });

  it("treats a worker on a pool profile as a pool instance, not a push agent", () => {
    const ids = poolProfileIds([pool()]);
    expect(isPoolAgent(agent("pooled", "worker-standard-9f2a", "worker-standard"), ids)).toBe(true);
    expect(isPoolAgent(agent("fixed", "Builder", "implementer"), ids)).toBe(false);
  });

  it("formats idle time in the largest whole unit", () => {
    expect(formatIdle(42)).toBe("42s idle");
    expect(formatIdle(605)).toBe("10m idle");
    expect(formatIdle(7300)).toBe("2h idle");
    expect(formatIdle(undefined)).toBe("0s idle");
  });
});

describe("pool selection keys", () => {
  it("round-trips a pool key with and without a pinned instance", () => {
    const bare = poolSelectionKey("agent-queue", "worker-standard");
    expect(parseAgentSelection(bare)).toEqual({
      key: bare, kind: "pool", projectId: "agent-queue", profileId: "worker-standard", instanceId: null,
    });
    const pinned = poolSelectionKey("agent-queue", "worker-standard", "p-x--y--1");
    expect(parseAgentSelection(pinned)).toMatchObject({ kind: "pool", instanceId: "p-x--y--1" });
    expect(selectionAddress(pinned)).toBe(bare);
  });

  it("reads a plain id as a fixed agent", () => {
    expect(parseAgentSelection("agent-7f1c")).toEqual({ key: "agent-7f1c", kind: "agent", agentId: "agent-7f1c" });
  });
});

describe("pool bounds validation", () => {
  it("reads the current bounds, with an unbounded max as an empty field", () => {
    expect(boundsOf(pool())).toEqual({ min: "1", max: "4" });
    expect(boundsOf(pool({ max_active: null }))).toEqual({ min: "1", max: "" });
  });

  it("accepts a min of zero and an empty max on an already-unbounded pool", () => {
    expect(validateBounds({ min: "0", max: "" }, pool({ max_active: null }))).toBeNull();
    expect(validateBounds({ min: "2", max: "2" }, pool())).toBeNull();
  });

  it("rejects a negative min", () => {
    expect(validateBounds({ min: "-1", max: "4" }, pool())).toBe("Min must be 0 or more.");
  });

  it("rejects a max below the min", () => {
    expect(validateBounds({ min: "3", max: "2" }, pool())).toBe("Max must be greater than or equal to min.");
  });

  it("rejects a max below one and non-numeric bounds", () => {
    expect(validateBounds({ min: "0", max: "0" }, pool())).toBe("Max must be 1 or more.");
    expect(validateBounds({ min: "", max: "4" }, pool())).toBe("Min must be a whole number of workers.");
    expect(validateBounds({ min: "1", max: "lots" }, pool())).toMatch(/whole number/);
  });

  it("refuses to clear a max that pool_scale cannot unset", () => {
    // pool_scale treats an omitted max as "unchanged" and rejects max < 1, so
    // there is no request that clears an existing bound.
    expect(validateBounds({ min: "1", max: "" }, pool())).toMatch(/cannot be cleared/);
  });

  it("omits an empty max from the request so the bound stays unset", () => {
    expect(scaleRequest({ min: "2", max: "" }, pool({ max_active: null })))
      .toEqual({ project_id: "agent-queue", profile_id: "worker-standard", min: 2 });
    expect(scaleRequest({ min: "2", max: "6" }, pool()))
      .toEqual({ project_id: "agent-queue", profile_id: "worker-standard", min: 2, max: 6 });
  });
});

describe("pools in the agent flock", () => {
  it("badges the pool and shows its live supply, hiding the pool's own worker rows", async () => {
    renderAgents("/");
    const row = await screen.findByRole("button", { name: "Open worker-standard pool" }, SLOW);
    expect(within(row).getByText("Pool")).toBeInTheDocument();
    expect(within(row).getByText("desired 3")).toBeInTheDocument();
    expect(within(row).getByText("idle 1")).toBeInTheDocument();
    expect(within(row).getByText("busy 2")).toBeInTheDocument();
    expect(within(row).getByText("starting 0")).toBeInTheDocument();
    expect(within(row).getByText("draining 0")).toBeInTheDocument();
    expect(within(row).getByText("ready 5")).toBeInTheDocument();
    expect(within(row).getByText("2 live instances")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open Builder" })).toBeInTheDocument();
    // The pool's own agent row is reachable through the pool, not beside it.
    expect(screen.queryByRole("button", { name: "Open worker-standard-9f2a" })).not.toBeInTheDocument();
  });

  it("shows the quarantine backoff when a launch has failed", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool({ quarantined_until: Date.now() / 1000 + 30 })] } });
    renderAgents("/");
    const row = await screen.findByRole("button", { name: "Open worker-standard pool" }, SLOW);
    expect(await within(row).findByText(/Quarantined for/, undefined, SLOW)).toBeInTheDocument();
  });
});

describe("pool instance selection", () => {
  it("binds the terminal to the selected instance and rebinds when it changes", async () => {
    renderAgents("/");
    fireEvent.click(await screen.findByRole("button", { name: "Open worker-standard pool" }, SLOW));
    const window = await screen.findByRole("region", { name: "worker-standard pool agent window" }, SLOW);
    // Oldest instance first, so the first live session is bound by default.
    await waitFor(() => expect(TerminalSocketMock.instances).toHaveLength(1), SLOW);
    expect(TerminalSocketMock.instances[0]!.url).toContain("p-worker-standard--agent-queue--aaa");

    const picker = within(window).getByLabelText("Instance");
    expect(within(picker).getByRole("option", { name: /p-worker-standard--agent-queue--bbb · quick-torrent-39 · 42s idle/ })).toBeInTheDocument();
    fireEvent.change(picker, { target: { value: "p-worker-standard--agent-queue--bbb" } });

    await waitFor(() => expect(TerminalSocketMock.instances).toHaveLength(2), SLOW);
    expect(TerminalSocketMock.instances[1]!.url).toContain("p-worker-standard--agent-queue--bbb");
    expect(TerminalSocketMock.instances[0]!.closed).toBe(true);
  });

  it("falls back to a live instance when the pinned one is gone", async () => {
    renderAgents("/agents?agent=" + encodeURIComponent(poolSelectionKey("agent-queue", "worker-standard", "p-worker-standard--agent-queue--zzz")));
    await waitFor(() => expect(TerminalSocketMock.instances).toHaveLength(1), SLOW);
    expect(TerminalSocketMock.instances[0]!.url).toContain("p-worker-standard--agent-queue--aaa");
  });

  it("explains an empty pool instead of opening a terminal", async () => {
    api.sessionList.mockResolvedValue({ data: { success: true, sessions: [], count: 0 } });
    renderAgents("/");
    fireEvent.click(await screen.findByRole("button", { name: "Open worker-standard pool" }, SLOW));
    expect(await screen.findByText("No live instances.", undefined, SLOW)).toBeInTheDocument();
    expect(await screen.findByText("No live pool instance", undefined, SLOW)).toBeInTheDocument();
    expect(TerminalSocketMock.instances).toHaveLength(0);
  });
});

describe("pool settings", () => {
  it("saves new bounds through pool_scale", async () => {
    renderAgents("/");
    fireEvent.click(await screen.findByRole("button", { name: "Open worker-standard pool" }, SLOW));
    const window = await screen.findByRole("region", { name: "worker-standard pool agent window" }, SLOW);
    fireEvent.click(within(window).getByRole("tab", { name: "Settings" }));

    const min = await within(window).findByLabelText("Minimum active workers", undefined, SLOW);
    expect(min).toHaveValue(1);
    fireEvent.change(min, { target: { value: "2" } });
    fireEvent.change(within(window).getByLabelText("Maximum active workers"), { target: { value: "6" } });
    fireEvent.click(within(window).getByRole("button", { name: "Save pool bounds" }));

    await waitFor(() => expect(api.poolScale).toHaveBeenCalledTimes(1), SLOW);
    expect(api.poolScale.mock.calls[0]![0].body).toEqual({
      project_id: "agent-queue", profile_id: "worker-standard", min: 2, max: 6,
    });
    expect(await within(window).findByText("Pool bounds saved.")).toBeInTheDocument();
  });

  it("blocks a save that pool_scale would reject", async () => {
    renderAgents("/");
    fireEvent.click(await screen.findByRole("button", { name: "Open worker-standard pool" }, SLOW));
    const window = await screen.findByRole("region", { name: "worker-standard pool agent window" }, SLOW);
    fireEvent.click(within(window).getByRole("tab", { name: "Settings" }));

    fireEvent.change(await within(window).findByLabelText("Minimum active workers", undefined, SLOW), { target: { value: "9" } });
    expect(await within(window).findByText("Max must be greater than or equal to min.")).toBeInTheDocument();
    expect(within(window).getByRole("button", { name: "Save pool bounds" })).toBeDisabled();
    expect(api.poolScale).not.toHaveBeenCalled();
  });

  it("surfaces an in-band pool_scale refusal", async () => {
    api.poolScale.mockResolvedValue({ data: { success: false, error: "no pool profile 'worker-standard' for agent-queue" } });
    renderAgents("/");
    fireEvent.click(await screen.findByRole("button", { name: "Open worker-standard pool" }, SLOW));
    const window = await screen.findByRole("region", { name: "worker-standard pool agent window" }, SLOW);
    fireEvent.click(within(window).getByRole("tab", { name: "Settings" }));
    fireEvent.change(await within(window).findByLabelText("Minimum active workers", undefined, SLOW), { target: { value: "2" } });
    fireEvent.click(within(window).getByRole("button", { name: "Save pool bounds" }));
    expect(await within(window).findByText(/no pool profile/)).toBeInTheDocument();
  });

  it("offers each project's bounds on a pool worker's own settings tab", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool(), pool({ project_id: "other-repo", min_active: 0, max_active: null })] } });
    // A pool worker is only reachable by URL once the flock hides its row.
    renderAgents("/agents?agent=pooled");
    const window = await screen.findByRole("region", { name: "worker-standard-9f2a agent window" }, SLOW);
    fireEvent.click(within(window).getByRole("tab", { name: "Settings" }));
    const section = await within(window).findByRole("region", { name: "Worker pool settings" }, SLOW);
    expect(within(section).getByText("agent-queue")).toBeInTheDocument();
    expect(within(section).getByText("other-repo")).toBeInTheDocument();
    expect(within(section).getAllByLabelText("Maximum active workers")[1]).toHaveValue(null);
  });
});
