import { afterEach, expect, it, vi } from "vitest";
import { cleanup, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { __dispatchEventForTests, useEventStream } from "../useEventStream";
import type { NotifyEvent } from "../types";
import type { ReactNode } from "react";

vi.hoisted(() => {
  vi.stubGlobal("WebSocket", class { static OPEN = 1; static CONNECTING = 0; readyState = 0; close() {} });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

it("refreshes wait explanations and graphs once for a burst of source events", () => {
  vi.useFakeTimers();
  const client = new QueryClient();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  renderHook(() => useEventStream(), { wrapper: ({ children }: { children: ReactNode }) =>
    <QueryClientProvider client={client}>{children}</QueryClientProvider> });
  for (const type of ["wait.registered", "wait.satisfied", "continuation.acknowledged", "reservation.overdue", "task.updated"]) {
    __dispatchEventForTests({ _event_type: type, event_type: type, project_id: "p" } as NotifyEvent);
  }
  vi.advanceTimersByTime(250);
  for (const key of ["wait-explain", "wait-graph"]) {
    expect(invalidate.mock.calls.filter(([options]) => options?.queryKey?.[0] === key)).toHaveLength(1);
  }
  client.clear();
});
