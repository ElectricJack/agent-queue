import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import TaskSubtaskList from "../TaskSubtaskList";

const api = vi.hoisted(() => ({ taskSubtasks: vi.fn() }));
vi.mock("../../api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api/client")>(), ...api,
}));

const subtask = (ordinal: number, title: string, status: string, note: string | null = null) => ({
  id: `t#s${ordinal}`, task_id: "t", project_id: "p", ordinal, title, status, note,
  created_at: 1755878400, updated_at: 1755878400,
});

let client: QueryClient;
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  api.taskSubtasks.mockReset();
});
afterEach(() => { cleanup(); client.clear(); });

function mount(taskId = "t") {
  return render(
    <QueryClientProvider client={client}>
      <TaskSubtaskList taskId={taskId} />
    </QueryClientProvider>,
  );
}

describe("TaskSubtaskList", () => {
  it("renders ordinal, status and title for each subtask, plus a note when present", async () => {
    api.taskSubtasks.mockResolvedValue({
      data: {
        success: true, task_id: "t", total: 2, settled: 1,
        subtasks: [
          subtask(1, "Write the failing test", "done"),
          subtask(2, "Wire the endpoint", "in_progress", "blocked on schema review"),
        ],
      },
    });
    mount();
    expect(await screen.findByText("Write the failing test")).toBeInTheDocument();
    expect(screen.getByText("Wire the endpoint")).toBeInTheDocument();
    expect(screen.getByText("blocked on schema review")).toBeInTheDocument();
    expect(screen.getByText("1 / 2 settled")).toBeInTheDocument();
    expect(screen.getByText("done")).toBeInTheDocument();
    expect(screen.getByText("in_progress")).toBeInTheDocument();
  });

  it("renders nothing when the task has no subtasks", async () => {
    api.taskSubtasks.mockResolvedValue({
      data: { success: true, task_id: "t", total: 0, settled: 0, subtasks: [] },
    });
    const { container } = mount();
    await new Promise((r) => setTimeout(r, 0));
    expect(container).toBeEmptyDOMElement();
  });

  it("is read-only: no edit or status controls are rendered", async () => {
    api.taskSubtasks.mockResolvedValue({
      data: {
        success: true, task_id: "t", total: 1, settled: 0,
        subtasks: [subtask(1, "Only item", "pending")],
      },
    });
    mount();
    await screen.findByText("Only item");
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.queryAllByRole("textbox")).toHaveLength(0);
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
  });
});
