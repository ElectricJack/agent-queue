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
  sourceMarkdown: "# review-flow source",
  sourceData: null as { markdown: string; source_hash: string; path: string } | null,
  updateSource: vi.fn(),
  deletePlaybook: vi.fn(),
  playbooks: [] as Array<Record<string, unknown>>,
}));

function sourceData() {
  if (!state.sourceData || state.sourceData.markdown !== state.sourceMarkdown) {
    state.sourceData = {
      markdown: state.sourceMarkdown,
      source_hash: "abc123def456",
      path: "/vault/playbooks/review-flow.md",
    };
  }
  return state.sourceData;
}
vi.mock("../../api/hooks", () => ({
  usePlaybooks: () => ({
    data: state.playbooks,
  }),
  // Mirror react-query: the same query data object identity across renders,
  // so the editor's reset-on-new-source effect does not fire on every render.
  usePlaybookSource: () => ({
    data: sourceData(),
    isLoading: false,
    refetch: vi.fn(),
  }),
  usePlaybookRuns: () => ({ data: [], isLoading: false }),
  useUpdatePlaybookSource: () => ({ mutateAsync: state.updateSource, isPending: false }),
  usePlaybookV2Graph: () => ({ ...state.semanticGraph, refetch: vi.fn() }),
  useSavePlaybookGraphLayout: () => ({ mutate: vi.fn() }),
  usePlaybookActivationHealth: () => state.activationHealth,
  usePlaybookArtifacts: () => ({ data: { artifacts: [] } }),
  usePlaybookArtifactDiff: () => ({ data: undefined }),
  usePlaybookPendingEvents: () => ({ data: { events: [] } }),
  useSetPlaybookActivation: () => ({ mutate: vi.fn() }),
  usePlaybookPendingEventAction: () => ({ mutate: vi.fn() }),
  usePlaybookRunOverlay: () => ({ data: undefined }),
  useDeletePlaybook: () => ({ mutateAsync: state.deletePlaybook, isPending: false }),
}));

function page() {
  return (
    <MemoryRouter initialEntries={["/settings/playbooks/review-flow"]}>
      <Routes>
        <Route path="/settings/playbooks/:playbookId" element={<PlaybookDetail />} />
        <Route path="/settings/playbooks" element={<p>Playbook list</p>} />
      </Routes>
    </MemoryRouter>
  );
}

/** The installed entry the delete dialog reads its scope and hash from. */
const installed = {
  playbook_id: "review-flow",
  scope: "system",
  scope_identifier: "",
  enabled: false,
  active_artifact_sha256: "d".repeat(64),
};

