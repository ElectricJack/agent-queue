import type { ReactNode } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AgentAvatarLayer from "../AgentAvatarLayer";

vi.mock("@xyflow/react", () => ({
  useNodes: () => [{ id: "parent", position: { x: 32, y: 32 } }],
  ViewportPortal: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));
afterEach(cleanup);

describe("collapsed task workers", () => {
  it("docks workers on hidden descendants together at their visible parent", () => {
    render(<AgentAvatarLayer agents={[
      { id: "agent-one", name: "Alice", current_task_id: "child-one" },
      { id: "agent-two", name: "Bob", current_task_id: "child-two" },
    ]} visibleTaskById={new Map([["child-one", "parent"], ["child-two", "parent"]])} />);
    expect(screen.getAllByRole("img")).toHaveLength(1);
    expect(screen.getByRole("img", { name: "Alice, Bob (working in collapsed tasks)" })).toHaveTextContent("+1");
  });

  it("does not show stale or filtered-out assignments as working on a loaded card", () => {
    render(<AgentAvatarLayer agents={[
      { id: "agent-one", name: "Alice", current_task_id: "removed" },
      { id: "agent-two", name: "Bob", current_task_id: "filtered" },
    ]} visibleTaskById={new Map()} />);
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});
