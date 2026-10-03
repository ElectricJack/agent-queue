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
  it("keeps All records as the task origin and leaves Work as default", () => {
    expect(projectNavigation("/projects/p/records").tab).toBe("records");
    expect(projectNavigation("/projects/p").tab).toBe("graph");
    expect(workspaceNavigation({ pathname: "/tasks/t", search: "", state: { from: "/projects/p/records?view=graph&kind=all" } })).toMatchObject({ tab: "records", projectId: "p", search: "?view=graph&kind=all" });
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
function route(path = "/projects/p/records") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}>
    <NavigationProbe /><Routes>
      <Route path="/projects/:projectId/records" element={<RecordsRoute />} />
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
  it("is inert when UI activation is disabled", async () => {
    sdk.recordCapabilities.mockResolvedValue({ data: { capabilities: { enabled: true, ui_enabled: false, enabled_projects: ["p"] } } });
    route();
    expect(await screen.findByText("All records is unavailable for this project.")).toBeInTheDocument();
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
    mixedSDK(); route("/projects/p/records?q=work");
    fireEvent.click(await screen.findByRole("button", { name: /Do work/ }));
    expect(await screen.findByText("Existing task detail")).toBeInTheDocument();
    expect(screen.getByTestId("url")).toHaveTextContent("/tasks/task-1");
    expect(screen.getByTestId("origin")).toHaveTextContent("/projects/p/records?q=work");
  });
  it("restores exact knowledge selections with Back and Forward", async () => {
    mixedSDK(); route("/projects/p/records?view=graph");
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
