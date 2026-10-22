import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { TaskExplain } from "../TaskGraph";

const { explain } = vi.hoisted(() => ({ explain: vi.fn() }));
vi.mock("../../../api/hooks", () => ({ useExplainTask: explain, useTaskDeps: vi.fn() }));

// TaskExplain links out to the delivery panel, so it renders inside a router.
const explainView = () => render(<MemoryRouter><TaskExplain taskId="task" /></MemoryRouter>);

describe("TaskExplain claim preparation", () => {
  it("shows completed phase times even when there are no blockers", () => {
    explain.mockReturnValue({ data: {
      reasons: [], claim_timing: { result: "claimed", total_seconds: 2.5,
        phases: { frontier_select: 0.05, fetch: 1.2 } },
    } });
    explainView();
    expect(screen.getByText("Claim preparation")).toBeInTheDocument();
    expect(screen.getByText("1.20s")).toBeInTheDocument();
    expect(screen.getByText(/No blockers/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "delivery timing" }))
      .toHaveAttribute("href", "/metrics?task=task#delivery");
  });

  it("shows the active phase together with existing blocker reasons", () => {
    explain.mockReturnValue({ data: {
      reasons: [{ code: "held", detail: "held by worker" }],
      claim_timing: { current_phase: "fetch", total_seconds: 3,
        phases: { fetch: 2 } },
    } });
    explainView();
    expect(screen.getByText(/Preparing: fetch/)).toBeInTheDocument();
    expect(screen.getByText("held by worker")).toBeInTheDocument();
  });
});
