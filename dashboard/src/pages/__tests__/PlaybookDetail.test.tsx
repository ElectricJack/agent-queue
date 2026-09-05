import type { ComponentType, MouseEvent, ReactNode } from "react";
import type { Edge, Node } from "@xyflow/react";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import PlaybookDetail from "../PlaybookDetail";
import { graph as semanticGraph } from "../playbook-graph-v2/__tests__/fixtures";

type FlowNodeData = Record<string, unknown>;

interface FlowProps {
  nodes: Node<FlowNodeData>[];
  edges: Edge[];
  nodeTypes: Record<string, ComponentType<{ id: string; data: FlowNodeData; selected: boolean }>>;
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
  semanticGraph: {} as Record<string, unknown>,
  activationHealth: {} as Record<string, unknown>,
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
  usePlaybookV2Graph: () => ({ ...state.semanticGraph, refetch: vi.fn() }),
  useSavePlaybookGraphLayout: () => ({ mutate: vi.fn() }),
  usePlaybookActivationHealth: () => state.activationHealth,
  usePlaybookArtifacts: () => ({ data: { artifacts: [] } }),
  usePlaybookArtifactDiff: () => ({ data: undefined }),
  usePlaybookPendingEvents: () => ({ data: { events: [] } }),
  useSetPlaybookActivation: () => ({ mutate: vi.fn() }),
  usePlaybookPendingEventAction: () => ({ mutate: vi.fn() }),
  usePlaybookRunOverlay: () => ({ data: undefined }),
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
  state.semanticGraph = { data: semanticGraph, isPending: false, isError: false, error: null };
  state.activationHealth = {
    data: { activations: [semanticGraph.activation] },
    isPending: false,
  };
});
afterEach(cleanup);

describe("PlaybookDetail tabs", () => {
  it("exposes the V2 Graph tab without legacy graph tabs", () => {
    render(page());
    expect(screen.getByRole("button", { name: "Source" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Graph" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Runs" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Semantic graph" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Compiled" })).not.toBeInTheDocument();
  });

  it("renders the V2 artifact graph on the Graph tab", async () => {
    const user = userEvent.setup();
    render(page());

    await user.click(screen.getByRole("button", { name: "Graph" }));
    expect(screen.getByRole("region", { name: "Playbook semantic graph" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Inspect step Ensure a review task/ })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Playbook graph" })).not.toBeInTheDocument();
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

  it("keeps Source and Runs usable when the graph endpoint fails", async () => {
    const user = userEvent.setup();
    state.semanticGraph = { data: undefined, isPending: false, isError: true, error: new Error("not compiled") };
    render(page());

    await user.click(screen.getByRole("button", { name: "Graph" }));
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Source" }));
    expect(screen.getByDisplayValue("# review-flow source")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Runs" }));
    expect(screen.getByText("No runs recorded for this playbook.")).toBeInTheDocument();
  });
});
