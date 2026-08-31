import { useState } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import Graph from "../Graph";
const mocks = vi.hoisted(() => ({
  mounts: 0, query: "", taskCount: 1, loading: false,
  project: { id: "alpha", name: "Alpha" },
}));
vi.mock("../TaskWorkspace", () => ({ useTaskWorkspace: () => ({ projectId: "alpha", projectIds: ["alpha"], projects: [mocks.project],
  filters: { query: mocks.query, status: "", showCompleted: false }, isLoadingProjects: false, projectsError: null }) }));
vi.mock("../../../api/graph", () => ({ useProjectGraphs: () => ({ data: {
  tasks: mocks.taskCount ? [{ id: "task-a", title: "Checkout", status: "READY" }] : [], taskProject: { "task-a": "alpha" }, edges: [], gates: [], agents: [],
}, isLoading: mocks.loading, errors: [] }) }));
vi.mock("../useTaskSelection", () => ({ useTaskSelection: () => ({ selectedTaskId: null, selectTask: vi.fn(), clearTask: vi.fn() }) }));
vi.mock("../GraphCanvas", () => ({ default: function Canvas({ matchingTaskIds }: { matchingTaskIds: ReadonlySet<string> }) {
  const [mount] = useState(() => ++mocks.mounts);
  return <div data-testid="canvas" data-mount={mount}>{matchingTaskIds.size}</div>;
} }));
vi.mock("../MobileCardList", () => ({ default: () => null }));
beforeEach(() => { mocks.mounts = 0; mocks.loading = false; mocks.query = ""; mocks.taskCount = 1; vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it("keeps the canvas mounted across empty search results and temporary empty snapshots", () => {
  const view = render(<Graph />);
  expect(screen.getByTestId("canvas")).toHaveAttribute("data-mount", "1");
  mocks.query = "no match";
  view.rerender(<Graph />);
  expect(screen.getByTestId("canvas")).toHaveTextContent("0");
  mocks.query = ""; mocks.taskCount = 0;
  view.rerender(<Graph />);
  expect(screen.getByTestId("canvas")).toHaveAttribute("data-mount", "1");
  mocks.taskCount = 1;
  view.rerender(<Graph />);
  expect(screen.getByTestId("canvas")).toHaveAttribute("data-mount", "1");
});

it("preserves the mounted canvas while a newly added project loads", () => {
  const view = render(<Graph />);
  mocks.loading = true;
  view.rerender(<Graph />);
  expect(screen.getByTestId("canvas")).toHaveAttribute("data-mount", "1");
  expect(screen.getByRole("status")).toHaveTextContent("Loading tasks");
  mocks.loading = false;
  view.rerender(<Graph />);
  expect(screen.getByTestId("canvas")).toHaveAttribute("data-mount", "1");
});
