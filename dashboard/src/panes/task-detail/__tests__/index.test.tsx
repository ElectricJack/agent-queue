import { describe, expect, it, vi, beforeEach } from "vitest";
import { useState } from "react";
import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import TaskDetailPane from "../index";
import type { Task } from "../../../api/hooks";

const mockUseTask = vi.fn();
const mockUseGates = vi.fn();
const mockUseResolveGate = vi.fn();

vi.mock("../../../api/hooks", async () => {
  const actual = await vi.importActual<typeof import("../../../api/hooks")>(
    "../../../api/hooks",
  );
  return {
    ...actual,
    useTask: (...args: unknown[]) => mockUseTask(...args),
    useGates: (...args: unknown[]) => mockUseGates(...args),
    useResolveGate: (...args: unknown[]) => mockUseResolveGate(...args),
  };
});

const mockOpen = vi.fn();
const mockClose = vi.fn();
vi.mock("../../store", () => ({
  useShellPaneStore: () => ({
    open: mockOpen,
    close: mockClose,
    state: { kind: "closed" },
    setArgs: vi.fn(),
    setWidth: vi.fn(),
    registry: {},
  }),
}));

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

const fixtureTask: Task = {
  id: "t1",
  project_id: "demo",
  title: "Fix the thing",
  description: "",
  status: "AWAITING_APPROVAL",
  priority: 2,
  assigned_agent: "agent-1",
  retry_count: 0,
  max_retries: 3,
  requires_approval: true,
  is_plan_subtask: false,
  task_type: "implementation",
  profile_id: "claude-sdk",
  auto_approve_plan: false,
  skip_verification: false,
  pr_url: null,
  depends_on: [],
  blocks: [],
  subtasks: [],
  created_at: 1755878400,
  updated_at: 1755878400,
};

function noopProps() {
  return {
    args: { taskId: "t1" },
    close: mockClose,
    setArgs: vi.fn(),
    setToolbar: vi.fn(),
    setShortcuts: vi.fn(),
  };
}

function renderWithRouter(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockUseTask.mockReset();
  mockUseGates.mockReset();
  mockUseResolveGate.mockReset();
  mockOpen.mockReset();
  mockClose.mockReset();
  mockNavigate.mockReset();
  mockUseGates.mockReturnValue({ data: [] });
  mockUseResolveGate.mockReturnValue({ mutate: vi.fn() });
});

describe("TaskDetailPane — header, description, actions", () => {
  it("renders title, status badge, and metadata badges without crashing", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    renderWithRouter(<TaskDetailPane {...noopProps()} />);
    expect(screen.getByText("Fix the thing")).toBeInTheDocument();
    expect(screen.getByText("t1")).toBeInTheDocument();
    expect(screen.getByText("demo")).toBeInTheDocument();
    expect(screen.getByText("P2")).toBeInTheDocument();
  });

  it("shows Loading… title while isLoading with no cached task", () => {
    mockUseTask.mockReturnValue({ data: undefined, isLoading: true, isError: false });
    renderWithRouter(<TaskDetailPane {...noopProps()} />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("renders the description block only when non-empty", () => {
    mockUseTask.mockReturnValue({
      data: { ...fixtureTask, description: "Some details here" },
      isLoading: false,
      isError: false,
    });
    renderWithRouter(<TaskDetailPane {...noopProps()} />);
    expect(screen.getByText("Some details here")).toBeInTheDocument();
  });

  it("shows the Approve action for an AWAITING_APPROVAL task", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    renderWithRouter(<TaskDetailPane {...noopProps()} />);
    expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument();
  });
});

describe("TaskDetailPane — metadata, PR link, relationships", () => {
  it("renders the metadata grid fields", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    renderWithRouter(<TaskDetailPane {...noopProps()} />);
    expect(screen.getByText("agent-1")).toBeInTheDocument();
    expect(screen.getByText("0 / 3")).toBeInTheDocument();
  });

  it("renders the PR link only when pr_url is set", () => {
    mockUseTask.mockReturnValue({
      data: { ...fixtureTask, pr_url: "https://github.com/org/repo/pull/1" },
      isLoading: false,
      isError: false,
    });
    renderWithRouter(<TaskDetailPane {...noopProps()} />);
    expect(screen.getByRole("link", { name: /pull\/1/i })).toHaveAttribute(
      "href",
      "https://github.com/org/repo/pull/1",
    );
  });

  it("does not render a Pull request section when pr_url is unset", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    renderWithRouter(<TaskDetailPane {...noopProps()} />);
    expect(screen.queryByText("Pull request")).not.toBeInTheDocument();
  });

  it("clicking a subtask row calls useShellPaneStore().open with the ref's taskId", () => {
    mockUseTask.mockReturnValue({
      data: { ...fixtureTask, subtasks: [{ id: "t2", title: "Sub one", status: "COMPLETED" }] },
      isLoading: false,
      isError: false,
    });
    renderWithRouter(<TaskDetailPane {...noopProps()} />);
    screen.getByText("Sub one").click();
    expect(mockOpen).toHaveBeenCalledWith("task-detail", { taskId: "t2" });
  });
});

