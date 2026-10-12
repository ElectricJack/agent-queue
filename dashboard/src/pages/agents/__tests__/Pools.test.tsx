import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import LeftRail from "../../../shell/LeftRail";
import AgentWorkspace from "../AgentWorkspace";
import type { FlockAgent } from "../../../api/agents";
import type { PoolProjectStatus, PoolStatusRow, SessionSummary } from "../../../api/hooks";
import { boundsOf, scaleRequest, validateBounds } from "../PoolScaleFields";
import { PoolSupplyRow } from "../PoolMetadata";
import { poolEntries, poolPlacement, poolProfileIds, isPoolAgent, formatIdle, outsideSessionAgent, outsideSessionName, splitBusyPoolEntries, useDebouncedBusyPoolEntries, type OutsidePoolSession } from "../pools";
import { parseAgentSelection, poolSelectionKey, selectionAddress } from "../useAgentSelection";
import { TerminalMock, FitAddonMock, TerminalSocketMock } from "../../../testUtils/terminal";
import { createFakeDashboardStateServer, TestDashboardState } from "../../../testUtils/dashboardState";

vi.mock("@xterm/xterm", async () => ({ Terminal: (await import("../../../testUtils/terminal")).TerminalMock }));
vi.mock("@xterm/addon-fit", async () => ({ FitAddon: (await import("../../../testUtils/terminal")).FitAddonMock }));

const api = vi.hoisted(() => ({
  listAgents: vi.fn(), listProjects: vi.fn(), listProfiles: vi.fn(), listIntelligenceClasses: vi.fn(),
  getAgent: vi.fn(), editAgent: vi.fn(), createAgent: vi.fn(), deleteAgent: vi.fn(),
  sessionInput: vi.fn(), startAgentTerminal: vi.fn(),
  poolStatus: vi.fn(), poolRename: vi.fn(), poolScale: vi.fn(), poolSetEnabled: vi.fn(), sessionList: vi.fn(),
  getProviderAllocationApiProvidersAllocationGet: vi.fn(),
}));
vi.mock("../../../api/client", () => api);

/** Mirrors one nested project breakdown (src/api/models/task.py PoolProjectStatus). */
function project(over: Partial<PoolProjectStatus> = {}): PoolProjectStatus {
  return {
    project_id: "agent-queue", ready: 5, running_idle: 1, running_busy: 2,
    starting: 0, draining: 0, max_concurrent_agents: 4, workspace_capacity: 2,
    quarantined_until: null, quarantined_reason: null, ...over,
  };
}

/**
 * Mirrors one `pool_status` row (src/api/models/task.py PoolStatusRow).
 *
 * A row is one *profile*, fleet-wide: the supply numbers are aggregates and
 * the projects they are spread across are nested inside it.
 */
function pool(over: Partial<PoolStatusRow> = {}): PoolStatusRow {
  return {
    profile_id: "worker-standard",
    min_active: 1, max_active: 4, desired: 3,
    running_idle: 1, running_busy: 2, starting: 0, draining: 0, ready: 5,
    projects: [project()], ...over,
  };
}

it("shows a pool's effective Codex service tier", () => {
  render(<PoolSupplyRow pool={pool({ service_tier: "fast" })} />);
  expect(screen.getByText("Codex fast")).toBeInTheDocument();
});

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

/**
 * Mirrors one `pool_status` ``outside_pools`` row: a live *task*-lifecycle
 * session on a route this pool serves — e.g. a task launched on a profile
 * before it switched to ``lifecycle: pool``. It is not a pool member.
 */
