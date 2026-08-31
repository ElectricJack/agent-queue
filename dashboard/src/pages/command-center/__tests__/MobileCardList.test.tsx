import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import MobileCardList from "../MobileCardList";
import { edge, graph, task } from "./fixtures";

afterEach(cleanup);
beforeEach(() => localStorage.clear());

describe("mobile task hierarchy", () => {
  it("includes waiting tasks and keeps expand controls separate from task selection", () => {
    const open = vi.fn();
    render(<MobileCardList graph={graph(
      [task("parent", { status: "AWAITING_PLAN_APPROVAL" }), task("child", { status: "WAITING_INPUT" })],
      [edge("child", "parent")],
    )} onTaskClick={open} selectedTaskId="child" />);
    expect(screen.getByText("Task parent")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Expand children of Task parent" }));
    expect(open).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Task child"));
    expect(open).toHaveBeenCalledExactlyOnceWith("child");
    expect(screen.getByRole("button", { name: "Open task Task child" })).toHaveAttribute("aria-pressed", "true");
    for (const button of screen.getAllByRole("button")) {
      expect(button.querySelector("button")).toBeNull();
    }
  });

  it("scrolls inside the bounded workspace instead of growing beyond the viewport", () => {
    render(<MobileCardList graph={graph(Array.from({ length: 20 }, (_, index) => task(String(index))))} onTaskClick={vi.fn()} />);
    expect(screen.getByRole("region", { name: "Task list" })).toHaveClass("h-full", "overflow-y-auto");
  });

  it("preserves filtered ancestors and clears only from blank list space or Escape", () => {
    const clear = vi.fn();
    render(<MobileCardList graph={graph(
      [task("parent"), task("child")], [edge("child", "parent")],
    )} onTaskClick={vi.fn()} onBackgroundClick={clear} matchingTaskIds={new Set(["child"])} filtering />);
    expect(screen.getByText("Task parent")).toBeInTheDocument();
    expect(screen.getByText("Task child")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Task child"));
    expect(clear).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("region", { name: "Task list" }));
    fireEvent.keyDown(screen.getByRole("region", { name: "Task list" }), { key: "Escape" });
    expect(clear).toHaveBeenCalledTimes(2);
  });

  it("shows why a dependency edge exists", () => {
    render(<MobileCardList graph={graph(
      [task("spawned"), task("origin")],
      [edge("spawned", "origin", "discovered-from", "The first task exposed a follow-up")],
    )} onTaskClick={vi.fn()} />);

    expect(screen.getByText(/The first task exposed a follow-up/)).toBeInTheDocument();
  });
});

it("retains the same playbook card when a run finishes, even with all tasks filtered out", () => {
  const openTask = vi.fn(), openPlaybook = vi.fn();
  const props = { graph: graph([task("done", { status: "COMPLETED" })]), onTaskClick: openTask,
    onPlaybookClick: openPlaybook, matchingTaskIds: new Set<string>() };
  const definition = { id: "audit", scope: "system", triggers: ["timer.24h"] };
  const view = render(<MobileCardList {...props} playbooks={[{ ...definition, running_count: 1 }]} />);
  const card = screen.getByRole("button", { name: "Open playbook audit" });
  expect(card).toHaveTextContent("Running");
  view.rerender(<MobileCardList {...props} playbooks={[{ ...definition, running_count: 0, last_run: { run_id: "r", status: "completed" } }]} />);
  expect(screen.getByRole("button", { name: "Open playbook audit" })).toBe(card);
  expect(card).toHaveTextContent("Waiting for trigger");
  expect(card).toHaveTextContent("Last run: completed");
  expect(screen.queryByText("Task done")).not.toBeInTheDocument();
  fireEvent.click(card);
  expect(openPlaybook).toHaveBeenCalledExactlyOnceWith("audit");
  expect(openTask).not.toHaveBeenCalled();
});
