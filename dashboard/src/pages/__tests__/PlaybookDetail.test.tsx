import type { ComponentType, MouseEvent, ReactNode } from "react";
import type { Edge, Node } from "@xyflow/react";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import PlaybookDetail from "../PlaybookDetail";
import type { PlaybookGraphNodeData } from "../playbook-graph/types";
import { REVIEW_PROMPT, graph, layout } from "../playbook-graph/__tests__/fixtures";

interface FlowProps {
  nodes: Node<PlaybookGraphNodeData>[];
  edges: Edge[];
  nodeTypes: Record<string, ComponentType<{ id: string; data: PlaybookGraphNodeData; selected: boolean }>>;
  children: ReactNode;
  onPaneClick?: (event: MouseEvent) => void;
}

vi.mock("@xyflow/react", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
  ReactFlowProvider: ({ children }: { children: ReactNode }) => children,
  ReactFlow: (props: FlowProps) => (
    <div data-testid="flow">
      {props.nodes.map((n) => {
        const NodeView = props.nodeTypes[n.type ?? "playbookStep"]!;
        return <NodeView key={n.id} id={n.id} data={n.data} selected={Boolean(n.selected)} />;
      })}
      {props.children}
    </div>
  ),
  Background: () => null,
  Handle: () => null,
  Controls: () => <div>Zoom controls</div>,
  Panel: ({ children }: { children: ReactNode }) => <aside>{children}</aside>,
}));

const state = vi.hoisted(() => ({
  graph: {} as Record<string, unknown>,
}));
vi.mock("../../api/hooks", () => ({
  usePlaybooks: () => ({
    data: [{ id: "review-flow", scope: "system", version: 3, node_count: 5, triggers: ["task.created"], running_count: 0 }],
  }),
  usePlaybookSource: () => ({
    data: { markdown: "# review-flow source", source_hash: "abc123def456", path: "/vault/playbooks/review-flow.md" },
    isLoading: false,
    refetch: vi.fn(),
  }),
  usePlaybookRuns: () => ({ data: [], isLoading: false }),
  useUpdatePlaybookSource: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeletePlaybook: () => ({ mutateAsync: vi.fn(), isPending: false }),
  usePlaybookGraph: () => ({ ...state.graph, refetch: vi.fn() }),
}));

function page() {
  return (
    <MemoryRouter initialEntries={["/settings/playbooks/review-flow"]}>
      <Routes>
        <Route path="/settings/playbooks/:playbookId" element={<PlaybookDetail />} />
      </Routes>
    </MemoryRouter>
  );
}

beforeEach(() => {
  state.graph = {
    data: { success: true, playbook: { id: "review-flow" }, graph, layout, legend: {} },
    isPending: false,
    isError: false,
    error: null,
  };
});
afterEach(cleanup);

describe("PlaybookDetail tabs", () => {
  it("exposes Source, Graph and Runs and no Compiled tab", () => {
    render(page());
    expect(screen.getByRole("button", { name: "Source" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Graph" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Runs" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Compiled" })).not.toBeInTheDocument();
  });

  it("no longer renders the playbook-summary JSON block anywhere", async () => {
    const user = userEvent.setup();
    render(page());
    for (const tab of ["Source", "Graph", "Runs"]) {
      await user.click(screen.getByRole("button", { name: tab }));
      expect(screen.queryByText(/"scope_identifier"|"running_count"/)).not.toBeInTheDocument();
      expect(screen.queryByText(/Compiled metadata from the active registry/)).not.toBeInTheDocument();
    }
  });

  it("renders the compiled graph and its inspector on the Graph tab", async () => {
    const user = userEvent.setup();
    render(page());
    await user.click(screen.getByRole("button", { name: "Graph" }));
    expect(screen.getByRole("region", { name: "Playbook graph" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Inspect node review" }));
    const inspector = screen.getByRole("complementary", { name: "Node inspector" });
    expect(within(inspector).getByText(REVIEW_PROMPT, { collapseWhitespace: false }).textContent).toBe(REVIEW_PROMPT);
  });

  it("keeps Source and Runs usable when the graph endpoint fails", async () => {
    const user = userEvent.setup();
    state.graph = { data: undefined, isPending: false, isError: true, error: new Error("not compiled") };
    render(page());

    await user.click(screen.getByRole("button", { name: "Graph" }));
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Source" }));
    expect(screen.getByDisplayValue("# review-flow source")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Runs" }));
    expect(screen.getByText("No runs recorded for this playbook.")).toBeInTheDocument();
  });
});