function outside(sessionId: string, over: Partial<OutsidePoolSession> = {}): OutsidePoolSession {
  return {
    session_id: sessionId, project_id: "agent-queue", profile_id: "worker-standard",
    harness: "claude", intelligence_class: "standard-high", name: "t-worker-standard--" + sessionId,
    state: "running", task_id: null, task_title: null, started_at: 100, ...over,
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

function NavigationProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return <>
    <output aria-label="Current URL">{location.pathname + location.search}</output>
    <button onClick={() => navigate(-1)}>Previous page</button>
    <button onClick={() => navigate(1)}>Next page</button>
  </>;
}

function renderAgents(initial = "/agents") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <TestDashboardState server={createFakeDashboardStateServer()}>
        <MemoryRouter initialEntries={[initial]}>
          <NavigationProbe />
          <LeftRail />
          <Routes><Route path="/agents" element={<AgentWorkspace />} /><Route path="*" element={null} /></Routes>
        </MemoryRouter>
      </TestDashboardState>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  TerminalSocketMock.instances = [];
  TerminalMock.instances = [];
  FitAddonMock.instances = [];
  vi.stubGlobal("WebSocket", TerminalSocketMock);
  vi.stubGlobal("EventSource", vi.fn());
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ width: 800, height: 400 } as DOMRect);
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
  api.poolSetEnabled.mockResolvedValue({ data: { success: true, profile_id: "worker-standard", enabled: false, warnings: [] } });
  api.poolRename.mockResolvedValue({ data: { success: true, profile_id: "worker-standard", name: "Space Bunny", changed: true } });
  api.getProviderAllocationApiProvidersAllocationGet.mockResolvedValue({ data: {
    success: true, now: 1, providers: [], projects: [], diagnostics: [],
  } });
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
    expect(entries[0]!.key).toBe("pool:worker-standard");
    expect(entries[0]!.instances.map((row) => row.id)).toEqual([
      "p-worker-standard--agent-queue--aaa",
      "p-worker-standard--agent-queue--bbb",
    ]);
  });

  it("keeps a pool's workers from every project and ignores other profiles", () => {
    // A pool is one profile fleet-wide, so a worker placed in another project
    // is still this pool's worker — filtering it out would hide most of the
    // fleet from the instance picker.
    const entries = poolEntries([pool()], [
      instance("aaa"),
      instance("other", { project_id: "elsewhere", started_at: 150 }),
      instance("push", { profile_id: "implementer" }),
    ]);
    expect(entries[0]!.instances.map((row) => row.id)).toEqual([
      "p-worker-standard--agent-queue--aaa",
      "p-worker-standard--agent-queue--other",
    ]);
  });

  it("carries the per-project breakdown, ordered by project id", () => {
    const entries = poolEntries([pool({
      projects: [project({ project_id: "zeta" }), project({ project_id: "alpha" })],
    })], []);
    expect(entries[0]!.projects.map((row) => row.project_id)).toEqual(["alpha", "zeta"]);
  });

  it("orders placement by live workers and drops projects holding none", () => {
    // Warmth concentrates in the busiest project, so the one line the rail has
    // room for shows the busiest first, not the alphabetically first.
    expect(poolPlacement([
      project({ project_id: "quiet", running_idle: 0, running_busy: 0, starting: 0 }),
      project({ project_id: "small", running_idle: 1, running_busy: 0 }),
      project({ project_id: "busy", running_idle: 1, running_busy: 3 }),
    ]).map((row) => row.project_id)).toEqual(["busy", "small"]);
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

  it("carries a pool's outside-pool sessions, oldest first", () => {
    const entries = poolEntries([pool({ outside_pools: [
      outside("late", { started_at: 200 }),
      outside("early", { started_at: 100 }),
    ] })], []);
    expect(entries[0]!.outside.map((session) => session.session_id)).toEqual(["early", "late"]);
    expect(poolEntries([pool()], [])[0]!.outside).toEqual([]);
  });

  it("counts a pool whose only work is outside-pool sessions as active", () => {
    // The sessions hold tasks on this pool's route even though none of them
    // is a pool member — hiding the pool as idle hid them from the rail.
    const entries = poolEntries([
      pool({ profile_id: "worker-outside", running_busy: 0, running_idle: 0,
        outside_pools: [outside("s-1", { task_id: "task-1" })] }),
      pool({ profile_id: "worker-idle", running_busy: 0, running_idle: 2, outside_pools: [] }),
    ], []);
    expect(splitBusyPoolEntries(entries)).toEqual({
      busy: [expect.objectContaining({ profileId: "worker-outside" })],
      hiddenCount: 1,
    });
  });

  it("matches an outside session to the roster agent running it", () => {
    const byTask = { ...agent("by-task", "Task holder", "worker-standard"), session_id: null, current_task_id: "task-1" };
    const bySession = { ...agent("by-session", "Session owner", "worker-standard"), session_id: "s-1", current_task_id: "other" };
    // The live session id is exact; the task id is the fallback the pool
    // status row and the roster always share.
    expect(outsideSessionAgent(outside("s-1", { task_id: "task-1" }), [byTask, bySession])?.id).toBe("by-session");
    expect(outsideSessionAgent(outside("s-2", { task_id: "task-1" }), [byTask, bySession])?.id).toBe("by-task");
    expect(outsideSessionAgent(outside("s-3", { task_id: "task-9" }), [byTask, bySession])).toBeNull();
    // A session with no task never matches an idle agent through a null id.
    expect(outsideSessionAgent(outside("s-4"), [{ ...byTask, current_task_id: null }])).toBeNull();
  });

  it("names an outside session by its agent only when the agent belongs to the session's profile", () => {
    const own = agent("own", "opencode-a", "worker-standard");
    // A stopped pool worker reused for a dedicated task session keeps its pool name.
    const borrowed = agent("borrowed", "deep-high-codex-0901", "deep-high-codex");
    const ingest = outside("s-1", { profile_id: "spec-ingest", name: "s-solid-apex-97" });
    expect(outsideSessionName(outside("s-2"), own)).toBe("opencode-a");
    expect(outsideSessionName(ingest, borrowed)).toBe("spec-ingest");
    expect(outsideSessionName(ingest, null)).toBe("s-solid-apex-97");
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
    // Still shown for the hold, but with the numbers it has now.
    expect(result.current).toEqual({ busy: idle, hiddenCount: 0 });
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

  it("renders a shown pool's latest data even when which pools are shown has not changed", () => {
    // The hold is on *visibility*. A pool that stays busy while its outside
    // session finishes and another starts must not keep showing the finished
    // one — that is the progress an operator opens the rail to watch.
    vi.useFakeTimers();
    const before = poolEntries([pool({ running_busy: 0, ready: 5,
      outside_pools: [outside("s-1", { task_id: "task-1", task_title: "First" })] })], []);
    const after = poolEntries([pool({ running_busy: 0, ready: 6,
      outside_pools: [outside("s-2", { task_id: "task-2", task_title: "Second" })] })], []);
    const { result, rerender } = renderHook(({ entries }) => useDebouncedBusyPoolEntries(entries), {
      initialProps: { entries: before },
    });
    rerender({ entries: after });
    expect(result.current.busy[0]!.pool.ready).toBe(6);
    expect(result.current.busy[0]!.outside.map((session) => session.task_title)).toEqual(["Second"]);
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
    const bare = poolSelectionKey("worker-standard");
    expect(bare).toBe("pool:worker-standard");
    expect(parseAgentSelection(bare)).toEqual({
      key: bare, kind: "pool", profileId: "worker-standard", instanceId: null,
    });
    const pinned = poolSelectionKey("worker-standard", "p-x--y--1");
    expect(parseAgentSelection(pinned)).toMatchObject({ kind: "pool", instanceId: "p-x--y--1" });
    expect(selectionAddress(pinned)).toBe(bare);
  });

  it("resolves a pre-global-pools key to no profile rather than a bogus one", () => {
    // ``pool:<project>:<profile>`` is a shareable, bookmarkable key that
    // outlived its format. A pool profile id never contains ":", so the second
    // colon identifies it; it must not resolve to a pool profile called
    // "agent-queue:worker-standard", which can never exist.
    expect(parseAgentSelection("pool:agent-queue:worker-standard")).toEqual({
      key: "pool:agent-queue:worker-standard", kind: "pool", profileId: "", instanceId: null,
    });
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

  describe("task-lifecycle sessions on a pool's route", () => {
    // standard-high-opencode switched to ``lifecycle: pool`` while four tasks
    // were already running on it under the per-task lifecycle: no pool member
    // is busy, and the roster hides every agent on a pool profile.
    const opencode = "standard-high-opencode";
    function outsideOnly(over: Partial<PoolStatusRow> = {}) {
      return pool({
        profile_id: opencode, running_busy: 0, running_idle: 0, desired: 0, ready: 0, projects: [],
        outside_pools: [
          outside("oc-s1", { profile_id: opencode, harness: "opencode", task_id: "task-1", task_title: "Wire the probe", started_at: 100 }),
          outside("oc-s2", { profile_id: opencode, harness: "opencode", task_id: "task-2", task_title: "Fix the parser", started_at: 200 }),
        ],
        ...over,
      });
    }
    beforeEach(() => {
      api.listAgents.mockResolvedValue({ data: { agents: [
        agent("fixed", "Builder", "implementer"),
        agent("pooled", "worker-standard-9f2a", "worker-standard"),
        { ...agent("oc-1", "opencode-a", opencode), state: "busy", session_id: "oc-s1", current_task_id: "task-1", current_task_title: "Wire the probe" },
        { ...agent("oc-2", "opencode-b", opencode), state: "busy", session_id: null, current_task_id: "task-2", current_task_title: "Fix the parser" },
      ], count: 4 } });
      api.listProfiles.mockResolvedValue({ data: { profiles: [
        { id: "implementer", name: "Implementer" },
        { id: "worker-standard", name: "Worker standard", lifecycle: "pool" },
        { id: opencode, name: "OpenCode", lifecycle: "pool" },
      ] } });
      api.sessionList.mockResolvedValue({ data: { success: true, sessions: [], count: 0 } });
    });

    it("shows the pool with one nested row per outside session", async () => {
      api.poolStatus.mockResolvedValue({ data: { success: true, pools: [outsideOnly()] } });
      renderAgents("/");

      expect(await screen.findByRole("button", { name: "Open " + opencode + " pool" }, SLOW)).toBeInTheDocument();
      const nested = await screen.findByRole("list", { name: "Sessions outside the " + opencode + " pool" }, SLOW);
      const rows = within(nested).getAllByRole("listitem");
      expect(rows).toHaveLength(2);
      // Oldest first, named by the agent running each one, with its task,
      // its state, and why it is not in the pool's supply numbers.
      expect(within(rows[0]!).getByRole("button", { name: "Open opencode-a (outside pool)" })).toBeInTheDocument();
      expect(within(rows[0]!).getByText("Wire the probe")).toBeInTheDocument();
      expect(within(rows[0]!).getByText("busy")).toBeInTheDocument();
      expect(within(rows[0]!).getByText("Outside pool")).toBeInTheDocument();
      expect(within(rows[1]!).getByRole("button", { name: "Open opencode-b (outside pool)" })).toBeInTheDocument();
      expect(within(rows[1]!).getByText("Fix the parser")).toBeInTheDocument();
      expect(screen.queryByRole("link", { name: /idle pool/ })).not.toBeInTheDocument();
    });

    it("still lists an outside session no roster agent is running, without a click target", async () => {
      api.poolStatus.mockResolvedValue({ data: { success: true, pools: [outsideOnly({ outside_pools: [
        outside("orphan", { profile_id: opencode, task_id: "task-9", task_title: "Unowned work", state: "idle" }),
      ] })] } });
      renderAgents("/");

      const nested = await screen.findByRole("list", { name: "Sessions outside the " + opencode + " pool" }, SLOW);
      const row = within(nested).getByRole("listitem");
      expect(within(row).getByText("t-worker-standard--orphan")).toBeInTheDocument();
      expect(within(row).getByText("Unowned work")).toBeInTheDocument();
      expect(within(row).getByText("idle")).toBeInTheDocument();
      expect(within(row).getByText("Outside pool")).toBeInTheDocument();
      expect(within(row).queryByRole("button")).not.toBeInTheDocument();
    });

    it("keeps a pool with only idle members and no outside sessions hidden", async () => {
      api.poolStatus.mockResolvedValue({ data: { success: true, pools: [
        outsideOnly(),
        pool({ profile_id: "worker-standard", running_busy: 0, running_idle: 2, outside_pools: [] }),
      ] } });
      renderAgents("/");

      expect(await screen.findByRole("button", { name: "Open " + opencode + " pool" }, SLOW)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Open worker-standard pool" })).not.toBeInTheDocument();
      expect(screen.getByRole("link", { name: "1 idle pool" })).toBeInTheDocument();
    });

    it("lists each agent once and still hides ordinary pool members", async () => {
      api.poolStatus.mockResolvedValue({ data: { success: true, pools: [outsideOnly()] } });
      renderAgents("/");

      await screen.findByRole("list", { name: "Sessions outside the " + opencode + " pool" }, SLOW);
      // Outside sessions live under their pool, not also beside it.
      expect(screen.getAllByRole("button", { name: /opencode-a/ })).toHaveLength(1);
      expect(screen.queryByRole("button", { name: "Open opencode-a" })).not.toBeInTheDocument();
      // A real pool member stays reachable through its pool only, as before.
      expect(screen.queryByRole("button", { name: /worker-standard-9f2a/ })).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Open Builder" })).toBeInTheDocument();
    });
  });

  describe("a Codex pool with a dedicated task session on its route", () => {
    // Live on 2026-10-08: the deep-high-codex pool had a busy member, and a
    // spec-ingest task session (codex, deep-high) ran on the same route. The
    // push scheduler had put that session on a stopped pool worker's agent
    // row, which keeps its pool identity: name deep-high-codex-0901, profile
    // deep-high-codex.
    const codex = "deep-high-codex";
    beforeEach(() => {
      api.listAgents.mockResolvedValue({ data: { agents: [
        agent("fixed", "Builder", "implementer"),
        { ...agent("member", "deep-high-codex-45fb", codex), harness: "codex", intelligence_class: "deep-high",
          state: "busy", session_id: "pool-s1", current_task_id: "task-pool", current_task_title: "Pool work" },
        { ...agent("borrowed", "deep-high-codex-0901", codex), harness: "codex", intelligence_class: "deep-high",
          state: "busy", session_id: "ingest-s1", current_task_id: "solid-apex-97", current_task_title: "Ingest spec" },
      ], count: 3 } });
      api.listProfiles.mockResolvedValue({ data: { profiles: [
        { id: "implementer", name: "Implementer" },
        { id: codex, name: "Deep high Codex", lifecycle: "pool" },
        { id: "spec-ingest", name: "Spec ingest" },
      ] } });
      api.sessionList.mockResolvedValue({ data: { success: true, sessions: [
        instance("pool-s1", { id: "pool-s1", name: "p-deep-high-codex--agent-queue--pool-s1", profile_id: codex,
          harness: "codex", model: "gpt-5.5", intelligence_class: "deep-high", task_id: "task-pool" }),
      ], count: 1 } });
      api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool({
        profile_id: codex, running_busy: 1, running_idle: 0, desired: 1, ready: 0,
        outside_pools: [outside("ingest-s1", { profile_id: "spec-ingest", harness: "codex",
          intelligence_class: "deep-high", name: "s-solid-apex-97", task_id: "solid-apex-97",
          task_title: "Ingest spec" })],
      })] } });
    });

    it("keeps the pool member inside its pool and names the outside row by its own profile", async () => {
      renderAgents("/");

      const poolRow = await screen.findByRole("button", { name: "Open " + codex + " pool" }, SLOW);
      expect(within(poolRow).getByText("1 live instance")).toBeInTheDocument();
      const nested = await screen.findByRole("list", { name: "Sessions outside the " + codex + " pool" }, SLOW);
      const rows = within(nested).getAllByRole("listitem");
      // Only the dedicated session is outside the pool, named for what it is.
      expect(rows).toHaveLength(1);
      expect(within(rows[0]!).getByRole("button", { name: "Open spec-ingest (outside pool)" })).toBeInTheDocument();
      expect(within(rows[0]!).getByText("spec-ingest")).toHaveAttribute("title", "s-solid-apex-97");
      expect(within(rows[0]!).getByText("Outside pool")).toBeInTheDocument();
      // No pool-worker identity is shown as outside the pool, and neither
      // agent row is listed beside the pool.
      expect(screen.queryByText("deep-high-codex-0901")).not.toBeInTheDocument();
      expect(screen.queryByText("deep-high-codex-45fb")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /deep-high-codex-/ })).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Open Builder" })).toBeInTheDocument();

      // The row still opens the agent window running the session.
      fireEvent.click(within(rows[0]!).getByRole("button", { name: "Open spec-ingest (outside pool)" }));
      await waitFor(() => expect(screen.getByLabelText("Current URL")).toHaveTextContent("agent=borrowed"), SLOW);
    });
  });

  it("lists every pool, idle ones included, on the agents page the idle link points at", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [
      pool({ profile_id: "worker-busy", running_busy: 1 }),
      pool({ profile_id: "worker-idle", running_busy: 0, running_idle: 2 }),
    ] } });
    renderAgents("/agents");

    const directory = await screen.findByRole("region", { name: "Worker pools" }, SLOW);
    expect(await within(directory).findByRole("link", { name: "Open pool worker-idle" }, SLOW)).toBeInTheDocument();
    expect(within(directory).getByRole("link", { name: "Open pool worker-busy" })).toBeInTheDocument();
    expect(within(directory).getByText("idle")).toBeInTheDocument();
    expect(within(directory).getByText("busy")).toBeInTheDocument();
  });

  it("opens settings from a directory row and links back to the list", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool({ profile_id: "worker-idle", running_busy: 0 })] } });
    renderAgents("/agents");

    const row = await screen.findByRole("link", { name: "Open pool worker-idle" }, SLOW);
    expect(row).toHaveAttribute("href", "/agents?agent=pool%3Aworker-idle&pool-view=settings");
    fireEvent.click(row.closest("li")!);
    expect(await screen.findByRole("region", { name: /worker-idle/ }, SLOW)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Settings" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByLabelText("Display name")).toHaveValue("worker-idle");
    expect(screen.getByLabelText("Current URL")).toHaveTextContent("pool-view=settings");
    expect(screen.queryByRole("region", { name: "Worker pools" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "Back to pools" }));
    expect(await screen.findByRole("region", { name: "Worker pools" }, SLOW)).toBeInTheDocument();
    expect(screen.getByLabelText("Current URL")).toHaveTextContent(/^\/agents$/);
  });

  it("offers an explicit Settings link and restores settings through browser history", async () => {
    renderAgents("/agents");
    const directory = await screen.findByRole("region", { name: "Worker pools" }, SLOW);
    fireEvent.click(within(directory).getByRole("link", { name: "Settings for worker-standard" }));
    expect(await screen.findByLabelText("Display name", undefined, SLOW)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Terminal" }));
    expect(screen.getByRole("tab", { name: "Terminal" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("button", { name: "Previous page" }));
    expect(await screen.findByRole("tab", { name: "Settings" }, SLOW)).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("button", { name: "Previous page" }));
    expect(await screen.findByRole("region", { name: "Worker pools" }, SLOW)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByLabelText("Display name", undefined, SLOW)).toBeInTheDocument();
  });

  it("loads a shared settings URL and displays the renamed pool across views", async () => {
    const renamed = pool({ name: "Space Bunny" });
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [renamed] } });
    renderAgents("/agents?agent=pool%3Aworker-standard&pool-view=settings");
    expect(await screen.findByRole("region", { name: "Space Bunny pool agent window" }, SLOW)).toBeInTheDocument();
    expect(screen.getByLabelText("Display name")).toHaveValue("Space Bunny");
    expect(screen.getByRole("button", { name: "Open Space Bunny pool" })).toBeInTheDocument();
    expect(screen.getByText("worker-standard")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "Back to pools" }));
    const directory = await screen.findByRole("region", { name: "Worker pools" }, SLOW);
    expect(within(directory).getByRole("link", { name: "Open pool Space Bunny" })).toBeInTheDocument();
    expect(within(directory).getByRole("link", { name: "Settings for Space Bunny" })).toHaveAttribute(
      "href", "/agents?agent=pool%3Aworker-standard&pool-view=settings",
    );
  });

  it("saves a new display name using the stable ID and refreshes the rail, window, list and pickers", async () => {
    api.poolRename.mockImplementation(async () => {
      api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool({ name: "Space Bunny" })] } });
      api.listProfiles.mockResolvedValue({ data: { profiles: [{ id: "worker-standard", name: "Space Bunny", lifecycle: "pool" }] } });
      return { data: { success: true, profile_id: "worker-standard", name: "Space Bunny", changed: true } };
    });
    renderAgents("/agents?agent=pool%3Aworker-standard&pool-view=settings");
    const name = await screen.findByLabelText("Display name", undefined, SLOW);
    fireEvent.change(name, { target: { value: " Space Bunny " } });
    fireEvent.click(screen.getByRole("button", { name: "Save pool name" }));
    await waitFor(() => expect(api.poolRename).toHaveBeenCalledTimes(1), SLOW);
    expect(api.poolRename.mock.calls[0]![0].body).toEqual({ profile_id: "worker-standard", name: "Space Bunny" });
    expect(await screen.findByText("Pool name saved.", undefined, SLOW)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Space Bunny pool agent window" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open Space Bunny pool" })).toBeInTheDocument();
    expect(name).toHaveValue("Space Bunny");
    await waitFor(() => expect(api.listProfiles.mock.calls.length).toBeGreaterThan(1), SLOW);
    fireEvent.click(screen.getByRole("link", { name: "Back to pools" }));
    const directory = await screen.findByRole("region", { name: "Worker pools" }, SLOW);
    expect(within(directory).getByRole("link", { name: "Settings for Space Bunny" })).toBeInTheDocument();
    fireEvent.click(within(screen.getByRole("region", { name: "Agent flock" }))
      .getByRole("button", { name: "Create agent or pool" }));
    fireEvent.click(within(screen.getByRole("region", { name: "Create agent or pool" }))
      .getByRole("button", { name: "Create agent pool" }));
    expect(await screen.findByRole("option", { name: "Space Bunny" }, SLOW)).toHaveValue("worker-standard");
  });

  it("rejects invalid names in the form and displays server refusals without claiming success", async () => {
    api.poolRename.mockResolvedValue({ data: { success: false, error: "Vault is read-only" } });
    renderAgents("/agents?agent=pool%3Aworker-standard&pool-view=settings");
    const name = await screen.findByLabelText("Display name", undefined, SLOW);
    for (const value of [" ", "x".repeat(121)]) {
      fireEvent.change(name, { target: { value } });
      expect(screen.getByRole("button", { name: "Save pool name" })).toBeDisabled();
    }
    expect(api.poolRename).not.toHaveBeenCalled();
    fireEvent.change(name, { target: { value: "Space Bunny" } });
    fireEvent.click(screen.getByRole("button", { name: "Save pool name" }));
    expect(await screen.findByText("Vault is read-only", undefined, SLOW)).toBeInTheDocument();
    expect(screen.queryByText("Pool name saved.")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "worker-standard pool agent window" })).toBeInTheDocument();
  });

  it("switches the directory to the Providers view and keeps the choice in the URL", async () => {
    renderAgents("/agents");

    const tabs = await screen.findByRole("tablist", { name: "Agents directory" }, SLOW);
    expect(within(tabs).getByRole("tab", { name: "Pools" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByRole("region", { name: "Worker pools" }, SLOW)).toBeInTheDocument();
    expect(api.getProviderAllocationApiProvidersAllocationGet).not.toHaveBeenCalled();

    fireEvent.click(within(tabs).getByRole("tab", { name: "Providers" }));
    expect(await screen.findByRole("region", { name: "Providers" }, SLOW)).toBeInTheDocument();
    expect(within(tabs).getByRole("tab", { name: "Providers" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("region", { name: "Worker pools" })).not.toBeInTheDocument();
    expect(await screen.findByText(/No ordinary worker profiles resolve to a known provider/, undefined, SLOW)).toBeInTheDocument();
  });

  it("opens straight onto the Providers view from a shared link", async () => {
    renderAgents("/agents?view=providers");
    expect(await screen.findByRole("region", { name: "Providers" }, SLOW)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Worker pools" })).not.toBeInTheDocument();
  });

  it("names the project a launch failure quarantined, not the whole pool", async () => {
    // A quarantine belongs to one project's workspace; the pool may be
    // perfectly healthy everywhere else.
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool({ projects: [
      project({ project_id: "agent-queue", quarantined_until: Date.now() / 1000 + 30, quarantined_reason: "claude: command not found" }),
      project({ project_id: "other-repo" }),
    ] })] } });
    renderAgents("/");
    const row = await screen.findByRole("button", { name: "Open worker-standard pool" }, SLOW);
    expect(await within(row).findByText(/Quarantined in agent-queue for/, undefined, SLOW)).toBeInTheDocument();
    expect(within(row).queryByText(/Quarantined in other-repo/)).not.toBeInTheDocument();
  });

  it("shows which projects a pool's workers are in", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool({ projects: [
      project({ project_id: "agent-queue", running_idle: 1, running_busy: 2 }),
      project({ project_id: "other-repo", running_idle: 1, running_busy: 0 }),
    ] })] } });
    renderAgents("/");
    const row = await screen.findByRole("button", { name: "Open worker-standard pool" }, SLOW);
    expect(await within(row).findByText("agent-queue 3 · other-repo 1", undefined, SLOW)).toBeInTheDocument();
  });
});

