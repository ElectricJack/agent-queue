import { describe, it, expect, vi, beforeEach } from "vitest";
import { useState } from "react";
import { render, screen, fireEvent, act } from "@testing-library/react";
import PlaybookRunInspectorPane from "../index";
import * as hooks from "../../../api/hooks";

const mockNavigate = vi.fn();
const mockLocation = { pathname: "/projects/project-1/playbooks", search: "?scope=project", state: { from: "/command-center/tasks?owner=me" } };
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useLocation: () => mockLocation, useNavigate: () => mockNavigate };
});

let capturedOnEvent: ((event: unknown) => void) | undefined;
vi.mock("../../../ws/useEventStream", () => ({
  useEventStream: (opts: { onEvent?: (event: unknown) => void }) => {
    capturedOnEvent = opts.onEvent;
  },
}));

const mockInvalidateQueries = vi.fn();
vi.mock("@tanstack/react-query", async () => {
  const actual = await vi.importActual<typeof import("@tanstack/react-query")>(
    "@tanstack/react-query",
  );
  return {
    ...actual,
    useQueryClient: () => ({ invalidateQueries: mockInvalidateQueries }),
  };
});

vi.mock("../../../api/hooks", async () => {
  const actual = await vi.importActual<typeof hooks>("../../../api/hooks");
  return {
    ...actual,
    useInspectPlaybookRun: vi.fn(),
    useResumePlaybookRun: vi.fn(() => ({
      mutate: vi.fn(),
      mutateAsync: vi.fn(),
      isPending: false,
    })),
    useCancelPlaybookRun: vi.fn(() => ({
      mutate: vi.fn(),
      mutateAsync: vi.fn(),
      isPending: false,
    })),
  };
});

function baseProps(overrides: Partial<Parameters<typeof PlaybookRunInspectorPane>[0]> = {}) {
  return {
    args: { runId: "run-1" },
    close: vi.fn(),
    setArgs: vi.fn(),
    setToolbar: vi.fn(),
    setShortcuts: vi.fn(),
    ...overrides,
  };
}

function mockRun(overrides: Record<string, unknown> = {}) {
  return {
    run_id: "run-1",
    playbook_id: "demo-review",
    playbook_version: 3,
    status: "running",
    current_node: "classify",
    started_at: 1000,
    completed_at: null,
    tokens_used: 120,
    node_trace: [
      { node_id: "intake", started_at: 1000, completed_at: 1000.4, status: "completed" },
      { node_id: "classify", started_at: 1000.4, completed_at: null, status: "running" },
    ],
    node_count: 2,
    conversation_history: [{ role: "assistant", content: "Working on it." }],
    message_count: 1,
    trigger_event: {},
    ...overrides,
  };
}

