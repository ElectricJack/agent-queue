import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useState, type ReactNode } from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, useLocation, useParams } from "react-router-dom";
import { useShellPaneStore } from "./panes/store";
import { useRightSurface } from "./shell/useRightSurface";
import App from "./App";
import ActivityDrawer from "./shell/ActivityDrawer";

const actions = vi.hoisted(() => ({ pause: vi.fn(), resume: vi.fn(), remove: vi.fn() }));
const projects = [{ id: "p1", name: "First project" }, { id: "p2", name: "Second project" }];
const initialProjects = projects.map((project) => ({ ...project }));
vi.mock("./panes/registry", () => ({ PANE_REGISTRY: {
  "task-detail": { manifest: {} }, "contextual-settings": { manifest: {} },
} }));
vi.mock("./api/hooks", () => ({
  useProjects: () => ({ data: projects }),
  useAllOpenGates: () => ({ data: [] }),
  useResolveGate: () => ({ mutate: vi.fn() }),
  useProject: (id: string) => ({ data: { ...projects.find((p) => p.id === id), id, paused: id === "p2" } }),
  usePauseProject: () => ({ mutate: actions.pause, isPending: false }),
  useResumeProject: () => ({ mutate: actions.resume, isPending: false }),
  useDeleteProject: () => ({ mutateAsync: actions.remove, isPending: false }),
}));
vi.mock("./ws/useEventStream", () => ({ useEventStream: () => {} }));
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
vi.mock("./pages/settings/SettingsLayout", () => ({ default: () => <Outlet /> }));
vi.mock("./pages/system/Playbooks", () => ({ default: () => <h1>Settings playbooks</h1> }));
vi.mock("./pages/system/Profiles", () => ({ default: () => <h1>Settings profiles</h1> }));
vi.mock("./pages/system/Config", () => ({ default: () => <h1>Settings config</h1> }));
vi.mock("./pages/settings/IntelligenceClassesStub", () => ({ default: () => <h1>Settings intelligence classes</h1> }));
vi.mock("./pages/PlaybookDetail", () => ({ default: () => <h1>Playbook detail</h1> }));

function WorkspaceProbe({ title }: { title: string }) {
  const { projectId } = useParams();
  const [draft, setDraft] = useState("");
  return <><h1>{title}</h1><output aria-label="Workspace project">{projectId ?? "all"}</output>
    <input aria-label="Resource draft" value={draft} onChange={(e) => setDraft(e.target.value)} /></>;
}

