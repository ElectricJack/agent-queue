import { afterAll, afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useEventStream } from "../useEventStream";
const transport = vi.hoisted(() => {
  class Socket {
    static OPEN = 1;
    static CONNECTING = 0;
    static instance: Socket;
    readyState = 0;
    onmessage?: (event: { data: string }) => void;
    constructor() { Socket.instance = this; }
    close() {}
  }
  vi.stubGlobal("WebSocket", Socket);
  return Socket;
});
afterEach(cleanup);
afterAll(() => vi.unstubAllGlobals());

describe("WebSocket wire discriminators", () => {
  it("normalizes live and replay bus frames before notifying subscribers and task caches", () => {
    const client = new QueryClient();
    client.setQueryData(["tasks", "p1"], []);
    client.setQueryData(["task", "t1"], {});
    client.setQueryData(["agents"], []);
    client.setQueryData(["task", "t1", "comments", 0], { comments: [] });
    client.setQueryData(["task", "other", "comments", 0], { comments: [] });
    const seen = vi.fn();
    renderHook(() => useEventStream({ onEvent: seen }), { wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider> });
    transport.instance.onmessage?.({ data: JSON.stringify({ _event_type: "task.updated", project_id: "p1", task_id: "t1", seq: 2 }) });
    expect(seen).toHaveBeenCalledWith(expect.objectContaining({ _event_type: "task.updated", event_type: "task.updated", seq: 2 }));
    for (const key of [["tasks", "p1"], ["task", "t1"], ["task", "t1", "comments", 0], ["agents"]]) expect(client.getQueryState(key)?.isInvalidated).toBe(true);
    expect(client.getQueryState(["task", "other", "comments", 0])?.isInvalidated).toBe(false);
    client.clear();
  });

  it("preserves unknown event payloads and ignores hello/heartbeat or malformed frames", () => {
    const client = new QueryClient();
    const seen = vi.fn();
    renderHook(() => useEventStream({ onEvent: seen }), { wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider> });
    for (const frame of ["not-json", "null", JSON.stringify({ type: "hello", epoch: "test" }), JSON.stringify({ type: "heartbeat" })]) transport.instance.onmessage?.({ data: frame });
    expect(seen).not.toHaveBeenCalled();
    transport.instance.onmessage?.({ data: JSON.stringify({ event_type: "future.error", error: "retry later", extra: { retained: true } }) });
    expect(seen).toHaveBeenCalledWith(expect.objectContaining({ event_type: "future.error", error: "retry later", extra: { retained: true } }));
    client.clear();
  });

  it("invalidates pool supply and instance queries when a pool event arrives", () => {
    const client = new QueryClient();
    client.setQueryData(["pools", "all"], []);
    client.setQueryData(["sessions", "pool"], []);
    client.setQueryData(["sessions", "task"], []);
    renderHook(() => useEventStream(), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });

    transport.instance.onmessage?.({
      data: JSON.stringify({
        _event_type: "pool.bounds_changed",
        project_id: "p1",
        profile_id: "worker",
        min_active: 1,
        max_active: 3,
      }),
    });

    expect(client.getQueryState(["pools", "all"])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["sessions", "pool"])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["sessions", "task"])?.isInvalidated).toBe(false);
    client.clear();
  });
});
