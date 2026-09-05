import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import LeftRail from "../../../shell/LeftRail";
import AgentWorkspace from "../AgentWorkspace";
import type { FlockAgent } from "../../../api/agents";
import { TerminalMock, FitAddonMock, TerminalSocketMock } from "../../../testUtils/terminal";

vi.mock("@xterm/xterm", async () => ({ Terminal: (await import("../../../testUtils/terminal")).TerminalMock }));
vi.mock("@xterm/addon-fit", async () => ({ FitAddon: (await import("../../../testUtils/terminal")).FitAddonMock }));

const api = vi.hoisted(() => ({
  listAgents: vi.fn(), listProjects: vi.fn(), listProfiles: vi.fn(),
  getAgent: vi.fn(), editAgent: vi.fn(), createAgent: vi.fn(), deleteAgent: vi.fn(), listIntelligenceClasses: vi.fn(),
  sessionInput: vi.fn(), startAgentTerminal: vi.fn(),
  poolStatus: vi.fn(), poolScale: vi.fn(), sessionList: vi.fn(),
}));
vi.mock("../../../api/client", () => api);
function agent(id: string, name: string): FlockAgent {
  return {
    id, name, profile_id: "implementer", role: "worker", enabled: true,
    state: "idle", provider: "anthropic", harness: "claude", model: "claude-sonnet-4-6",
    intelligence_class: "standard-high", current_task_id: null as string | null,
    current_task_title: null as string | null, current_project_id: null,
    session_id: "session-" + id, session_state: "running",
    session_provider: "tmux", project_id: null, workspace_id: null,
    active_subagent_count: 0 as number | null, subagent_count_complete: true,
    aq_subagent_count: 0, native_subagent_count: 0 as number | null,
    subagents_spawned_total: 0,
    settings: { name, profile_id: "implementer", harness: null, model: null,
      intelligence_class: null, enabled: true },
  };
}

let roster = [agent("a", "Supervisor"), agent("b", "Builder"), agent("c", "Reviewer"),
  agent("d", "Tester"), agent("e", "Writer")];
// The daemon's own rollup over that roster — the rail renders it rather than
// re-summing rows in the browser.
let rollup: {
  active_total: number; native_total: number; aq_total: number;
  spawned_total: number; complete: boolean;
} | null = null;
const clients: QueryClient[] = [];

function Location() {
  const location = useLocation();
  return <output aria-label="Current location">{location.pathname}{location.search}</output>;
}

function renderFlock(initial: string | { pathname: string; search?: string; state?: unknown } = "/", workspace = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initial]}>
        <LeftRail />
        {workspace && <Routes><Route path="/agents" element={<AgentWorkspace />} /><Route path="*" element={null} /></Routes>}
        <Location />
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
  roster = [agent("a", "Supervisor"), agent("b", "Builder"), agent("c", "Reviewer"),
    agent("d", "Tester"), agent("e", "Writer")];
  roster[0] = { ...roster[0]!, role: "supervisor", state: "busy", current_task_id: "task-1",
    current_task_title: "Review deployment", active_subagent_count: 2, aq_subagent_count: 2,
    native_subagent_count: null, subagent_count_complete: false };
  roster[1] = { ...roster[1]!, active_subagent_count: null, native_subagent_count: null,
    subagent_count_complete: false };
  rollup = null;
  api.getAgent.mockImplementation(async ({ body }: { body: { agent_id: string } }) => ({ data: roster.find((a) => a.id === body.agent_id) }));
  api.editAgent.mockImplementation(async ({ body }: { body: Record<string, unknown> }) => {
    roster = roster.map((row) => row.id === body.agent_id
      ? { ...row, name: body.name as string, settings: { ...row.settings, ...body } }
      : row);
    return { data: roster.find((row) => row.id === body.agent_id) };
  });
  api.createAgent.mockImplementation(async ({ body }: { body: Record<string, unknown> }) => {
    const created = { ...agent("new-agent", body.name as string), session_id: null, session_state: null };
    roster = [...roster, created];
    return { data: created };
  });
  api.deleteAgent.mockImplementation(async ({ body }: { body: { agent_id: string } }) => {
    const deleted = roster.find((row) => row.id === body.agent_id)!;
    roster = roster.filter((row) => row.id !== body.agent_id);
    return { data: { deleted: deleted.id, name: deleted.name } };
  });
  api.sessionInput.mockResolvedValue({ data: { success: true, session_id: "session-b", accepted: true } });
  api.startAgentTerminal.mockImplementation(async ({ body }: { body: { agent_id: string } }) => {
    roster = roster.map((row) => row.id === body.agent_id
      ? { ...row, session_id: "started-" + row.id, session_state: "running", session_provider: "tmux" }
      : row);
    return { data: roster.find((row) => row.id === body.agent_id) };
  });
  api.listAgents.mockImplementation(async () => ({
    data: { agents: roster, count: roster.length, subagents: rollup },
  }));
  api.listProjects.mockResolvedValue({ data: { projects: [] } });
  api.listIntelligenceClasses.mockResolvedValue({ data: { classes: [{ id: "standard-high", name: "Standard high" }, { id: "deep-high", name: "Deep high" }] } });
  api.listProfiles.mockResolvedValue({ data: { profiles: [{ id: "implementer", name: "Implementer" }, { id: "supervisor", name: "Supervisor" }] } });
  // No pools configured: every agent in this file is a fixed push worker.
  api.poolStatus.mockResolvedValue({ data: { success: true, pools: [] } });
  api.sessionList.mockResolvedValue({ data: { success: true, sessions: [], count: 0 } });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  clients.splice(0).forEach((client) => client.clear());
});

