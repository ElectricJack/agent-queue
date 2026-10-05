import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { testQueryClient } from "../../../testUtils/dashboardState";
import FocusShell from "../FocusShell";
import FocusBatch from "../FocusBatch";

const wire = vi.hoisted(() => ({ projects: vi.fn(), list: vi.fn() }));

vi.mock("../../../api/hooks", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../api/hooks")>()),
  useProjects: wire.projects,
}));
vi.mock("../../../api/graphLayout", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../api/graphLayout")>()),
  fetchList: wire.list,
}));

/** A layout node as the graph sends it: geometry plus the delivery projection. */
function node(overrides: Record<string, unknown> = {}) {
  return {
    id: "epic-1",
    title: "Discord as a chat extension",
    status: "IN_PROGRESS",
    priority: 100,
    x: 0,
    y: 0,
    w: 1,
    h: 1,
    depth: 0,
    kind: "task",
    pr_url: null,
    delivery: null,
    ...overrides,
  };
}

const integrating = {
  state: "integrating",
  label: "Integrating",
  display_status: "Integrating",
  hold: null,
  reason: "3 commits on the integration branch",
  remedy: "Waiting on CI",
  responsible: { kind: "batch", id: "batch-77", label: "batch 77" },
  since: 1790000000,
  links: [],
  evidence: "current",
  implementation_completed: 1,
  implementation_total: 2,
};

function renderAt(path: string) {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/focus" element={<FocusShell />}>
            <Route path="batches/:batchId" element={<FocusBatch />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  wire.projects.mockReturnValue({
    data: [{ id: "agent-queue", name: "Agent Queue" }],
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  });
  // `fetchList` resolves the parsed body, not the HTTP envelope.
  wire.list.mockResolvedValue({
    nodes: [
      node({ delivery: integrating }),
      node({ id: "child-1", title: "Focus routes", container_id: "epic-1", status: "COMPLETED" }),
      node({ id: "child-2", title: "Digest", container_id: "epic-1", status: "IN_PROGRESS" }),
      node({ id: "epic-other", title: "Unrelated epic" }),
    ],
    next_cursor: null,
    layout_version: 3,
  });
});

describe("FocusBatch", () => {
  it("resolves the batch from the epic that names it, with its tasks", async () => {
    renderAt("/focus/batches/batch-77");
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent("Batch batch-77");
    expect(await screen.findByText("Discord as a chat extension")).toBeInTheDocument();
    expect(screen.getByText(/1 collection integrating under this batch/)).toBeInTheDocument();
    expect(screen.getByText("3 commits on the integration branch")).toBeInTheDocument();
    expect(screen.getByText("Waiting on CI")).toBeInTheDocument();
    expect(screen.getByText("Evidence: current")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Focus routes/ })).toHaveAttribute(
      "href",
      "/focus/tasks/child-1",
    );
    expect(screen.getByRole("link", { name: /Digest/ })).toHaveAttribute("href", "/focus/tasks/child-2");
    expect(wire.list).toHaveBeenCalledWith("agent-queue", expect.objectContaining({ variant: "all" }), expect.anything());
  });

  it("links the pull request the batch is producing", async () => {
    wire.list.mockResolvedValue({
      nodes: [node({ delivery: integrating, pr_url: "https://github.com/o/r/pull/9" })],
      next_cursor: null,
      layout_version: 3,
    });
    renderAt("/focus/batches/batch-77");
    expect(await screen.findByRole("link", { name: "Pull request" })).toHaveAttribute(
      "href",
      "https://github.com/o/r/pull/9",
    );
  });

  it("says so when no project reports the batch", async () => {
    renderAt("/focus/batches/batch-unknown");
    expect(await screen.findByText("No project reports this integration batch.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open in full dashboard" })).toHaveAttribute(
      "href",
      "/command-center/graph",
    );
  });

  it("waits out a layout the server has not built yet", async () => {
    wire.list.mockResolvedValue({ pending: true });
    renderAt("/focus/batches/batch-77");
    expect(await screen.findByText("No project reports this integration batch.")).toBeInTheDocument();
  });

  it("offers Retry when the project read fails", async () => {
    const refetch = vi.fn();
    wire.projects.mockReturnValue({ data: undefined, isLoading: false, isError: true, error: new Error("project list unavailable"), refetch });
    renderAt("/focus/batches/batch-77");
    expect(await screen.findByRole("alert")).toHaveTextContent("project list unavailable");
    screen.getByRole("button", { name: "Retry" }).click();
    expect(refetch).toHaveBeenCalled();
  });
});