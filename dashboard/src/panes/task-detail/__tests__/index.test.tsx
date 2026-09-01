import { describe, expect, it, vi, beforeEach } from "vitest";
import { useState } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import TaskDetailPane from "../index";
import type { Task } from "../../../api/hooks";

const mockUseAgentFlock = vi.fn();
vi.mock("../../../api/agents", () => ({ useAgentFlock: () => mockUseAgentFlock() }));

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

const mockLegacyFetch = vi.fn();
vi.mock("../../../api/legacy-fetch", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../api/legacy-fetch")>()),
  legacyFetch: (...args: unknown[]) => mockLegacyFetch(...args),
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
  status: "WAITING_INPUT",
  priority: 2,
  assigned_agent: "agent-1",
  retry_count: 0,
  max_retries: 3,
  integration_mode: "pull_request",
  is_plan_subtask: false,
  task_type: "implementation",
  profile_id: "claude-sdk",
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
  mockUseAgentFlock.mockReturnValue({ data: [] });
  mockUseTask.mockReset();
  mockUseGates.mockReset();
  mockUseResolveGate.mockReset();
  mockOpen.mockReset();
  mockClose.mockReset();
  mockNavigate.mockReset();
  mockUseGates.mockReturnValue({ data: [] });
  mockUseResolveGate.mockReturnValue({ mutate: vi.fn() });
  mockLegacyFetch.mockReset().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ success: true, attachments: [], attachment: {} }),
  });
});

const attachmentUploads = () =>
  mockLegacyFetch.mock.calls.filter(
    ([url, init]) =>
      typeof url === "string" &&
      url.includes("/attachments") &&
      (init as RequestInit | undefined)?.method === "POST",
  );

describe("TaskDetailPane — header, description, actions", () => {
  it("jumps from a selected task to its current worker and closes the pane", () => {
    mockUseTask.mockReturnValue({ data: { ...fixtureTask, status: "IN_PROGRESS" } });
    mockUseAgentFlock.mockReturnValue({ data: [{
      id: "agent-1", name: "Solar Eagle", current_task_id: "t1", current_project_id: "demo",
      session_id: "session-1", session_state: "running", session_provider: "tmux",
    }] });
    renderWithRouter(<TaskDetailPane {...noopProps()} />);
    screen.getByRole("button", { name: "Open agent terminal" }).click();
    expect(mockNavigate).toHaveBeenCalledWith(
      { pathname: "/agents", search: "agent=agent-1" },
      { state: { agentSelection: "replace" } },
    );
    expect(mockClose).toHaveBeenCalledOnce();
  });

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

  it("shows the Answer Question action for a WAITING_INPUT task", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    renderWithRouter(<TaskDetailPane {...noopProps()} />);
    expect(screen.getByRole("button", { name: /answer question/i })).toBeInTheDocument();
  });
});

describe("TaskDetailPane — screenshot attachments", () => {
  const pngFile = () =>
    new File([new Uint8Array([137, 80, 78, 71])], "shot.png", { type: "image/png" });

  it("uploads a file dropped anywhere on the pane", async () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const { container } = renderWithRouter(<TaskDetailPane {...noopProps()} />);
    const paneRoot = container.firstElementChild as HTMLElement;
    fireEvent.drop(paneRoot, {
      dataTransfer: { files: [pngFile()], types: ["Files"] },
    });
    await waitFor(() => expect(attachmentUploads()).toHaveLength(1));
    expect(attachmentUploads()[0]?.[0]).toBe("/api/tasks/t1/attachments");
  });

  it("uploads a screenshot pasted while the pane is open", async () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    renderWithRouter(<TaskDetailPane {...noopProps()} />);
    const event = new Event("paste", { bubbles: true }) as ClipboardEvent;
    Object.defineProperty(event, "clipboardData", { value: { files: [pngFile()] } });
    await act(async () => {
      document.dispatchEvent(event);
    });
    await waitFor(() => expect(attachmentUploads()).toHaveLength(1));
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

  it("renders the structured completion story when present", () => {
    mockUseTask.mockReturnValue({
      data: {
        ...fixtureTask,
        status: "COMPLETED",
        completion: {
          id: "completion-1",
          task_id: "t1",
          outcome: "pass",
          work_outcome: "shipped",
          changes: "Added durable completion records.",
          verification: "Focused backend and dashboard tests passed.",
          tests: ["pytest tests/test_surface_commands.py -q"],
          commands: ["ruff check src tests"],
          branch: "feature/completion",
          commits: ["abc123"],
          pr_url: "https://github.com/org/repo/pull/17",
          summary: "Completion story stored.",
          notes: "Ready for review.",
          completed_at: 1234.5,
        },
      },
      isLoading: false,
      isError: false,
    });

    renderWithRouter(<TaskDetailPane {...noopProps()} />);

    expect(screen.getByRole("heading", { name: "Completion" })).toBeInTheDocument();
    expect(screen.getByText("Added durable completion records.")).toBeInTheDocument();
    expect(screen.getByText("Focused backend and dashboard tests passed.")).toBeInTheDocument();
    expect(screen.getByText("pytest tests/test_surface_commands.py -q")).toBeInTheDocument();
    expect(screen.getByText("ruff check src tests")).toBeInTheDocument();
    expect(screen.getByText("abc123")).toBeInTheDocument();
    expect(screen.getByText("Ready for review.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /pull\/17/i })).toHaveAttribute(
      "href",
      "https://github.com/org/repo/pull/17",
    );
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
