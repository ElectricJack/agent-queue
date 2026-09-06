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

describe("useDebouncedBusyPoolEntries under a live flock", () => {
  afterEach(() => { vi.useRealTimers(); });

  it("shows a pool that turned busy even when the rail re-renders faster than the debounce", () => {
    // Every agent/session/task/message event invalidates the flock query, so
    // during real activity AgentFlock re-renders several times a second and
    // usePoolFlock hands the hook a *new* entries array each time.  A debounce
    // keyed on that array's identity re-armed on every render and never
    // fired, so a pool that had just claimed work stayed hidden for as long
    // as the fleet was busy — exactly when an operator looks for it.
    vi.useFakeTimers();
    const idle = () => poolEntries([pool({ running_busy: 0, running_idle: 1 })], []);
    const busy = () => poolEntries([pool({ running_busy: 1 })], []);
    const { result, rerender } = renderHook(({ entries }) => useDebouncedBusyPoolEntries(entries), {
      initialProps: { entries: idle() },
    });
    expect(result.current.busy).toEqual([]);

    // The pool claims a task; the rail then keeps re-rendering every 300ms
    // with fresh-but-equal entries for the next three seconds.
    for (let tick = 0; tick < 10; tick += 1) {
      rerender({ entries: busy() });
      act(() => { vi.advanceTimersByTime(300); });
    }
    expect(result.current.busy.map((entry) => entry.profileId)).toEqual(["worker-standard"]);
    expect(result.current.hiddenCount).toBe(0);
  });

  it("still holds a flip that is reverted within the debounce window", () => {
    vi.useFakeTimers();
    const busy = poolEntries([pool({ running_busy: 1 })], []);
    const idle = poolEntries([pool({ running_busy: 0, running_idle: 1 })], []);
    const { result, rerender } = renderHook(({ entries }) => useDebouncedBusyPoolEntries(entries), {
      initialProps: { entries: busy },
    });
    rerender({ entries: idle });
    act(() => { vi.advanceTimersByTime(400); });
    rerender({ entries: busy });
    act(() => { vi.advanceTimersByTime(1_000); });
    expect(result.current.busy).toHaveLength(1);
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
    expect(scaleRequest({ min: "2", max: "" }, pool({ max_active: null }).profile_id))
      .toEqual({ profile_id: "worker-standard", min: 2, max: null });
    expect(scaleRequest({ min: "2", max: "6" }, pool().profile_id))
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
  });

  it("lists every pool, idle ones included, on the agents page the idle link points at", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [
      pool({ profile_id: "worker-busy", running_busy: 1 }),
      pool({ profile_id: "worker-idle", running_busy: 0, running_idle: 2 }),
    ] } });
    renderAgents("/agents");

    const directory = await screen.findByRole("region", { name: "Worker pools" }, SLOW);
    expect(await within(directory).findByRole("button", { name: "Open pool worker-idle" }, SLOW)).toBeInTheDocument();
    expect(within(directory).getByRole("button", { name: "Open pool worker-busy" })).toBeInTheDocument();
    expect(within(directory).getByText("idle")).toBeInTheDocument();
    expect(within(directory).getByText("busy")).toBeInTheDocument();
  });

  it("opens a pool window when a directory row is clicked", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool({ profile_id: "worker-idle", running_busy: 0 })] } });
    renderAgents("/agents");

    fireEvent.click(await screen.findByRole("button", { name: "Open pool worker-idle" }, SLOW));
    expect(await screen.findByRole("region", { name: /worker-idle/ }, SLOW)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Worker pools" })).not.toBeInTheDocument();
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

describe("creating an agent or a pool", () => {
  beforeEach(() => {
    api.listProjects.mockResolvedValue({ data: { projects: [
      { id: "agent-queue", name: "Agent Queue", status: "active" },
      { id: "retired", name: "Retired", status: "archived" },
    ] } });
    api.listProfiles.mockResolvedValue({ data: { profiles: [
      { id: "implementer", name: "Implementer", lifecycle: "task" },
      { id: "worker-standard", name: "Worker standard", lifecycle: "pool", min_active: 1, max_active: 4 },
    ] } });
  });

  /** Open the fork, then one of its two forms. */
  async function openCreate(choice?: "Create agent" | "Create agent pool") {
    renderAgents("/agents");
    const rail = within(await screen.findByRole("region", { name: "Agent flock" }, SLOW));
    fireEvent.click(rail.getByRole("button", { name: "Create agent or pool" }));
    const fork = within(screen.getByRole("region", { name: "Create agent or pool" }));
    if (!choice) return fork;
    fireEvent.click(fork.getByRole("button", { name: choice }));
    return within(await screen.findByRole("form", { name: choice }, SLOW));
  }

  it("asks which of the two objects to create before showing either form", async () => {
    const fork = await openCreate();
    expect(fork.getByRole("button", { name: "Create agent" })).toBeInTheDocument();
    expect(fork.getByRole("button", { name: "Create agent pool" })).toBeInTheDocument();
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    // Scope and lifecycle are what tell the two apart, so both are on the fork.
    expect(fork.getByText(/One durable worker · global/)).toBeInTheDocument();
    expect(fork.getByText(/Elastic capacity · per project/)).toBeInTheDocument();
  });

  it("keeps a pool profile unselectable on the create-agent form and says why", async () => {
    const form = await openCreate("Create agent");
    const option = await form.findByRole("option", { name: "Worker standard — pool profile" }, SLOW);
    expect(option).toBeDisabled();
    expect(form.getByRole("option", { name: "Implementer" })).toBeEnabled();
  });

  it("refuses to create a durable agent on a pool profile and offers the pool form", async () => {
    const form = await openCreate("Create agent");
    await form.findByRole("option", { name: "Implementer" }, SLOW);
    fireEvent.change(form.getByLabelText("Name"), { target: { value: "Designer" } });
    // A profile's lifecycle can flip under an open form; the option is disabled
    // in the picker, so this is the path that has to fail visibly.
    fireEvent.change(form.getByLabelText("Profile"), { target: { value: "worker-standard" } });
    fireEvent.click(form.getByRole("button", { name: "Create agent" }));

    expect(await form.findByText(/is a pool profile/)).toBeInTheDocument();
    expect(form.getByRole("button", { name: "Create agent" })).toBeDisabled();
    expect(api.createAgent).not.toHaveBeenCalled();

    fireEvent.click(form.getByRole("button", { name: "Create an agent pool" }));
    expect(await screen.findByRole("form", { name: "Create agent pool" }, SLOW)).toBeInTheDocument();
  });

  it("offers only pool-eligible profiles and active projects on the pool form", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [] } });
    const form = await openCreate("Create agent pool");
    expect(await form.findByRole("option", { name: "Worker standard" }, SLOW)).toBeInTheDocument();
    expect(form.queryByRole("option", { name: "Implementer" })).not.toBeInTheDocument();
    expect(form.getByRole("option", { name: "Agent Queue" })).toBeInTheDocument();
    expect(form.queryByRole("option", { name: "Retired" })).not.toBeInTheDocument();
  });

  it("configures the pool through pool_scale and opens its view", async () => {
    const form = await openCreate("Create agent pool");
    await form.findByRole("option", { name: "Worker standard" }, SLOW);
    fireEvent.change(form.getByLabelText("Project"), { target: { value: "agent-queue" } });
    fireEvent.change(form.getByLabelText("Pool profile"), { target: { value: "worker-standard" } });
    // An existing pool is a reconfiguration, not a second pool — say so.
    expect(await form.findByText(/already runs a pool in agent-queue/)).toBeInTheDocument();
    fireEvent.change(form.getByLabelText("Minimum active workers"), { target: { value: "2" } });
    fireEvent.change(form.getByLabelText("Maximum active workers"), { target: { value: "6" } });
    fireEvent.click(form.getByRole("button", { name: "Create agent pool" }));

    await waitFor(() => expect(api.poolScale).toHaveBeenCalledTimes(1), SLOW);
    expect(api.poolScale.mock.calls[0]![0].body).toEqual({
      profile_id: "worker-standard", min: 2, max: 6,
    });
    expect(await screen.findByRole("region", { name: "worker-standard pool agent window" }, SLOW)).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Create agent pool" })).not.toBeInTheDocument();
    expect(api.createAgent).not.toHaveBeenCalled();
  });

  it("sends an unbounded max as an explicit null", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [] } });
    const form = await openCreate("Create agent pool");
    await form.findByRole("option", { name: "Worker standard" }, SLOW);
    fireEvent.change(form.getByLabelText("Project"), { target: { value: "agent-queue" } });
    fireEvent.change(form.getByLabelText("Pool profile"), { target: { value: "worker-standard" } });
    fireEvent.click(form.getByRole("button", { name: "Create agent pool" }));

    await waitFor(() => expect(api.poolScale).toHaveBeenCalledTimes(1), SLOW);
    expect(api.poolScale.mock.calls[0]![0].body).toEqual({
      profile_id: "worker-standard", min: 1, max: null,
    });
  });

  it("blocks bounds pool_scale would reject before submitting them", async () => {
    const form = await openCreate("Create agent pool");
    await form.findByRole("option", { name: "Worker standard" }, SLOW);
    fireEvent.change(form.getByLabelText("Project"), { target: { value: "agent-queue" } });
    fireEvent.change(form.getByLabelText("Pool profile"), { target: { value: "worker-standard" } });
    fireEvent.change(form.getByLabelText("Minimum active workers"), { target: { value: "9" } });

    expect(await form.findByText("Max must be greater than or equal to min.")).toBeInTheDocument();
    expect(form.getByRole("button", { name: "Create agent pool" })).toBeDisabled();
    expect(api.poolScale).not.toHaveBeenCalled();
  });

  it("shows an in-band pool_scale refusal without opening a pool view", async () => {
    api.poolScale.mockResolvedValue({ data: { success: false, error: "no pool profile 'worker-standard'" } });
    const form = await openCreate("Create agent pool");
    await form.findByRole("option", { name: "Worker standard" }, SLOW);
    fireEvent.change(form.getByLabelText("Project"), { target: { value: "agent-queue" } });
    fireEvent.change(form.getByLabelText("Pool profile"), { target: { value: "worker-standard" } });
    fireEvent.click(form.getByRole("button", { name: "Create agent pool" }));

    expect(await form.findByText(/no pool profile/, undefined, SLOW)).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Create agent pool" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "worker-standard pool agent window" })).not.toBeInTheDocument();
  });

  it("explains an empty profile list rather than offering an unusable form", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [] } });
    api.listProfiles.mockResolvedValue({ data: { profiles: [{ id: "implementer", name: "Implementer", lifecycle: "task" }] } });
    const form = await openCreate("Create agent pool");
    expect(await form.findByText(/No profile runs as a pool yet/, undefined, SLOW)).toBeInTheDocument();
    expect(form.getByRole("button", { name: "Create agent pool" })).toBeDisabled();
  });

  it.each([
    ["Create agent", "Create agent"],
    ["Create agent pool", "Create agent pool"],
  ] as const)("cancels %s back to no open form", async (choice, formName) => {
    const form = await openCreate(choice);
    fireEvent.click(form.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("form", { name: formName })).not.toBeInTheDocument());
    expect(screen.queryByRole("region", { name: "Create agent or pool" })).not.toBeInTheDocument();
    expect(api.poolScale).not.toHaveBeenCalled();
    expect(api.createAgent).not.toHaveBeenCalled();
  });
});
