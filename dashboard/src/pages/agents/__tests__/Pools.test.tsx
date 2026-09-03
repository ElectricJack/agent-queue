import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import LeftRail from "../../../shell/LeftRail";
import AgentWorkspace from "../AgentWorkspace";
import type { FlockAgent } from "../../../api/agents";
import type { PoolStatusRow, SessionSummary } from "../../../api/hooks";
import { boundsOf, scaleRequest, validateBounds } from "../PoolScaleFields";
import { poolEntries, poolProfileIds, isPoolAgent, formatIdle, splitBusyPoolEntries, useDebouncedBusyPoolEntries } from "../pools";
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

/** Mirrors one roster row (src/api/models/agent.py AgentSummary): a hand-made idle worker unless overridden. */
function agent(id: string, name: string, profileId: string, over: Partial<FlockAgent> = {}): FlockAgent {
  return {
    id, name, profile_id: profileId, role: "worker", enabled: true, state: "idle",
    provider: "anthropic", harness: "claude", model: "claude-sonnet-4-6",
    intelligence_class: "standard-high", current_task_id: null, current_task_title: null,
    current_project_id: null, session_id: "session-" + id, session_state: "running",
    session_provider: "tmux", project_id: null, workspace_id: null,
    origin: "manual", session_lifecycle: null,
    active_subagent_count: 0, subagent_count_complete: true,
    aq_subagent_count: 0, native_subagent_count: 0,
    settings: { name, profile_id: profileId, harness: null, model: null, intelligence_class: null, enabled: true },
    ...over,
  };
}

/** A row `_launch_pool_session` minted, currently running one of the pool's sessions. */
function pooledAgent(id: string, name: string, profileId: string, over: Partial<FlockAgent> = {}): FlockAgent {
  return agent(id, name, profileId, { origin: "pool", session_lifecycle: "pool", ...over });
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
    pooledAgent("pooled", "worker-standard-9f2a", "worker-standard"),
    // Added by hand on the pool's profile; a durable worker like Builder.
    agent("handmade", "Spare", "worker-standard"),
  ], count: 3 } });
  api.listProjects.mockResolvedValue({ data: { projects: [] } });
  api.listProfiles.mockResolvedValue({ data: { profiles: [{ id: "implementer", name: "Implementer" }, { id: "worker-standard", name: "Worker standard" }] } });
  api.listIntelligenceClasses.mockResolvedValue({ data: { classes: [] } });
  api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool()] } });
  api.sessionList.mockResolvedValue({ data: { success: true, sessions: [instance("aaa"), instance("bbb", { task_id: "quick-torrent-39", started_at: 200 })], count: 2 } });
  api.poolScale.mockResolvedValue({ data: { success: true, profile_id: "worker-standard", min_active: 2, max_active: 6, project_caps: [], terminated: [], warnings: [] } });
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

  it("classifies a pool instance by origin or live pool session, never by profile", () => {
    expect(poolProfileIds([pool()])).toEqual(new Set(["worker-standard"]));
    // Minted by a pool: an instance whether it is running or idle between sessions.
    expect(isPoolAgent(pooledAgent("pooled", "worker-standard-9f2a", "worker-standard"))).toBe(true);
    expect(isPoolAgent(agent("resting", "worker-standard-1c2d", "worker-standard", { origin: "pool" }))).toBe(true);
    // Hand-made on the pool's profile: a durable worker until a pool session owns it.
    expect(isPoolAgent(agent("handmade", "Spare", "worker-standard"))).toBe(false);
    expect(isPoolAgent(agent("handmade", "Spare", "worker-standard", { session_lifecycle: "pool" }))).toBe(true);
    expect(isPoolAgent(agent("fixed", "Builder", "implementer"))).toBe(false);
    expect(isPoolAgent(agent("fixed", "Builder", "implementer", { session_lifecycle: "task" }))).toBe(false);
  });

  it("formats idle time in the largest whole unit", () => {
    expect(formatIdle(42)).toBe("42s idle");
    expect(formatIdle(605)).toBe("10m idle");
    expect(formatIdle(7300)).toBe("2h idle");
    expect(formatIdle(undefined)).toBe("0s idle");
  });

  it("separates busy pools from configured pools without claimed work", () => {
    const entries = poolEntries([
      pool({ profile_id: "worker-busy", running_busy: 1 }),
      pool({ profile_id: "worker-idle", running_busy: 0, running_idle: 2 }),
    ], []);

    expect(splitBusyPoolEntries(entries)).toEqual({
      busy: [expect.objectContaining({ profileId: "worker-busy" })],
      hiddenCount: 1,
    });
  });

  it("debounces a pool's visibility after a status update", () => {
    vi.useFakeTimers();
    const busy = poolEntries([pool({ running_busy: 1 })], []);
    const idle = poolEntries([pool({ running_busy: 0, running_idle: 1 })], []);
    const { result, rerender } = renderHook(({ entries }) => useDebouncedBusyPoolEntries(entries), {
      initialProps: { entries: busy },
    });

    expect(result.current).toEqual({ busy, hiddenCount: 0 });
    rerender({ entries: idle });
    expect(result.current).toEqual({ busy, hiddenCount: 0 });
    act(() => { vi.advanceTimersByTime(1_000); });
    expect(result.current).toEqual({ busy: [], hiddenCount: 1 });
    vi.useRealTimers();
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
    expect(validateBounds({ min: "0", max: "" })).toBeNull();
    expect(validateBounds({ min: "2", max: "2" })).toBeNull();
  });

  it("rejects a negative min", () => {
    expect(validateBounds({ min: "-1", max: "4" })).toBe("Min must be 0 or more.");
  });

  it("rejects a max below the min", () => {
    expect(validateBounds({ min: "3", max: "2" })).toBe("Max must be greater than or equal to min.");
  });

  it("rejects a max below one and non-numeric bounds", () => {
    expect(validateBounds({ min: "0", max: "0" })).toBe("Max must be 1 or more.");
    expect(validateBounds({ min: "", max: "4" })).toBe("Min must be a whole number of workers.");
    expect(validateBounds({ min: "1", max: "lots" })).toMatch(/whole number/);
  });

  it("accepts an empty max as an unbounded pool", () => {
    expect(validateBounds({ min: "1", max: "" })).toBeNull();
  });

  it("sends an explicit null max so the API removes the profile limit", () => {
    expect(scaleRequest({ min: "2", max: "" }, pool({ max_active: null })))
      .toEqual({ profile_id: "worker-standard", min: 2, max: null });
    expect(scaleRequest({ min: "2", max: "6" }, pool()))
      .toEqual({ profile_id: "worker-standard", min: 2, max: 6 });
  });
});

