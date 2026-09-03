import type { ComponentType, MouseEvent, ReactNode } from "react";
import type { Edge, Node } from "@xyflow/react";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PlaybookSemanticGraphView from "../PlaybookSemanticGraphView";
import { graph } from "./fixtures";

const api = vi.hoisted(() => ({
  useGraph: vi.fn(),
  refetch: vi.fn(),
}));
vi.mock("../../../api/hooks", () => ({
  usePlaybookV2Graph: (...args: unknown[]) => api.useGraph(...args),
}));

interface FlowProps {
  nodes: Node<Record<string, unknown>>[];
  edges: Edge[];
  nodeTypes: Record<string, ComponentType<Record<string, unknown>>>;
  children: ReactNode;
  onPaneClick?: (event: MouseEvent) => void;
  onNodeClick?: (event: MouseEvent, node: { id: string; type?: string }) => void;
}
vi.mock("@xyflow/react", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
  ReactFlowProvider: ({ children }: { children: ReactNode }) => children,
  ReactFlow: (props: FlowProps) => (
    <div data-testid="flow">
      {props.nodes.map((n) => {
        const NodeView = props.nodeTypes[n.type!]!;
        return (
          <div key={n.id} data-testid={`node-${n.id}`}>
            <NodeView id={n.id} data={n.data} selected={Boolean(n.selected)} width={n.width} height={n.height} />
          </div>
        );
      })}
      {props.children}
    </div>
  ),
  Background: () => null,
  Handle: () => null,
  Controls: () => null,
  Panel: ({ children }: { children: ReactNode }) => <aside>{children}</aside>,
}));

function ok(data = graph) {
  return { data, isPending: false, isError: false, error: null, refetch: api.refetch };
}

beforeEach(() => {
  api.useGraph.mockReset();
  api.refetch.mockReset();
  api.useGraph.mockReturnValue(ok());
});
afterEach(cleanup);

describe("PlaybookSemanticGraphView", () => {
  it("shows the pinned artifact hash, version, health and activation state", () => {
    render(<PlaybookSemanticGraphView playbookId="default-pipeline" />);
    expect(screen.getByTitle(graph.artifact.artifact_sha256)).toHaveTextContent("a1a1a1a1a1a1");
    expect(screen.getByText("v5")).toBeInTheDocument();
    expect(screen.getByText("question_required")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
  });

  it("says so when the projected artifact is not the active one", () => {
    api.useGraph.mockReturnValue(
      ok({ ...graph, activation: { ...graph.activation, active_artifact_sha256: "sha256:other" } }),
    );
    render(<PlaybookSemanticGraphView playbookId="default-pipeline" artifactSha="sha256:pinned" />);
    expect(screen.getByText("not the active artifact")).toBeInTheDocument();
    expect(api.useGraph).toHaveBeenCalledWith("default-pipeline", {
      artifactSha: "sha256:pinned",
      eventType: undefined,
    });
  });

  it("refetches with the selected event type and never re-derives the filter locally", async () => {
    render(<PlaybookSemanticGraphView playbookId="default-pipeline" />);
    expect(api.useGraph).toHaveBeenLastCalledWith("default-pipeline", {
      artifactSha: undefined,
      eventType: undefined,
    });
    await userEvent.selectOptions(screen.getByLabelText("Event scope"), "spec.approved");
    expect(api.useGraph).toHaveBeenLastCalledWith("default-pipeline", {
      artifactSha: undefined,
      eventType: "spec.approved",
    });
    // The option list still offers every event, so a narrowed scope is reversible.
    expect(screen.getAllByRole("option")).toHaveLength(graph.event_groups!.length + 1);
  });

  it("shows diagnostics without hiding any node of the graph", () => {
    render(<PlaybookSemanticGraphView playbookId="default-pipeline" />);
    expect(
      within(screen.getByRole("region", { name: "Graph diagnostics" })).getByText(
        /contract changed since this artifact was compiled/,
      ),
    ).toBeInTheDocument();
    for (const node of graph.nodes!) {
      expect(screen.getByTestId(`node-${node.id}`)).toBeInTheDocument();
    }
  });

  it("renders no inspector until a step is selected, then the selected step's", async () => {
    render(<PlaybookSemanticGraphView playbookId="default-pipeline" />);
    expect(screen.queryByRole("complementary", { name: "Node inspector" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Inspect step Wait for human approval/ }));
    const inspector = within(screen.getByRole("complementary", { name: "Node inspector" }));
    expect(inspector.getByRole("group", { name: "Wait" })).toBeInTheDocument();
  });

  it("keeps Advanced open across a selection change", async () => {
    render(<PlaybookSemanticGraphView playbookId="default-pipeline" />);
    await userEvent.click(screen.getByRole("button", { name: /Inspect step Ensure a review task/ }));
    await userEvent.click(screen.getByRole("button", { name: "Advanced" }));
    expect(screen.getByTestId("advanced-detail")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Inspect step Classify review risk/ }));
    expect(screen.getByTestId("advanced-detail")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide advanced" })).toBeInTheDocument();
  });

  it("drops a selection that the current event scope no longer contains", async () => {
    const { rerender } = render(<PlaybookSemanticGraphView playbookId="default-pipeline" />);
    await userEvent.click(screen.getByRole("button", { name: /Inspect step Open a spec-ingest gate/ }));
    expect(screen.getByRole("complementary", { name: "Node inspector" })).toBeInTheDocument();

    api.useGraph.mockReturnValue(
      ok({ ...graph, nodes: graph.nodes!.filter((n) => n.rule_id === "review-on-task-completed") }),
    );
    rerender(<PlaybookSemanticGraphView playbookId="default-pipeline" />);
    expect(screen.queryByRole("complementary", { name: "Node inspector" })).not.toBeInTheDocument();
  });

  it("reports loading and offers a retry on failure", async () => {
    api.useGraph.mockReturnValue({ data: undefined, isPending: true, isError: false, error: null, refetch: api.refetch });
    const { rerender } = render(<PlaybookSemanticGraphView playbookId="default-pipeline" />);
    expect(screen.getByText("Loading semantic graph…")).toBeInTheDocument();

    api.useGraph.mockReturnValue({
      data: undefined,
      isPending: false,
      isError: true,
      error: new Error("API 404: no activation"),
      refetch: api.refetch,
    });
    rerender(<PlaybookSemanticGraphView playbookId="default-pipeline" />);
    expect(screen.getByText("API 404: no activation")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(api.refetch).toHaveBeenCalled();
  });
});
