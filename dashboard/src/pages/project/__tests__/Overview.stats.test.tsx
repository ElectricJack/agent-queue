import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import ProjectOverview from "../Overview";

let tasksResp: Record<string, unknown>;
let tasksPending: boolean;
let tasksError: boolean;

vi.mock("../../../api/hooks", () => ({
  useProject: () => ({ data: { id: "p1", name: "Project one" } }),
  useTasks: () => ({
    data: tasksPending || tasksError ? undefined : tasksResp,
    isPending: tasksPending,
    isError: tasksError,
  }),
  useAgents: () => ({ data: [] }),
  useWorkspaces: () => ({ data: [] }),
}));

afterEach(() => {
  cleanup();
  tasksResp = {};
  tasksPending = false;
  tasksError = false;
});

function renderOverview() {
  render(
    <MemoryRouter initialEntries={["/projects/p1/overview"]}>
      <Routes>
        <Route path="/projects/:projectId/overview" element={<ProjectOverview />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("project overview completion stats", () => {
  it("uses backend authoritative numbers (hidden_completed + total) rather than the capped active list", () => {
    tasksResp = {
      display_mode: "active",
      tasks: [0, 1, 2, 3].map((i) => ({ id: `t${i}`, title: `Task ${i}`, status: "READY" })),
      total: 50,
      hidden_completed: 500,
      filtered: false,
    };
    renderOverview();
    expect(screen.getByText("500 done · 91%")).toBeInTheDocument();
    expect(screen.getByText("550")).toBeInTheDocument();
  });

  it("shows a loading state while the task summary is in flight", () => {
    tasksPending = true;
    renderOverview();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText(/done · /)).not.toBeInTheDocument();
  });

  it("shows an unavailable state when reading the task summary fails", () => {
    tasksError = true;
    renderOverview();
    expect(screen.getByText("Task summary unavailable.")).toBeInTheDocument();
    expect(screen.queryByText(/done · /)).not.toBeInTheDocument();
  });
});
