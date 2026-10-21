import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TaskExplain } from "../TaskGraph";

const { explain } = vi.hoisted(() => ({ explain: vi.fn() }));
vi.mock("../../../api/hooks", () => ({ useExplainTask: explain, useTaskDeps: vi.fn() }));

describe("TaskExplain claim preparation", () => {
  it("shows completed phase times even when there are no blockers", () => {
    explain.mockReturnValue({ data: {
      reasons: [], claim_timing: { result: "claimed", total_seconds: 2.5,
        phases: { frontier_select: 0.05, fetch: 1.2 } },
    } });
    render(<TaskExplain taskId="task" />);
    expect(screen.getByText("Claim preparation")).toBeInTheDocument();
    expect(screen.getByText("1.20s")).toBeInTheDocument();
    expect(screen.getByText(/No blockers/)).toBeInTheDocument();
  });

  it("shows the active phase together with existing blocker reasons", () => {
    explain.mockReturnValue({ data: {
      reasons: [{ code: "held", detail: "held by worker" }],
      claim_timing: { current_phase: "fetch", total_seconds: 3,
        phases: { fetch: 2 } },
    } });
    render(<TaskExplain taskId="task" />);
    expect(screen.getByText(/Preparing: fetch/)).toBeInTheDocument();
    expect(screen.getByText("held by worker")).toBeInTheDocument();
  });
});
