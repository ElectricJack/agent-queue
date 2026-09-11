import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BrowserRouter, MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import App from "../../App";
import { ShellPaneProvider, useShellPaneStore } from "../../panes/store";
import type { PaneEntry } from "../../panes/registry";
import TaskAgentTerminalButton from "../../components/TaskAgentTerminalButton";
import type { Task } from "../../api/hooks";
import ShellPaneHost from "../ShellPaneHost";
import { BrowserHistoryContext } from "../historyState";
import { viewTitle } from "../viewTitle";
import { SHELL_PREFERENCE_DEFAULTS, type ShellPrefs } from "../useShellPreferences";
import {
  createFakeDashboardStateServer,
  TestDashboardState,
  testQueryClient,
  type FakeDashboardStateServer,
} from "../../testUtils/dashboardState";

const projects = [{ id: "p1", name: "First project" }, { id: "p2", name: "Second project" }];
const TASK = { id: "task-p1", project_id: "p1", assigned_agent: "worker-1" } as unknown as Task;

vi.mock("../../panes/registry", () => ({ PANE_REGISTRY: {
  "task-detail": { manifest: { name: "Task" } },
  "session-peek": { manifest: { name: "Session Peek" } },
  "playbook-run-inspector": { manifest: { name: "Playbook Run" } },
} }));
vi.mock("../../api/hooks", () => ({
  useProjects: () => ({ data: projects }),
  useAllOpenGates: () => ({ data: [] }),
  useResolveGate: () => ({ mutate: vi.fn() }),
  useProject: (id: string) => ({ data: { ...projects.find((p) => p.id === id), id, paused: false } }),
  usePauseProject: () => ({ mutate: vi.fn(), isPending: false }),
  useResumeProject: () => ({ mutate: vi.fn(), isPending: false }),
  useDeleteProject: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock("../../api/agents", () => ({
  useAgentFlock: () => ({
    isError: false,
    data: [{
      id: "worker-1", name: "worker-1", current_task_id: "task-p1", current_project_id: "p1",
      session_id: "session-1", session_provider: "tmux", session_state: "running",
    }],
  }),
}));
vi.mock("../../ws/useEventStream", () => ({ useEventStream: () => {} }));
vi.mock("../../panes/agentPush", () => ({ useAgentPushBridge: () => {} }));
vi.mock("../AgentFlock", () => ({ default: () => null }));
vi.mock("../RightSurface", () => ({ default: () => <PaneProbe /> }));
vi.mock("../palette/Palette", () => ({ Palette: () => null }));
vi.mock("../hotkeys/CheatSheetModal", () => ({ default: () => null }));
vi.mock("../../pages/command-center/Graph", () => ({ default: () => <h1>Command Center graph</h1> }));
vi.mock("../../pages/command-center/Tasks", () => ({ default: () => <h1>Command Center tasks</h1> }));
vi.mock("../../pages/command-center/TaskWorkspace", () => ({
  TaskWorkspaceProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
vi.mock("../../pages/command-center/TaskToolbar", () => ({ default: () => null }));
vi.mock("../../pages/agents/AgentWorkspace", () => ({ default: () => <h1>Agent flock</h1> }));
vi.mock("../../pages/SessionDetail", () => ({ default: () => <h1>Session detail</h1> }));
vi.mock("../../pages/PlaybookDetail", () => ({ default: () => <h1>Playbook detail</h1> }));

/** Stands in for the right surface: shows the pane and offers the steps a user takes. */
function PaneProbe() {
  const pane = useShellPaneStore();
  const navigate = useNavigate();
  const open = pane.state.kind === "open" ? pane.state : null;
  return (
    <>
      <output aria-label="Current pane">{open ? `${open.view} ${JSON.stringify(open.args)}` : "closed"}</output>
      <button onClick={() => pane.open("task-detail", { taskId: "task-p1" })}>Open task pane</button>
      <button onClick={() => pane.open("session-peek", { sessionId: "session-1" })}>Open session pane</button>
      <button onClick={() => pane.open("playbook-run-inspector", { runId: "run-1" })}>Open run pane</button>
      <button onClick={() => navigate("/agents?agent=agent-a")}>Show agent a</button>
      <button onClick={() => navigate("/sessions/session-1")}>Open session page</button>
      {/* Wired exactly as the task-detail pane wires it. */}
      {open?.view === "task-detail" && <TaskAgentTerminalButton task={TASK} onOpen={pane.close} />}
    </>
  );
}

function Location() {
  const location = useLocation();
  return <output aria-label="Current location">{location.pathname}{location.search}</output>;
}

let server: FakeDashboardStateServer;

beforeEach(() => {
  server = createFakeDashboardStateServer();
});
afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/");
});

function renderApp(path: string) {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <TestDashboardState server={server}>
        <MemoryRouter initialEntries={[path]}><App /><Location /></MemoryRouter>
      </TestDashboardState>
    </QueryClientProvider>,
  );
}

/** The real app's wiring: the router is the window's, and entries record their view there. */
function renderBrowserApp() {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <TestDashboardState server={server}>
        <BrowserRouter>
          <BrowserHistoryContext.Provider value={window.history}>
            <App /><Location />
          </BrowserHistoryContext.Provider>
        </BrowserRouter>
      </TestDashboardState>
    </QueryClientProvider>,
  );
}

const pane = () => screen.getByLabelText("Current pane");
const where = () => screen.getByLabelText("Current location");
const backButton = () => screen.getByRole("button", { name: "Back" });
const forwardButton = () => screen.getByRole("button", { name: "Forward" });
const TASK_PANE = 'task-detail {"taskId":"task-p1"}';
const SESSION_PANE = 'session-peek {"sessionId":"session-1"}';
const RUN_PANE = 'playbook-run-inspector {"runId":"run-1"}';

describe("dashboard back / forward", () => {
  it("returns from an agent terminal to the task pane it was opened from, and Forward reopens it", async () => {
    const user = userEvent.setup();
    renderApp("/projects/p1/tasks");
    await screen.findByRole("heading", { name: "Command Center tasks" });
    expect(backButton()).toBeDisabled();
    expect(forwardButton()).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Open task pane" }));
    await user.click(await screen.findByRole("button", { name: "Open agent terminal" }));
    await screen.findByRole("heading", { name: "Agent flock" });
    expect(where()).toHaveTextContent("/agents?agent=worker-1");
    expect(pane()).toHaveTextContent("closed");
    expect(backButton()).toHaveAttribute("title", "Back to First project · Tasks — Task task-p1 (Alt+←)");

    await user.click(backButton());
    await screen.findByRole("heading", { name: "Command Center tasks" });
    expect(where()).toHaveTextContent("/projects/p1/tasks");
    expect(pane()).toHaveTextContent(TASK_PANE);
    expect(forwardButton()).toHaveAttribute("title", "Forward to Agents: worker-1 (Alt+→)");

    await user.click(forwardButton());
    await screen.findByRole("heading", { name: "Agent flock" });
    expect(where()).toHaveTextContent("/agents?agent=worker-1");
    expect(pane()).toHaveTextContent("closed");
    expect(forwardButton()).toBeDisabled();
  });

  it("makes each pane change on one route a step, with the buttons disabled at either end", async () => {
    const user = userEvent.setup();
    renderApp("/projects/p1/graph");
    await screen.findByRole("heading", { name: "Command Center graph" });
    await user.click(screen.getByRole("button", { name: "Open task pane" }));
    await user.click(screen.getByRole("button", { name: "Open session pane" }));
    expect(backButton()).toHaveAttribute("title", "Back to First project · Graph — Task task-p1 (Alt+←)");

    await user.click(backButton());
    expect(pane()).toHaveTextContent(TASK_PANE);
    await user.click(backButton());
    expect(pane()).toHaveTextContent("closed");
    expect(backButton()).toBeDisabled();
    expect(where()).toHaveTextContent("/projects/p1/graph");

    await user.click(forwardButton());
    expect(pane()).toHaveTextContent(TASK_PANE);
    await user.click(forwardButton());
    expect(pane()).toHaveTextContent(SESSION_PANE);
    expect(forwardButton()).toBeDisabled();
  });

  it("goes back and forward with Alt+Left / Alt+Right and Cmd+[ / Cmd+]", async () => {
    const user = userEvent.setup();
    renderApp("/projects/p1/graph");
    await screen.findByRole("heading", { name: "Command Center graph" });
    await user.click(screen.getByRole("button", { name: "Open task pane" }));
    await user.click(screen.getByRole("button", { name: "Open session pane" }));

    await user.keyboard("{Alt>}{ArrowLeft}{/Alt}");
    expect(pane()).toHaveTextContent(TASK_PANE);
    await user.keyboard("{Meta>}[BracketLeft]{/Meta}");
    expect(pane()).toHaveTextContent("closed");
    await user.keyboard("{Alt>}{ArrowRight}{/Alt}");
    expect(pane()).toHaveTextContent(TASK_PANE);
    await user.keyboard("{Meta>}[BracketRight]{/Meta}");
    expect(pane()).toHaveTextContent(SESSION_PANE);
    // Neither modified bracket reached the bare "[" / "]" surface toggles.
    expect(screen.getByLabelText("Current pane")).not.toHaveTextContent("__stub-smoke");
  });

  it("walks back through project, pane, agent and session views in order", async () => {
    const user = userEvent.setup();
    renderApp("/projects/p1/graph");
    await screen.findByRole("heading", { name: "Command Center graph" });
    await user.click(screen.getByRole("button", { name: "Open run pane" }));
    await user.click(screen.getByRole("link", { name: "Second project" }));
    await waitFor(() => expect(where()).toHaveTextContent("/projects/p2/graph"));
    await user.click(screen.getByRole("button", { name: "Open session pane" }));
    await user.click(screen.getByRole("button", { name: "Show agent a" }));
    await screen.findByRole("heading", { name: "Agent flock" });
    await user.click(screen.getByRole("button", { name: "Open session page" }));
    await screen.findByRole("heading", { name: "Session detail" });
    expect(backButton()).toHaveAttribute("title", "Back to Agents: agent-a — Session Peek session-1 (Alt+←)");

    await user.click(backButton());
    await screen.findByRole("heading", { name: "Agent flock" });
    expect(where()).toHaveTextContent("/agents?agent=agent-a");
    expect(backButton()).toHaveAttribute("title", "Back to Second project · Graph — Session Peek session-1 (Alt+←)");

    await user.click(backButton());
    await screen.findByRole("heading", { name: "Command Center graph" });
    expect(where()).toHaveTextContent("/projects/p2/graph");
    expect(pane()).toHaveTextContent(SESSION_PANE);

    await user.click(backButton());
    expect(where()).toHaveTextContent("/projects/p2/graph");
    expect(pane()).toHaveTextContent(RUN_PANE);

    await user.click(backButton());
    await waitFor(() => expect(where()).toHaveTextContent("/projects/p1/graph"));
    expect(pane()).toHaveTextContent(RUN_PANE);

    await user.click(backButton());
    expect(pane()).toHaveTextContent("closed");
    expect(backButton()).toBeDisabled();
  });

  it("brings back a task pane that a project switch closed", async () => {
    const user = userEvent.setup();
    renderApp("/projects/p1/tasks");
    await screen.findByRole("heading", { name: "Command Center tasks" });
    await user.click(screen.getByRole("button", { name: "Open task pane" }));
    await user.click(screen.getByRole("link", { name: "Second project" }));
    await waitFor(() => expect(where()).toHaveTextContent("/projects/p2/tasks"));
    expect(pane()).toHaveTextContent("closed");

    await user.click(backButton());
    await waitFor(() => expect(where()).toHaveTextContent("/projects/p1/tasks"));
    expect(pane()).toHaveTextContent(TASK_PANE);

    await user.click(forwardButton());
    await waitFor(() => expect(where()).toHaveTextContent("/projects/p2/tasks"));
    expect(pane()).toHaveTextContent("closed");
  });

  it("does not add a step for the pane the server restores on load", async () => {
    const user = userEvent.setup();
    const prefs: ShellPrefs = {
      ...SHELL_PREFERENCE_DEFAULTS,
      right_surface: {
        ...SHELL_PREFERENCE_DEFAULTS.right_surface,
        kind: "pane",
        pane: { view: "task-detail", args: { taskId: "task-p1" } },
      },
    };
    server.write("shell_preferences", prefs);
    renderApp("/projects/p1/tasks");
    await waitFor(() => expect(pane()).toHaveTextContent(TASK_PANE));
    expect(backButton()).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Open session pane" }));
    await user.click(backButton());
    expect(pane()).toHaveTextContent(TASK_PANE);
    expect(backButton()).toBeDisabled();
  });

  it("keeps each entry's pane in the browser's history, so the browser's Back restores it after a reload", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", "/projects/p1/tasks");
    const first = renderBrowserApp();
    await screen.findByRole("heading", { name: "Command Center tasks" });
    await user.click(screen.getByRole("button", { name: "Open task pane" }));
    await user.click(await screen.findByRole("button", { name: "Open agent terminal" }));
    await screen.findByRole("heading", { name: "Agent flock" });
    first.unmount();

    // A reload: the page's memory is gone, the browser's history is not.
    renderBrowserApp();
    await screen.findByRole("heading", { name: "Agent flock" });
    await waitFor(() => expect(backButton()).toBeEnabled());
    expect(backButton()).toHaveAttribute("title", "Back to the previous view (Alt+←)");

    // The browser's own Back (toolbar, mouse button 4, swipe) is the same POP.
    window.history.back();
    await screen.findByRole("heading", { name: "Command Center tasks" });
    await waitFor(() => expect(pane()).toHaveTextContent(TASK_PANE));
    expect(where()).toHaveTextContent("/projects/p1/tasks");
    expect(forwardButton()).toBeEnabled();
  });
});

describe("pane scroll", () => {
  it("returns to where the pane was scrolled when Back revisits the entry", async () => {
    const user = userEvent.setup();
    const registry = {
      tall: {
        Component: () => <p>Tall pane</p>,
        manifest: { id: "tall", name: "Tall", description: "", icon: () => null },
      },
    } as unknown as Record<string, PaneEntry>;
    function Controls() {
      const store = useShellPaneStore();
      const navigate = useNavigate();
      return (
        <>
          <button onClick={() => store.open("tall", {})}>Open tall</button>
          <button onClick={() => navigate("/elsewhere")}>Go elsewhere</button>
          <button onClick={() => navigate(-1)}>Go back</button>
        </>
      );
    }
    render(
      <QueryClientProvider client={testQueryClient()}>
        <TestDashboardState server={server}>
          <MemoryRouter initialEntries={["/here"]}>
            <ShellPaneProvider registryOverride={registry}>
              <Controls />
              <ShellPaneHost />
            </ShellPaneProvider>
          </MemoryRouter>
        </TestDashboardState>
      </QueryClientProvider>,
    );
    await user.click(screen.getByRole("button", { name: "Open tall" }));
    const scroller = await screen.findByTestId("shell-pane-scroller");
    Object.defineProperty(scroller, "scrollTop", { configurable: true, writable: true, value: 120 });
    fireEvent.scroll(scroller);

    await user.click(screen.getByRole("button", { name: "Go elsewhere" }));
    scroller.scrollTop = 0;
    fireEvent.scroll(scroller);
    await user.click(screen.getByRole("button", { name: "Go back" }));
    expect(screen.getByTestId("shell-pane-scroller").scrollTop).toBe(120);
  });
});

describe("viewTitle", () => {
  const names = {
    project: (id: string) => (id === "p1" ? "First project" : undefined),
    pane: (view: string) => (view === "task-detail" ? "Task" : undefined),
  };
  it.each([
    [{ pathname: "/projects/p1/tasks", search: "", pane: null }, "First project · Tasks"],
    [{ pathname: "/projects/p9/graph", search: "", pane: null }, "p9 · Graph"],
    [{ pathname: "/agents", search: "?agent=a&agent=pool%3Aworker-standard%40inst", pane: null }, "Agents: a, pool worker-standard"],
    [{ pathname: "/agents", search: "", pane: null }, "Agent flock"],
    [{ pathname: "/settings/project-roots", search: "", pane: null }, "Settings · Project roots"],
    [{ pathname: "/playbooks/demo%2Freview", search: "", pane: null }, "Playbook demo/review"],
    [{ pathname: "/sessions/s1", search: "", pane: { view: "task-detail", args: { taskId: "t1" } } }, "Session s1 — Task t1"],
    [{ pathname: "/metrics", search: "", pane: { view: "file-browser", args: { workspaceId: "w1", path: "" } } }, "Metrics — file-browser w1"],
  ])("titles %j as %s", (view, expected) => {
    expect(viewTitle(view, names)).toBe(expected);
  });
});
