import { describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { TerminalLinkModeProvider, type TerminalLinkMode } from "../terminalLinks";
import TaskAgentTerminalButton from "../TaskAgentTerminalButton";
import TaskSessions from "../TaskSessions";
import type { Task } from "../../api/hooks";

vi.mock("../../api/agents", () => ({
  useAgentFlock: () => ({
    isError: false,
    data: [{ id: "worker-a", name: "worker-a", current_task_id: "t1", current_project_id: "p1",
      session_id: "sess-1", session_provider: "tmux", session_state: "running" }],
  }),
}));
vi.mock("../../pages/agents/useAgentSelection", () => ({ useAgentSelection: () => ({ select: vi.fn() }) }));
vi.mock("../../api/taskSessions", () => ({
  useTaskSessions: () => ({
    isPending: false, isError: false,
    data: { sessions: [{ id: "att-1", session_id: "sess-1", task_id: "t1", agent_name: "worker-a",
      session_started_at: 1790000000, started_at: 1790000000, ended_at: null, state: "running" }] },
  }),
}));

const task = { id: "t1", project_id: "p1", title: "T", assigned_agent: "worker-a" } as Task;

function inMode(mode: TerminalLinkMode, ui: ReactNode) {
  return render(<MemoryRouter><TerminalLinkModeProvider mode={mode}>{ui}</TerminalLinkModeProvider></MemoryRouter>);
}

describe("terminal links follow the link mode", () => {
  it("watch mode links the agent terminal to the focus session", () => {
    inMode("watch", <TaskAgentTerminalButton task={task} />);
    expect(screen.getByRole("link", { name: "Open agent terminal" })).toHaveAttribute("href", "/focus/sessions/sess-1");
  });

  it("interactive mode keeps the agents-page button", () => {
    inMode("interactive", <TaskAgentTerminalButton task={task} />);
    expect(screen.getByRole("button", { name: "Open agent terminal" })).toBeInTheDocument();
  });

  it("watch mode pins a session attempt to its process", () => {
    inMode("watch", <TaskSessions taskId="t1" />);
    expect(screen.getByRole("link", { name: "worker-a" })).toHaveAttribute("href", "/focus/sessions/sess-1?started=1790000000");
  });

  it("watch mode leaves the page by the link alone, without closing its host", () => {
    const onOpenSession = vi.fn();
    inMode("watch", <TaskSessions taskId="t1" onOpenSession={onOpenSession} />);
    screen.getByRole("link", { name: "worker-a" }).click();
    expect(onOpenSession).not.toHaveBeenCalled();
  });

  it("interactive mode keeps the session page link and closes its pane", () => {
    const onOpenSession = vi.fn();
    inMode("interactive", <TaskSessions taskId="t1" onOpenSession={onOpenSession} fromTaskPane />);
    const link = screen.getByRole("link", { name: "worker-a" });
    expect(link).toHaveAttribute("href", "/sessions/sess-1?attempt=att-1&taskId=t1");
    link.click();
    expect(onOpenSession).toHaveBeenCalledOnce();
  });
});
