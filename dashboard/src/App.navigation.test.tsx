import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useState, type ReactNode } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useParams } from "react-router-dom";
import { useShellPaneStore } from "./panes/store";
import App from "./App";

const actions = vi.hoisted(() => ({ pause: vi.fn(), resume: vi.fn(), remove: vi.fn() }));
const projects = [{ id: "p1", name: "First project" }, { id: "p2", name: "Second project" }];
vi.mock("./panes/registry", () => ({ PANE_REGISTRY: {
  "task-detail": { manifest: {} }, "contextual-settings": { manifest: {} },
} }));
vi.mock("./api/hooks", () => ({
  useProjects: () => ({ data: projects }),
  useProject: (id: string) => ({ data: { ...projects.find((p) => p.id === id), id, paused: id === "p2" } }),
  usePauseProject: () => ({ mutate: actions.pause, isPending: false }),
  useResumeProject: () => ({ mutate: actions.resume, isPending: false }),
  useDeleteProject: () => ({ mutateAsync: actions.remove, isPending: false }),
}));
vi.mock("./panes/agentPush", () => ({ useAgentPushBridge: () => {} }));
vi.mock("./shell/AgentFlock", () => ({ default: () => <div>Global flock sidebar</div> }));
vi.mock("./shell/TopBar", () => ({ default: () => null }));
vi.mock("./shell/RightSurface", () => ({ default: () => <PaneProbe /> }));
vi.mock("./shell/palette/Palette", () => ({ Palette: () => null }));
vi.mock("./shell/hotkeys/CheatSheetModal", () => ({ default: () => null }));
vi.mock("./pages/command-center/Graph", () => ({ default: () => <WorkspaceProbe title="Command Center graph" /> }));
vi.mock("./pages/command-center/Tasks", () => ({ default: () => <WorkspaceProbe title="Command Center tasks" /> }));
vi.mock("./pages/command-center/TaskWorkspace", () => ({ TaskWorkspaceProvider: ({ children }: { children: ReactNode }) => <>{children}</> }));
vi.mock("./pages/command-center/TaskToolbar", () => ({ default: () => <div role="toolbar" aria-label="Task controls" /> }));
vi.mock("./pages/project/Overview", () => ({ default: () => <WorkspaceProbe title="Project overview" /> }));
vi.mock("./pages/project/Workspaces", () => ({ default: () => <WorkspaceProbe title="Project workspaces" /> }));
vi.mock("./pages/project/Config", () => ({ default: () => <WorkspaceProbe title="Project config" /> }));
vi.mock("./pages/chat/ChatConversation", () => ({ default: () => <h1>Former project chat</h1> }));
vi.mock("./pages/GlobalChat", () => ({ default: () => <h1>Former Home chat</h1> }));
vi.mock("./pages/agents/AgentWorkspace", () => ({ default: () => <h1>Agent flock</h1> }));

function WorkspaceProbe({ title }: { title: string }) {
  const { projectId } = useParams();
  const [draft, setDraft] = useState("");
  return <><h1>{title}</h1><output aria-label="Workspace project">{projectId ?? "all"}</output>
    <input aria-label="Resource draft" value={draft} onChange={(e) => setDraft(e.target.value)} /></>;
}

function PaneProbe() {
  const pane = useShellPaneStore();
  return <><output aria-label="Current pane">{JSON.stringify(pane.state)}</output>
    <button onClick={() => pane.open("task-detail", { taskId: "task-p1" })}>Open task pane</button>
    <button onClick={() => pane.open("contextual-settings", { subject: "project", subjectId: "p1" })}>Open settings pane</button></>;
}

function Location() {
  const location = useLocation();
  return <output aria-label="Current location">{location.pathname}{location.search}</output>;
}

function renderApp(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><App /><Location /></MemoryRouter>);
}

beforeEach(() => { vi.clearAllMocks(); actions.remove.mockResolvedValue({ success: true }); });
afterEach(cleanup);

