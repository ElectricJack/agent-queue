import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import Graph from "../Graph";
import type { PlaybookSummary } from "../../../api/hooks";
const mocks = vi.hoisted(() => ({
  playbooks: [] as PlaybookSummary[], open: vi.fn(), close: vi.fn(), pane: { kind: "closed" } as { kind: string; view?: string; args?: unknown },
  mounts: 0, query: "", taskCount: 1, loading: false,
  project: { id: "alpha", name: "Alpha" },
}));
vi.mock("../../../api/hooks", () => ({ usePlaybooks: () => ({ data: mocks.playbooks, isLoading: false }) }));
vi.mock("../../../panes/store", () => ({ useShellPaneStore: () => ({ state: mocks.pane, open: mocks.open, close: mocks.close }) }));
vi.mock("../TaskWorkspace", () => ({ useTaskWorkspace: () => ({ projectId: "alpha", projectIds: ["alpha"], projects: [mocks.project],
  filters: { query: mocks.query, status: "", showCompleted: false, focus: "" }, isLoadingProjects: false, projectsError: null }) }));
vi.mock("../../../api/graph", () => ({ useProjectGraphs: () => ({ data: {
  tasks: mocks.taskCount ? [{ id: "task-a", title: "Checkout", status: "READY" }] : [], taskProject: { "task-a": "alpha" }, edges: [], gates: [], agents: [],
}, isLoading: mocks.loading, errors: [] }) }));
vi.mock("../useTaskSelection", () => ({ useTaskSelection: () => ({ selectedTaskId: null, selectTask: vi.fn(), clearTask: vi.fn() }) }));
vi.mock("../GraphCanvas", () => ({ default: function Canvas({ matchingTaskIds, playbooks, onPlaybookClick }: { matchingTaskIds: ReadonlySet<string>; playbooks: PlaybookSummary[]; onPlaybookClick: (id: string) => void }) {
  const [mount] = useState(() => ++mocks.mounts);
  return <div data-testid="canvas" data-mount={mount}>{matchingTaskIds.size}{playbooks.map(p => <button key={p.id} onClick={() => onPlaybookClick(p.id)}>{p.id}</button>)}</div>;
} }));
vi.mock("../MobileCardList", () => ({ default: () => null }));
beforeEach(() => { mocks.playbooks = []; mocks.pane = { kind: "closed" }; mocks.open.mockClear(); mocks.close.mockClear(); mocks.mounts = 0; mocks.loading = false; mocks.query = ""; mocks.taskCount = 1; vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })); });
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

it("adds completed playbook definitions despite hidden completed tasks and opens their own pane", () => {
  mocks.playbooks = [
    { id: "audit", scope: "project", scope_identifier: "alpha", last_run: { run_id: "r", status: "completed" } },
    { id: "other", scope: "project", scope_identifier: "beta" },
    { id: "memory", scope: "system" },
  ];
  const view = render(<Graph />);
  expect(screen.getByRole("button", { name: "audit" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "memory" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "other" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "audit" }));
  expect(mocks.open).toHaveBeenCalledWith("playbook-detail", { playbookId: "audit" });
  mocks.pane = { kind: "open", view: "playbook-detail", args: { playbookId: "audit" } };
  view.rerender(<Graph />);
  fireEvent.click(screen.getByText("2 playbooks · recurring definitions stay visible"));
  expect(mocks.close).toHaveBeenCalledOnce();
});