describe("PlaybookRunInspectorPane", () => {
  beforeEach(() => {
    vi.mocked(hooks.useInspectPlaybookRun).mockReset();
    mockNavigate.mockClear();
    mockInvalidateQueries.mockClear();
    capturedOnEvent = undefined;
  });

  it("renders a loading state while isLoading", () => {
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps()} />);
    expect(screen.getByTestId("run-loading")).toBeInTheDocument();
  });

  it("renders node list rows from node_trace", () => {
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun(),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps()} />);
    expect(screen.getByText("intake")).toBeInTheDocument();
    expect(screen.getByText("classify")).toBeInTheDocument();
  });

  it("defaults selection to the last trace entry", () => {
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun(),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps()} />);
    const detail = screen.getByTestId("node-detail");
    expect(detail).toHaveTextContent("classify");
  });

  it("selecting a different row updates the detail panel", () => {
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun(),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps()} />);
    fireEvent.click(screen.getByText("intake"));
    expect(screen.getByTestId("node-detail")).toHaveTextContent("intake");
  });

  it("renders conversation history fallback when command/output are absent", () => {
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun(),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps()} />);
    expect(screen.getByText(/isn't available for this run yet/i)).toBeInTheDocument();
    expect(screen.getByText("Working on it.")).toBeInTheDocument();
  });

  it("renders node output directly when present", () => {
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun({
        node_trace: [
          {
            node_id: "intake",
            started_at: 1000,
            completed_at: 1000.4,
            status: "completed",
            output: "Parsed the request.",
          },
        ],
      }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps()} />);
    expect(screen.getByText("Parsed the request.")).toBeInTheDocument();
  });

  it("renders a not-found message when the run doesn't exist", () => {
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error("Playbook run 'run-1' not found"),
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps()} />);
    expect(screen.getByText(/run-1.*not found/i)).toBeInTheDocument();
  });

  it("renders a generic error with retry for other fetch errors", () => {
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error("network error"),
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps()} />);
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  // --- HITL banner ---

  it("renders the HITL banner when paused and not waiting_for_event", () => {
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun({ status: "paused" }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps()} />);
    expect(screen.getByText(/waiting on you/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reject/i })).toBeInTheDocument();
  });

  it("renders a waiting_for_event info line instead of the HITL banner", () => {
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun({ status: "paused", waiting_for_event: "task.completed" }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps()} />);
    expect(screen.getByText(/waiting for event: task\.completed/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
  });

  it("does not render the HITL banner for a running run", () => {
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun({ status: "running" }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps()} />);
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
  });

  it("does not render the HITL banner for a terminal run", () => {
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun({ status: "completed" }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps()} />);
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
  });

  it("clicking Approve calls resume with human_input='approve'", () => {
    const mutate = vi.fn();
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun({ status: "paused" }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);
    vi.mocked(hooks.useResumePlaybookRun).mockReturnValue({
      mutate,
      mutateAsync: vi.fn(),
      isPending: false,
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps()} />);
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(mutate).toHaveBeenCalledWith({ run_id: "run-1", human_input: "approve" });
  });

  it("clicking Reject calls resume with human_input='reject'", () => {
    const mutate = vi.fn();
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun({ status: "paused" }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);
    vi.mocked(hooks.useResumePlaybookRun).mockReturnValue({
      mutate,
      mutateAsync: vi.fn(),
      isPending: false,
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps()} />);
    fireEvent.click(screen.getByRole("button", { name: /reject/i }));
    expect(mutate).toHaveBeenCalledWith({ run_id: "run-1", human_input: "reject" });
  });

  it("typing free text and clicking Send calls resume with that text", () => {
    const mutate = vi.fn();
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun({ status: "paused" }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);
    vi.mocked(hooks.useResumePlaybookRun).mockReturnValue({
      mutate,
      mutateAsync: vi.fn(),
      isPending: false,
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps()} />);
    fireEvent.change(screen.getByPlaceholderText(/reply/i), {
      target: { value: "looks good, ship it" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^send$/i }));
    expect(mutate).toHaveBeenCalledWith({ run_id: "run-1", human_input: "looks good, ship it" });
  });

  // --- Toolbar + shortcuts ---

  it("registers Refresh, Cancel, and Open playbook page on the toolbar when running", () => {
    const setToolbar = vi.fn();
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun({ status: "running" }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps({ setToolbar })} />);

    const lastCall = setToolbar.mock.calls[setToolbar.mock.calls.length - 1]![0];
    const ids = lastCall.map((a: { id: string }) => a.id);
    expect(ids).toEqual(["refresh", "cancel", "open-playbook"]);
  });

  it("includes Resume on the toolbar only when paused", () => {
    const setToolbar = vi.fn();
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun({ status: "paused" }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps({ setToolbar })} />);

    const lastCall = setToolbar.mock.calls[setToolbar.mock.calls.length - 1]![0];
    const ids = lastCall.map((a: { id: string }) => a.id);
    expect(ids).toEqual(["refresh", "resume", "cancel", "open-playbook"]);
  });

  it("disables Cancel for a terminal run", () => {
    const setToolbar = vi.fn();
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun({ status: "completed" }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps({ setToolbar })} />);

    const lastCall = setToolbar.mock.calls[setToolbar.mock.calls.length - 1]![0];
    const cancelAction = lastCall.find((a: { id: string }) => a.id === "cancel");
    expect(cancelAction.disabled).toBe(true);
  });

  it("Open playbook page closes the pane and keeps the encoded source route", () => {
    const setToolbar = vi.fn();
    const close = vi.fn();
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun({ playbook_id: "demo/review" }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps({ setToolbar, close })} />);

    const lastCall = setToolbar.mock.calls[setToolbar.mock.calls.length - 1]![0];
    const openAction = lastCall.find((a: { id: string }) => a.id === "open-playbook");
    openAction.onClick();
    expect(close).toHaveBeenCalledOnce();
    expect(mockNavigate).toHaveBeenCalledWith("/playbooks/demo%2Freview", {
      state: { from: "/command-center/tasks?owner=me" },
    });
  });

  it("registers ArrowUp/ArrowDown/Enter/r/x shortcuts", () => {
    const setShortcuts = vi.fn();
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun(),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps({ setShortcuts })} />);

    const lastCall = setShortcuts.mock.calls[setShortcuts.mock.calls.length - 1]![0];
    const keys = lastCall.map((b: { key: string }) => b.key);
    expect(keys).toEqual(["ArrowUp", "ArrowDown", "Enter", "r", "x"]);
  });

  it("clicking Cancel opens a confirm modal; confirming calls the cancel mutation", async () => {
    const mutateAsync = vi.fn().mockResolvedValue(undefined);
    const setToolbar = vi.fn();
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun({ status: "running" }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);
    vi.mocked(hooks.useCancelPlaybookRun).mockReturnValue({
      mutate: vi.fn(),
      mutateAsync,
      isPending: false,
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps({ setToolbar })} />);
    const lastCall = setToolbar.mock.calls[setToolbar.mock.calls.length - 1]![0];
    const cancelAction = lastCall.find((a: { id: string }) => a.id === "cancel");
    act(() => {
      cancelAction!.onClick();
    });

    expect(screen.getByText(/cancel this run/i)).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^cancel run$/i }));
    });
    expect(mutateAsync).toHaveBeenCalledWith({ run_id: "run-1" });
  });

  // --- Live WS updates ---

  it("invalidates the run query on a matching playbook_run_paused event", () => {
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun(),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps()} />);
    capturedOnEvent?.({ event_type: "notify.playbook_run_paused", run_id: "run-1" });

    expect(mockInvalidateQueries).toHaveBeenCalledWith({
      queryKey: ["playbook-run", "run-1"],
    });
  });

  it("does not invalidate on an event for a different run_id", () => {
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun(),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps()} />);
    capturedOnEvent?.({ event_type: "notify.playbook_run_paused", run_id: "some-other-run" });

    expect(mockInvalidateQueries).not.toHaveBeenCalled();
  });

  it("ignores unrelated event types even for the matching run_id", () => {
    vi.mocked(hooks.useInspectPlaybookRun).mockReturnValue({
      data: mockRun(),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never);

    render(<PlaybookRunInspectorPane {...baseProps()} />);
    capturedOnEvent?.({ event_type: "notify.task_started", run_id: "run-1" });

    expect(mockInvalidateQueries).not.toHaveBeenCalled();
  });
});

// Regression: these panes published toolbar/shortcuts from the render body.
// `setToolbar`/`setShortcuts` are ShellPaneHost useState setters, so a
// render-phase call with a fresh array literal re-rendered the parent, which
// re-rendered the pane, which published again — an unbounded loop that froze
// the browser tab. See task-detail for the original report.
describe("playbook-run-inspector — publishing is effect-scoped", () => {
  it("does not re-publish the toolbar when the pane re-renders", () => {
    const props = baseProps();

    function Harness() {
      const [n, setN] = useState(0);
      return (
        <>
          <button onClick={() => setN(n + 1)}>bump {n}</button>
          <PlaybookRunInspectorPane {...props} />
        </>
      );
    }

    render(<Harness />);
    const toolbarCalls = (props.setToolbar as ReturnType<typeof vi.fn>).mock.calls.length;
    const shortcutCalls = (props.setShortcuts as ReturnType<typeof vi.fn>).mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: /bump/i }));

    expect((props.setToolbar as ReturnType<typeof vi.fn>).mock.calls.length).toBe(toolbarCalls);
    expect((props.setShortcuts as ReturnType<typeof vi.fn>).mock.calls.length).toBe(shortcutCalls);
  });
});
