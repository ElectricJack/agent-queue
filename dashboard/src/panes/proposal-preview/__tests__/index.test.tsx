import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ProposalPreviewPane from "../index";
import type { ProposalDetail } from "../hooks";
import type { IngestBatchDetail } from "../../../api/client";

const mockUseProposal = vi.fn();
const mockUseProposalGate = vi.fn();
const mockUsePrepareProposalDecision = vi.fn();
const mockUseApplyProposalDecision = vi.fn();
const mockUseIngestBatch = vi.fn();
const mockUseTask = vi.fn();

vi.mock("../../../api/hooks", () => ({ useTask: (...args: unknown[]) => mockUseTask(...args) }));

vi.mock("../hooks", async () => {
  const actual = await vi.importActual<typeof import("../hooks")>("../hooks");
  return {
    ...actual,
    useProposal: (...args: unknown[]) => mockUseProposal(...args),
    useIngestBatch: (...args: unknown[]) => mockUseIngestBatch(...args),
    useProposalGate: (...args: unknown[]) => mockUseProposalGate(...args),
    usePrepareProposalDecision: (...args: unknown[]) => mockUsePrepareProposalDecision(...args),
    useApplyProposalDecision: (...args: unknown[]) => mockUseApplyProposalDecision(...args),
  };
});

const mockOpen = vi.fn();
vi.mock("../../store", () => ({
  useShellPaneStore: () => ({
    open: mockOpen,
    close: vi.fn(),
    state: { kind: "closed" },
    setArgs: vi.fn(),
    setWidth: vi.fn(),
    registry: {},
  }),
}));

const fixtureProposal: ProposalDetail = {
  proposal_id: "prop-abc123",
  project_id: "demo",
  source: "spec:projects/demo/specs/2026-08-21-thing.md",
  status: "ready",
  tasks: [
    { tempId: "a", title: "Setup schema", description: "Add table", priority: 100 },
    { tempId: "b", title: "Add API", description: "Route", priority: 90 },
  ],
  edges: [{ from: "b", to: "a", dep_type: "blocks" }],
};

function noopProps(overrides: Partial<Parameters<typeof ProposalPreviewPane>[0]> = {}) {
  return {
    args: { proposalId: "prop-abc123" },
    close: vi.fn(),
    setArgs: vi.fn(),
    setToolbar: vi.fn(),
    setShortcuts: vi.fn(),
    ...overrides,
  };
}

function renderPane(overrides: Partial<Parameters<typeof ProposalPreviewPane>[0]> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const props = noopProps(overrides);
  render(
    <QueryClientProvider client={queryClient}>
      <ProposalPreviewPane {...props} />
    </QueryClientProvider>,
  );
  return props;
}

beforeEach(() => {
  mockUseTask.mockReset();
  mockUseIngestBatch.mockReset();
  mockUseIngestBatch.mockReturnValue({ data: undefined, isPending: false, isError: false, refetch: vi.fn() });
  mockUseProposal.mockReset();
  mockUseProposalGate.mockReset();
  mockUsePrepareProposalDecision.mockReset();
  mockUseApplyProposalDecision.mockReset();
  mockOpen.mockReset();
  mockUsePrepareProposalDecision.mockReturnValue({ mutateAsync: vi.fn().mockImplementation(async (args) => ({ args, preview_id: "preview", idempotency_key: "key", effect: "Confirm the exact proposed effect." })), isPending: false });
  mockUseApplyProposalDecision.mockReturnValue({ mutateAsync: vi.fn(), isPending: false });
  mockUseProposalGate.mockReturnValue({ data: [], gate: undefined });
});

const batch: IngestBatchDetail = {
  batch_id: "prop-abc123", project_id: "demo", state: "reviewing", structure_revision: 2,
  structure_digest: "graph-sha", source: "spec:approved.md", source_review_id: "rev-source",
  source_revision: 3, source_content_sha256: "source-sha", decision_id: "decision",
  lineage: { lineage_kind: "resolved", design_review_id: "rev-design", design_revision: 1 },
  reviewer_task_id: "reviewer", members: { api: "real-api" },
  membership: [{ task_id: "real-api", role: "leaf", state: "active", section_anchor: "Scope", primary_outcome: "Preserve constraint" }],
  section_verdicts: { Scope: { verdict: "fixed", rationale: "Restored constraint" } },
  reverse_coverage_verdict: "faithful", walkthrough_verdict: "faithful",
  structural_findings: [{ code: "size", disposition: "fixed" }], diff: { edited: ["real-api"] },
  receipts: [{ structure_revision: 1, passing: true }],
};

