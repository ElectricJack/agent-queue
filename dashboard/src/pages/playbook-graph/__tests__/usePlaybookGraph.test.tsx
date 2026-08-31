import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { usePlaybookGraph } from "../../../api/hooks";
import { graph, layout } from "./fixtures";

const transport = vi.hoisted(() => ({ graphView: vi.fn() }));
vi.mock("../../../api/client", async () => ({
  ...(await vi.importActual<typeof import("../../../api/client")>("../../../api/client")),
  playbookGraphView: (...args: unknown[]) => transport.graphView(...args),
}));

const clients: QueryClient[] = [];
function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  transport.graphView.mockReset();
  transport.graphView.mockResolvedValue({
    data: { success: true, playbook: { id: "review-flow" }, graph, layout, legend: {} },
  });
});
afterEach(() => { clients.splice(0).forEach((client) => client.clear()); });

describe("usePlaybookGraph", () => {
  it("requests the static compiled definition for the current playbook", async () => {
    const { result } = renderHook(() => usePlaybookGraph("review-flow"), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(transport.graphView).toHaveBeenCalledTimes(1);
    expect(transport.graphView.mock.calls[0]![0]).toMatchObject({
      body: {
        playbook_id: "review-flow",
        direction: "TD",
        show_prompts: true,
        include_live_state: false,
        include_metrics: false,
        include_history: false,
      },
      throwOnError: true,
    });
    expect(result.current.data?.graph?.nodes).toHaveLength(5);
  });

  it("scopes the cache key by playbook id and refetches when the playbook changes", async () => {
    const render = renderHook(({ id }: { id: string }) => usePlaybookGraph(id), {
      wrapper: wrapper(),
      initialProps: { id: "review-flow" },
    });
    await waitFor(() => expect(render.result.current.isSuccess).toBe(true));
    render.rerender({ id: "other-flow" });
    await waitFor(() => expect(transport.graphView).toHaveBeenCalledTimes(2));
    expect(transport.graphView.mock.calls[1]![0].body.playbook_id).toBe("other-flow");
  });

  it("does not fetch without a valid playbook id", () => {
    const { result } = renderHook(() => usePlaybookGraph(""), { wrapper: wrapper() });
    expect(transport.graphView).not.toHaveBeenCalled();
    expect(result.current.fetchStatus).toBe("idle");
  });
});