function PaneProbe() {
  const pane = useShellPaneStore();
  const surface = useRightSurface();
  return <><output aria-label="Current pane">{JSON.stringify(pane.state)}</output>
    <output aria-label="Current surface">{surface.kind ?? "closed"}</output>
    {surface.kind === "drawer" && <ActivityDrawer />}
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

beforeEach(() => {
  vi.clearAllMocks();
  projects.splice(0, projects.length, ...initialProjects.map((project) => ({ ...project })));
  window.localStorage.clear();
  actions.remove.mockImplementation(async ({ project_id }: { project_id: string }) => {
    const index = projects.findIndex((project) => project.id === project_id);
    if (index >= 0) projects.splice(index, 1);
    return { success: true };
  });
});
afterEach(cleanup);

describe("Dashboard navigation", () => {
  it.each([
    ["/system", "/projects/p1/graph"],
    ["/tasks", "/projects/p1/tasks"],
    ["/playbooks", "/settings/playbooks"],
    ["/system/playbooks", "/settings/playbooks"],
    ["/system/profiles", "/settings/profiles"],
    ["/system/config", "/settings/config"],
    ["/system/intelligence-classes", "/settings/intelligence-classes"],
    ["/work", "/projects/p1/tasks"],
    ["/work/tasks", "/projects/p1/tasks"],
    ["/work/agents", "/agents"],
    ["/work/sessions", "/agents"],
  ])("redirects legacy route %s to %s while preserving filters", async (from, to) => {
    renderApp(from + "?q=needle&status=READY");
    await waitFor(() => expect(screen.getByLabelText("Current location")).toHaveTextContent(to + "?q=needle&status=READY"));
  });

  it.each(["demo%2Freview", "demo%2520review", "draft%25complete"])(
    "redirects a legacy playbook settings detail without decoding %s twice", async (id) => {
      renderApp(`/settings/playbooks/${id}?q=needle`);
      await screen.findByRole("heading", { name: "Playbook detail" });
      expect(screen.getByLabelText("Current location")).toHaveTextContent(`/playbooks/${id}?q=needle`);
    },
  );

  it.each([
    ["/system/events", "Waiting for events…"],
    ["/system/gates", "No open gates."],
    ["/work/events", "Waiting for events…"],
    ["/work/gates", "No open gates."],
  ])("routes legacy %s to tasks and opens the requested activity tab", async (path, content) => {
    renderApp(path + "?q=needle");
    await screen.findByRole("heading", { name: "Command Center tasks" });
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/projects/p1/tasks?q=needle");
    expect(screen.getByLabelText("Current surface")).toHaveTextContent("drawer");
    expect(await screen.findByText(content)).toBeInTheDocument();
  });

  it("requires a project for bare Command Center routes and preserves filters", async () => {
    renderApp("/projects/p1/tasks?q=needle&status=READY");
    await screen.findByRole("heading", { name: "Command Center tasks" });
    expect(screen.getByLabelText("Current location")).toHaveTextContent(
      "/projects/p1/tasks?q=needle&status=READY",
    );
    expect(screen.queryByRole("link", { name: "All projects" })).not.toBeInTheDocument();
  });

  it("remembers the last valid project when returning to Command Center", async () => {
    renderApp("/projects/p2/graph");
    await screen.findByRole("heading", { name: "Command Center graph" });
    await userEvent.click(screen.getByRole("link", { name: "Settings" }));
    await screen.findByRole("heading", { name: "Settings playbooks" });
    await userEvent.click(screen.getByRole("link", { name: "Command Center" }));
    await screen.findByRole("heading", { name: "Command Center graph" });
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/projects/p2/graph");
  });

  it("collapses the project list directly beneath Command Center", async () => {
    renderApp("/projects/p1/graph");
    await screen.findByRole("heading", { name: "Command Center graph" });
    const projectsToggle = screen.getByRole("button", { name: "Projects" });
    expect(projectsToggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("link", { name: "First project" })).toBeInTheDocument();
    await userEvent.click(projectsToggle);
    expect(projectsToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("link", { name: "First project" })).not.toBeInTheDocument();
  });

  it("shows an add-project empty state when no projects are available", async () => {
    projects.splice(0);
    renderApp("/command-center");
    expect(await screen.findByRole("heading", { name: "No projects yet" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add project" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "All projects" })).not.toBeInTheDocument();
  });

  it("keeps only Graph and Tasks in Command Center navigation", async () => {
    renderApp("/command-center/graph");
    await screen.findByRole("heading", { name: "Command Center graph" });
    expect(screen.getByRole("link", { name: "Graph" })).toHaveAttribute("href", "/projects/p1/graph");
    expect(screen.getByRole("link", { name: "Tasks" })).toHaveAttribute("href", "/projects/p1/tasks");
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
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/projects/p1/graph");
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
  });

  it("keeps resource tabs scoped and returns to the selected project tab", async () => {
    renderApp("/projects/p1/workspaces?q=keep");
    await screen.findByRole("heading", { name: "Project workspaces" });
    expect(screen.getByRole("link", { name: "Graph" })).toHaveAttribute("href", "/projects/p1/graph?q=keep");
    expect(screen.getByRole("link", { name: "Config" })).toHaveAttribute("href", "/projects/p1/config?q=keep");
    expect(screen.queryByRole("toolbar", { name: "Task controls" })).not.toBeInTheDocument();
    await userEvent.type(screen.getByRole("textbox", { name: "Resource draft" }), "First project's draft");
    await userEvent.click(screen.getByRole("link", { name: "Second project" }));
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/projects/p2/workspaces?q=keep");
    expect(screen.getByRole("textbox", { name: "Resource draft" })).toHaveValue("");
    await userEvent.click(screen.getByRole("link", { name: "Command Center" }));
    await screen.findByRole("heading", { name: "Project workspaces" });
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/projects/p2/workspaces?q=keep");
  });

  it.each([["/projects/p1", "/projects/p1/graph"], ["/command-center", "/projects/p1/graph"]])("preserves filters on the default Graph redirect from %s", async (path, expected) => {
    renderApp(path + "?q=keep&completed=1");
    await screen.findByRole("heading", { name: "Command Center graph" });
    expect(screen.getByLabelText("Current location")).toHaveTextContent(expected + "?q=keep&completed=1");
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
    await userEvent.click(screen.getByRole("link", { name: "Second project" }));
    expect(screen.getByLabelText("Current pane")).toHaveTextContent("contextual-settings");
    await userEvent.click(screen.getByRole("link", { name: "First project" }));
    await userEvent.click(screen.getByRole("button", { name: "Open task pane" }));
    await userEvent.click(screen.getByRole("link", { name: "Second project" }));
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
    await waitFor(() => expect(screen.getByLabelText("Current location")).toHaveTextContent("/projects/p1/graph"));
  });
});