beforeEach(() => {
  state.playbooks = [{ id: "review-flow", scope: "system", version: 3, node_count: 5, triggers: ["task.created"], running_count: 0 }];
  state.semanticGraph = { data: semanticGraph, isPending: false, isError: false, error: null };
  state.activationHealth = {
    data: { activations: [semanticGraph.activation] },
    isPending: false,
  };
  state.sourceMarkdown = "# review-flow source";
  state.sourceData = null;
  state.updateSource = vi.fn().mockResolvedValue({
    compiled: true,
    version: 4,
    node_count: 5,
    source_hash: "def456abc123",
  });
  state.deletePlaybook.mockReset();
  state.deletePlaybook.mockResolvedValue({ success: true, deleted: true });
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
    expect(screen.getByRole("heading", { name: "review-flow source" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Runs" }));
    expect(screen.getByText("No runs recorded for this playbook.")).toBeInTheDocument();
  });
});

describe("PlaybookDetail source view/edit cycle", () => {
  const MD = [
    "# Heading",
    "",
    "- first item",
    "- second item",
    "",
    "[docs](https://example.com/docs)",
    "",
    "```yaml",
    "when: task.created",
    "```",
    "",
  ].join("\n");

  it("renders the source as markdown by default, with no textarea", () => {
    state.sourceMarkdown = MD;
    render(page());

    expect(screen.getByRole("heading", { name: "Heading" })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "first item",
      "second item",
    ]);
    expect(screen.getByRole("link", { name: "docs" })).toHaveAttribute(
      "href",
      "https://example.com/docs",
    );
    expect(screen.getByText("when: task.created").closest("pre")).not.toBeNull();

    expect(screen.queryByRole("textbox", { name: "Playbook markdown source" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save & Compile" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
  });

  it("opens the raw markdown only after Edit is clicked", async () => {
    const user = userEvent.setup();
    state.sourceMarkdown = MD;
    render(page());

    await user.click(screen.getByRole("button", { name: "Edit" }));

    const box = screen.getByRole("textbox", { name: "Playbook markdown source" });
    expect(box).toHaveValue(MD);
    expect(screen.queryByRole("heading", { name: "Heading" })).not.toBeInTheDocument();
  });

  it("saves the edit and returns to the rendered updated content", async () => {
    const user = userEvent.setup();
    state.sourceMarkdown = "# Before";
    render(page());

    await user.click(screen.getByRole("button", { name: "Edit" }));
    const box = screen.getByRole("textbox", { name: "Playbook markdown source" });
    await user.clear(box);
    await user.type(box, "# After");
    await user.click(screen.getByRole("button", { name: "Save & Compile" }));

    expect(state.updateSource).toHaveBeenCalledWith({
      playbook_id: "review-flow",
      markdown: "# After",
      expected_source_hash: "abc123def456",
    });
    expect(screen.getByRole("heading", { name: "After" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Playbook markdown source" })).not.toBeInTheDocument();
  });

  it("cancels back to the saved content without persisting the edit", async () => {
    const user = userEvent.setup();
    state.sourceMarkdown = "# Before";
    render(page());

    await user.click(screen.getByRole("button", { name: "Edit" }));
    const box = screen.getByRole("textbox", { name: "Playbook markdown source" });
    await user.clear(box);
    await user.type(box, "# Scratch");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(state.updateSource).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "Before" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByRole("textbox", { name: "Playbook markdown source" })).toHaveValue("# Before");
  });

  it("keeps the source text intact through a view/edit round trip", async () => {
    const user = userEvent.setup();
    state.sourceMarkdown = MD;
    render(page());

    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByRole("textbox", { name: "Playbook markdown source" })).toHaveValue(MD);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByRole("textbox", { name: "Playbook markdown source" })).toHaveValue(MD);
  });

  it("stays in the editor and surfaces the conflict when the vault moved", async () => {
    const user = userEvent.setup();
    state.sourceMarkdown = "# Before";
    state.updateSource = vi.fn().mockResolvedValue({ compiled: false, error: "conflict" });
    render(page());

    await user.click(screen.getByRole("button", { name: "Edit" }));
    const box = screen.getByRole("textbox", { name: "Playbook markdown source" });
    await user.clear(box);
    await user.type(box, "# Before edit");
    await user.click(screen.getByRole("button", { name: "Save & Compile" }));

    expect(screen.getByRole("textbox", { name: "Playbook markdown source" })).toBeInTheDocument();
    expect(screen.getByText(/Vault changed underneath this editor/)).toBeInTheDocument();
  });
});

describe("PlaybookDetail delete", () => {
  it("deletes the playbook and leaves the page it can no longer show", async () => {
    state.activationHealth = { data: { activations: [installed] }, isPending: false };
    const user = userEvent.setup();
    render(page());

    await user.click(screen.getByRole("button", { name: "Delete playbook review-flow" }));
    await user.click(screen.getByRole("button", { name: "Delete playbook" }));

    expect(state.deletePlaybook).toHaveBeenCalledWith({
      playbook_id: "review-flow",
      scope: "system",
      scope_identifier: "",
      artifact_sha256: "d".repeat(64),
    });
    expect(await screen.findByText("Playbook list")).toBeInTheDocument();
  });

  it("stays on the page and shows the refusal when the daemon says no", async () => {
    state.activationHealth = { data: { activations: [installed] }, isPending: false };
    state.deletePlaybook.mockRejectedValue(new Error("API 422: playbook is enabled or artifact changed; deletion refused"));
    const user = userEvent.setup();
    render(page());

    await user.click(screen.getByRole("button", { name: "Delete playbook review-flow" }));
    await user.click(screen.getByRole("button", { name: "Delete playbook" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("deletion refused");
    expect(screen.queryByText("Playbook list")).not.toBeInTheDocument();
  });

  it("cancels without deleting", async () => {
    state.activationHealth = { data: { activations: [installed] }, isPending: false };
    const user = userEvent.setup();
    render(page());

    await user.click(screen.getByRole("button", { name: "Delete playbook review-flow" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(state.deletePlaybook).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("refuses an ambiguous duplicate ID instead of deleting the first scope", async () => {
    state.playbooks = [
      { id: "review-flow", scope: "system", version: 3, node_count: 5, triggers: [], running_count: 0 },
      { id: "review-flow", scope: "project", scope_identifier: "alpha", version: 3, node_count: 5, triggers: [], running_count: 0 },
    ];
    state.activationHealth = {
      data: {
        activations: [
          installed,
          { ...installed, scope: "project", scope_identifier: "alpha", active_artifact_sha256: "e".repeat(64) },
        ],
      },
      isPending: false,
    };
    const user = userEvent.setup();
    render(page());

    await user.click(screen.getByRole("button", { name: "Delete playbook review-flow" }));

    expect(await screen.findByRole("status")).toHaveTextContent("installed in more than one scope");
    expect(screen.getByRole("button", { name: "Delete playbook" })).toBeDisabled();
    expect(state.deletePlaybook).not.toHaveBeenCalled();
  });
});
