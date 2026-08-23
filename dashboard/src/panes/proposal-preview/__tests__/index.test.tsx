import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ProposalPreviewPane from "../index";
import type { ProposalDetail } from "../hooks";

const mockUseProposal = vi.fn();
const mockUseProposalGate = vi.fn();
const mockUseResolveGate = vi.fn();
const mockUseDiscardProposal = vi.fn();

vi.mock("../hooks", async () => {
  const actual = await vi.importActual<typeof import("../hooks")>("../hooks");
  return {
    ...actual,
    useProposal: (...args: unknown[]) => mockUseProposal(...args),
    useProposalGate: (...args: unknown[]) => mockUseProposalGate(...args),
    useResolveGate: (...args: unknown[]) => mockUseResolveGate(...args),
    useDiscardProposal: (...args: unknown[]) => mockUseDiscardProposal(...args),
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
  mockUseProposal.mockReset();
  mockUseProposalGate.mockReset();
  mockUseResolveGate.mockReset();
  mockUseDiscardProposal.mockReset();
  mockOpen.mockReset();
  mockUseResolveGate.mockReturnValue({ mutateAsync: vi.fn(), isPending: false });
  mockUseDiscardProposal.mockReturnValue({ mutateAsync: vi.fn(), isPending: false });
  mockUseProposalGate.mockReturnValue({ data: [], gate: undefined });
});

describe("ProposalPreviewPane — header + graph + list", () => {
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
  it("clicking Approve with a resolved gate calls resolveGate.mutateAsync and then close()", async () => {
    mockUseProposal.mockReturnValue({
      data: fixtureProposal,
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    });
    const mutateAsync = vi.fn().mockResolvedValue({});
    mockUseResolveGate.mockReturnValue({ mutateAsync, isPending: false });
    mockUseProposalGate.mockReturnValue({ data: [{ id: "g1" }], gate: { id: "g1" } });
    const close = vi.fn();
    renderPane({ close });
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    await vi.waitFor(() => expect(mutateAsync).toHaveBeenCalledWith({
      gate_id: "g1",
      resolved_by: "dashboard",
      resolution: "approved",
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
    mockUseDiscardProposal.mockReturnValue({ mutateAsync, isPending: false });
    mockUseProposalGate.mockReturnValue({ data: [{ id: "g1" }], gate: { id: "g1" } });
    const close = vi.fn();
    renderPane({ close });
    fireEvent.click(screen.getByRole("button", { name: /^discard$/i }));
    fireEvent.click(screen.getByRole("button", { name: /^yes$/i }));
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
    const mutateAsync = vi.fn().mockRejectedValue(new Error("boom"));
    mockUseDiscardProposal.mockReturnValue({ mutateAsync, isPending: false });
    const close = vi.fn();
    renderPane({ close });
    fireEvent.click(screen.getByRole("button", { name: /^discard$/i }));
    fireEvent.click(screen.getByRole("button", { name: /^yes$/i }));
    await vi.waitFor(() => expect(screen.getByText(/Discard failed: boom/)).toBeInTheDocument());
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
