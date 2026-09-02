import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import Graph from "../Graph";
import type { PlaybookSummary } from "../../../api/hooks";
const mocks = vi.hoisted(() => ({
  playbooks: [] as PlaybookSummary[], open: vi.fn(), close: vi.fn(), pane: { kind: "closed" } as { kind: string; view?: string; args?: unknown },
  mounts: 0, query: "", taskCount: 1, loading: false, layoutV2: false, showCompleted: false,
  statusPending: false, projectIds: ["alpha"],
  project: { id: "alpha", name: "Alpha" },
  graphs: vi.fn(), layoutProps: { current: null as Record<string, unknown> | null },
  listProps: { current: null as Record<string, unknown> | null }, selectTask: vi.fn(),
}));
vi.mock("../../../api/hooks", () => ({
  usePlaybooks: () => ({ data: mocks.playbooks, isLoading: false }),
  useSystemStatus: () => ({
    data: mocks.statusPending ? undefined : { graph_layout_enabled: mocks.layoutV2 },
    isPending: mocks.statusPending,
  }),
}));
vi.mock("../../../api/graphLayout", () => ({
  useLayoutExtents: (ids: string[]) => ids.map(() => ({ layout_version: 1, extent_w: 4, extent_h: 4, node_count: 7 })),
}));
vi.mock("../layout-v2/LayoutCanvas", () => ({ default: (props: Record<string, unknown>) => {
  mocks.layoutProps.current = props;
  return <div data-testid="layout-canvas" />;
} }));
vi.mock("../layout-v2/MobileLayoutList", () => ({
  default: () => <div data-testid="layout-list" />,
  MobileLayoutLists: (props: Record<string, unknown>) => {
    mocks.listProps.current = props;
    return <div data-testid="layout-lists" />;
  },
}));
vi.mock("../../../panes/store", () => ({ useShellPaneStore: () => ({ state: mocks.pane, open: mocks.open, close: mocks.close }) }));
vi.mock("../TaskWorkspace", () => ({ useTaskWorkspace: () => ({ projectId: "alpha", projectIds: mocks.projectIds, projects: [mocks.project],
  filters: { query: mocks.query, status: "", showCompleted: mocks.showCompleted, focus: "" }, focusId: null, setFocus: vi.fn(),
  isLoadingProjects: false, projectsError: null }) }));
vi.mock("../../../api/graph", () => ({ useProjectGraphs: (...args: unknown[]) => { mocks.graphs(...args); return ({ data: {
  tasks: mocks.taskCount ? [{ id: "task-a", title: "Checkout", status: "READY" }] : [], taskProject: { "task-a": "alpha" }, edges: [], gates: [], agents: [],
}, isLoading: mocks.loading, errors: [] }); } }));
vi.mock("../useTaskSelection", () => ({ useTaskSelection: () => ({ selectedTaskId: null, selectTask: mocks.selectTask, clearTask: vi.fn() }) }));
vi.mock("../GraphCanvas", () => ({ default: function Canvas({ matchingTaskIds, playbooks, onPlaybookClick }: { matchingTaskIds: ReadonlySet<string>; playbooks: PlaybookSummary[]; onPlaybookClick: (id: string) => void }) {
  const [mount] = useState(() => ++mocks.mounts);
  return <div data-testid="canvas" data-mount={mount}>{matchingTaskIds.size}{playbooks.map(p => <button key={p.id} onClick={() => onPlaybookClick(p.id)}>{p.id}</button>)}</div>;
} }));
vi.mock("../MobileCardList", () => ({ default: () => null }));
beforeEach(() => { mocks.playbooks = []; mocks.pane = { kind: "closed" }; mocks.open.mockClear(); mocks.close.mockClear(); mocks.mounts = 0; mocks.loading = false; mocks.query = ""; mocks.taskCount = 1; mocks.layoutV2 = false; mocks.showCompleted = false; mocks.graphs.mockClear(); mocks.selectTask.mockClear(); mocks.layoutProps.current = null;
  mocks.listProps.current = null; mocks.statusPending = false; mocks.projectIds = ["alpha"]; vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })); });
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

it("uses the layout canvas when the flag is on and maps Show completed to the variant", () => {
  mocks.layoutV2 = true;
  const view = render(<Graph />);
  expect(screen.getByTestId("layout-canvas")).toBeInTheDocument();
  expect(screen.queryByTestId("canvas")).toBeNull();
  expect(mocks.layoutProps.current).toMatchObject({ projectIds: ["alpha"], variant: "active" });
  // The legacy full-graph fetch must not run behind the flag.
  expect(mocks.graphs).not.toHaveBeenCalled();
  // The status strip counts nodes from the layout extents, not the legacy graph.
  expect(screen.getByText(/7 tasks total/)).toBeInTheDocument();

  mocks.showCompleted = true;
  view.rerender(<Graph />);
  expect(mocks.layoutProps.current).toMatchObject({ variant: "all" });
});

it("uses the mobile layout list behind the flag on portrait phones, one per project", () => {
  mocks.layoutV2 = true;
  mocks.projectIds = ["alpha", "beta"];
  vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  render(<Graph />);
  expect(screen.getByTestId("layout-lists")).toBeInTheDocument();
  expect(screen.queryByTestId("layout-canvas")).toBeNull();
  expect(mocks.listProps.current).toMatchObject({ projectIds: ["alpha", "beta"] });
});

it("mounts neither branch while the system status is still loading", () => {
  mocks.statusPending = true;
  render(<Graph />);
  // Guessing "flag off" here would fire the legacy full-graph fetch on every
  // cold load, behind the tiled canvas.
  expect(mocks.graphs).not.toHaveBeenCalled();
  expect(screen.queryByTestId("canvas")).toBeNull();
  expect(screen.queryByTestId("layout-canvas")).toBeNull();
  expect(screen.getByRole("status")).toHaveTextContent("Loading tasks…");
});

it("routes a clicked card that belongs to a playbook run through its own payload", () => {
  mocks.layoutV2 = true;
  render(<Graph />);
  const onTaskClick = mocks.layoutProps.current!.onTaskClick as (id: string, task?: unknown) => void;
  // The tiled canvas holds the card, so the run id has to travel with the click.
  onTaskClick("task-a", { id: "task-a", playbook_run_id: "run-1" });
  expect(mocks.selectTask).toHaveBeenCalledWith({ id: "task-a", playbook_run_id: "run-1" });
  onTaskClick("task-b");
  expect(mocks.selectTask).toHaveBeenLastCalledWith({ id: "task-b" });
});
