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
afterEach(cleanup);

describe("Agent flock live invalidation", () => {
  it.each(["agent.created", "agent.updated", "agent.question", "agent.question.updated", "session.started", "session.exited", "session.adopted", "task.claimed", "task.blocked", "message.sent", "message.replied"])(
    "refreshes the roster when %s changes assignments, settings, or subagent activity", (eventType) => {
      const client = new QueryClient();
      client.setQueryData(["agents", "flock"], { agents: [{ id: "a" }], count: 1 });
      renderHook(() => useEventStream(), { wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ) });
      __dispatchEventForTests({ event_type: eventType, task_id: "t1", session_id: "s1" } as NotifyEvent);
      expect(client.getQueryState(["agents", "flock"])?.isInvalidated).toBe(true);
      client.clear();
    },
  );
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
