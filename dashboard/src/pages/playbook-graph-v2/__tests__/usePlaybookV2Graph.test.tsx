import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { playbookV2GraphKey, usePlaybookV2Graph } from "../../../api/hooks";
import { graph } from "./fixtures";

const transport = vi.hoisted(() => ({ v2Graph: vi.fn() }));
vi.mock("../../../api/client", async () => ({
  ...(await vi.importActual<typeof import("../../../api/client")>("../../../api/client")),
  playbookV2Graph: (...args: unknown[]) => transport.v2Graph(...args),
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
  transport.v2Graph.mockReset();
  transport.v2Graph.mockResolvedValue({ data: graph });
});
afterEach(() => {
  clients.splice(0).forEach((client) => client.clear());
});

describe("usePlaybookV2Graph", () => {
  it("requests the active artifact's graph for the current playbook", async () => {
    const { result } = renderHook(() => usePlaybookV2Graph("default-pipeline"), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(transport.v2Graph).toHaveBeenCalledTimes(1);
    expect(transport.v2Graph.mock.calls[0]![0]).toMatchObject({
      body: { playbook_id: "default-pipeline", direction: "TD", include_advanced: true },
      throwOnError: true,
    });
    expect(transport.v2Graph.mock.calls[0]![0].body).not.toHaveProperty("artifact_sha256");
    expect(transport.v2Graph.mock.calls[0]![0].body).not.toHaveProperty("event_type");
    expect(result.current.data).toBe(graph);
  });

  it("pins an exact artifact and narrows the event scope when asked", async () => {
    const { result } = renderHook(
      () => usePlaybookV2Graph("default-pipeline", { artifactSha: "sha256:abc", eventType: "spec.approved" }),
      { wrapper: wrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(transport.v2Graph.mock.calls[0]![0].body).toMatchObject({
      artifact_sha256: "sha256:abc",
      event_type: "spec.approved",
    });
  });

  it("does not fetch without a playbook id", () => {
    const { result } = renderHook(() => usePlaybookV2Graph(undefined), { wrapper: wrapper() });
    expect(transport.v2Graph).not.toHaveBeenCalled();
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("keys two artifacts and two event scopes apart", () => {
    expect(playbookV2GraphKey("p")).toEqual(["playbook-v2-graph", "p", "active", "all"]);
    expect(playbookV2GraphKey("p", "sha256:a")).not.toEqual(playbookV2GraphKey("p", "sha256:b"));
    expect(playbookV2GraphKey("p", undefined, "a")).not.toEqual(playbookV2GraphKey("p", undefined, "b"));
  });
});
