import type { ReactNode } from "react";
import { act, cleanup, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const transport = vi.hoisted(() => ({ tidy: vi.fn(), job: vi.fn() }));
vi.mock("@aq/ts-client", async () => ({
  ...await vi.importActual<typeof import("@aq/ts-client")>("@aq/ts-client"),
  postTidyApiProjectsProjectIdGraphTidyPost: (...args: unknown[]) => transport.tidy(...args),
  getJobApiProjectsProjectIdGraphJobsJobIdGet: (...args: unknown[]) => transport.job(...args),
}));

import { useTidyLayout } from "../graphLayout";
import { registerLayoutRefetch } from "../../pages/command-center/layout-v2/liveRegistry";

const clients: QueryClient[] = [];
// One client per test: a fresh one per render would reset the mutation observer.
function makeWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return ({ children }: { children: ReactNode }) =>
    <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => { transport.tidy.mockReset(); transport.job.mockReset(); });
afterEach(() => { cleanup(); clients.splice(0).forEach((c) => c.clear()); vi.useRealTimers(); });

describe("useTidyLayout", () => {
  it("stays pending until the tidy job finishes, then reloads the layout", async () => {
    const refetch = vi.fn();
    const unregister = registerLayoutRefetch("p1", refetch);
    transport.tidy.mockResolvedValue({ data: { jobs: [{ id: "job-1", status: "queued" }] } });
    transport.job
      .mockResolvedValueOnce({ data: { id: "job-1", status: "running" } })
      .mockResolvedValue({ data: { id: "job-1", status: "done" } });

    const { result } = renderHook(() => useTidyLayout("p1"), { wrapper: makeWrapper() });
    vi.useFakeTimers();
    act(() => result.current.mutate());
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(result.current.isPending).toBe(true);
    expect(refetch).not.toHaveBeenCalled();

    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(refetch).toHaveBeenCalledOnce();
    // One more turn: the observer's notification lands after the tick that
    // resolved the mutation.
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(result.current.isPending).toBe(false);
    unregister();
  });

  it("stops waiting when the job fails", async () => {
    const refetch = vi.fn();
    const unregister = registerLayoutRefetch("p2", refetch);
    transport.tidy.mockResolvedValue({ data: { jobs: [{ id: "job-2", status: "queued" }] } });
    transport.job.mockResolvedValue({ data: { id: "job-2", status: "failed", error: "boom" } });

    const { result } = renderHook(() => useTidyLayout("p2"), { wrapper: makeWrapper() });
    vi.useFakeTimers();
    act(() => result.current.mutate());
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(result.current.isPending).toBe(false);
    // A failed job still leaves whatever the layout has: reload rather than
    // keep showing a half-tidied graph.
    expect(refetch).toHaveBeenCalledOnce();
    unregister();
  });
});
