import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import ProjectOverview from "../Overview";

vi.mock("../../../api/hooks", () => ({
  useProject: () => ({ data: { id: "p1", name: "Project one" } }),
  useTasks: () => ({ data: [{ id: "failed", status: "FAILED" }, ...Array.from({ length: 9 }, (_, i) => ({ id: `task-${i}`, title: `Task ${i}`, status: "READY" }))] }),
  useAgents: () => ({ data: [] }),
  useWorkspaces: () => ({ data: [{ id: "w1" }, { id: "w2" }] }),
}));
afterEach(cleanup);

function Destination() {
  const location = useLocation();
  return <output aria-label="Return route">{(location.state as { from?: string })?.from}</output>;
}
function renderOverview() {
  render(<MemoryRouter initialEntries={["/projects/p1/overview?q=keep&completed=1"]}>
    <Routes>
      <Route path="/projects/:projectId/overview" element={<ProjectOverview />} />
      <Route path="/tasks/:taskId" element={<Destination />} />
    </Routes>
  </MemoryRouter>);
}

describe("project overview navigation", () => {
  it("keeps task and workspace links as scoped siblings of Overview", () => {
    renderOverview();
    for (const name of ["review", "View all →", "Tasks tab"]) {
      expect(screen.getByRole("link", { name })).toHaveAttribute("href", "/projects/p1/tasks?q=keep&completed=1");
    }
    expect(screen.getByRole("link", { name: "Workspaces tab" })).toHaveAttribute("href", "/projects/p1/workspaces?q=keep&completed=1");
    expect(screen.getByRole("link", { name: "Open task graph" })).toHaveAttribute("href", "/projects/p1/graph?q=keep&completed=1");
    expect(screen.getByRole("link", { name: "Open agent flock" })).toHaveAttribute("href", "/agents");
    expect(screen.queryByRole("link", { name: /chat/i })).not.toBeInTheDocument();
  });

  it("retains workspace filters when opening a task detail", async () => {
    renderOverview();
    await userEvent.click(screen.getByRole("link", { name: /Task 0/ }));
    expect(screen.getByLabelText("Return route")).toHaveTextContent("/projects/p1/overview?q=keep&completed=1");
  });
});
