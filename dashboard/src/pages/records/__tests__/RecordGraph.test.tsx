import { sdk, resetSDK, recordId, revisionId } from "../../knowledge/__tests__/liveMocks";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RecordEdge, RecordNode } from "../../../api/records";
import { workspaceNavigation, projectNavigation } from "../../../shell/projectNavigation";
import RecordGraph from "../RecordGraph";
import RecordsRoute from "../RecordsRoute";
import { readRecordFilters, readRecordSelection, writeRecordFilters, writeRecordSelection } from "../recordUrlState";

vi.mock("../../../panes/knowledge/KnowledgePane", () => ({ default: ({ recordId: id, revisionId: revision, onRevisionChange }: {
  recordId: string; revisionId: string | null; onRevisionChange: (id: string | null) => void;
}) => <div>Selected knowledge {id} · {revision ?? "current"}
  <button type="button" onClick={() => onRevisionChange("earlier-revision")}>Pin earlier</button>
</div> }));


vi.mock("../../../panes/task-detail/TaskDetailBody", () => ({ default: ({ taskId, onOpenTask }: { taskId: string; onOpenTask: (id: string) => void }) => <div>Task detail {taskId}<button onClick={() => onOpenTask("task-2")}>Related task</button></div> }));
vi.mock("../../command-center/TaskToolbar", () => ({ default: ({ onCreated }: { onCreated: (id: string) => void }) =>
  <div>Task filters<button onClick={() => onCreated("new-task")}>Create task</button></div> }));
vi.mock("../../command-center/Tasks", () => ({ default: ({ selection }: { selection: { selectedTaskId: string | null; selectTask: (task: { id: string }) => void } }) => <div role="region" aria-label="Task list"><button data-task-row="task-1" aria-pressed={selection.selectedTaskId === "task-1"} onClick={() => selection.selectTask({ id: "task-1" })}>Fallback task</button></div> }));

const nodes: RecordNode[] = [
  { kind: "task", recordId: "task-record", taskId: "task-1", title: "Do work", status: "READY", archived: false },
  { kind: "task", recordId: "dependent-record", taskId: "task-2", title: "Follow up", status: "DEFINED", archived: false },
  { kind: "knowledge", recordId, revisionId, title: "Retained finding", category: "incident", lifecycle: "active", verification: "unverified" },
];
const edges: RecordEdge[] = [
  { edge_id: "dependency", source_record_id: "task-record", target_record_id: "dependent-record", domain: "execution", type: "blocks", availability: "available" },
  { edge_id: "citation", source_record_id: "task-record", target_record_id: recordId, target_revision_id: "earlier-revision", domain: "informational", type: "produces", availability: "available" },
];

beforeEach(resetSDK);

describe("RecordGraph", () => {
  it("labels kinds and edge domains with text and distinct strokes", () => {
    const { container } = render(<RecordGraph nodes={nodes} edges={edges} onSelect={vi.fn()} />);
    const legend = screen.getByRole("list", { name: "Graph legend" });
    expect(legend).toHaveTextContent("Execution: task dependency, solid arrow");
    expect(legend).toHaveTextContent("Informational: record relationship, dashed arrow");
    expect(container.querySelector('path[data-edge-domain="informational"]')).toHaveAttribute("stroke-dasharray", "6 5");
    expect(container.querySelector('path[data-edge-domain="execution"]')).not.toHaveAttribute("stroke-dasharray");
    expect(screen.getByRole("button", { name: /^Retained finding/ })).toHaveTextContent("Kind: knowledge");
    for (const control of ["Claim", "Complete", "Retry", "Push", "Priority", "Progress"]) {
      expect(screen.queryByRole("button", { name: control })).toBeNull();
    }
  });
  it("preserves node kind and opens the exact pin only through its control", () => {
    const select = vi.fn();
    render(<RecordGraph nodes={nodes} edges={edges} onSelect={select} />);
    fireEvent.click(screen.getByRole("button", { name: /Do work/ }));
    expect(select).toHaveBeenLastCalledWith(nodes[0], null);
    fireEvent.click(screen.getByRole("button", { name: /Open pinned revision/ }));
    expect(select).toHaveBeenLastCalledWith(nodes[2], "earlier-revision");
  });
  it("omits endpoints outside the readable projection and refuses unavailable pins", () => {
    const { container } = render(<RecordGraph nodes={nodes} edges={[
      { ...edges[0]!, edge_id: "private", target_record_id: "private-secret" },
      { ...edges[1]!, availability: "record.revision_redacted" },
    ]} onSelect={vi.fn()} />);
    expect(container.innerHTML).not.toContain("private-secret");
    expect(screen.getByRole("list", { name: "Record relationships" })).toHaveTextContent("revision unavailable");
    expect(screen.queryByRole("button", { name: /Open pinned/ })).toBeNull();
  });
  it("renders a readable empty state", () => {
    render(<RecordGraph nodes={[]} edges={[]} onSelect={vi.fn()} />);
    expect(screen.getByText("No records match these filters.")).toBeInTheDocument();
  });
});