describe("TaskDetailPane — gates", () => {
  it("shows only gates whose task_ids include this task", () => {
    mockUseTask.mockReturnValue({
      data: { ...fixtureTask, status: "IN_PROGRESS" },
      isLoading: false,
      isError: false,
    });
    mockUseGates.mockReturnValue({
      data: [
        { id: "g1", gate_type: "human", status: "open", task_ids: ["t1"], project_id: "demo", title: "g1" },
        { id: "g2", gate_type: "human", status: "open", task_ids: ["other"], project_id: "demo", title: "g2" },
      ],
    });
    renderWithRouter(<TaskDetailPane {...noopProps()} />);
    expect(screen.getByText(/human/)).toBeInTheDocument();
    expect(screen.getAllByText(/human/)).toHaveLength(1);
  });

  it("Approve calls useResolveGate().mutate with the gate id", () => {
    const mutate = vi.fn();
    mockUseTask.mockReturnValue({
      data: { ...fixtureTask, status: "IN_PROGRESS" },
      isLoading: false,
      isError: false,
    });
    mockUseGates.mockReturnValue({
      data: [
        { id: "g1", gate_type: "human", status: "open", task_ids: ["t1"], project_id: "demo", title: "g1" },
      ],
    });
    mockUseResolveGate.mockReturnValue({ mutate });
    renderWithRouter(<TaskDetailPane {...noopProps()} />);
    screen.getByRole("button", { name: /approve/i, hidden: false }).click();
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ gate_id: "g1", resolution: "approve" }),
    );
  });

  it("omits the Gates section entirely when there are no matching gates", () => {
    mockUseTask.mockReturnValue({
      data: { ...fixtureTask, status: "IN_PROGRESS" },
      isLoading: false,
      isError: false,
    });
    mockUseGates.mockReturnValue({ data: [] });
    renderWithRouter(<TaskDetailPane {...noopProps()} />);
    expect(screen.queryByText("Gates")).not.toBeInTheDocument();
  });
});

describe("TaskDetailPane — toolbar and shortcuts", () => {
  it("registers Open full detail page and Copy id toolbar actions", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const setToolbar = vi.fn();
    renderWithRouter(<TaskDetailPane {...noopProps()} setToolbar={setToolbar} />);
    const lastCall = setToolbar.mock.calls[setToolbar.mock.calls.length - 1]?.[0];
    expect(lastCall.map((a: { id: string }) => a.id)).toEqual(["open-full", "copy-id"]);
  });

  it("Open full detail page navigates to /tasks/:id", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const setToolbar = vi.fn();
    renderWithRouter(<TaskDetailPane {...noopProps()} setToolbar={setToolbar} />);
    const actions = setToolbar.mock.calls[setToolbar.mock.calls.length - 1]?.[0];
    actions.find((a: { id: string }) => a.id === "open-full").onClick();
    expect(mockClose).toHaveBeenCalledOnce();
    expect(mockNavigate).toHaveBeenCalledWith("/tasks/t1", { state: { from: "/" } });
  });

  it("Copy id writes the task id to the clipboard", () => {
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const setToolbar = vi.fn();
    renderWithRouter(<TaskDetailPane {...noopProps()} setToolbar={setToolbar} />);
    const actions = setToolbar.mock.calls[setToolbar.mock.calls.length - 1]?.[0];
    actions.find((a: { id: string }) => a.id === "copy-id").onClick();
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith("t1");
  });

  it("registers exactly the o/c/r/. shortcut keys", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const setShortcuts = vi.fn();
    renderWithRouter(<TaskDetailPane {...noopProps()} setShortcuts={setShortcuts} />);
    const lastCall = setShortcuts.mock.calls[setShortcuts.mock.calls.length - 1]?.[0];
    expect(lastCall.map((b: { key: string }) => b.key)).toEqual(["o", "c", "r", "."]);
  });

  it("o shortcut navigates to the full detail page", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const setShortcuts = vi.fn();
    renderWithRouter(<TaskDetailPane {...noopProps()} setShortcuts={setShortcuts} />);
    const bindings = setShortcuts.mock.calls[setShortcuts.mock.calls.length - 1]?.[0];
    bindings.find((b: { key: string }) => b.key === "o").onFire();
    expect(mockClose).toHaveBeenCalledOnce();
    expect(mockNavigate).toHaveBeenCalledWith("/tasks/t1", { state: { from: "/" } });
  });

  it("c shortcut opens the close/delete confirmation", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const setShortcuts = vi.fn();
    renderWithRouter(<TaskDetailPane {...noopProps()} setShortcuts={setShortcuts} />);
    const bindings = setShortcuts.mock.calls[setShortcuts.mock.calls.length - 1]?.[0];
    act(() => {
      bindings.find((b: { key: string }) => b.key === "c").onFire();
    });
    expect(screen.getByText(/cannot be undone/i)).toBeInTheDocument();
  });

  it(". shortcut opens the more-actions dropdown", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const setShortcuts = vi.fn();
    renderWithRouter(<TaskDetailPane {...noopProps()} setShortcuts={setShortcuts} />);
    const bindings = setShortcuts.mock.calls[setShortcuts.mock.calls.length - 1]?.[0];
    act(() => {
      bindings.find((b: { key: string }) => b.key === ".").onFire();
    });
    expect(screen.getByRole("menu")).toBeInTheDocument();
  });
});

