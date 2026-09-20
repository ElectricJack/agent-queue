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
    expect(onToggleChildren).toHaveBeenCalledWith("e", false);
    expect(onOpenTask).toHaveBeenCalledWith("e", { id: "e", playbook_run_id: undefined });
  });

  it("reports when the expanded container is finished", async () => {
    const onToggleChildren = vi.fn();
    render(<ContainerNode id="e" data={{ node: { ...node, status: "COMPLETED" }, projectId: "p1", onToggleChildren }} /> as never);

    await userEvent.click(screen.getByRole("button", { name: "Collapse children of Epic" }));

    expect(onToggleChildren).toHaveBeenCalledWith("e", true);
  });

  it("passes the container's own run id so a run task keeps its routing", async () => {
    const onOpenTask = vi.fn();
    render(<ContainerNode id="e" data={{ node: { ...node, playbook_run_id: "run-1" }, projectId: "p1", onOpenTask }} /> as never);
    await userEvent.click(screen.getByRole("button", { name: "Open task Epic" }));
    expect(onOpenTask).toHaveBeenCalledWith("e", { id: "e", playbook_run_id: "run-1" });
  });

  it("renders a progress bar from the aggregates", () => {
    render(<ContainerNode id="e" data={{ node, projectId: "p1" }} /> as never);
    expect(screen.getByRole("progressbar", { name: "2 of 5 done" })).toBeInTheDocument();
  });

  it("shows nothing for a phase-less container", () => {
    render(<ContainerNode id="e" data={{ node, projectId: "p1" }} /> as never);
    expect(screen.queryByText(/^Phase /)).not.toBeInTheDocument();
  });

  it("shows the phase order and label, and a lock glyph while gated", () => {
    render(
      <ContainerNode
        id="e"
        data={{ node: { ...node, phase_order: 2, phase_label: "Build", is_blocked: true }, projectId: "p1" }}
      /> as never,
    );
    expect(screen.getByText("Phase 2 · Build")).toBeInTheDocument();
    expect(screen.getByLabelText("Phase gated")).toBeInTheDocument();
  });

  it("omits the lock glyph when a phase container is not gated", () => {
    render(
      <ContainerNode
        id="e"
        data={{ node: { ...node, phase_order: 1, phase_label: "Foundation", is_blocked: false }, projectId: "p1" }}
      /> as never,
    );
    expect(screen.getByText("Phase 1 · Foundation")).toBeInTheDocument();
    expect(screen.queryByLabelText("Phase gated")).not.toBeInTheDocument();
  });
});