describe("record URL state", () => {
  it("round trips filters and exact selection while preserving other navigation", () => {
    const params = new URLSearchParams("view=graph&drawer=gates");
    const filters = { ...readRecordFilters(params), kind: "knowledge" as const, query: "evidence", lifecycle: "retired" as const };
    const selection = { kind: "knowledge" as const, recordId, revisionId: "old" };
    const written = writeRecordSelection(writeRecordFilters(params, filters), selection);
    expect(readRecordFilters(written)).toEqual(filters);
    expect(readRecordSelection(written)).toEqual(selection);
    expect(written.get("view")).toBe("graph");
    expect(params.has("record")).toBe(false);
  });
  it("does not confuse a task with a knowledge pin", () => {
    expect(readRecordSelection(new URLSearchParams("record=r&revision=old"))).toBeNull();
    const params = writeRecordSelection(new URLSearchParams("revision=old"), { kind: "task", recordId: "r", revisionId: "old" });
    expect(params.has("revision")).toBe(false);
    expect(readRecordSelection(params)).toEqual({ kind: "task", recordId: "r", revisionId: null });
    expect(readRecordFilters(new URLSearchParams("kind=unknown")).kind).toBe("all");
  });
  it("keeps Tasks & Knowledge as the task origin and leaves Graph as default", () => {
    expect(projectNavigation("/projects/p/tasks-knowledge").tab).toBe("tasks-knowledge");
    expect(projectNavigation("/projects/p").tab).toBe("graph");
    expect(workspaceNavigation({ pathname: "/tasks/t", search: "", state: { from: "/projects/p/tasks-knowledge?view=graph&kind=all" } })).toMatchObject({ tab: "tasks-knowledge", projectId: "p", search: "?view=graph&kind=all" });
  });
});

function NavigationProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return <><output data-testid="url">{location.pathname}{location.search}</output>
    <output data-testid="origin">{(location.state as { from?: string } | null)?.from}</output>
    <button type="button" onClick={() => navigate(-1)}>Back</button>
    <button type="button" onClick={() => navigate(1)}>Forward</button></>;
}
function route(path = "/projects/p/tasks-knowledge") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}>
    <NavigationProbe /><Routes>
      <Route path="/projects/:projectId/tasks-knowledge" element={<RecordsRoute />} />
      <Route path="/tasks/:taskId" element={<div>Existing task detail</div>} />
    </Routes>
  </MemoryRouter></QueryClientProvider>);
}
function mixedSDK() {
  sdk.recordSearch.mockResolvedValue({ data: { success: true, items: [
    { kind: "task", record_id: "task-record", task_id: "task-1", title: "Do work", status: "READY" },
    { kind: "knowledge", record_id: recordId, revision_id: revisionId, title: "Retained finding", category: "incident", lifecycle: "active", verification: "unverified" },
  ], next_cursor: null } });
  sdk.recordShow.mockImplementation(async ({ body }: { body: { identity: string } }) => ({ data: {
    success: true, edges: body.identity === "record:task-record" ? [edges[1]] : [],
  } }));
}

