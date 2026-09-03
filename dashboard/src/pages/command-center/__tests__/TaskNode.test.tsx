import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@xyflow/react", () => ({
  Handle: () => null,
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
}));

import { TaskCard } from "../TaskNode";
import type { TaskNodeData } from "../types";

afterEach(cleanup);

const card = (status: string, over: Partial<TaskNodeData["hierarchy"]> = {}): TaskNodeData => ({
  task: { id: "task-one", title: "Ship it", status, priority: 100 },
  gates: [],
  projectId: "p1",
  hierarchy: {
    parentId: null, parentTitle: null, depth: 0, childCount: 2, visibleChildCount: 2,
    descendantCount: 4, completedCount: 3, runningCount: 0, blockedCount: 0,
    expanded: true, autoExpanded: false, contextOnly: false, ...over,
  },
});

describe("finished cards", () => {
  it("drops the progress ring once a card is settled", () => {
    render(<TaskCard data={card("COMPLETED")} />);
    // The number stays — it is the card's only completion signal now.
    expect(screen.getByText("3/4 descendants completed")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("keeps the live variant while work is still running underneath", () => {
    render(<TaskCard data={card("COMPLETED", { runningCount: 1 })} />);
    expect(screen.getByRole("progressbar", { name: "Child completion for Ship it" })).toBeInTheDocument();
    expect(screen.getByText("1 running")).toBeInTheDocument();
  });

  it("keeps the live variant for a card that has not finished", () => {
    render(<TaskCard data={card("IN_PROGRESS")} />);
    expect(screen.getByRole("progressbar", { name: "Child completion for Ship it" })).toBeInTheDocument();
  });
});
