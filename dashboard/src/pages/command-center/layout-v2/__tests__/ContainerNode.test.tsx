import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
vi.mock("@xyflow/react", () => ({ Handle: () => null, Position: { Top: "t", Bottom: "b", Left: "l", Right: "r" } }));
import ContainerNode from "../ContainerNode";

const node = { id: "e", title: "Epic", status: "IN_PROGRESS", priority: 100, is_blocked: false, x: 0, y: 0, w: 3, h: 2, depth: 0,
  container_id: null, kind: "container", context_only: false, agg_children: 3, agg_descendants: 5, agg_completed: 2, agg_running: 1, agg_blocked: 0, agg_active: 3 };

describe("ContainerNode", () => {
  it("shows aggregates and wires enter and open", async () => {
    const onFocus = vi.fn(), onOpenTask = vi.fn();
    render(<ContainerNode id="e" data={{ node, projectId: "p1", onFocus, onOpenTask }} selected={false} /> as never);
    expect(screen.getByText("2/5 done")).toBeInTheDocument();
    expect(screen.getByText("1 running")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Enter Epic" }));
    await userEvent.click(screen.getByRole("button", { name: "Open task Epic" }));
    expect(onFocus).toHaveBeenCalledWith("e");
    expect(onOpenTask).toHaveBeenCalledWith("e", { id: "e", playbook_run_id: undefined });
  });

  it("offers no expand/collapse toggle: a container is entered, never expanded", () => {
    render(<ContainerNode id="e" data={{ node, projectId: "p1", onFocus: vi.fn() }} /> as never);
    expect(screen.queryByRole("button", { name: /children of/i })).not.toBeInTheDocument();
  });

  it("offers no enter control for the container already entered", () => {
    render(<ContainerNode id="e" data={{ node, projectId: "p1" }} /> as never);
    expect(screen.queryByRole("button", { name: /^Enter/ })).not.toBeInTheDocument();
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

  it("renders a failed-phase hold with status-bearing links to the affected children", async () => {
    const onOpenTask = vi.fn();
    render(
      <ContainerNode
        id="e"
        data={{
          node: {
            ...node,
            phase_order: 1,
            phase_label: "Foundation",
            phase_hold: {
              phase_id: "e",
              failed_children_total: 2,
              descendant_blocker_count: 3,
              remedies: [],
              failed_children: [
                { id: "e.1", status: "FAILED" },
                { id: "e.2", status: "BLOCKED" },
              ],
            },
          },
          projectId: "p1",
          onOpenTask,
        }}
      /> as never,
    );
    expect(screen.getByText("Waiting for failed work")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open failed work e.1 (FAILED)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open failed work e.2 (BLOCKED)" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Open failed work e.2 (BLOCKED)" }));
    expect(onOpenTask).toHaveBeenCalledWith("e.2", { id: "e.2" });
  });

  /**
   * The engine only reserves `headerPx` (0.35 * UNIT_H, mirroring
   * `HEADER_H` in src/task_graph/layout/constants.py) before a container's
   * first child row. Anything stacked above or below the header as a
   * sibling grows the header block past what the engine reserved and
   * overlaps the first row of children on the real canvas.
   */
  describe("header height stays within the engine's reservation", () => {
    const headerPx = 0.35 * 156;

    function headerRow(container: HTMLElement): HTMLElement {
      const row = container.querySelector<HTMLElement>('[data-container-id="e"] > *');
      if (!row) throw new Error("no header row rendered");
      return row;
    }

    it("renders exactly one row child under the container -- no stacked banner", () => {
      const { container } = render(
        <ContainerNode
          id="e"
          data={{ node: { ...node, phase_order: 2, phase_label: "Build", is_blocked: true }, projectId: "p1" }}
        /> as never,
      );
      // Handle is mocked to render nothing, so every remaining child of the
      // outer container div is real chrome -- there must be only the one
      // fixed-height header row, whether or not the container is a phase.
      expect(container.querySelectorAll('[data-container-id="e"] > *')).toHaveLength(1);
    });

    it("keeps the header row's inline height at headerPx for a phase container", () => {
      const { container } = render(
        <ContainerNode
          id="e"
          data={{ node: { ...node, phase_order: 2, phase_label: "Build", is_blocked: true }, projectId: "p1" }}
        /> as never,
      );
      expect(headerRow(container).style.height).toBe(`${headerPx}px`);
    });

    it("renders the phase chip and lock glyph inside the header row", () => {
      const { container } = render(
        <ContainerNode
          id="e"
          data={{ node: { ...node, phase_order: 2, phase_label: "Build", is_blocked: true }, projectId: "p1" }}
        /> as never,
      );
      const row = headerRow(container);
      expect(row.contains(screen.getByText(/Phase 2/))).toBe(true);
      expect(row.contains(screen.getByLabelText("Phase gated"))).toBe(true);
    });

    it("truncates a long phase label but keeps the full text on the title attribute", () => {
      const longLabel = "A very long phase label that would otherwise blow out the header row";
      render(
        <ContainerNode
          id="e"
          data={{ node: { ...node, phase_order: 3, phase_label: longLabel, is_blocked: false }, projectId: "p1" }}
        /> as never,
      );
      const chip = screen.getByTitle(`Phase 3 · ${longLabel}`);
      expect(chip.className).toMatch(/truncate/);
    });

    it("keeps the progress bar inside the fixed-height header, not a height-adding sibling", () => {
      const { container } = render(<ContainerNode id="e" data={{ node, projectId: "p1" }} /> as never);
      const row = headerRow(container);
      const bar = screen.getByRole("progressbar");
      expect(row.contains(bar)).toBe(true);
      expect(container.querySelectorAll('[data-container-id="e"] > *')).toHaveLength(1);
    });
  });
});
