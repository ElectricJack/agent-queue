import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, parsePath } from "react-router-dom";
import TaskDetail from "../../pages/TaskDetail";
import TaskDetailPane from "../../panes/task-detail";
import { client } from "../../api/client";

const fixture = vi.hoisted(() => ({
  id: "task-a", project_id: "demo", title: "Fix the queue", description: "Requirements",
  status: "BLOCKED", needs_attention: "Workspace acquisition failed: disk full",
}));
vi.mock("../../api/hooks", () => ({
  useTask: () => ({ data: fixture }),
  useProfiles: () => ({ data: [] }),
  useEditTask: () => ({}), useGates: () => ({ data: [] }),
  useResolveGate: () => ({}), useDeleteTask: () => ({}), useReopenWithFeedback: () => ({}),
  useTaskAttachments: () => ({ data: { success: true, attachments: [] } }),
  useUploadTaskAttachment: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteTaskAttachment: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("../TaskActions", () => ({ default: () => null }));
vi.mock("../TaskComments", () => ({ default: () => null }));
vi.mock("../../pages/task/TaskGraph", () => ({ default: () => null, TaskExplain: () => null }));
vi.mock("../../panes/store", () => ({ useShellPaneStore: () => ({ open: vi.fn(), close: vi.fn() }) }));

const attempts = [
  { id: "attempt-2", session_id: "worker-a", task_id: "task-a", agent_id: "agent-2", agent_name: "New Worker", model: "gpt-new", intelligence_class: "advanced", harness: "codex", provider: "tmux", state: "running", work_dir: "/work/task", started_at: 1755878500, session_started_at: 1755878500, ended_at: null, end_reason: null, outcome: null, session_key: "conversation-2" },
  { id: "attempt-1", session_id: "worker-a", task_id: "task-a", agent_id: "agent-1", agent_name: "Original Worker", model: "gpt-original", intelligence_class: "standard", harness: "codex", provider: "tmux", state: "stopped", work_dir: "/work/task", started_at: 1755878400, session_started_at: 1755878400, ended_at: 1755878450, end_reason: "Process exited with code 1", outcome: "failed", session_key: "conversation-1" },
];
let queryClient: QueryClient;
let request: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  request = vi.spyOn(client, "get").mockResolvedValue({ data: { task_id: "task-a", sessions: attempts } } as never);
});
afterEach(() => { cleanup(); queryClient.clear(); vi.restoreAllMocks(); });
const noop = () => {};
function LocationProbe() {
  const location = useLocation();
  return <><p>{location.pathname + location.search} from {(location.state as { from?: string })?.from}</p><output aria-label="Task pane return">{JSON.stringify((location.state as { taskPane?: unknown })?.taskPane ?? null)}</output></>;
}
const origins = { full: "/tasks/task-a?tab=details#history", drawer: "/work/tasks?task=task-a&status=BLOCKED#board" };
function mount(surface: "full" | "drawer" = "full") {
  return render(<QueryClientProvider client={queryClient}>
    <MemoryRouter initialEntries={[{ ...parsePath(origins[surface]), state: { from: "/unrelated-previous-page" } }]}>
      <Routes>
        <Route path="/tasks/:taskId" element={<TaskDetail />} />
        <Route path="/work/tasks" element={<TaskDetailPane args={{ taskId: "task-a" }} close={noop} setArgs={noop} setToolbar={noop} setShortcuts={noop} />} />
        <Route path="/sessions/:sessionId" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>
  </QueryClientProvider>);
}
describe("task session history", () => {
  it.each(["full", "drawer"] as const)("renders every attempt and exact origin in the %s surface", async (surface) => {
    mount(surface);
    const section = await screen.findByRole("region", { name: "Sessions (2)" });
    const rows = within(section).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[1]).toHaveTextContent("Original Worker");
    expect(rows[1]).toHaveTextContent("gpt-original");
    expect(rows[1]).toHaveTextContent("standard");
    expect(rows[1]).toHaveTextContent("stopped");
    expect(rows[1]).toHaveTextContent("Process exited with code 1");
    expect(rows[1]).toHaveTextContent("failed");
    expect(rows[1]!.querySelectorAll("time[datetime]")).toHaveLength(2);
    expect(rows[0]).not.toHaveTextContent("Not recorded");
    expect(within(rows[0]!).queryByText("Details")).not.toBeInTheDocument();
    expect(within(rows[1]!).getByText("Process exited with code 1")).not.toBeVisible();
    fireEvent.click(within(rows[1]!).getByText("Details"));
    expect(within(rows[1]!).getByText("Process exited with code 1")).toBeVisible();
    expect(screen.getByText("Workspace acquisition failed: disk full")).toBeInTheDocument();
    const link = within(rows[1]!).getByRole("link", { name: /Original Worker/ });
    expect(link).toHaveAttribute("href", "/sessions/worker-a?attempt=attempt-1&taskId=task-a");
    fireEvent.click(link);
    expect(screen.getByLabelText("Task pane return")).toHaveTextContent(surface === "drawer" ? '{"taskId":"task-a"}' : "null");
    expect(screen.getByText("/sessions/worker-a?attempt=attempt-1&taskId=task-a from " + origins[surface])).toBeInTheDocument();
  });
  it("shows loading before an empty history without inventing attempts", async () => {
    let resolve!: (value: unknown) => void;
    request.mockReturnValue(new Promise((done) => { resolve = done; }));
    mount();
    expect(screen.getByText("Loading sessions…")).toBeInTheDocument();
    await act(async () => resolve({ data: { task_id: "task-a", sessions: [] } }));
    expect(await screen.findByText("No recorded sessions yet.")).toBeInTheDocument();
  });
  it("shows history errors and allows a retry", async () => {
    request.mockRejectedValueOnce(new Error("Permission denied"));
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load sessions");
    fireEvent.click(screen.getByRole("button", { name: "Retry sessions" }));
    expect(await screen.findByRole("region", { name: "Sessions (2)" })).toBeInTheDocument();
  });
});