describe("Dashboard navigation", () => {
  it("keeps only Graph and Tasks in Command Center navigation", async () => {
    renderApp("/command-center/graph");
    await screen.findByRole("heading", { name: "Command Center graph" });
    expect(screen.getByRole("link", { name: "Graph" })).toHaveAttribute("href", "/command-center/graph");
    expect(screen.getByRole("link", { name: "Tasks" })).toHaveAttribute("href", "/command-center/tasks");
    expect(screen.queryByRole("link", { name: "Agents" })).not.toBeInTheDocument();
  });

  it.each(["/agents", "/command-center/agents", "/work/agents", "/work/sessions"])(
    "opens the sidebar Agent flock from %s", async (path) => {
      renderApp(path);
      expect(await screen.findByRole("heading", { name: "Agent flock" })).toBeInTheDocument();
      expect(screen.getByLabelText("Current location").textContent).toBe("/agents");
    },
  );

  it.each(["/", "/old-missing-page"])("lands on Command Center from %s", async (path) => {
    renderApp(path);
    expect(await screen.findByRole("heading", { name: "Command Center graph" })).toBeInTheDocument();
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/command-center/graph");
    expect(screen.queryByRole("heading", { name: "Former Home chat" })).not.toBeInTheDocument();
  });

  it("routes the former Home shortcut to the supervisor's terminal", async () => {
    renderApp("/command-center/graph");
    await screen.findByRole("heading", { name: "Command Center graph" });
    await userEvent.keyboard("g");
    expect(screen.queryByText(/\bhome\b/i)).not.toBeInTheDocument();
    await userEvent.keyboard("h");
    expect(await screen.findByRole("heading", { name: "Agent flock" })).toBeInTheDocument();
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/agents?agent=supervisor-global");
  });

  it("provides a direct Agent flock shortcut", async () => {
    renderApp("/command-center/graph");
    await screen.findByRole("heading", { name: "Command Center graph" });
    await userEvent.keyboard("ga");
    expect(await screen.findByRole("heading", { name: "Agent flock" })).toBeInTheDocument();
    expect(screen.getByLabelText("Current location").textContent).toBe("/agents");
  });
});


