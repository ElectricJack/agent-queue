import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import AgentShowTaskButton from "../AgentShowTaskButton";
import type { FlockAgent } from "../../../api/agents";

const mocks = vi.hoisted(() => ({ open: vi.fn() }));
vi.mock("../../../panes/store", () => ({
  useShellPaneStore: () => ({ open: mocks.open }),
}));

const agent: FlockAgent = {
  id: "worker", name: "Solar Eagle",
  current_task_id: "task-1", current_task_title: "Fix the queue",
} as FlockAgent;

describe("agent show-task shortcut", () => {
  afterEach(() => { cleanup(); mocks.open.mockReset(); });

  it("opens the agent's current task in the shell pane", async () => {
    render(<AgentShowTaskButton agent={agent} />);
    const button = screen.getByRole("button", { name: "Show task" });
    expect(button).toHaveAttribute("title", "Fix the queue (task-1)");
    await userEvent.setup().click(button);
    expect(mocks.open).toHaveBeenCalledOnce();
    expect(mocks.open).toHaveBeenCalledWith("task-detail", { taskId: "task-1" });
  });

  it("renders for an untitle task with only its id", () => {
    render(<AgentShowTaskButton agent={{ ...agent, current_task_title: null }} />);
    expect(screen.getByRole("button", { name: "Show task" })).toHaveAttribute("title", "task-1 (task-1)");
  });

  it("renders nothing while the agent is idle", () => {
    render(<AgentShowTaskButton agent={{ ...agent, current_task_id: null, current_task_title: null }} />);
    expect(screen.queryByRole("button", { name: "Show task" })).not.toBeInTheDocument();
    expect(mocks.open).not.toHaveBeenCalled();
  });
});
