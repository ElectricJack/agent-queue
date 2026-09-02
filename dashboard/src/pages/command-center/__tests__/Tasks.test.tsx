import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Tasks from "../Tasks";

const mocks = vi.hoisted(() => ({
  open: vi.fn(), close: vi.fn(), edit: vi.fn(), stop: vi.fn(), list: vi.fn(),
  state: { kind: "closed" } as { kind: "closed" } | { kind: "open"; view: string; args: unknown; width: number },
  filters: { query: "", status: "", showCompleted: false, focus: "" },
  tasks: [
    { id: "first", title: "Fix checkout", project_id: "alpha", status: "IN_PROGRESS", priority: 25, assigned_agent: "Sol" },
    { id: "done", title: "Completed checkout", project_id: "alpha", status: "COMPLETED", priority: 100 },
    { id: "other", title: "Other project", project_id: "beta", status: "READY", priority: 100 },
  ],
}));
vi.mock("../TaskWorkspace", () => ({ useTaskWorkspace: () => ({ projectId: "alpha", projectIds: ["alpha"], isLoadingProjects: false, projectsError: null, projects: [{ id: "alpha", name: "Alpha" }], filters: mocks.filters }) }));
vi.mock("../../../api/graph", () => ({
  useProjectGraphs: (projectIds: string[]) => {
    mocks.list(projectIds);
    return { data: {
      tasks: mocks.tasks.map((task) => ({ ...task, assigned_agent_id: task.assigned_agent })),
      taskProject: Object.fromEntries(mocks.tasks.map((task) => [task.id, task.project_id])),
      edges: [], gates: [], agents: [],
    }, isLoading: false, errors: [] };
  },
}));
vi.mock("../../../panes/store", () => ({ useShellPaneStore: () => ({ open: mocks.open, close: mocks.close, state: mocks.state }) }));
vi.mock("../../../api/hooks", () => ({
  useEditTask: () => ({ mutate: mocks.edit, isPending: false, error: null }),
  useDeleteTask: () => ({ mutateAsync: vi.fn(), isPending: false }),
  usePauseTask: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useResumeTask: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useStopTask: () => ({ mutate: mocks.stop, isPending: false, error: null }),
  useRestartTask: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useApproveTask: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useApprovePlan: () => ({ mutate: vi.fn(), isPending: false, error: null }),
}));
afterEach(cleanup);
beforeEach(() => { vi.clearAllMocks(); mocks.tasks[0]!.priority = 25; mocks.state = { kind: "closed" }; mocks.filters = { query: "", status: "", showCompleted: false, focus: "" }; });

describe("unified task table", () => {
  it("scopes the query and filters, and opens details from any ordinary row cell", async () => {
    render(<Tasks />);
    expect(mocks.list).toHaveBeenCalledWith(["alpha"]);
    expect(screen.queryByText("Completed checkout")).not.toBeInTheDocument();
    expect(screen.queryByText("Other project")).not.toBeInTheDocument();
    await userEvent.click(screen.getByText("Sol"));
    expect(mocks.open).toHaveBeenCalledWith("task-detail", { taskId: "first" });
  });

  it("keeps inline editing and quick actions separate from row selection", async () => {
    render(<Tasks />);
    const priority = screen.getByRole("spinbutton", { name: "Priority for Fix checkout" });
    await userEvent.click(priority);
    fireEvent.change(priority, { target: { value: "40" } });
    fireEvent.blur(priority);
    expect(mocks.edit).toHaveBeenCalledWith({ task_id: "first", priority: 40 });
    await userEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(mocks.stop).toHaveBeenCalledWith({ task_id: "first" });
    expect(mocks.open).not.toHaveBeenCalled();
  });

  it("leaves editing keys in inputs/selects while row arrows still navigate", async () => {
    mocks.filters.showCompleted = true;
    render(<Tasks />);
    const priority = screen.getByRole("spinbutton", { name: "Priority for Fix checkout" });
    await userEvent.click(priority);
    expect(fireEvent.keyDown(priority, { key: "ArrowDown" })).toBe(true);
    expect(priority).toHaveFocus();
    const status = screen.getByRole("combobox", { name: "Status for Fix checkout" });
    await userEvent.click(status);
    expect(fireEvent.keyDown(status, { key: "ArrowDown" })).toBe(true);
    expect(status).toHaveFocus();
    const row = screen.getByText("Fix checkout").closest("tr")!;
    row.focus();
    fireEvent.keyDown(row, { key: "ArrowDown" });
    expect(screen.getByText("Completed checkout").closest("tr")).toHaveFocus();
  });

  it("reflects remote priority updates after editing and does not write on an unchanged blur", async () => {
    const view = render(<Tasks />);
    const priority = screen.getByRole("spinbutton", { name: "Priority for Fix checkout" });
    await userEvent.click(priority);
    fireEvent.change(priority, { target: { value: "40" } });
    fireEvent.blur(priority);
    mocks.tasks[0]!.priority = 40;
    view.rerender(<Tasks />);
    mocks.tasks[0]!.priority = 90;
    view.rerender(<Tasks />);
    await waitFor(() => expect(priority).toHaveValue(90));
    mocks.edit.mockClear();
    await userEvent.click(priority);
    fireEvent.blur(priority);
    expect(mocks.edit).not.toHaveBeenCalled();
  });

  it("closes a delete dialog backdrop without selecting its task row", async () => {
    render(<Tasks />);
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("dialog", { name: "Delete task" });
    fireEvent.click(dialog.parentElement!);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mocks.open).not.toHaveBeenCalled();
  });

  it("clears only task selection on blank-area clicks", async () => {
    mocks.state = { kind: "open", view: "task-detail", args: { taskId: "first" }, width: 480 };
    const view = render(<Tasks />);
    await userEvent.click(screen.getByRole("region", { name: "Task list" }));
    expect(mocks.close).toHaveBeenCalledOnce();
    mocks.close.mockClear();
    mocks.state = { kind: "open", view: "contextual-settings", args: {}, width: 480 };
    view.rerender(<Tasks />);
    await userEvent.click(screen.getByRole("region", { name: "Task list" }));
    expect(mocks.close).not.toHaveBeenCalled();
  });

  it("supports row keyboard selection without intercepting input keys", () => {
    render(<Tasks />);
    const row = screen.getByText("Fix checkout").closest("tr")!;
    fireEvent.keyDown(row, { key: "Enter" });
    expect(mocks.open).toHaveBeenCalledOnce();
    mocks.open.mockClear();
    fireEvent.keyDown(screen.getByRole("spinbutton"), { key: "Enter" });
    expect(mocks.open).not.toHaveBeenCalled();
  });
  it("searches completed history beyond the ordinary task-list cap", () => {
    const original = mocks.tasks;
    mocks.tasks = [...Array.from({ length: 205 }, (_, i) => ({ id: `active-${i}`, title: `Active ${i}`, project_id: "alpha", status: "READY", priority: 100 })),
      { id: "historical", title: "Historical task", project_id: "alpha", status: "COMPLETED", priority: 100 }];
    mocks.filters = { query: "historical", status: "", showCompleted: true, focus: "" };
    try {
      render(<Tasks />);
      expect(screen.getByText("Historical task")).toBeInTheDocument();
    } finally { mocks.tasks = original; }
  });

});