describe("Shared project workspace navigation", () => {
  it("uses the same Graph/Tasks tabs and controls for the selected sidebar project", async () => {
    renderApp("/projects/p1/graph?q=needle&status=READY&completed=1");
    await screen.findByRole("heading", { name: "Command Center graph" });
    expect(screen.getByLabelText("Workspace project")).toHaveTextContent("p1");
    expect(screen.getByRole("link", { name: "First project" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Command Center" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("toolbar", { name: "Task controls" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Chat" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Tasks" }));
    await screen.findByRole("heading", { name: "Command Center tasks" });
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/projects/p1/tasks?q=needle&status=READY&completed=1");
    await userEvent.click(screen.getByRole("link", { name: "Second project" }));
    expect(screen.getByLabelText("Workspace project")).toHaveTextContent("p2");
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/projects/p2/tasks?q=needle&status=READY&completed=1");
    await userEvent.click(screen.getByRole("link", { name: "All projects" }));
    expect(screen.getByLabelText("Workspace project")).toHaveTextContent("all");
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/command-center/tasks?q=needle&status=READY&completed=1");
  });

  it("keeps resource tabs scoped, resets their drafts, and falls back to Graph for All projects", async () => {
    renderApp("/projects/p1/workspaces?q=keep");
    await screen.findByRole("heading", { name: "Project workspaces" });
    expect(screen.getByRole("link", { name: "Graph" })).toHaveAttribute("href", "/projects/p1/graph?q=keep");
    expect(screen.getByRole("link", { name: "Config" })).toHaveAttribute("href", "/projects/p1/config?q=keep");
    expect(screen.queryByRole("toolbar", { name: "Task controls" })).not.toBeInTheDocument();
    await userEvent.type(screen.getByRole("textbox", { name: "Resource draft" }), "First project's draft");
    await userEvent.click(screen.getByRole("link", { name: "Second project" }));
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/projects/p2/workspaces?q=keep");
    expect(screen.getByRole("textbox", { name: "Resource draft" })).toHaveValue("");
    await userEvent.click(screen.getByRole("link", { name: "All projects" }));
    await screen.findByRole("heading", { name: "Command Center graph" });
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/command-center/graph?q=keep");
  });

  it.each(["/projects/p1", "/command-center"])("preserves filters on the default Graph redirect from %s", async (path) => {
    renderApp(path + "?q=keep&completed=1");
    await screen.findByRole("heading", { name: "Command Center graph" });
    expect(screen.getByLabelText("Current location")).toHaveTextContent(path + "/graph?q=keep&completed=1");
  });

  it("retains the former project overview as a resource tab", async () => {
    renderApp("/projects/p1/overview");
    await screen.findByRole("heading", { name: "Project overview" });
    expect(screen.getByRole("link", { name: "Overview" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("toolbar", { name: "Task controls" })).not.toBeInTheDocument();
  });

  it.each(["/projects/p1/chat", "/chat/p1"])("redirects old chat %s to the flock", async (path) => {
    renderApp(path);
    await screen.findByRole("heading", { name: "Agent flock" });
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/agents");
  });

  it("clears only a task pane when the sidebar project changes", async () => {
    renderApp("/projects/p1/graph");
    await screen.findByRole("heading", { name: "Command Center graph" });
    await userEvent.click(screen.getByRole("button", { name: "Open task pane" }));
    await userEvent.click(screen.getByRole("link", { name: "Tasks" }));
    expect(screen.getByLabelText("Current pane")).toHaveTextContent("task-p1");
    await userEvent.click(screen.getByRole("link", { name: "Second project" }));
    expect(screen.getByLabelText("Current pane")).toHaveTextContent('"kind":"closed"');
    await userEvent.click(screen.getByRole("button", { name: "Open settings pane" }));
    await userEvent.click(screen.getByRole("link", { name: "First project" }));
    expect(screen.getByLabelText("Current pane")).toHaveTextContent("contextual-settings");
    expect(screen.getByLabelText("Current pane")).toHaveTextContent('"subjectId":"p1"');
    await userEvent.click(screen.getByRole("link", { name: "All projects" }));
    expect(screen.getByLabelText("Current pane")).toHaveTextContent("contextual-settings");
    await userEvent.click(screen.getByRole("link", { name: "First project" }));
    await userEvent.click(screen.getByRole("button", { name: "Open task pane" }));
    await userEvent.click(screen.getByRole("link", { name: "All projects" }));
    expect(screen.getByLabelText("Current pane")).toHaveTextContent('"kind":"closed"');
  });

  it("keeps project pause, resume and confirmed deletion working in the unified header", async () => {
    renderApp("/projects/p1/graph");
    await screen.findByRole("heading", { name: "Command Center graph" });
    await userEvent.click(screen.getByRole("button", { name: "Pause project" }));
    expect(actions.pause).toHaveBeenCalledWith({ project_id: "p1" });
    await userEvent.click(screen.getByRole("link", { name: "Second project" }));
    await userEvent.click(screen.getByRole("button", { name: "Resume project" }));
    expect(actions.resume).toHaveBeenCalledWith({ project_id: "p2" });
    await userEvent.click(screen.getByRole("button", { name: "Delete project" }));
    await userEvent.type(screen.getByPlaceholderText("Second project"), "Second project");
    const deleteButtons = screen.getAllByRole("button", { name: "Delete project" });
    await userEvent.click(deleteButtons[deleteButtons.length - 1]!);
    expect(actions.remove).toHaveBeenCalledWith({ project_id: "p2" });
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/command-center/graph");
  });
});
