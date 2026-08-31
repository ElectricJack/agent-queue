import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useTaskSelection } from "../useTaskSelection";

const mocks = vi.hoisted(() => ({
  open: vi.fn(),
  close: vi.fn(),
  state: { kind: "closed" } as Record<string, unknown>,
}));

vi.mock("../../../panes/store", () => ({
  useShellPaneStore: () => ({ open: mocks.open, close: mocks.close, state: mocks.state }),
}));

describe("useTaskSelection", () => {
  beforeEach(() => {
    mocks.open.mockReset();
    mocks.close.mockReset();
    mocks.state = { kind: "closed" };
  });

  it("opens the run graph pane for a playbook root", () => {
    const { result } = renderHook(() => useTaskSelection());
    act(() => result.current.selectTask({ id: "root-task", playbook_run_id: "run-123" }));
    expect(mocks.open).toHaveBeenCalledWith("playbook-run-inspector", {
      runId: "run-123",
      taskId: "root-task",
    });
  });

  it("keeps ordinary tasks on task detail", () => {
    const { result } = renderHook(() => useTaskSelection());
    act(() => result.current.selectTask({ id: "ordinary-task" }));
    expect(mocks.open).toHaveBeenCalledWith("task-detail", { taskId: "ordinary-task" });
  });

  it("derives selection from an open run inspector", () => {
    mocks.state = {
      kind: "open",
      view: "playbook-run-inspector",
      args: { runId: "run-123", taskId: "root-task" },
    };
    const { result } = renderHook(() => useTaskSelection());
    expect(result.current.selectedTaskId).toBe("root-task");
  });
});