describe("All records live route", () => {
  it("opens newly created tasks and clears stale task filters in one URL update", async () => {
    mixedSDK(); route("/projects/p/tasks-knowledge?kind=task&q=old&status=FAILED&window=week&held=1&completed=1&focus=container");
    fireEvent.click(await screen.findByRole("button", { name: "Create task" }));
    expect(await screen.findByText("Task detail new-task")).toBeInTheDocument();
    const url = new URL(screen.getByTestId("url").textContent!, "http://aq.local");
    expect(url.searchParams.get("task")).toBe("new-task");
    expect(url.searchParams.get("kind")).toBe("task");
    expect(url.searchParams.get("focus")).toBe("container");
    for (const key of ["q", "status", "window", "held", "completed"]) expect(url.searchParams.has(key)).toBe(false);
  });
  it("is inert when UI activation is disabled", async () => {
    sdk.recordCapabilities.mockResolvedValue({ data: { capabilities: { enabled: true, ui_enabled: false, enabled_projects: ["p"] } } });
    route();
    expect(await screen.findByText("Knowledge is unavailable for this project. Tasks remain available.")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Task list" })).toBeInTheDocument();
    expect(sdk.recordSearch).not.toHaveBeenCalled();
    expect(sdk.recordShow).not.toHaveBeenCalled();
  });
  it("searches explicit mixed kinds and sends facets through the generated SDK", async () => {
    mixedSDK(); route();
    const list = await screen.findByRole("list", { name: "Record results" });
    expect(within(list).getByRole("button", { name: /Do work/ })).toBeInTheDocument();
    expect(sdk.recordSearch).toHaveBeenCalledWith({ body: expect.objectContaining({ project_id: "p", kind: "all" }) });
    fireEvent.change(screen.getByLabelText("Record kind"), { target: { value: "knowledge" } });
    await waitFor(() => expect(sdk.recordSearch).toHaveBeenLastCalledWith({ body: expect.objectContaining({ kind: "knowledge" }) }));
    expect(screen.getByTestId("url")).toHaveTextContent("kind=knowledge");
    expect(sdk.recordShow).not.toHaveBeenCalled();
  });
  it("opens task detail with the exact originating workspace", async () => {
    mixedSDK(); route("/projects/p/tasks-knowledge?q=work");
    fireEvent.click(await screen.findByRole("button", { name: /Do work/ }));
    expect(await screen.findByText("Task detail task-1")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Record results" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Do work/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("link", { name: "Open full page" })).toHaveAttribute("href", "/tasks/task-1");
    expect(screen.getByTestId("url")).toHaveTextContent("/projects/p/tasks-knowledge?q=work&task=task-1");

  });
  it("restores exact knowledge selections with Back and Forward", async () => {
    mixedSDK(); route("/projects/p/tasks-knowledge?view=graph");
    fireEvent.click(await screen.findByRole("button", { name: /^Retained finding/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Pin earlier" }));
    expect(screen.getByTestId("url")).toHaveTextContent("recordKind=knowledge");
    expect(screen.getByTestId("url")).toHaveTextContent("revision=earlier-revision");
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    await waitFor(() => expect(screen.getByTestId("url")).not.toHaveTextContent("revision="));
    fireEvent.click(screen.getByRole("button", { name: "Forward" }));
    await waitFor(() => expect(screen.getByTestId("url")).toHaveTextContent("revision=earlier-revision"));
    expect(sdk.recordShow).toHaveBeenCalledWith({ body: expect.objectContaining({ include_edges: true, revision_id: revisionId }) });
  });
  it("reports a fixed search error without echoing server information", async () => {
    sdk.recordSearch.mockRejectedValue(new Error("private server details"));
    route();
    expect(await screen.findByRole("alert")).toHaveTextContent("Record search is temporarily unavailable.");
    expect(screen.queryByText(/private server/)).toBeNull();
  });
});

 describe("combined detail interaction", () => {
  it("closes with Escape and keeps the mounted list scroll, filters and query", async () => {
    mixedSDK(); const view = route("/projects/p/tasks-knowledge?q=work&category=incident&status=READY&drawer=gates");
    const row = await screen.findByRole("button", { name: /Do work/ });
    const scroll = view.container.querySelector("[data-record-scroll]")!;
    scroll.scrollTop = 150;
    fireEvent.click(row);
    expect(await screen.findByText("Task detail task-1")).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByRole("complementary", { name: "Record detail" })).toBeNull();
    expect(screen.getByTestId("url")).toHaveTextContent("q=work&category=incident&status=READY&drawer=gates");
    expect(screen.getByTestId("url")).not.toHaveTextContent("task=");
    expect(view.container.querySelector("[data-record-scroll]")).toBe(scroll);
    expect(scroll.scrollTop).toBe(150);
  });
  it("moves selection with arrows and J/K, preserving editable field behavior", async () => {
    mixedSDK(); route();
    const task = await screen.findByRole("button", { name: /Do work/ });
    task.focus(); fireEvent.keyDown(task, { key: "j" });
    expect(screen.getByRole("button", { name: /Retained finding/ })).toHaveAttribute("aria-pressed", "true");
    fireEvent.keyDown(document.activeElement!, { key: "ArrowUp" });
    expect(task).toHaveAttribute("aria-pressed", "true");
    fireEvent.keyDown(task, { key: "ArrowDown" });
    fireEvent.keyDown(document.activeElement!, { key: "k" });
    expect(task).toHaveAttribute("aria-pressed", "true");
    const search = screen.getByRole("searchbox", { name: "Search records" });
    fireEvent.keyDown(search, { key: "j" });
    expect(task).toHaveAttribute("aria-pressed", "true");
  });
  it("restores a task selection from a shared URL and browser history", async () => {
    mixedSDK(); route("/projects/p/tasks-knowledge?q=work&task=task-1");
    expect(await screen.findByText("Task detail task-1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Related task" }));
    expect(await screen.findByText("Task detail task-2")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByText("Task detail task-1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close detail" }));
    expect(screen.getByTestId("url")).not.toHaveTextContent("task=");
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByText("Task detail task-1")).toBeInTheDocument();
  });
  it("resolves legacy task record identities before exposing a full-page link", async () => {
    mixedSDK(); sdk.recordShow.mockResolvedValue({ data: { kind: "task", task: { id: "task-1" } } });
    route("/projects/p/tasks-knowledge?record=opaque-record&recordKind=task");
    expect(await screen.findByText("Task detail task-1")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open full page" })).toHaveAttribute("href", "/tasks/task-1");
  });
  it("keeps the exact knowledge revision in its full-page URL and close removes only selection", async () => {
    mixedSDK(); route("/projects/p/tasks-knowledge?record=finding&recordKind=knowledge&revision=exact&q=needle");
    expect(await screen.findByText(/Selected knowledge finding/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open full page" })).toHaveAttribute("href", "/projects/p/knowledge/finding?revision=exact");
    fireEvent.click(screen.getByRole("button", { name: "Close detail" }));
    expect(screen.getByTestId("url")).toHaveTextContent("/projects/p/tasks-knowledge?q=needle");
  });
  it("uses a modal sheet with a Back button at narrow widths", async () => {
    const original = window.matchMedia;
    Object.defineProperty(window, "matchMedia", { configurable: true, writable: true, value: (query: string) => ({
      matches: query.includes("1023"), media: query, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    }) });
    try {
      mixedSDK(); const view = route("/projects/p/tasks-knowledge?q=work&task=task-1");
      const sheet = await screen.findByRole("dialog", { name: "Record detail" });
      expect(sheet).toHaveAttribute("aria-modal", "true");
      expect(sheet).toHaveAttribute("data-layout", "sheet");
      expect(view.container.querySelector("[data-record-list]")).toHaveAttribute("inert");
      expect(screen.queryByRole("separator")).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Back to list" }));
      expect(screen.queryByRole("dialog", { name: "Record detail" })).toBeNull();
      expect(screen.getByTestId("url")).toHaveTextContent("q=work");
    } finally { Object.defineProperty(window, "matchMedia", { configurable: true, writable: true, value: original }); }
  });
 });