describe("pool instance selection", () => {
  it("switches live terminals from the single-row header without opening details", async () => {
    renderAgents("/");
    fireEvent.click(await screen.findByRole("button", { name: "Open worker-standard pool" }, SLOW));
    const window = await screen.findByRole("region", { name: "worker-standard pool agent window" }, SLOW);
    const header = window.querySelector("header")!;
    const picker = within(header).getByRole("combobox", { name: "Terminal for worker-standard pool" });
    expect(within(window).queryByRole("dialog")).not.toBeInTheDocument();
    // Oldest instance first, so the first live session is bound by default.
    await waitFor(() => expect(TerminalSocketMock.instances).toHaveLength(1), SLOW);
    expect(TerminalSocketMock.instances[0]!.url).toContain("p-worker-standard--agent-queue--aaa");
    act(() => TerminalSocketMock.instances[0]!.open());

    expect(picker).toHaveValue("p-worker-standard--agent-queue--aaa");
    expect(within(picker).getByRole("option", { name: /p-worker-standard--agent-queue--bbb · agent-queue · quick-torrent-39 · 42s idle/ })).toBeInTheDocument();
    const user = userEvent.setup();
    picker.focus();
    expect(picker).toHaveFocus();
    await user.selectOptions(picker, "p-worker-standard--agent-queue--bbb");

    await waitFor(() => expect(TerminalSocketMock.instances).toHaveLength(2), SLOW);
    expect(TerminalSocketMock.instances[1]!.url).toContain("p-worker-standard--agent-queue--bbb");
    expect(TerminalSocketMock.instances[0]!.closed).toBe(true);
    expect(picker).toHaveValue("p-worker-standard--agent-queue--bbb");
    expect(within(window).queryByRole("dialog")).not.toBeInTheDocument();
    act(() => TerminalSocketMock.instances[1]!.open());

    // The existing details picker stays in sync and uses the same action.
    fireEvent.click(within(header).getByRole("button", { name: /^Details for / }));
    const detailsPicker = within(window).getByLabelText("Instance");
    expect(detailsPicker).toHaveValue("p-worker-standard--agent-queue--bbb");
    fireEvent.change(detailsPicker, { target: { value: "p-worker-standard--agent-queue--aaa" } });
    await waitFor(() => expect(TerminalSocketMock.instances).toHaveLength(3), SLOW);
    expect(picker).toHaveValue("p-worker-standard--agent-queue--aaa");
    expect(TerminalSocketMock.instances[1]!.closed).toBe(true);
  });

  it("preserves selection when instances arrive and falls back when the selected instance leaves", async () => {
    renderAgents("/agents?agent=" + encodeURIComponent(poolSelectionKey("worker-standard", instance("bbb").id)));
    const window = await screen.findByRole("region", { name: "worker-standard pool agent window" }, SLOW);
    const header = window.querySelector("header")!;
    const picker = within(header).getByRole("combobox", { name: "Terminal for worker-standard pool" });
    await waitFor(() => expect(TerminalSocketMock.instances).toHaveLength(1), SLOW);
    act(() => TerminalSocketMock.instances[0]!.open());
    expect(picker).toHaveValue(instance("bbb").id);
    await act(async () => { clients[clients.length - 1]!.setQueryData(["sessions", "pool"], [instance("aaa"), instance("bbb"), instance("ccc")]); });
    await waitFor(() => expect(within(picker).getAllByRole("option")).toHaveLength(3), SLOW);
    expect(picker).toHaveValue(instance("bbb").id);
    expect(TerminalSocketMock.instances).toHaveLength(1);

    await act(async () => { clients[clients.length - 1]!.setQueryData(["sessions", "pool"], [instance("aaa"), instance("ccc")]); });
    await waitFor(() => expect(TerminalSocketMock.instances).toHaveLength(2), SLOW);
    expect(picker).toHaveValue(instance("aaa").id);
    expect(TerminalSocketMock.instances[1]!.url).toContain(instance("aaa").id);
    expect(TerminalSocketMock.instances[0]!.closed).toBe(true);
    act(() => TerminalSocketMock.instances[1]!.open());
    expect(within(picker).queryByRole("option", { name: /--bbb/ })).not.toBeInTheDocument();

    await act(async () => { clients[clients.length - 1]!.setQueryData(["sessions", "pool"], [instance("aaa")]); });
    await waitFor(() => expect(within(header).queryByRole("combobox")).not.toBeInTheDocument(), SLOW);
    expect(TerminalSocketMock.instances).toHaveLength(2);
    await act(async () => { clients[clients.length - 1]!.setQueryData(["sessions", "pool"], []); });
    expect(await screen.findByText("No live pool instance", undefined, SLOW)).toBeInTheDocument();
    expect(TerminalSocketMock.instances[1]!.closed).toBe(true);
    await act(async () => { clients[clients.length - 1]!.setQueryData(["sessions", "pool"], [instance("ddd"), instance("eee")]); });
    await waitFor(() => expect(within(header).getByRole("combobox")).toHaveValue(instance("ddd").id), SLOW);
  });

  it("keeps the picker in details only for a single live instance", async () => {
    api.sessionList.mockResolvedValue({ data: { success: true, sessions: [instance("aaa")], count: 1 } });
    renderAgents("/agents?agent=pool%3Aworker-standard");
    const window = await screen.findByRole("region", { name: "worker-standard pool agent window" }, SLOW);
    expect(within(window.querySelector("header")!).queryByRole("combobox")).not.toBeInTheDocument();
    fireEvent.click(within(window).getByRole("button", { name: /^Details for / }));
    expect(within(window).getByLabelText("Instance")).toHaveValue(instance("aaa").id);
  });

  it("falls back to a live instance when the pinned one is gone", async () => {
    renderAgents("/agents?agent=" + encodeURIComponent(poolSelectionKey("worker-standard", "p-worker-standard--agent-queue--zzz")));
    await waitFor(() => expect(TerminalSocketMock.instances).toHaveLength(1), SLOW);
    expect(TerminalSocketMock.instances[0]!.url).toContain("p-worker-standard--agent-queue--aaa");
    expect(screen.getByRole("combobox", { name: "Terminal for worker-standard pool" })).toHaveValue(instance("aaa").id);
  });

  it("drops a pre-global-pools key from the URL instead of opening a dead view", async () => {
    // Selection is shareable and bookmarkable, so an old-format key outlives
    // the format that wrote it. It must degrade to "nothing selected" — the
    // directory — rather than a tile the operator has to close by hand.
    renderAgents("/agents?agent=pool%3Aagent-queue%3Aworker-standard");
    expect(await screen.findByRole("region", { name: "Worker pools" }, SLOW)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /agent window/ })).not.toBeInTheDocument();
  });

  it("explains an empty pool instead of opening a terminal", async () => {
    api.sessionList.mockResolvedValue({ data: { success: true, sessions: [], count: 0 } });
    renderAgents("/");
    fireEvent.click(await screen.findByRole("button", { name: "Open worker-standard pool" }, SLOW));
    fireEvent.click(await screen.findByRole("button", { name: "Details for worker-standard pool" }, SLOW));
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
    // Settings is reachable from the header; no disclosure round trip first.
    const header = window.querySelector("header")!;
    fireEvent.click(within(header).getByRole("tab", { name: "Settings" }));
    expect(within(window).queryByRole("dialog")).not.toBeInTheDocument();
    expect(within(header).getByRole("tab", { name: "Settings" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(within(header).getByRole("button", { name: /^Details for / }));
    // The pool disclosure keeps its own picker and carries no second copy of the switch.
    const dialog = within(window).getByRole("dialog");
    expect(within(dialog).queryByRole("tab")).not.toBeInTheDocument();
    expect(within(dialog).getByLabelText("Instance")).toBeVisible();
    fireEvent.click(within(dialog).getByRole("button", { name: /^Dismiss / }));

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

  it("breaks the pool's supply down by project and shows a quarantine reason in full", async () => {
    // The reason is captured harness startup output: truncating it to a line
    // is what made the field useless, so the detail view renders all of it.
    const reason = "Traceback (most recent call last):\n  File /opt/harness/claude, line 1\n  OSError: [Errno 2] No such file or directory: claude";
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool({ projects: [
      project({ project_id: "agent-queue", running_idle: 1, running_busy: 2 }),
      project({ project_id: "other-repo", running_idle: 0, running_busy: 0, starting: 1,
        quarantined_until: Date.now() / 1000 + 45, quarantined_reason: reason }),
    ] })] } });
    renderAgents("/");
    fireEvent.click(await screen.findByRole("button", { name: "Open worker-standard pool" }, SLOW));
    const window = await screen.findByRole("region", { name: "worker-standard pool agent window" }, SLOW);
    fireEvent.click(within(window).getByRole("button", { name: /^Details for / }));
    fireEvent.click(within(window).getByRole("tab", { name: "Settings" }));

    const table = within(await within(window).findByRole("region", { name: "Workers by project" }, SLOW)).getByRole("table");
    const busiest = within(table).getByRole("row", { name: /^agent-queue/ });
    expect(within(busiest).getAllByRole("cell").map((cell) => cell.textContent))
      .toEqual(["5", "1", "2", "0", "0", "3/4", "2"]);
    expect(within(table).getByRole("rowheader", { name: "other-repo" })).toBeInTheDocument();
    expect(within(window).getByText(/quarantined for \d+s/)).toBeInTheDocument();
    // Not a first line and an ellipsis: the whole captured output is present.
    expect(within(window).getByText(/No such file or directory/).textContent).toBe(reason);
  });

  it("blocks a save that pool_scale would reject", async () => {
    renderAgents("/");
    fireEvent.click(await screen.findByRole("button", { name: "Open worker-standard pool" }, SLOW));
    const window = await screen.findByRole("region", { name: "worker-standard pool agent window" }, SLOW);
    fireEvent.click(within(window).getByRole("button", { name: /^Details for / }));
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
    fireEvent.click(within(window).getByRole("button", { name: /^Details for / }));
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
    fireEvent.click(within(window).getByRole("button", { name: /^Details for / }));
    fireEvent.click(within(window).getByRole("tab", { name: "Settings" }));
    fireEvent.change(await within(window).findByLabelText("Minimum active workers", undefined, SLOW), { target: { value: "2" } });
    fireEvent.click(within(window).getByRole("button", { name: "Save pool bounds" }));
    expect(await within(window).findByText(/no pool profile/)).toBeInTheDocument();
  });

  it("offers one set of fleet-wide bounds and the project breakdown on a pool worker's settings tab", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool({ projects: [
      project({ project_id: "agent-queue" }),
      project({ project_id: "other-repo", running_idle: 0, running_busy: 1 }),
    ] })] } });
    // A pool worker is only reachable by URL once the flock hides its row.
    renderAgents("/agents?agent=pooled");
    const window = await screen.findByRole("region", { name: "worker-standard-9f2a agent window" }, SLOW);
    fireEvent.click(within(window).getByRole("button", { name: /^Details for / }));
    fireEvent.click(within(window).getByRole("tab", { name: "Settings" }));
    const section = await within(window).findByRole("region", { name: "Worker pool settings" }, SLOW);
    // Bounds are fleet-wide, so there is exactly one pair of fields now.
    expect(within(section).getAllByLabelText("Maximum active workers")).toHaveLength(1);
    const table = within(section).getByRole("table");
    expect(within(table).getByRole("rowheader", { name: "agent-queue" })).toBeInTheDocument();
    expect(within(table).getByRole("rowheader", { name: "other-repo" })).toBeInTheDocument();
  });
});