describe("ProposalPreviewPane — ingestion batch", () => {
  it("shows an operational root's recorded condition, real delivery edges and unassigned owner", () => {
    mockUseProposal.mockReturnValue({ data: { ...fixtureProposal, status: "held" } });
    mockUseIngestBatch.mockReturnValue({ data: { ...batch, members: { ops: "ops" }, membership: [
      { task_id: "ops", role: "operational_root", state: "active", primary_outcome: "Cut over" },
    ] } });
    mockUseTask.mockReturnValue({ data: { assigned_agent: null,
      delivery_condition: { kind: "delivery-of", targets: ["source"] },
      depends_on: [{ id: "source", title: "Land source", dep_type: "delivery-of" }],
    } });
    renderPane();
    expect(screen.getByText("Owner: Unassigned")).toBeInTheDocument();
    expect(screen.getByText("Recorded delivery condition")).toBeInTheDocument();
    expect(screen.getByText(/Requires landed delivery of/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Land source (source)" }));
    expect(mockOpen).toHaveBeenCalledWith("task-detail", { taskId: "source" });
  });

  it("shows revision-bound review evidence and real members without proposal actions or shortcuts", () => {
    mockUseProposal.mockReturnValue({ data: { ...fixtureProposal, status: "held" }, isPending: false });
    mockUseIngestBatch.mockReturnValue({ data: batch });
    const setShortcuts = vi.fn();
    const setArgs = vi.fn();
    renderPane({ setShortcuts, setArgs });
    expect(setArgs).toHaveBeenCalledWith({ proposalId: "prop-abc123", batchProjectId: "demo" });
    expect(screen.getByRole("heading", { name: "Held for fidelity review" })).toBeInTheDocument();
    expect(screen.getByText(/Review rev-source revision 3/)).toBeInTheDocument();
    expect(screen.getByText(/rev-design/)).toBeInTheDocument();
    expect(screen.getByText(/Restored constraint/)).toBeInTheDocument();
    expect(screen.getByText(/"disposition": "fixed"/)).toBeInTheDocument();
    expect(screen.getByText(/"edited"/)).toBeInTheDocument();
    expect(screen.getByText("No release receipt recorded.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve|discard/i })).not.toBeInTheDocument();
    expect(setShortcuts.mock.lastCall?.[0].map((s: { key: string }) => s.key)).toEqual(["r", "s"]);
    fireEvent.click(screen.getByRole("button", { name: "real-api" }));
    expect(mockOpen).toHaveBeenCalledWith("task-detail", { taskId: "real-api" });
    fireEvent.click(screen.getByRole("button", { name: "reviewer" }));
    expect(mockOpen).toHaveBeenCalledWith("task-detail", { taskId: "reviewer" });
    expect(screen.queryByTestId("proposal-graph")).not.toBeInTheDocument();
  });

  it("keeps superseded receipts separate from empty current verdicts and shows no design lineage", () => {
    mockUseProposal.mockReturnValue({ data: { ...fixtureProposal, status: "held" } });
    mockUseIngestBatch.mockReturnValue({ data: { ...batch, state: "needs_scope", lineage: { lineage_kind: "none" }, section_verdicts: {} } });
    renderPane();
    expect(screen.getByText("Design lineage: none")).toBeInTheDocument();
    expect(screen.getByText("No current verdicts recorded.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("batch remains held");
    expect(screen.getByText(/"passing": true/)).toBeInTheDocument();
  });

  it("opens released evidence from task membership without requiring a proposal read", () => {
    mockUseProposal.mockReturnValue({ isError: true });
    mockUseIngestBatch.mockReturnValue({ data: { ...batch, state: "released", release_actor: "supervisor", release_revision: 2, release_digest: "graph-sha", release_receipt: { task_ids: ["real-api"] } } });
    renderPane({ args: { proposalId: batch.batch_id, batchProjectId: "demo" } });
    expect(screen.getByText("Batch released")).toBeInTheDocument();
    expect(screen.getByText(/Actor: supervisor/)).toBeInTheDocument();
    expect(screen.getByText("Release proof")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve|discard/i })).not.toBeInTheDocument();
  });

  it("fails visibly when batch evidence cannot load, without falling back to staged tasks", () => {
    const refetch = vi.fn();
    mockUseProposal.mockReturnValue({ data: { ...fixtureProposal, status: "held" } });
    mockUseIngestBatch.mockReturnValue({ isError: true, refetch });
    renderPane();
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(refetch).toHaveBeenCalledOnce();
    expect(screen.queryByTestId("proposal-task-list")).not.toBeInTheDocument();
  });
});

describe("ProposalPreviewPane — header + graph + list", () => {
  it("shows existing-task diff, controls, removed dependencies and comments for approval", () => {
    mockUseProposal.mockReturnValue({
      data: {
        ...fixtureProposal,
        tasks: [],
        edges: [],
        edits: [{ task_id: "existing", action: "pause" }],
        remove_edges: [{ from: "existing", to: "old", dep_type: "blocks" }],
        comments: [{ task_id: "existing", body: "Review this finding" }],
        diff: { tasks: [{ task_id: "existing", before: { status: "READY" }, after: { status: "PAUSED" } }] },
      },
      isPending: false, isError: false, refetch: vi.fn(),
    });
    renderPane();
    expect(screen.getByTestId("proposal-change-diff")).toHaveTextContent('"READY" → "PAUSED"');
    expect(screen.getByText(/existing:.*pause/)).toBeInTheDocument();
    expect(screen.getByTestId("proposal-edge-changes")).toHaveTextContent("Remove existing → old (blocks)");
    expect(screen.getByTestId("proposal-comments")).toHaveTextContent("Review this finding");
  });

  it("renders header (id, status pill, source line) for a ready proposal", () => {
    mockUseProposal.mockReturnValue({
      data: fixtureProposal,
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    });
    renderPane();
    expect(screen.getByText("prop-abc123")).toBeInTheDocument();
    expect(screen.getByText("ready")).toBeInTheDocument();
    expect(screen.getByText(/projects\/demo\/specs/)).toBeInTheDocument();
  });

  it("renders the graph container with a node per task plus ghost nodes", () => {
    mockUseProposal.mockReturnValue({
      data: fixtureProposal,
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    });
    renderPane();
    const graph = screen.getByTestId("proposal-graph");
    expect(within(graph).getAllByTestId("proposal-graph-node").length).toBe(2);
  });

  it("task list renders one row per proposed task; sort toggle re-orders rows", () => {
    mockUseProposal.mockReturnValue({
      data: fixtureProposal,
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    });
    renderPane();
    const list = screen.getByTestId("proposal-task-list");
    expect(within(list).getByText("Setup schema")).toBeInTheDocument();
    expect(within(list).getByText("Add API")).toBeInTheDocument();
    fireEvent.click(screen.getByText("priority"));
    const rows = within(list).getAllByText(/^P\d+$/);
    expect(rows[0]).toHaveTextContent("P100");
  });
});

describe("ProposalPreviewPane — terminal states", () => {
  it("ready state shows both action buttons", () => {
    mockUseProposal.mockReturnValue({
      data: fixtureProposal,
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    });
    mockUseProposalGate.mockReturnValue({ data: [{ id: "g1" }], gate: { id: "g1" } });
    renderPane();
    expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^discard$/i })).toBeInTheDocument();
  });

  it("committed fixture hides action buttons and shows the terminal banner", () => {
    mockUseProposal.mockReturnValue({
      data: { ...fixtureProposal, status: "committed" },
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    });
    renderPane();
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
    expect(screen.getByText(/Committed — 2 tasks created/)).toBeInTheDocument();
  });

  it("discarded fixture hides action buttons and shows the terminal banner", () => {
    mockUseProposal.mockReturnValue({
      data: { ...fixtureProposal, status: "discarded" },
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    });
    renderPane();
    expect(screen.queryByRole("button", { name: /^discard$/i })).not.toBeInTheDocument();
    expect(screen.getByText(/Discarded — no tasks were created/)).toBeInTheDocument();
  });
});

describe("ProposalPreviewPane — approve / discard", () => {
  it("approval confirms the prepared packet before applying and closing", async () => {
    mockUseProposal.mockReturnValue({
      data: fixtureProposal,
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    });
    const mutateAsync = vi.fn().mockResolvedValue({});
    mockUseApplyProposalDecision.mockReturnValue({ mutateAsync, isPending: false });
    mockUseProposalGate.mockReturnValue({ data: [{ id: "g1" }], gate: { id: "g1" } });
    const close = vi.fn();
    renderPane({ close });
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    const confirm = await screen.findByRole("button", { name: /confirm approval/i });
    expect(mutateAsync).not.toHaveBeenCalled();
    expect(close).not.toHaveBeenCalled();
    fireEvent.click(confirm);
    await vi.waitFor(() => expect(mutateAsync).toHaveBeenCalledWith({
      args: { gate_id: "g1", option_key: "approve" }, preview_id: "preview",
      idempotency_key: "key", effect: "Confirm the exact proposed effect.",
    }));
    await vi.waitFor(() => expect(close).toHaveBeenCalled());
  });

  it("Approve is a no-op (disabled) when no gate is found", () => {
    mockUseProposal.mockReturnValue({
      data: fixtureProposal,
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    });
    mockUseProposalGate.mockReturnValue({ data: [], gate: undefined });
    renderPane();
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
    expect(screen.getByText(/Waiting for approval gate/i)).toBeInTheDocument();
  });

  it("Discard requires a confirm click before calling the mutation, then close()", async () => {
    mockUseProposal.mockReturnValue({
      data: fixtureProposal,
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    });
    const mutateAsync = vi.fn().mockResolvedValue({});
    mockUseApplyProposalDecision.mockReturnValue({ mutateAsync, isPending: false });
    mockUseProposalGate.mockReturnValue({ data: [{ id: "g1" }], gate: { id: "g1" } });
    const close = vi.fn();
    renderPane({ close });
    fireEvent.click(screen.getByRole("button", { name: /^discard$/i }));
    fireEvent.click(await screen.findByRole("button", { name: /confirm decline/i }));
    await vi.waitFor(() => expect(mutateAsync).toHaveBeenCalled());
    await vi.waitFor(() => expect(close).toHaveBeenCalled());
  });

  it("a failed discard renders an inline error banner and does not close the pane", async () => {
    mockUseProposal.mockReturnValue({
      data: fixtureProposal,
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    });
    mockUseProposalGate.mockReturnValue({ data: [{ id: "g1" }], gate: { id: "g1" } });
    const mutateAsync = vi.fn().mockRejectedValue(new Error("boom"));
    mockUseApplyProposalDecision.mockReturnValue({ mutateAsync, isPending: false });
    const close = vi.fn();
    renderPane({ close });
    fireEvent.click(screen.getByRole("button", { name: /^discard$/i }));
    fireEvent.click(await screen.findByRole("button", { name: /confirm decline/i }));
    await vi.waitFor(() => expect(screen.getByText(/Decision failed: boom/)).toBeInTheDocument());
    expect(close).not.toHaveBeenCalled();
  });
});

describe("ProposalPreviewPane — not found", () => {
  it("404 fixture renders the not-found state with working Retry / Close", () => {
    const refetch = vi.fn();
    const close = vi.fn();
    mockUseProposal.mockReturnValue({
      data: undefined,
      isPending: false,
      isError: true,
      refetch,
    });
    renderPane({ close });
    expect(screen.getByText(/not found/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(refetch).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /^close$/i }));
    expect(close).toHaveBeenCalled();
  });
});

