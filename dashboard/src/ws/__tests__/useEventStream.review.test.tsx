import { afterAll, afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { __dispatchEventForTests, useEventStream } from "../useEventStream";
import type { NotifyEvent } from "../types";

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

describe("useEventStream — review events", () => {
  it("invalidates the review list and changed review for every review event", () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    renderHook(() => useEventStream(), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });

    __dispatchEventForTests({
      _event_type: "review.commented",
      event_type: "review.commented",
      review_id: "review-1",
    } as NotifyEvent);

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["reviews"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["review", "review-1"] });
    client.clear();
  });
});
