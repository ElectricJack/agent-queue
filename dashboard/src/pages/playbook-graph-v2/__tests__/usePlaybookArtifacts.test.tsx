import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { playbookArtifactsKey, usePlaybookArtifacts } from "../../../api/hooks";

const transport = vi.hoisted(() => ({ artifacts: vi.fn() }));
vi.mock("../../../api/client", async () => ({
  ...(await vi.importActual<typeof import("../../../api/client")>("../../../api/client")),
  playbookArtifacts: (...args: unknown[]) => transport.artifacts(...args),
}));

const listing = {
  success: true,
  playbook_id: "default-pipeline",
  count: 1,
  active_artifact_sha256: "sha256:" + "5".repeat(64),
  artifacts: [
    {
      artifact: {
        playbook_id: "default-pipeline",
        artifact_sha256: "sha256:" + "6".repeat(64),
        schema_generation: 2,
        contract_fingerprint: "sha256:" + "b".repeat(64),
        source_digest: "sha256:" + "c".repeat(64),
        compiler_build: "test",
        version: 6,
      },
      scope: "system",
      is_active: false,
    },
  ],
};

const clients: QueryClient[] = [];
function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  transport.artifacts.mockReset();
  transport.artifacts.mockResolvedValue({ data: listing });
});
afterEach(() => {
  clients.splice(0).forEach((client) => client.clear());
});

describe("usePlaybookArtifacts", () => {
  it("lists one playbook's artifacts, including the inactive candidates", async () => {
    const { result } = renderHook(() => usePlaybookArtifacts("default-pipeline"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(transport.artifacts).toHaveBeenCalledTimes(1);
    expect(transport.artifacts.mock.calls[0]![0]).toMatchObject({
      body: { playbook_id: "default-pipeline" },
      throwOnError: true,
    });
    expect(result.current.data).toBe(listing);
    expect(result.current.data!.artifacts![0]!.is_active).toBe(false);
  });

  it("does not fetch without a playbook id", () => {
    const { result } = renderHook(() => usePlaybookArtifacts(undefined), { wrapper: wrapper() });
    expect(transport.artifacts).not.toHaveBeenCalled();
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("keys two playbooks apart", () => {
    expect(playbookArtifactsKey("a")).toEqual(["playbook-artifacts", "a"]);
    expect(playbookArtifactsKey("a")).not.toEqual(playbookArtifactsKey("b"));
  });
});
