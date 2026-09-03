import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PlaybookSemanticReview from "../PlaybookSemanticReview";

const V5 = "sha256:" + "5".repeat(64);
const V6 = "sha256:" + "6".repeat(64);

const transport = vi.hoisted(() => ({
  health: vi.fn(),
  artifacts: vi.fn(),
  diff: vi.fn(),
  graph: vi.fn(),
  pending: vi.fn(),
  overlay: vi.fn(),
  runs: vi.fn(),
  activate: vi.fn(),
}));
vi.mock("../../../api/client", async () => ({
  ...(await vi.importActual<typeof import("../../../api/client")>("../../../api/client")),
  playbookActivationHealth: (...args: unknown[]) => transport.health(...args),
  playbookArtifacts: (...args: unknown[]) => transport.artifacts(...args),
  playbookArtifactDiff: (...args: unknown[]) => transport.diff(...args),
  playbookV2Graph: (...args: unknown[]) => transport.graph(...args),
  playbookPendingEvents: (...args: unknown[]) => transport.pending(...args),
  playbookRunOverlay: (...args: unknown[]) => transport.overlay(...args),
  listPlaybookRuns: (...args: unknown[]) => transport.runs(...args),
  playbookActivate: (...args: unknown[]) => transport.activate(...args),
}));
// The canvas is covered by its own suites and drags xyflow in with it; this
// suite is about which artifact the review is looking at.
vi.mock("../PlaybookSemanticGraphView", () => ({ default: () => <div data-testid="graph" /> }));

function ref(sha: string, version: number) {
  return {
    playbook_id: "review",
    artifact_sha256: sha,
    schema_generation: 2,
    contract_fingerprint: "sha256:" + "b".repeat(64),
    source_digest: "sha256:" + "c".repeat(64),
    compiler_build: "test",
    version,
  };
}

const clients: QueryClient[] = [];
function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  Object.values(transport).forEach((fn) => fn.mockReset());
  transport.health.mockResolvedValue({
    data: {
      success: true,
      count: 1,
      activations: [
        { playbook_id: "review", scope: "system", enabled: true, active_artifact_sha256: V5, health: "ready", reasons: [] },
      ],
    },
  });
  transport.artifacts.mockResolvedValue({
    data: {
      success: true,
      playbook_id: "review",
      count: 2,
      active_artifact_sha256: V5,
      artifacts: [
        { artifact: ref(V6, 6), scope: "system", is_active: false },
        { artifact: ref(V5, 5), scope: "system", is_active: true },
      ],
    },
  });
  transport.diff.mockImplementation(async (options: { body: { target_sha256: string; base_sha256?: string } }) => {
    const executable = options.body.target_sha256 !== options.body.base_sha256;
    return {
      data: {
        executable_change: executable,
        semantic_change_count: executable ? 1 : 0,
        presentation_change_count: 0,
        steps: executable
          ? [{ step_id: "gate", change: "modified", field_changes: [{ path: "/command", executable: true }] }]
          : [],
      },
    };
  });
  transport.graph.mockImplementation(async (options: { body: { artifact_sha256?: string } }) => ({
    data: { artifact: ref(options.body.artifact_sha256 ?? V5, options.body.artifact_sha256 === V6 ? 6 : 5) },
  }));
  transport.pending.mockResolvedValue({ data: { events: [] } });
  transport.runs.mockResolvedValue({ data: { runs: [] } });
  transport.overlay.mockResolvedValue({ data: undefined });
  transport.activate.mockResolvedValue({ data: { success: true, changed: true } });
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});

describe("PlaybookSemanticReview artifact selection", () => {
  it("selects the inactive candidate, diffs it against the active artifact, and activates it", async () => {
    const user = userEvent.setup();
    render(<PlaybookSemanticReview playbookId="review" />, { wrapper: wrapper() });

    const chooser = await screen.findByLabelText("Artifact under review");
    expect(transport.artifacts).toHaveBeenCalledWith(
      expect.objectContaining({ body: { playbook_id: "review" } }),
    );
    // The active artifact is what the review opens on, and it is labelled.
    expect(chooser).toHaveValue(V5);
    expect(screen.getByRole("option", { name: /v5 .* \(active\)/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "v6 666666666666" })).toBeInTheDocument();

    await user.selectOptions(chooser, V6);

    // The regression this closes: the target moves to the candidate while the
    // base stays on the active artifact, instead of diffing v5 against itself.
    await waitFor(() =>
      expect(transport.diff).toHaveBeenCalledWith(
        expect.objectContaining({ body: { playbook_id: "review", target_sha256: V6, base_sha256: V5 } }),
      ),
    );
    expect(await screen.findByText("gate/command")).toBeInTheDocument();

    const activate = await screen.findByRole("button", { name: "Activate displayed artifact" });
    await waitFor(() => expect(activate).toBeDisabled());
    await user.click(screen.getByRole("checkbox", { name: "I reviewed the executable diff" }));
    await user.click(activate);

    await waitFor(() =>
      expect(transport.activate).toHaveBeenCalledWith(
        expect.objectContaining({
          body: { playbook_id: "review", artifact_sha256: V6, acknowledge_diff: V6 },
        }),
      ),
    );
  });

  it("diffs the active artifact against itself only until a candidate is chosen", async () => {
    render(<PlaybookSemanticReview playbookId="review" />, { wrapper: wrapper() });

    await screen.findByLabelText("Artifact under review");
    await waitFor(() =>
      expect(transport.diff).toHaveBeenCalledWith(
        expect.objectContaining({ body: { playbook_id: "review", target_sha256: V5, base_sha256: V5 } }),
      ),
    );
    expect(screen.queryByRole("checkbox", { name: "I reviewed the executable diff" })).toBeNull();
  });
});