describe("Agent flock sidebar", () => {

  it("refreshes the visible waiting badge when question events arrive", async () => {
    const { useEventStream, __dispatchEventForTests } = await import("../../../ws/useEventStream");
    function LiveUpdates() { useEventStream(); return null; }
    roster[1] = { ...roster[1]!, state: "busy", current_task_id: "task-1" };
    renderFlock();
    const row = await screen.findByRole("button", { name: "Open Builder" });
    const client = clients[clients.length - 1]!;
    render(<QueryClientProvider client={client}><LiveUpdates /></QueryClientProvider>);
    expect(within(row).queryByText("Waiting for input")).not.toBeInTheDocument();
    roster = roster.map((agent) => agent.id === "b" ? { ...agent, waiting_question: {
      id: "q-live", question: "May I deploy?", state: "human", requires_human: true, created_at: 1,
    } } : agent);
    act(() => __dispatchEventForTests({
      _event_type: "agent.question", event_type: "agent.question", id: "q-live", agent_id: "b", task_id: "task-1",
      session_id: "session-b", state: "human",
    }));
    expect(await within(row).findByText("Waiting for your reply")).toBeInTheDocument();
    roster = roster.map((agent) => agent.id === "b" ? { ...agent, waiting_question: null } : agent);
    act(() => __dispatchEventForTests({
      _event_type: "agent.question.updated", event_type: "agent.question.updated", id: "q-live", agent_id: "b", task_id: "task-1",
      session_id: "session-b", state: "delivered",
    }));
    await waitFor(() => expect(within(row).queryByText("Waiting for input")).not.toBeInTheDocument());
    expect(within(row).getByText("busy")).toBeInTheDocument();
  });


  it.each([
    ["supervisor", "Awaiting supervisor"],
    ["human", "Waiting for your reply"],
    ["answered", "Answer queued"],
  ] as const)("shows %s questions without losing the assigned task or terminal", async (state, label) => {
    roster[1] = {
      ...roster[1]!, state: "busy", current_task_id: "task-1", current_task_title: "Deploy safely",
      waiting_question: { id: "q-1", question: "May I deploy **these** changes?",
        state, requires_human: state === "human", created_at: 1 },
    };
    renderFlock("/agents", true);
    const row = await screen.findByRole("button", { name: "Open Builder" });
    expect(within(row).getByText("Waiting for input")).toBeInTheDocument();
    expect(within(row).getByText(label)).toBeInTheDocument();
    expect(within(row).getByText("May I deploy **these** changes?")).toBeInTheDocument();
    expect(within(row).getByText("Deploy safely")).toBeInTheDocument();
    fireEvent.click(row);
    const pane = await screen.findByRole("region", { name: "Builder agent window" });
    expect(within(pane).getByRole("textbox", { name: "Builder terminal input" })).toBeInTheDocument();
    expect(api.startAgentTerminal).not.toHaveBeenCalled();
  });


  it.each([
    ["/tasks/task-1", "/projects/first/tasks?q=worktree&completed=1", "/projects/second/tasks?q=worktree&completed=1"],
    ["/tasks/task-1/files", "/projects/first/graph?status=READY", "/projects/second/graph?status=READY"],
    ["/sessions/session-1", "/projects/first/sessions?q=triage", "/projects/second/sessions?q=triage"],
    ["/playbooks/audit", "/projects/first/playbooks?completed=1", "/projects/second/playbooks?completed=1"],
  ])("keeps the originating project and view when navigating from %s", async (pathname, from, destination) => {
    api.listProjects.mockResolvedValue({ data: { projects: [{ id: "first", name: "First project" }, { id: "second", name: "Second project" }] } });
    renderFlock({ pathname, state: { from } });
    const currentProject = await screen.findByRole("link", { name: "First project" });
    expect(currentProject).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Command Center" })).toHaveAttribute("href", from);
    fireEvent.click(screen.getByRole("link", { name: "Second project" }));
    expect(screen.getByLabelText("Current location")).toHaveTextContent(destination);
  });

  it("keeps graph filters selected through a task detail without an All projects option", async () => {
    renderFlock({ pathname: "/tasks/task-1", state: { from: "/command-center/graph?q=review&completed=1" } });
    expect(screen.queryByRole("link", { name: "All projects" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "Command Center" }));
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/command-center/graph?q=review&completed=1");
  });


  it.each([
    ["/settings/config", "/projects/first/tasks?q=old"],
    ["/tasks/task-1", "//other.example/projects/first/tasks?q=old"],
    ["/tasks/task-1", "/settings/profiles"],
  ])("does not apply an unrelated origin to %s", async (pathname, from) => {
    renderFlock({ pathname, state: { from } });
    expect(screen.getByRole("link", { name: "Command Center" })).toHaveAttribute("href", "/command-center/graph");
  });

  it("uses the explicit workspace URL ahead of stale detail origin state", () => {
    renderFlock({ pathname: "/projects/second/config", search: "?q=current", state: { from: "/projects/first/tasks?q=old" } });
    expect(screen.getByRole("link", { name: "Command Center" })).toHaveAttribute("href", "/projects/second/config?q=current");
  });

  it("keeps Command Center navigation and removes Home", async () => {
    renderFlock();
    await screen.findByRole("button", { name: "Open Supervisor" });
    expect(screen.getByRole("link", { name: "Command Center" })).toHaveAttribute("href", "/command-center/graph");
    expect(screen.queryByRole("link", { name: "Home" })).not.toBeInTheDocument();
  });

  it("shows the global supervisor and metadata even when there are no projects", async () => {
    renderFlock();
    const row = await screen.findByRole("button", { name: /open supervisor/i });
    expect(within(row).getByText(/anthropic/)).toBeInTheDocument();
    expect(within(row).getByText(/claude-sonnet-4-6/)).toBeInTheDocument();
    expect(within(row).getByText("int: standard-high")).toBeInTheDocument();
    expect(within(row).getByText("Review deployment")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Agent flock" })).queryByText(/sub-agents/i)).not.toBeInTheDocument();
    expect(api.listAgents).toHaveBeenCalledWith({ body: {}, throwOnError: true });
  });

  it("shows the flock's own sub-agent total in the rail header", async () => {
    rollup = { active_total: 6, native_total: 4, aq_total: 2, spawned_total: 9, complete: true };
    renderFlock();
    await screen.findByRole("button", { name: /open supervisor/i });
    const header = screen.getByRole("button", { name: /agent flock/i });
    expect(within(header).getByText("6 sub")).toBeInTheDocument();
    // The roster count stays beside it — they answer different questions.
    expect(within(header).getByText("5")).toBeInTheDocument();
  });

  it("marks the rail total as a floor when some live session lacks hooks", async () => {
    rollup = { active_total: 3, native_total: 0, aq_total: 3, spawned_total: 0, complete: false };
    renderFlock();
    await screen.findByRole("button", { name: /open supervisor/i });
    const header = screen.getByRole("button", { name: /agent flock/i });
    expect(within(header).getByText("≥3 sub")).toBeInTheDocument();
  });

  it("remembers collapse without changing the open agent selection", async () => {
    const view = renderFlock("/agents?agent=a");
    await screen.findByRole("button", { name: /open supervisor/i });
    fireEvent.click(screen.getByRole("button", { name: /agent flock/i }));
    expect(screen.queryByRole("button", { name: /open supervisor/i })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/agents?agent=a");
    view.unmount();
    renderFlock("/agents?agent=a");
    expect(screen.getByRole("button", { name: /agent flock/i })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: /open supervisor/i })).not.toBeInTheDocument();
  });

  it("adds distinct Shift selections in order and replaces them on ordinary click", async () => {
    renderFlock();
    fireEvent.click(await screen.findByRole("button", { name: /open supervisor/i }));
    fireEvent.click(screen.getByRole("button", { name: /open builder/i }), { shiftKey: true });
    fireEvent.click(screen.getByRole("button", { name: /open reviewer/i }), { shiftKey: true });
    fireEvent.click(screen.getByRole("button", { name: /open builder/i }), { shiftKey: true });
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/agents?agent=a&agent=b&agent=c");
    fireEvent.click(screen.getByRole("button", { name: /open writer/i }));
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/agents?agent=e");
    expect(screen.getByRole("button", { name: /open writer/i })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /open supervisor/i })).toHaveAttribute("aria-pressed", "false");
  });

  it("keeps four views and announces the limit when a fifth agent is Shift clicked", async () => {
    renderFlock("/agents?agent=a&agent=b&agent=c&agent=d");
    fireEvent.click(await screen.findByRole("button", { name: /open writer/i }), { shiftKey: true });
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/agents?agent=a&agent=b&agent=c&agent=d");
    expect(screen.getByRole("status", { name: "Agent view limit" })).toHaveTextContent(/four|4/i);
    fireEvent.click(screen.getByRole("button", { name: /open writer/i }));
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/agents?agent=e");
    expect(screen.queryByText(/close a view/i)).not.toBeInTheDocument();
  });
});