describe("ProposalPreviewPane — toolbar and shortcuts", () => {
  it("setToolbar is called with refresh + view-source actions", () => {
    mockUseProposal.mockReturnValue({
      data: fixtureProposal,
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    });
    const setToolbar = vi.fn();
    renderPane({ setToolbar });
    const lastCall = setToolbar.mock.calls[setToolbar.mock.calls.length - 1]?.[0];
    expect(lastCall.map((a: { id: string }) => a.id)).toEqual(["refresh", "view-source"]);
  });

  it("view-source is disabled when source doesn't parse to a spec path", () => {
    mockUseProposal.mockReturnValue({
      data: { ...fixtureProposal, source: "" },
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    });
    const setToolbar = vi.fn();
    renderPane({ setToolbar });
    const actions = setToolbar.mock.calls[setToolbar.mock.calls.length - 1]?.[0];
    expect(actions.find((a: { id: string }) => a.id === "view-source").disabled).toBe(true);
  });

  it("view-source opens the spec-doc-reader pane with the stripped path", () => {
    mockUseProposal.mockReturnValue({
      data: fixtureProposal,
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    });
    const setToolbar = vi.fn();
    renderPane({ setToolbar });
    const actions = setToolbar.mock.calls[setToolbar.mock.calls.length - 1]?.[0];
    actions.find((a: { id: string }) => a.id === "view-source").onClick();
    expect(mockOpen).toHaveBeenCalledWith("spec-doc-reader", {
      url: "projects/demo/specs/2026-08-21-thing.md",
    });
  });

  it("setShortcuts includes a/d only when status === ready", () => {
    mockUseProposal.mockReturnValue({
      data: fixtureProposal,
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    });
    const setShortcuts = vi.fn();
    renderPane({ setShortcuts });
    const lastCall = setShortcuts.mock.calls[setShortcuts.mock.calls.length - 1]?.[0];
    expect(lastCall.map((b: { key: string }) => b.key)).toEqual(["r", "s", "a", "d"]);
  });

  it("excludes a/d shortcuts for a committed proposal", () => {
    mockUseProposal.mockReturnValue({
      data: { ...fixtureProposal, status: "committed" },
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    });
    const setShortcuts = vi.fn();
    renderPane({ setShortcuts });
    const lastCall = setShortcuts.mock.calls[setShortcuts.mock.calls.length - 1]?.[0];
    expect(lastCall.map((b: { key: string }) => b.key)).toEqual(["r", "s"]);
  });
});

describe("ProposalPreviewPane — loading", () => {
  it("renders a skeleton while pending, without crashing", () => {
    mockUseProposal.mockReturnValue({
      data: undefined,
      isPending: true,
      isError: false,
      refetch: vi.fn(),
    });
    expect(() => renderPane()).not.toThrow();
    expect(screen.getByTestId("proposal-preview-pane")).toBeInTheDocument();
  });
});
