import { afterAll, afterEach, describe, it, expect, vi } from "vitest";
import { cleanup, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useEventStream, __dispatchEventForTests } from "../useEventStream";
import type { BatchEvent, ProposalStatusChangedEvent } from "../types";

// The singleton connects on import; keep this test off the real network.
vi.hoisted(() => {
  vi.stubGlobal("WebSocket", class {
    static OPEN = 1;
    static CONNECTING = 0;
    readyState = 0;
    close() {}
  });
});
afterEach(cleanup);
afterAll(() => vi.unstubAllGlobals());

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe("useEventStream — proposal.status_changed", () => {
  it.each(["batch.held", "batch.receipt_recorded", "batch.needs_scope", "batch.released"] as const)("refetches authoritative state for %s, including stale replay revisions", (type) => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    renderHook(() => useEventStream(), { wrapper: makeWrapper(client) });
    const event: BatchEvent = { _event_type: type, event_type: type, batch_id: "batch", project_id: "p1",
      structure_revision: 1, structure_digest: "old-digest", reviewer_task_id: "reviewer" };
    __dispatchEventForTests(event);
    for (const queryKey of [["batch", "p1", "batch"], ["proposal", "batch"], ["tasks"], ["task"], ["explain"], ["asks"]]) {
      expect(invalidate).toHaveBeenCalledWith({ queryKey });
    }
    client.clear();
  });
  it("invalidates the proposal detail query on a proposal.status_changed frame", () => {
    const client = new QueryClient();
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    renderHook(() => useEventStream(), { wrapper: makeWrapper(client) });

    const event: ProposalStatusChangedEvent = {
      _event_type: "proposal.status_changed",
      event_type: "proposal.status_changed",
      severity: "info",
      category: "proposal",
      project_id: "p1",
      proposal_id: "prop-abc",
      status: "committed",
    };
    __dispatchEventForTests(event);

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["proposal", "prop-abc"] });
    client.clear();
  });
});
