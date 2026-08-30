import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import LeftRail from "../../../shell/LeftRail";
import AgentWorkspace from "../AgentWorkspace";
import type { FlockAgent } from "../../../api/agents";

const api = vi.hoisted(() => ({
  listAgents: vi.fn(), listProjects: vi.fn(), listProfiles: vi.fn(),
  getAgent: vi.fn(), editAgent: vi.fn(), createAgent: vi.fn(), listIntelligenceClasses: vi.fn(),
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
    settings: { name, profile_id: "implementer", harness: null, model: null,
      intelligence_class: null, enabled: true },
  };
}

let roster = [agent("a", "Supervisor"), agent("b", "Builder"), agent("c", "Reviewer"),
  agent("d", "Tester"), agent("e", "Writer")];
const clients: QueryClient[] = [];

class PaneSource {
  static sources: PaneSource[] = [];
  onmessage: ((message: { data: string }) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  readyState = 1;
  constructor(public url: string) { PaneSource.sources.push(this); }
  close() { this.closed = true; this.readyState = 2; }
}


function Location() {
  const location = useLocation();
  return <output aria-label="Current location">{location.pathname}{location.search}</output>;
}

function renderFlock(initial = "/", workspace = false) {
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
  PaneSource.sources = [];
  vi.stubGlobal("EventSource", PaneSource);
  roster = [agent("a", "Supervisor"), agent("b", "Builder"), agent("c", "Reviewer"),
    agent("d", "Tester"), agent("e", "Writer")];
  roster[0] = { ...roster[0]!, role: "supervisor", state: "busy", current_task_id: "task-1",
    current_task_title: "Review deployment", active_subagent_count: 2, aq_subagent_count: 2,
    native_subagent_count: null, subagent_count_complete: false };
  roster[1] = { ...roster[1]!, active_subagent_count: null, native_subagent_count: null,
    subagent_count_complete: false };
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
  api.listAgents.mockImplementation(async () => ({ data: { agents: roster, count: roster.length } }));
  api.listProjects.mockResolvedValue({ data: { projects: [] } });
  api.listIntelligenceClasses.mockResolvedValue({ data: { classes: [{ id: "standard-high", name: "Standard high" }, { id: "deep-high", name: "Deep high" }] } });
  api.listProfiles.mockResolvedValue({ data: { profiles: [{ id: "implementer", name: "Implementer" }] } });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  clients.splice(0).forEach((client) => client.clear());
});

describe("Agent flock sidebar", () => {
  it("shows the global supervisor and metadata even when there are no projects", async () => {
    renderFlock();
    const row = await screen.findByRole("button", { name: /open supervisor/i });
    expect(within(row).getByText(/anthropic/)).toBeInTheDocument();
    expect(within(row).getByText(/claude-sonnet-4-6/)).toBeInTheDocument();
    expect(within(row).getByText(/standard-high/)).toBeInTheDocument();
    expect(within(row).getByText("Review deployment")).toBeInTheDocument();
    expect(within(row).getByText("2+ active sub-agents")).toHaveAttribute("title", expect.stringMatching(/partial|incomplete|unavailable/i));
    expect(within(await screen.findByRole("button", { name: /open builder/i })).getByText("Sub-agents unknown")).toBeInTheDocument();
    expect(within(await screen.findByRole("button", { name: /open reviewer/i })).getByText("0 active sub-agents")).toBeInTheDocument();
    expect(api.listAgents).toHaveBeenCalledWith({ body: {}, throwOnError: true });
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
    expect(PaneSource.sources.map((source) => new URL(source.url).pathname)).toEqual(["/api/sessions/session-a/pane"]);
    act(() => PaneSource.sources[0]!.onmessage?.({ data: JSON.stringify({ type: "screen", screen: "REAL TMUX SCREEN", seq: 1 }) }));
    expect(within(window).getByText("REAL TMUX SCREEN")).toBeInTheDocument();
    expect(within(window).getByText("REAL TMUX SCREEN").closest("button")).toBeNull();
    expect(api.createAgent).not.toHaveBeenCalled();
    expect(api.editAgent).not.toHaveBeenCalled();
  });

  it("closes only the selected window and releases that terminal stream", async () => {
    renderFlock("/agents?agent=a&agent=b", true);
    await screen.findByRole("region", { name: "Builder agent window" });
    expect(PaneSource.sources).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Close Supervisor view" }));
    expect(PaneSource.sources.find((source) => source.url.includes("session-a"))?.closed).toBe(true);
    expect(PaneSource.sources.find((source) => source.url.includes("session-b"))?.closed).toBe(false);
    expect(screen.queryByRole("region", { name: "Supervisor agent window" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/agents?agent=b");
    fireEvent.click(screen.getByRole("button", { name: "Close Builder view" }));
    expect(PaneSource.sources.every((source) => source.closed)).toBe(true);
    expect(screen.getByText(/select an agent from the flock/i)).toBeInTheDocument();
    expect(api.editAgent).not.toHaveBeenCalled();
  });

  it("keeps four streams after a fifth Shift click and leaves one after a plain click", async () => {
    renderFlock("/agents?agent=a&agent=b&agent=c&agent=d", true);
    await screen.findByRole("region", { name: "Tester agent window" });
    fireEvent.click(screen.getByRole("button", { name: "Open Writer" }), { shiftKey: true });
    expect(screen.getAllByRole("region", { name: /agent window/ })).toHaveLength(4);
    expect(PaneSource.sources.filter((source) => !source.closed)).toHaveLength(4);
    fireEvent.click(screen.getByRole("button", { name: "Open Writer" }));
    expect(screen.getAllByRole("region", { name: /agent window/ })).toHaveLength(1);
    expect(PaneSource.sources.filter((source) => !source.closed)).toHaveLength(1);
    expect(screen.getByRole("region", { name: "Writer agent window" })).toBeInTheDocument();
  });

  it("normalizes duplicate and excessive URL selections before subscribing", async () => {
    renderFlock("/agents?agent=a&agent=a&agent=b&agent=c&agent=d&agent=e", true);
    await screen.findByRole("region", { name: "Tester agent window" });
    expect(screen.getAllByRole("region", { name: /agent window/ })).toHaveLength(4);
    expect(PaneSource.sources.filter((source) => !source.closed)).toHaveLength(4);
    expect(screen.queryByRole("region", { name: "Writer agent window" })).not.toBeInTheDocument();
  });

  it("shows idle and sleeping states without starting or waking sessions", async () => {
    roster[0] = { ...roster[0]!, session_id: null, session_state: null };
    roster[1] = { ...roster[1]!, session_state: "sleeping" };
    renderFlock("/agents?agent=a&agent=b", true);
    expect(await screen.findByText("No active tmux session")).toBeInTheDocument();
    expect(screen.getByText(/session is sleeping/i)).toBeInTheDocument();
    expect(PaneSource.sources).toHaveLength(0);
    expect(api.createAgent).not.toHaveBeenCalled();
    expect(api.editAgent).not.toHaveBeenCalled();
  });

  it("stops streaming while Settings is visible and resumes on Terminal", async () => {
    renderFlock("/agents?agent=a", true);
    const window = await screen.findByRole("region", { name: "Supervisor agent window" });
    fireEvent.click(within(window).getByRole("tab", { name: "Settings" }));
    expect(PaneSource.sources[0]!.closed).toBe(true);
    expect(within(window).getByRole("tab", { name: "Settings" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(within(window).getByRole("tab", { name: "Terminal" }));
    expect(PaneSource.sources.filter((source) => !source.closed)).toHaveLength(1);
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
    fireEvent.click(await screen.findByRole("button", { name: "Add agent" }));
    const form = screen.getByRole("form", { name: "Add agent" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Designer" } });
    await within(form).findByRole("option", { name: "Implementer" });
    fireEvent.change(within(form).getByLabelText("Profile"), { target: { value: "implementer" } });
    fireEvent.click(within(form).getByRole("button", { name: "Create agent" }));
    expect(await screen.findByRole("button", { name: "Open Designer" })).toBeInTheDocument();
    await screen.findByRole("region", { name: "Designer agent window" });
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/agents?agent=new-agent");
    expect(PaneSource.sources).toHaveLength(0);
  });
});

describe("Agent terminal transport", () => {
  it.each(["subprocess", null])("does not claim or subscribe to tmux for %s transport", async (provider) => {
    roster[0]!.session_provider = provider;
    renderFlock("/agents?agent=a", true);
    const window = await screen.findByRole("region", { name: "Supervisor agent window" });
    expect(within(window).getByText("Tmux view unavailable")).toBeInTheDocument();
    expect(within(window).queryByText(/live tmux/i)).not.toBeInTheDocument();
    expect(PaneSource.sources).toHaveLength(0);
    if (provider) expect(within(window).getByText(/uses subprocess/)).toBeInTheDocument();
    else expect(within(window).getByText(/transport is unknown/)).toBeInTheDocument();
  });
});

describe("Agents finishing current work", () => {
  it("keeps streaming a draining tmux session until it actually exits", async () => {
    roster[0]!.session_state = "draining";
    renderFlock("/agents?agent=a", true);
    await screen.findByRole("region", { name: "Supervisor agent window" });
    expect(PaneSource.sources.filter((source) => !source.closed)).toHaveLength(1);
    act(() => PaneSource.sources[0]!.onmessage?.({ data: JSON.stringify({ type: "screen", screen: "FINISHING CURRENT TASK", seq: 1 }) }));
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
    expect(PaneSource.sources.filter((source) => !source.closed)).toHaveLength(1);
  });
});