describe("enabling and disabling a pool from the directory", () => {
  // The rail hides pools without a task-holding worker, so an idle pool — the
  // one an operator wants to switch off when its models run low — is only
  // reachable here.  Both fixtures below are idle for exactly that reason.
  const idle = { running_busy: 0, running_idle: 1 };

  it("shows task-lifecycle sessions that run outside the pool", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool({
      ...idle,
      outside_pools: [{
        session_id: "outside-1", project_id: "agent-queue", profile_id: "legacy-standard",
        harness: "claude", intelligence_class: "standard-medium", name: "legacy worker",
        state: "running", task_id: "outside-task", task_title: "Outside task", started_at: 100,
      }],
    })] } });
    renderAgents("/agents");

    const directory = within(await screen.findByRole("region", { name: "Worker pools" }, SLOW));
    expect(await directory.findByText(/outside pools: 1 task-lifecycle session/i)).toBeInTheDocument();
  });

  it("disables an idle pool and keeps its row listed for re-enabling", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool(idle)] } });
    // The daemon persists the change before answering, so every later status
    // read reports it.
    const setEnabled = api.poolSetEnabled.getMockImplementation();
    api.poolSetEnabled.mockImplementation(async (...args: unknown[]) => {
      api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool({ ...idle, enabled: false })] } });
      return setEnabled ? setEnabled(...(args as [never])) : { data: { success: true, profile_id: "worker-standard", enabled: false, warnings: [] } };
    });
    renderAgents("/agents");
    const directory = within(await screen.findByRole("region", { name: "Worker pools" }, SLOW));

    fireEvent.click(await directory.findByRole("switch", { name: "Disable worker-standard pool" }, SLOW));

    await waitFor(() => expect(api.poolSetEnabled).toHaveBeenCalledTimes(1), SLOW);
    expect(api.poolSetEnabled.mock.calls[0]![0].body).toEqual({
      profile_id: "worker-standard", enabled: false,
    });
    // The mutation's own refresh (not a later poll) reports the persisted
    // state; the row stays in the list.
    expect(await directory.findByRole("switch", { name: "Enable worker-standard pool" }, SLOW)).toBeInTheDocument();
    expect(directory.getByRole("link", { name: "Open pool worker-standard" })).toBeInTheDocument();
    expect(screen.getByLabelText("Current URL")).toHaveTextContent(/^\/agents$/);
    expect(directory.getByText(/no new work is claimed/i)).toBeInTheDocument();
    expect(directory.getByText("disabled")).toBeInTheDocument();
  });

  it("enables a disabled pool again", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool({ ...idle, enabled: false })] } });
    api.poolSetEnabled.mockResolvedValue({ data: { success: true, profile_id: "worker-standard", enabled: true, warnings: [] } });
    renderAgents("/agents");
    const directory = within(await screen.findByRole("region", { name: "Worker pools" }, SLOW));

    fireEvent.click(await directory.findByRole("switch", { name: "Enable worker-standard pool" }, SLOW));

    await waitFor(() => expect(api.poolSetEnabled).toHaveBeenCalledTimes(1), SLOW);
    expect(api.poolSetEnabled.mock.calls[0]![0].body).toEqual({
      profile_id: "worker-standard", enabled: true,
    });
  });

  it("keeps the confirmed state while the status refresh is slow", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool(idle)] } });
    renderAgents("/agents");
    const directory = within(await screen.findByRole("region", { name: "Worker pools" }, SLOW));
    const toggle = await directory.findByRole("switch", { name: "Disable worker-standard pool" }, SLOW);
    // The write succeeds immediately, but rebuilding the fleet status has
    // not finished. The response itself must update the visible switch.
    api.poolStatus.mockImplementation(() => new Promise(() => {}));
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(toggle).toBeEnabled();
      expect(toggle).toHaveAttribute("aria-checked", "false");
    }, SLOW);
    expect(directory.getByText("disabled")).toBeInTheDocument();
  });

  it("discards a status read that started before the pool was changed", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool(idle)] } });
    renderAgents("/agents");
    const directory = within(await screen.findByRole("region", { name: "Worker pools" }, SLOW));
    const toggle = await directory.findByRole("switch", { name: "Disable worker-standard pool" }, SLOW);
    let finishRead!: (value: unknown) => void;
    api.poolStatus.mockImplementationOnce(() => new Promise((resolve) => { finishRead = resolve; }));
    const queryClient = clients[clients.length - 1]!;
    let read!: Promise<void>;
    act(() => { read = queryClient.refetchQueries({ queryKey: ["pools"] }); });
    await waitFor(() => expect(finishRead).toBeDefined(), SLOW);
    api.poolStatus.mockImplementation(() => new Promise(() => {}));
    fireEvent.click(toggle);
    await waitFor(() => {
      expect(toggle).toBeEnabled();
      expect(toggle).toHaveAttribute("aria-checked", "false");
    }, SLOW);
    await act(async () => {
      finishRead({ data: { success: true, pools: [pool(idle)] } });
      await read;
    });

    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(queryClient.getQueryData<PoolStatusRow[]>(["pools", "all"])?.[0]?.enabled).toBe(false);
  });

  it("shows a transport error beside the switch", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool(idle)] } });
    api.poolSetEnabled.mockRejectedValue(new Error("API 504: daemon_timeout"));
    renderAgents("/agents");
    const directory = within(await screen.findByRole("region", { name: "Worker pools" }, SLOW));
    fireEvent.click(await directory.findByRole("switch", { name: "Disable worker-standard pool" }, SLOW));

    expect(await directory.findByRole("alert", undefined, SLOW)).toHaveTextContent("daemon_timeout");
    expect(directory.getByRole("switch")).toHaveAttribute("aria-checked", "true");
  });

  it("ends a stalled save with a visible timeout and reconciles its state", async () => {
    const controller = new AbortController();
    const timeout = vi.spyOn(AbortSignal, "timeout").mockReturnValue(controller.signal);
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool(idle)] } });
    api.poolSetEnabled.mockImplementation(({ signal }: { signal: AbortSignal }) =>
      new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(signal.reason))),
    );
    try {
      renderAgents("/agents");
      const directory = within(await screen.findByRole("region", { name: "Worker pools" }, SLOW));
      fireEvent.click(await directory.findByRole("switch", { name: "Disable worker-standard pool" }, SLOW));
      await waitFor(() => expect(api.poolSetEnabled).toHaveBeenCalledTimes(1), SLOW);
      expect(directory.getByRole("switch")).toBeDisabled();
      // Persistence can succeed before the relay fails to deliver its reply.
      // Re-read it, but keep the error visible so an ambiguous write is clear.
      api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool({ ...idle, enabled: false })] } });
      act(() => controller.abort(new DOMException("Timed out", "TimeoutError")));

      expect(await directory.findByRole("alert", undefined, SLOW)).toHaveTextContent("Saving this pool timed out");
      await waitFor(() => {
        expect(directory.getByRole("switch")).toBeEnabled();
        expect(directory.getByRole("switch")).toHaveAttribute("aria-checked", "false");
      }, SLOW);
    } finally {
      timeout.mockRestore();
    }
  });

  it("restores the shown state and reports an in-band refusal", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool(idle)] } });
    api.poolSetEnabled.mockResolvedValue({ data: { success: false, error: "no pool profile 'worker-standard'" } });
    renderAgents("/agents");
    const directory = within(await screen.findByRole("region", { name: "Worker pools" }, SLOW));

    fireEvent.click(await directory.findByRole("switch", { name: "Disable worker-standard pool" }, SLOW));

    expect(await directory.findByRole("alert", undefined, SLOW)).toHaveTextContent(/no pool profile/);
    // Nothing was persisted, so the switch goes back to what pool_status says.
    expect(directory.getByRole("switch", { name: "Disable worker-standard pool" }))
      .toHaveAttribute("aria-checked", "true");
  });

  it("does not open the pool view when the switch is clicked", async () => {
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [pool(idle)] } });
    renderAgents("/agents");
    const directory = within(await screen.findByRole("region", { name: "Worker pools" }, SLOW));

    fireEvent.click(await directory.findByRole("switch", { name: "Disable worker-standard pool" }, SLOW));

    await waitFor(() => expect(api.poolSetEnabled).toHaveBeenCalled(), SLOW);
    expect(screen.queryByRole("region", { name: "worker-standard pool agent window" })).not.toBeInTheDocument();
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
    expect(fork.getByText(/Elastic capacity · fleet-wide/)).toBeInTheDocument();
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

  it("offers only pool-eligible profiles, and no project at all, on the pool form", async () => {
    // A pool is identified by its profile alone; which project a worker lands
    // in is the placer's call at launch, so there is nothing to choose here.
    api.poolStatus.mockResolvedValue({ data: { success: true, pools: [] } });
    const form = await openCreate("Create agent pool");
    expect(await form.findByRole("option", { name: "Worker standard" }, SLOW)).toBeInTheDocument();
    expect(form.queryByRole("option", { name: "Implementer" })).not.toBeInTheDocument();
    expect(form.queryByLabelText("Project")).not.toBeInTheDocument();
    expect(form.queryByRole("option", { name: "Agent Queue" })).not.toBeInTheDocument();
  });

  it("configures the pool through pool_scale and opens its view", async () => {
    const form = await openCreate("Create agent pool");
    await form.findByRole("option", { name: "Worker standard" }, SLOW);
    fireEvent.change(form.getByLabelText("Pool profile"), { target: { value: "worker-standard" } });
    // An existing pool is a reconfiguration, not a second pool — say so.
    expect(await form.findByText(/already runs a pool/)).toBeInTheDocument();
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
