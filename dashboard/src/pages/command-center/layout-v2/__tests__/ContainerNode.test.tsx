import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
vi.mock("@xyflow/react", () => ({ Handle: () => null, Position: { Top: "t", Bottom: "b", Left: "l", Right: "r" } }));
import ContainerNode from "../ContainerNode";

const node = { id: "e", title: "Epic", status: "IN_PROGRESS", priority: 100, is_blocked: false, x: 0, y: 0, w: 3, h: 2, depth: 0,
  container_id: null, kind: "container", context_only: false, agg_children: 3, agg_descendants: 5, agg_completed: 2, agg_running: 1, agg_blocked: 0, agg_active: 3 };

describe("ContainerNode", () => {
  it("shows aggregates and wires collapse, focus, and open", async () => {
    const onFocus = vi.fn(), onToggleChildren = vi.fn(), onOpenTask = vi.fn();
    render(<ContainerNode id="e" data={{ node, projectId: "p1", onFocus, onToggleChildren, onOpenTask }} selected={false} /> as never);
    expect(screen.getByText("2/5 done")).toBeInTheDocument();
    expect(screen.getByText("1 running")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Focus on Epic" }));
    await userEvent.click(screen.getByRole("button", { name: "Collapse children of Epic" }));
    await userEvent.click(screen.getByRole("button", { name: "Open task Epic" }));
    expect(onFocus).toHaveBeenCalledWith("e");
    expect(onToggleChildren).toHaveBeenCalledWith("e");
    expect(onOpenTask).toHaveBeenCalledWith("e", { id: "e", playbook_run_id: undefined });
  });

  it("passes the container's own run id so a run task keeps its routing", async () => {
    const onOpenTask = vi.fn();
    render(<ContainerNode id="e" data={{ node: { ...node, playbook_run_id: "run-1" }, projectId: "p1", onOpenTask }} /> as never);
    await userEvent.click(screen.getByRole("button", { name: "Open task Epic" }));
    expect(onOpenTask).toHaveBeenCalledWith("e", { id: "e", playbook_run_id: "run-1" });
  });
});
