import { afterAll, afterEach, describe, expect, it, vi } from "vitest";
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
      client.setQueryData(["agents", "flock"], [{ id: "a" }]);
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
  it.each(["notify.playbook_run_started", "notify.playbook_run_completed", "notify.playbook_run_failed", "notify.playbook_run_paused", "notify.playbook_run_resumed", "notify.playbook_run_cancelled", "playbook.compiled", "playbook.deleted"])(
    "invalidates definitions and run history after %s", event_type => {
      const client = new QueryClient();
      client.setQueryData(["playbooks", "all"], []);
      client.setQueryData(["playbook-runs", "audit"], []);
      renderHook(() => useEventStream(), { wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider> });
      __dispatchEventForTests({ event_type, run_id: "r" } as NotifyEvent);
      expect(client.getQueryState(["playbooks", "all"])?.isInvalidated).toBe(true);
      expect(client.getQueryState(["playbook-runs", "audit"])?.isInvalidated).toBe(true);
      client.clear();
    },
  );
});