describe("pools in the agent flock", () => {
  it("shows only busy pools, with an idle-pool link to management", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [
      pool({ profile_id: "worker-busy", running_busy: 1 }),
      pool({ profile_id: "worker-idle", running_busy: 0, running_idle: 2 }),
    ] } });
    renderAgents("/");

    expect(await screen.findByRole("button", { name: "Open worker-busy pool" }, SLOW)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open worker-idle pool" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "1 idle pool" })).toHaveAttribute("href", "/agents");
  });

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
    // Sharing the pool's profile does not make a hand-made worker a pool instance.
    expect(screen.getByRole("button", { name: "Open Spare" })).toBeInTheDocument();
    const header = screen.getByRole("button", { name: /Agent flock/ });
    expect(within(header).getByText("3")).toBeInTheDocument();
  });

  it("folds a hand-made worker under the pool only while a pool session owns it", async () => {
    api.listAgents.mockResolvedValue({ data: { agents: [
      agent("fixed", "Builder", "implementer"),
      pooledAgent("pooled", "worker-standard-9f2a", "worker-standard"),
      agent("handmade", "Spare", "worker-standard", {
        state: "busy", session_lifecycle: "pool", session_id: "p-worker-standard--agent-queue--ccc",
        current_task_id: "quick-torrent-40", current_project_id: "agent-queue", project_id: "agent-queue",
      }),
    ], count: 3 } });
    api.sessionList.mockResolvedValue({ data: { success: true, sessions: [
      instance("aaa"), instance("bbb", { task_id: "quick-torrent-39", started_at: 200 }),
      instance("ccc", { agent_id: "handmade", task_id: "quick-torrent-40", started_at: 300 }),
    ], count: 3 } });
    renderAgents("/");
    const row = await screen.findByRole("button", { name: "Open worker-standard pool" }, SLOW);
    expect(await within(row).findByText("3 live instances", undefined, SLOW)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open Builder" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open Spare" })).not.toBeInTheDocument();
    expect(within(screen.getByRole("button", { name: /Agent flock/ })).getByText("2")).toBeInTheDocument();
  });

  it("keeps another project's pool instances out of this pool and off the rail", async () => {
    api.listAgents.mockResolvedValue({ data: { agents: [
      agent("fixed", "Builder", "implementer"),
      pooledAgent("pooled", "worker-standard-9f2a", "worker-standard"),
      pooledAgent("elsewhere", "worker-standard-77aa", "worker-standard", {
        session_id: "p-worker-standard--elsewhere--ddd", current_project_id: "elsewhere", project_id: "elsewhere",
      }),
    ], count: 3 } });
    api.sessionList.mockResolvedValue({ data: { success: true, sessions: [
      instance("aaa"), instance("bbb", { task_id: "quick-torrent-39", started_at: 200 }),
      instance("ddd", { project_id: "elsewhere", agent_id: "elsewhere" }),
    ], count: 3 } });
    renderAgents("/");
    const row = await screen.findByRole("button", { name: "Open worker-standard pool" }, SLOW);
    expect(await within(row).findByText("2 live instances", undefined, SLOW)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open worker-standard-77aa" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /pool$/ })).toHaveLength(1);
  });

  it("still opens a pool instance directly from /agents?agent=<id>", async () => {
    renderAgents("/agents?agent=pooled");
    expect(await screen.findByRole("region", { name: "worker-standard-9f2a agent window" }, SLOW)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open worker-standard-9f2a" })).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Open Spare" }, SLOW)).toBeInTheDocument();
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
    // Bounds are configured on the (global) system profile — no project_id.
    expect(api.poolScale.mock.calls[0]![0].body).toEqual({
      profile_id: "worker-standard", min: 2, max: 6,
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

  it("clears a maximum bound through the typed API", async () => {
    renderAgents("/");
    fireEvent.click(await screen.findByRole("button", { name: "Open worker-standard pool" }, SLOW));
    const window = await screen.findByRole("region", { name: "worker-standard pool agent window" }, SLOW);
    fireEvent.click(within(window).getByRole("tab", { name: "Settings" }));

    fireEvent.change(await within(window).findByLabelText("Maximum active workers", undefined, SLOW), {
      target: { value: "" },
    });
    fireEvent.click(within(window).getByRole("button", { name: "Save pool bounds" }));

    await waitFor(() => expect(api.poolScale).toHaveBeenCalledTimes(1), SLOW);
    expect(api.poolScale.mock.calls[0]![0].body).toEqual({
      profile_id: "worker-standard", min: 1, max: null,
    });
    expect(await within(window).findByText("Pool bounds saved.")).toBeInTheDocument();
  });

  it("surfaces an in-band pool_scale refusal", async () => {
    api.poolScale.mockResolvedValue({ data: { success: false, error: "no pool profile 'worker-standard'" } });
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
