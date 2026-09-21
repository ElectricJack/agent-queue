import { afterAll, afterEach, describe, it, expect, vi } from "vitest";
import { cleanup, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useEventStream, __dispatchEventForTests } from "../useEventStream";
import type { NotifyEvent } from "../types";

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

describe("useEventStream — provider availability", () => {
  it.each(["provider.state_changed", "provider.reroute_batch", "notify.provider_state"])(
    "refetches availability and the held list on %s",
    (eventType) => {
      const client = new QueryClient();
      const invalidateSpy = vi.spyOn(client, "invalidateQueries");
      renderHook(() => useEventStream(), { wrapper: makeWrapper(client) });

      __dispatchEventForTests({ event_type: eventType, provider: "codex" } as unknown as NotifyEvent);

      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["providers", "availability"] });
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["providers", "held-tasks"] });
      client.clear();
    },
  );
});
