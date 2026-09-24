import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useEventStream, __dispatchEventForTests } from "../useEventStream";
import type { NotifyEvent } from "../types";

vi.hoisted(() => {
  vi.stubGlobal("WebSocket", class {
    static OPEN = 1;
    static CONNECTING = 0;
    readyState = 0;
    close() {}
  });
});
afterAll(() => vi.unstubAllGlobals());
beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  if (vi.isFakeTimers()) {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
  }
});

describe("Agent flock live invalidation", () => {
  it.each(["agent.created", "agent.updated", "agent.question", "agent.question.updated", "session.started", "session.exited", "session.adopted", "task.claimed", "task.blocked", "message.sent", "message.replied"])(
    "refreshes the roster when %s changes assignments, settings, or subagent activity", (eventType) => {
      const client = new QueryClient();
      client.setQueryData(["agents", "flock"], { agents: [{ id: "a" }], count: 1 });
      renderHook(() => useEventStream(), { wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ) });
      __dispatchEventForTests({ event_type: eventType, task_id: "t1", session_id: "s1" } as NotifyEvent);
      vi.advanceTimersByTime(1_000);
      expect(client.getQueryState(["agents", "flock"])?.isInvalidated).toBe(true);
      client.clear();
    },
  );

  it("refreshes the roster once for a burst of frames, after the last of them", () => {
    const client = new QueryClient();
    client.setQueryData(["agents", "flock"], { agents: [], count: 0 });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    renderHook(() => useEventStream(), { wrapper });
    const flock = () => invalidate.mock.calls
      .filter(([filters]) => (filters as { queryKey: unknown[] }).queryKey[0] === "agents");
    for (let i = 0; i < 20; i++) {
      __dispatchEventForTests({ event_type: "task.updated", task_id: "t" + i } as NotifyEvent);
      vi.advanceTimersByTime(40);
    }
    // 20 frames over 800ms: none refreshed yet, one refresh when the window closes.
    expect(flock()).toHaveLength(0);
    vi.advanceTimersByTime(200);
    expect(flock()).toHaveLength(1);
    // A frame after the window opens a new one.
    __dispatchEventForTests({ event_type: "session.started", session_id: "s" } as NotifyEvent);
    vi.advanceTimersByTime(1_000);
    expect(flock()).toHaveLength(2);
    client.clear();

    function wrapper({ children }: { children: ReactNode }) {
      return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    }
  });
});

describe("one cache pass per frame", () => {
  it("invalidates once however many hooks are mounted on the client", () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    // The root provider, the agent-push bridge, the project graph and an open
    // pane each mount one.
    const onEvent = vi.fn();
    renderHook(() => useEventStream({ onEvent }), { wrapper });
    renderHook(() => useEventStream(), { wrapper });
    renderHook(() => useEventStream(), { wrapper });
    renderHook(() => useEventStream({ onEvent }), { wrapper });
    __dispatchEventForTests({ event_type: "task.updated", task_id: "t1" } as NotifyEvent);
    const keys = invalidate.mock.calls.map(([filters]) => JSON.stringify((filters as { queryKey: unknown[] }).queryKey));
    expect(keys).toEqual(['["tasks"]', '["task","t1"]', '["explain","t1"]']);
    // Callbacks are still per hook.
    expect(onEvent).toHaveBeenCalledTimes(2);
    client.clear();
  });

  it("stops following the stream when the last hook unmounts", () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const first = renderHook(() => useEventStream(), { wrapper });
    const second = renderHook(() => useEventStream(), { wrapper });
    first.unmount();
    __dispatchEventForTests({ event_type: "review.submitted", review_id: "r1" } as unknown as NotifyEvent);
    expect(invalidate).toHaveBeenCalled();
    invalidate.mockClear();
    second.unmount();
    __dispatchEventForTests({ event_type: "review.submitted", review_id: "r1" } as unknown as NotifyEvent);
    expect(invalidate).not.toHaveBeenCalled();
    client.clear();
  });

  it("hands notify.task_message to each hook's callback", () => {
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const a = vi.fn();
    const b = vi.fn();
    renderHook(() => useEventStream({ onTaskMessage: a }), { wrapper });
    renderHook(() => useEventStream({ onTaskMessage: b }), { wrapper });
    __dispatchEventForTests({ event_type: "notify.task_message", task_id: "t1", message: "hi" } as unknown as NotifyEvent);
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1);
    client.clear();
  });
});

describe("playbook graph live updates", () => {
  const render = (client: QueryClient) => renderHook(() => useEventStream(), {
    wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
  });
  const seeded = () => {
    const client = new QueryClient();
    client.setQueryData(["playbooks", "all"], []);
    client.setQueryData(["playbook-runs", "audit"], []);
    return client;
  };
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => { vi.runOnlyPendingTimers(); vi.useRealTimers(); });

  it.each([
    "notify.playbook_run_started",
    "notify.playbook_run_completed",
    "notify.playbook_run_failed",
    "notify.playbook_run_paused",
    // What the V2 engine actually emits — the notify.* family above has no
    // publisher, so these are the frames that keep the cards honest.
    "playbook.v2.run.started",
    "playbook.v2.run.finished",
  ])("invalidates definitions and run history after %s", event_type => {
    const client = seeded();
    render(client);
    __dispatchEventForTests({ event_type, run_id: "r" } as NotifyEvent);
    vi.advanceTimersByTime(500);
    expect(client.getQueryState(["playbooks", "all"])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["playbook-runs", "audit"])?.isInvalidated).toBe(true);
    client.clear();
  });

  it("collapses a step-by-step burst into a single refetch of each list", () => {
    const client = seeded();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    render(client);
    for (let i = 0; i < 10; i++) {
      __dispatchEventForTests({ event_type: "playbook.v2.step.completed", run_id: "r" } as unknown as NotifyEvent);
    }
    expect(invalidate).not.toHaveBeenCalled();
    vi.advanceTimersByTime(500);
    expect(invalidate.mock.calls.map(([filters]) => (filters as { queryKey: string[] }).queryKey[0]))
      .toEqual(["playbooks", "playbook-runs"]);
    client.clear();
  });
});
