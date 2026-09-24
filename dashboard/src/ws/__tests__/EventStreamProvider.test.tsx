import { afterAll, afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { EventStreamProvider, useEventBuffer, useEventStreamStatus } from "../EventStreamProvider";
import { __dispatchEventForTests } from "../useEventStream";
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

describe("EventStreamProvider", () => {
  it("does not re-render connection-status readers on every frame", () => {
    const statusRenders = vi.fn();
    function StatusReader() {
      statusRenders(useEventStreamStatus());
      return null;
    }
    function BufferReader() {
      return <p data-testid="count">{useEventBuffer().events.length}</p>;
    }
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <EventStreamProvider>
          <StatusReader />
          <BufferReader />
        </EventStreamProvider>
      </QueryClientProvider>,
    );
    const before = statusRenders.mock.calls.length;
    act(() => {
      for (let i = 0; i < 5; i++) {
        __dispatchEventForTests({ event_type: "notify.task_message", task_id: "t", message: "m" } as unknown as NotifyEvent);
      }
    });
    expect(screen.getByTestId("count")).toHaveTextContent("5");
    expect(statusRenders.mock.calls.length).toBe(before);
    client.clear();
  });
});
