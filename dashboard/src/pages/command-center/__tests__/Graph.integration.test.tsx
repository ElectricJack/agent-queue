import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import Graph from "../Graph";
import type { PlaybookSummary } from "../../../api/hooks";
const mocks = vi.hoisted(() => ({
  playbooks: [] as PlaybookSummary[], open: vi.fn(), close: vi.fn(), pane: { kind: "closed" } as { kind: string; view?: string; args?: unknown },
  query: "", showCompleted: false, extentPending: false, projectIds: ["alpha"],
  project: { id: "alpha", name: "Alpha" },
  layoutProps: { current: null as Record<string, unknown> | null },
  listProps: { current: null as Record<string, unknown> | null }, selectTask: vi.fn(),
}));
vi.mock("../../../api/hooks", () => ({
  usePlaybooks: () => ({ data: mocks.playbooks, isLoading: false }),
}));
vi.mock("../../../api/graphLayout", () => ({
  useLayoutExtents: (ids: string[]) => ids.map(() => (mocks.extentPending
    ? { pending: true }
    : { layout_version: 1, extent_w: 4, extent_h: 4, node_count: 7 })),
}));
vi.mock("../layout-v2/LayoutCanvas", () => ({ default: (props: Record<string, unknown>) => {
  mocks.layoutProps.current = props;
  const playbooks = (props.playbooks ?? []) as PlaybookSummary[];
  const onPlaybookClick = props.onPlaybookClick as (id: string) => void;
  return <div data-testid="layout-canvas">{playbooks.map((p) => <button key={p.id} onClick={() => onPlaybookClick(p.id)}>{p.id}</button>)}</div>;
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
vi.mock("../useTaskSelection", () => ({ useTaskSelection: () => ({ selectedTaskId: null, selectTask: mocks.selectTask, clearTask: vi.fn() }) }));
beforeEach(() => { mocks.playbooks = []; mocks.pane = { kind: "closed" }; mocks.open.mockClear(); mocks.close.mockClear();
  mocks.query = ""; mocks.showCompleted = false; mocks.extentPending = false; mocks.selectTask.mockClear();
  mocks.layoutProps.current = null; mocks.listProps.current = null; mocks.projectIds = ["alpha"];
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

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

it("renders the layout canvas and maps Show completed to the variant", () => {
  const view = render(<Graph />);
  expect(screen.getByTestId("layout-canvas")).toBeInTheDocument();
  expect(mocks.layoutProps.current).toMatchObject({ projectIds: ["alpha"], variant: "active" });
  // The status strip counts nodes from the layout extents.
  expect(screen.getByText(/7 tasks total/)).toBeInTheDocument();

  mocks.showCompleted = true;
  view.rerender(<Graph />);
  expect(mocks.layoutProps.current).toMatchObject({ variant: "all" });
});

it("keeps the canvas mounted and reports progress while an extent is still building", () => {
  mocks.extentPending = true;
  render(<Graph />);
  expect(screen.getByTestId("layout-canvas")).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Loading tasks…");
  expect(screen.getByText(/0 tasks total/)).toBeInTheDocument();
});

it("uses the mobile layout list on portrait phones, one per project", () => {
  mocks.projectIds = ["alpha", "beta"];
  vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  render(<Graph />);
  expect(screen.getByTestId("layout-lists")).toBeInTheDocument();
  expect(screen.queryByTestId("layout-canvas")).toBeNull();
  expect(mocks.listProps.current).toMatchObject({ projectIds: ["alpha", "beta"] });
});

it("routes a clicked card that belongs to a playbook run through its own payload", () => {
  render(<Graph />);
  const onTaskClick = mocks.layoutProps.current!.onTaskClick as (id: string, task?: unknown) => void;
  // The tiled canvas holds the card, so the run id has to travel with the click.
  onTaskClick("task-a", { id: "task-a", playbook_run_id: "run-1" });
  expect(mocks.selectTask).toHaveBeenCalledWith({ id: "task-a", playbook_run_id: "run-1" });
  onTaskClick("task-b");
  expect(mocks.selectTask).toHaveBeenLastCalledWith({ id: "task-b" });
});