describe("Tiled agent workspace", () => {
  it("opens the exact live session in the main Terminal tab by default", async () => {
    renderFlock("/agents?agent=a", true);
    const window = await screen.findByRole("region", { name: "Supervisor agent window" });
    expect(within(window).getByRole("tab", { name: "Terminal" })).toHaveAttribute("aria-selected", "true");
    expect(TerminalSocketMock.instances.map((source) => new URL(source.url).pathname)).toEqual(["/ws/terminal/session-a"]);
    act(() => {
      TerminalSocketMock.instances[0]!.ready();
      TerminalSocketMock.instances[0]!.message(new TextEncoder().encode("REAL TMUX SCREEN"));
    });
    expect(within(window).getByText("REAL TMUX SCREEN")).toBeInTheDocument();
    expect(within(window).getByText("REAL TMUX SCREEN").closest("button")).toBeNull();
    expect(api.createAgent).not.toHaveBeenCalled();
    expect(api.editAgent).not.toHaveBeenCalled();
  });

  it("closes only the selected window and releases that terminal stream", async () => {
    renderFlock("/agents?agent=a&agent=b", true);
    await screen.findByRole("region", { name: "Builder agent window" });
    expect(TerminalSocketMock.instances).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Close Supervisor view" }));
    expect(TerminalSocketMock.instances.find((source) => source.url.includes("session-a"))?.closed).toBe(true);
    expect(TerminalSocketMock.instances.find((source) => source.url.includes("session-b"))?.closed).toBe(false);
    expect(screen.queryByRole("region", { name: "Supervisor agent window" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/agents?agent=b");
    fireEvent.click(screen.getByRole("button", { name: "Close Builder view" }));
    expect(TerminalSocketMock.instances.every((source) => source.closed)).toBe(true);
    expect(screen.getByText(/select an agent from the flock/i)).toBeInTheDocument();
    expect(api.editAgent).not.toHaveBeenCalled();
  });

  it("keeps four streams after a fifth Shift click and leaves one after a plain click", async () => {
    renderFlock("/agents?agent=a&agent=b&agent=c&agent=d", true);
    await screen.findByRole("region", { name: "Tester agent window" });
    fireEvent.click(screen.getByRole("button", { name: "Open Writer" }), { shiftKey: true });
    expect(screen.getAllByRole("region", { name: /agent window/ })).toHaveLength(4);
    expect(TerminalSocketMock.instances.filter((source) => !source.closed)).toHaveLength(4);
    fireEvent.click(screen.getByRole("button", { name: "Open Writer" }));
    expect(screen.getAllByRole("region", { name: /agent window/ })).toHaveLength(1);
    expect(TerminalSocketMock.instances.filter((source) => !source.closed)).toHaveLength(1);
    expect(screen.getByRole("region", { name: "Writer agent window" })).toBeInTheDocument();
  });

  it("normalizes duplicate and excessive URL selections before subscribing", async () => {
    renderFlock("/agents?agent=a&agent=a&agent=b&agent=c&agent=d&agent=e", true);
    await screen.findByRole("region", { name: "Tester agent window" });
    expect(screen.getAllByRole("region", { name: /agent window/ })).toHaveLength(4);
    expect(TerminalSocketMock.instances.filter((source) => !source.closed)).toHaveLength(4);
    expect(screen.queryByRole("region", { name: "Writer agent window" })).not.toBeInTheDocument();
  });

  it("shows idle and sleeping states without starting or waking sessions", async () => {
    roster[0] = { ...roster[0]!, session_id: null, session_state: null };
    roster[1] = { ...roster[1]!, session_state: "sleeping" };
    renderFlock("/agents?agent=a&agent=b", true);
    expect(await screen.findByText("No active tmux session")).toBeInTheDocument();
    expect(screen.getByText(/session is sleeping/i)).toBeInTheDocument();
    expect(TerminalSocketMock.instances).toHaveLength(0);
    expect(api.createAgent).not.toHaveBeenCalled();
    expect(api.editAgent).not.toHaveBeenCalled();
  });

  it("stops streaming while Settings is visible and resumes on Terminal", async () => {
    renderFlock("/agents?agent=a", true);
    const window = await screen.findByRole("region", { name: "Supervisor agent window" });
    fireEvent.click(within(window).getByRole("tab", { name: "Settings" }));
    expect(TerminalSocketMock.instances[0]!.closed).toBe(true);
    expect(within(window).getByRole("tab", { name: "Settings" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(within(window).getByRole("tab", { name: "Terminal" }));
    expect(TerminalSocketMock.instances.filter((source) => !source.closed)).toHaveLength(1);
  });

  it("saves only individual configured overrides and refreshes the roster", async () => {
    renderFlock("/agents?agent=b", true);
    const window = await screen.findByRole("region", { name: "Builder agent window" });
    fireEvent.click(within(window).getByRole("tab", { name: "Settings" }));
    expect(within(window).getByLabelText("Model override")).toHaveValue("");
    fireEvent.change(within(window).getByLabelText("Name"), { target: { value: "New Builder" } });
    fireEvent.change(within(window).getByLabelText("Provider / harness"), { target: { value: "codex" } });
    fireEvent.change(within(window).getByLabelText("Model override"), { target: { value: "custom-model" } });
    fireEvent.change(within(window).getByLabelText("Intelligence level"), { target: { value: "deep-high" } });
    fireEvent.click(within(window).getByRole("button", { name: "Save settings" }));
    expect(await screen.findByRole("button", { name: "Open New Builder" })).toBeInTheDocument();
    expect(api.editAgent).toHaveBeenCalledWith({
      body: { agent_id: "b", name: "New Builder", profile_id: "implementer", harness: "codex",
        model: "custom-model", intelligence_class: "deep-high", enabled: true }, throwOnError: true,
    });
    expect(within(window).getByText(/saved.*next session/i)).toBeInTheDocument();
  });

  it("clears overrides with empty strings and displays save errors without losing edits", async () => {
    roster[1]!.settings.model = "existing-override";
    renderFlock("/agents?agent=b", true);
    const window = await screen.findByRole("region", { name: "Builder agent window" });
    fireEvent.click(within(window).getByRole("tab", { name: "Settings" }));
    fireEvent.change(within(window).getByLabelText("Model override"), { target: { value: "" } });
    api.editAgent.mockRejectedValueOnce(new Error("Agent settings are read-only for this caller"));
    fireEvent.click(within(window).getByRole("button", { name: "Save settings" }));
    expect(await within(window).findByRole("alert")).toHaveTextContent(/read-only/);
    expect(within(window).getByLabelText("Model override")).toHaveValue("");
    expect(api.editAgent).toHaveBeenCalledWith(expect.objectContaining({ body: expect.objectContaining({ model: "", harness: "", intelligence_class: "" }) }));
  });

  it("does not confuse next-session configuration with unknown current model snapshots", async () => {
    roster[0]!.model = null;
    roster[0]!.settings.model = "configured-for-next-run";
    renderFlock("/agents?agent=a", true);
    const window = await screen.findByRole("region", { name: "Supervisor agent window" });
    expect(within(window).getByText(/model unknown/i)).toBeInTheDocument();
    expect(within(window).queryByText(/configured-for-next-run/)).not.toBeInTheDocument();
    fireEvent.click(within(window).getByRole("tab", { name: "Settings" }));
    expect(within(window).getByLabelText("Model override")).toHaveValue("configured-for-next-run");
  });

  it("defines a new shared worker without creating a running session", async () => {
    renderFlock("/agents", true);
    const rail = within(await screen.findByRole("region", { name: "Agent flock" }));
    fireEvent.click(rail.getByRole("button", { name: "Create agent or pool" }));
    fireEvent.click(within(screen.getByRole("region", { name: "Create agent or pool" }))
      .getByRole("button", { name: "Create agent" }));
    const form = screen.getByRole("form", { name: "Create agent" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Designer" } });
    await within(form).findByRole("option", { name: "Implementer" });
    fireEvent.change(within(form).getByLabelText("Profile"), { target: { value: "implementer" } });
    fireEvent.click(within(form).getByRole("button", { name: "Create agent" }));
    expect(await screen.findByRole("button", { name: "Open Designer" })).toBeInTheDocument();
    await screen.findByRole("region", { name: "Designer agent window" });
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/agents?agent=new-agent");
    expect(TerminalSocketMock.instances).toHaveLength(0);
  });

  it("opens the create fork on the agents page from any other page", async () => {
    renderFlock("/tasks", true);
    const rail = within(await screen.findByRole("region", { name: "Agent flock" }));
    fireEvent.click(rail.getByRole("button", { name: "Create agent or pool" }));
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/agents?add=1");
    expect(screen.getByRole("region", { name: "Create agent or pool" })).toBeInTheDocument();
    // The fork comes first: neither form is mounted until a choice is made.
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    expect(rail.queryByRole("link", { name: /manage agent/i })).not.toBeInTheDocument();
  });

  it("gives the whole page to the agent window once one is selected", async () => {
    renderFlock("/agents?agent=b", true);
    await screen.findByRole("region", { name: "Builder agent window" });
    expect(screen.queryByRole("heading", { name: "Agent flock" })).not.toBeInTheDocument();
    // The only remaining create control is the left rail's; the page header is gone.
    const addButtons = screen.getAllByRole("button", { name: "Create agent or pool" });
    expect(addButtons).toHaveLength(1);
    expect(screen.getByRole("region", { name: "Agent flock" })).toContainElement(addButtons[0]!);
  });
});

describe("Agent terminal transport", () => {
  it.each(["subprocess", null])("does not claim or subscribe to tmux for %s transport", async (provider) => {
    roster[0]!.session_provider = provider;
    renderFlock("/agents?agent=a", true);
    const window = await screen.findByRole("region", { name: "Supervisor agent window" });
    expect(within(window).getByText("Tmux view unavailable")).toBeInTheDocument();
    expect(within(window).queryByText(/live tmux/i)).not.toBeInTheDocument();
    expect(TerminalSocketMock.instances).toHaveLength(0);
    if (provider) expect(within(window).getByText(/uses subprocess/)).toBeInTheDocument();
    else expect(within(window).getByText(/transport is unknown/)).toBeInTheDocument();
  });
});

describe("Agents finishing current work", () => {
  it("keeps streaming a draining tmux session until it actually exits", async () => {
    roster[0]!.session_state = "draining";
    renderFlock("/agents?agent=a", true);
    await screen.findByRole("region", { name: "Supervisor agent window" });
    expect(TerminalSocketMock.instances.filter((source) => !source.closed)).toHaveLength(1);
    act(() => {
      TerminalSocketMock.instances[0]!.ready();
      TerminalSocketMock.instances[0]!.message(new TextEncoder().encode("FINISHING CURRENT TASK"));
    });
    expect(screen.getByText("FINISHING CURRENT TASK")).toBeInTheDocument();
  });

  it("shows a disabled worker's busy state separately from new-work eligibility", async () => {
    roster[0]!.enabled = false;
    roster[0]!.settings.enabled = false;
    renderFlock("/agents?agent=a", true);
    const sidebar = await screen.findByRole("button", { name: "Open Supervisor" });
    const window = screen.getByRole("region", { name: "Supervisor agent window" });
    expect(within(sidebar).getByText("busy")).toBeInTheDocument();
    expect(within(sidebar).getByText("New work disabled")).toBeInTheDocument();
    expect(within(window).getByText("busy")).toBeInTheDocument();
    expect(within(window).getByText("New work disabled")).toBeInTheDocument();
    expect(TerminalSocketMock.instances.filter((source) => !source.closed)).toHaveLength(1);
  });
});

describe("Deleting a defined worker", () => {
  beforeEach(() => {
    roster[1] = { ...roster[1]!, session_id: null, session_state: null };
  });

  async function builderSettings(initial = "/agents?agent=b") {
    renderFlock(initial, true);
    const window = await screen.findByRole("region", { name: "Builder agent window" });
    fireEvent.click(within(window).getByRole("tab", { name: "Settings" }));
    return window;
  }

  it("requires confirmation and lets cancellation leave the worker intact", async () => {
    const window = await builderSettings();
    fireEvent.click(within(window).getByRole("button", { name: "Delete agent" }));
    expect(within(window).getByText(/task and session history.*preserved/i)).toBeInTheDocument();
    expect(api.deleteAgent).not.toHaveBeenCalled();
    fireEvent.click(within(window).getByRole("button", { name: "Cancel deletion" }));
    expect(within(window).queryByRole("button", { name: "Confirm delete" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open Builder" })).toBeInTheDocument();
    expect(api.deleteAgent).not.toHaveBeenCalled();
  });

  it("deletes only after confirmation, removes the row, and closes only that view", async () => {
    const window = await builderSettings("/agents?agent=a&agent=b");
    fireEvent.click(within(window).getByRole("button", { name: "Delete agent" }));
    expect(api.deleteAgent).not.toHaveBeenCalled();
    fireEvent.click(within(window).getByRole("button", { name: "Confirm delete" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Builder agent window" })).not.toBeInTheDocument());
    expect(api.deleteAgent).toHaveBeenCalledWith({ body: { agent_id: "b" }, throwOnError: true });
    expect(api.deleteAgent).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Open Builder" })).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Supervisor agent window" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Current location").textContent).toBe("/agents?agent=a"));
    expect(api.editAgent).not.toHaveBeenCalled();
  });

  it("closes a deleted view after a tab switch without losing agents added during the request", async () => {
    let finishDelete!: () => void;
    api.deleteAgent.mockImplementationOnce(() => new Promise((resolve) => {
      finishDelete = () => {
        roster = roster.filter((row) => row.id !== "b");
        resolve({ data: { deleted: "b", name: "Builder" } });
      };
    }));
    const window = await builderSettings();
    fireEvent.click(within(window).getByRole("button", { name: "Delete agent" }));
    fireEvent.click(within(window).getByRole("button", { name: "Confirm delete" }));
    await waitFor(() => expect(api.deleteAgent).toHaveBeenCalledTimes(1));
    fireEvent.click(within(window).getByRole("tab", { name: "Terminal" }));
    fireEvent.click(screen.getByRole("button", { name: "Open Reviewer" }), { shiftKey: true });
    await act(async () => { finishDelete(); });
    await waitFor(() => expect(screen.getByLabelText("Current location").textContent).toBe("/agents?agent=c"));
    expect(screen.queryByRole("region", { name: "Builder agent window" })).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Reviewer agent window" })).toBeInTheDocument();
  });

  it("shows an authoritative backend refusal without closing or removing the agent", async () => {
    api.deleteAgent.mockRejectedValueOnce(new Error("Agent has a live session; stop it first"));
    const window = await builderSettings();
    fireEvent.click(within(window).getByRole("button", { name: "Delete agent" }));
    fireEvent.click(within(window).getByRole("button", { name: "Confirm delete" }));
    expect(await within(window).findByRole("alert")).toHaveTextContent("Agent has a live session; stop it first");
    expect(screen.getByRole("button", { name: "Open Builder" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Builder agent window" })).toBeInTheDocument();
  });

  it.each(["starting", "running", "draining"])("prevents deleting a worker with a %s session", async (state) => {
    roster[1] = { ...roster[1]!, session_id: "live-session", session_state: state };
    const window = await builderSettings();
    expect(within(window).getByRole("button", { name: "Delete agent" })).toBeDisabled();
    expect(within(window).getByText(/assigned work or a live session/i)).toBeInTheDocument();
    expect(api.deleteAgent).not.toHaveBeenCalled();
  });

  it("prevents deleting an assigned worker even without a visible session", async () => {
    roster[1] = { ...roster[1]!, state: "busy", current_task_id: "claimed-task" };
    const window = await builderSettings();
    expect(within(window).getByRole("button", { name: "Delete agent" })).toBeDisabled();
    expect(api.deleteAgent).not.toHaveBeenCalled();
  });

  it("protects the supervisor from deletion", async () => {
    renderFlock("/agents?agent=a", true);
    const supervisor = await screen.findByRole("region", { name: "Supervisor agent window" });
    fireEvent.click(within(supervisor).getByRole("tab", { name: "Settings" }));
    expect(within(supervisor).queryByRole("button", { name: "Delete agent" })).not.toBeInTheDocument();
    expect(within(supervisor).getByText(/supervisor agents cannot be deleted/i)).toBeInTheDocument();
    expect(api.deleteAgent).not.toHaveBeenCalled();
  });

  it("does not offer the reserved Supervisor profile for new or existing workers", async () => {
    const window = await builderSettings();
    await within(window).findByRole("option", { name: "Implementer" });
    expect(within(window).queryByRole("option", { name: "Supervisor" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create agent or pool" }));
    fireEvent.click(within(screen.getByRole("region", { name: "Create agent or pool" }))
      .getByRole("button", { name: "Create agent" }));
    const form = screen.getByRole("form", { name: "Create agent" });
    await within(form).findByRole("option", { name: "Implementer" });
    expect(within(form).queryByRole("option", { name: "Supervisor" })).not.toBeInTheDocument();
  });
});

describe("Starting and using agent terminals", () => {
  it("offers direct terminal input for workers and the supervisor without a Chat tab", async () => {
    renderFlock("/agents?agent=a&agent=b", true);
    await screen.findByRole("region", { name: "Supervisor agent window" });
    act(() => TerminalSocketMock.instances.forEach((source) => source.ready()));
    const builder = TerminalMock.instances.find((term) => term.textarea?.getAttribute("aria-label") === "Builder terminal input")!;
    const supervisor = TerminalMock.instances.find((term) => term.textarea?.getAttribute("aria-label") === "Supervisor terminal input")!;
    act(() => { builder.emitData("b"); supervisor.emitData("s"); });
    const builderSocket = TerminalSocketMock.instances.find((socket) => socket.url.includes("/session-b"))!;
    const supervisorSocket = TerminalSocketMock.instances.find((socket) => socket.url.includes("/session-a"))!;
    expect(builderSocket.inputs().map((data) => new TextDecoder().decode(data))).toEqual(["b"]);
    expect(supervisorSocket.inputs().map((data) => new TextDecoder().decode(data))).toEqual(["s"]);
    expect(api.sessionInput).not.toHaveBeenCalled();
    expect(EventSource).not.toHaveBeenCalled();
    expect(screen.queryByRole("tab", { name: "Chat" })).not.toBeInTheDocument();
    expect(api.startAgentTerminal).not.toHaveBeenCalled();
  });

  it.each([null, "sleeping"])("starts or resumes a %s session only after clicking the button", async (state) => {
    roster[1] = { ...roster[1]!, session_state: state, session_id: state ? "sleeping-b" : null };
    renderFlock("/agents?agent=b", true);
    const window = await screen.findByRole("region", { name: "Builder agent window" });
    expect(api.startAgentTerminal).not.toHaveBeenCalled();
    expect(TerminalSocketMock.instances).toHaveLength(0);
    fireEvent.click(within(window).getByRole("button", { name: state ? "Resume terminal" : "Start terminal" }));
    await waitFor(() => expect(TerminalSocketMock.instances).toHaveLength(1));
    expect(api.startAgentTerminal).toHaveBeenCalledWith({ body: { agent_id: "b" }, throwOnError: true });
    expect(new URL(TerminalSocketMock.instances[0]!.url).pathname).toBe("/ws/terminal/started-b");
    expect(api.sessionInput).not.toHaveBeenCalled();
  });

  it.each(["disabled", "busy", "starting", "subprocess"])("does not offer to launch an unavailable %s agent", async (reason) => {
    roster[1] = { ...roster[1]!, session_id: null, session_state: null,
      enabled: reason !== "disabled", state: reason === "busy" ? "busy" : "idle",
      current_task_id: reason === "busy" ? "task-owned" : null };
    if (reason === "starting") roster[1]!.session_state = "starting";
    if (reason === "subprocess") roster[1]!.session_provider = "subprocess";
    renderFlock("/agents?agent=b", true);
    const window = await screen.findByRole("region", { name: "Builder agent window" });
    expect(within(window).getByRole("button", { name: /Start terminal|Starting/ })).toBeDisabled();
    expect(api.startAgentTerminal).not.toHaveBeenCalled();
  });

  it("reports a rejected launch and does not retry or send input", async () => {
    roster[1] = { ...roster[1]!, session_id: null, session_state: null };
    api.startAgentTerminal.mockRejectedValueOnce(new Error("Terminal launch unavailable"));
    renderFlock("/agents?agent=b", true);
    const window = await screen.findByRole("region", { name: "Builder agent window" });
    fireEvent.click(within(window).getByRole("button", { name: "Start terminal" }));
    expect(await within(window).findByRole("alert")).toHaveTextContent("Terminal launch unavailable");
    expect(api.startAgentTerminal).toHaveBeenCalledTimes(1);
    expect(api.sessionInput).not.toHaveBeenCalled();
  });
});