describe("TaskDetailPane — not found and loading", () => {
  it("renders Task not found on isError, keeping Open full detail page usable", () => {
    mockUseTask.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    const setToolbar = vi.fn();
    renderWithRouter(<TaskDetailPane {...noopProps()} setToolbar={setToolbar} />);
    expect(screen.getByText("Task not found.")).toBeInTheDocument();
    const actions = setToolbar.mock.calls[setToolbar.mock.calls.length - 1]?.[0];
    expect(actions.find((a: { id: string }) => a.id === "open-full")).toBeDefined();
  });

  it("renders Loading… without crashing when isLoading with no cached data", () => {
    mockUseTask.mockReturnValue({ data: undefined, isLoading: true, isError: false });
    expect(() => renderWithRouter(<TaskDetailPane {...noopProps()} />)).not.toThrow();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });
});

describe("TaskDetailPane — close affordance", () => {
  it("never calls close() itself — the shell header owns the × button", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const close = vi.fn();
    renderWithRouter(<TaskDetailPane {...noopProps()} close={close} />);
    expect(screen.queryByRole("button", { name: /^close$/i })).not.toBeInTheDocument();
    expect(close).not.toHaveBeenCalled();
  });
});

describe("TaskDetailPane — toolbar/shortcut publishing is effect-scoped", () => {
  // Regression: these were called in the render body with fresh array
  // literals. `setToolbar`/`setShortcuts` are `useState` setters owned by
  // ShellPaneHost, so a render-phase call with a never-equal value made the
  // parent re-render, which re-rendered the pane, which called them again —
  // an unbounded loop that locked up the browser tab whenever a task was
  // opened from the list or the graph.
  it("does not re-publish the toolbar when the pane re-renders", async () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const props = noopProps();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    // Stand-in for ShellPaneHost: owns state, so bumping it forces a genuine
    // re-render of the pane (a referentially-identical element would let
    // React bail out and hide the bug).
    function Harness() {
      const [n, setN] = useState(0);
      return (
        <>
          <button onClick={() => setN(n + 1)}>bump {n}</button>
          <TaskDetailPane {...props} />
        </>
      );
    }

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <Harness />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(props.setToolbar).toHaveBeenCalledTimes(1);
    expect(props.setShortcuts).toHaveBeenCalledTimes(1);

    await act(async () => {
      screen.getByRole("button", { name: /bump/i }).click();
    });

    expect(props.setToolbar).toHaveBeenCalledTimes(1);
    expect(props.setShortcuts).toHaveBeenCalledTimes(1);
  });

  it("clears the toolbar and shortcuts on unmount", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const props = noopProps();
    const { unmount } = renderWithRouter(<TaskDetailPane {...props} />);
    unmount();
    expect(props.setToolbar).toHaveBeenLastCalledWith([]);
    expect(props.setShortcuts).toHaveBeenLastCalledWith([]);
  });
});
