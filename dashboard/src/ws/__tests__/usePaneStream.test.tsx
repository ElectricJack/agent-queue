import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { MAX_SCREEN_CHARS, usePaneStream } from "../usePaneStream";

class MockEventSource {
  static last: MockEventSource | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  readyState = 1; // OPEN
  constructor(public url: string) {
    MockEventSource.last = this;
  }
  close() {
    this.closed = true;
    this.readyState = 2; // CLOSED
  }
}

beforeEach(() => {
  MockEventSource.last = null;
  vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

function send(frame: Record<string, unknown>) {
  act(() => {
    MockEventSource.last?.onmessage?.({ data: JSON.stringify(frame) });
  });
}

describe("usePaneStream", () => {
  it("replaces the screen rather than appending", () => {
    const { result } = renderHook(() => usePaneStream("s1"));
    send({ source: "pane", type: "screen", screen: "first", seq: 1, ts: 1 });
    expect(result.current.screen).toBe("first");
    send({ source: "pane", type: "screen", screen: "second", seq: 2, ts: 2 });
    expect(result.current.screen).toBe("second");
  });

  it("surfaces a stopped frame as status", () => {
    const { result } = renderHook(() => usePaneStream("s1"));
    send({ source: "pane", type: "screen", screen: "last", seq: 1, ts: 1 });
    send({ source: "pane", type: "stopped", seq: 2, ts: 2 });
    expect(result.current.status).toBe("stopped");
    expect(result.current.screen).toBe("last");
  });

  it("surfaces an error frame with its message", () => {
    const { result } = renderHook(() => usePaneStream("s1"));
    send({ source: "pane", type: "error", message: "tmux is gone", seq: 1, ts: 1 });
    expect(result.current.status).toBe("error");
    expect(result.current.error).toBe("tmux is gone");
  });

  it("closes the stream on a stopped frame so the browser cannot reconnect", () => {
    // The server returns from its generator on a terminal frame, and per the
    // SSE spec a normally-closed stream is retried after ~3s. Left open, that
    // re-subscribes forever.
    const { result } = renderHook(() => usePaneStream("s1"));
    send({ source: "pane", type: "screen", screen: "last", seq: 1, ts: 1 });
    send({ source: "pane", type: "stopped", seq: 2, ts: 2 });
    expect(MockEventSource.last?.closed).toBe(true);
    expect(result.current.status).toBe("stopped");
    expect(result.current.screen).toBe("last");
  });

  it("closes the stream on an error frame", () => {
    renderHook(() => usePaneStream("s1"));
    send({ source: "pane", type: "error", message: "cap reached", seq: 1, ts: 1 });
    expect(MockEventSource.last?.closed).toBe(true);
  });

  it("keeps the last good screen when an empty one arrives after a terminal frame", () => {
    // A re-subscribe peeks a reaped tmux session and gets "", which is not
    // nullish — `f.screen ?? prev.screen` would happily blank the banner.
    const { result } = renderHook(() => usePaneStream("s1"));
    send({ source: "pane", type: "screen", screen: "last words", seq: 1, ts: 1 });
    send({ source: "pane", type: "stopped", seq: 2, ts: 2 });
    send({ source: "pane", type: "screen", screen: "", seq: 3, ts: 3 });
    expect(result.current.screen).toBe("last words");
    expect(result.current.status).toBe("stopped");
  });

  it("ignores onerror after we closed the stream ourselves", () => {
    const { result } = renderHook(() => usePaneStream("s1"));
    send({ source: "pane", type: "stopped", seq: 1, ts: 1 });
    act(() => {
      MockEventSource.last?.onerror?.();
    });
    expect(result.current.status).toBe("stopped");
  });

  it("says a closed EventSource will not retry", () => {
    const { result } = renderHook(() => usePaneStream("s1"));
    act(() => {
      MockEventSource.last!.readyState = 2;
      MockEventSource.last?.onerror?.();
    });
    expect(result.current.status).toBe("error");
    expect(result.current.error).toMatch(/no reconnect/);
  });

  it("opens no connection when disabled", () => {
    renderHook(() => usePaneStream("s1", { enabled: false }));
    expect(MockEventSource.last).toBeNull();
  });

  it("closes the connection on unmount", () => {
    const { unmount } = renderHook(() => usePaneStream("s1"));
    const es = MockEventSource.last;
    unmount();
    expect(es?.closed).toBe(true);
  });
});

describe("usePaneStream — stale state and retry", () => {
  it("marks the screen interrupted on a connection error and clears it on the next frame", () => {
    const { result } = renderHook(() => usePaneStream("s1"));
    send({ source: "pane", type: "screen", screen: "kept", seq: 1, ts: 1 });
    expect(result.current.lastFrameAt).not.toBeNull();
    act(() => MockEventSource.last?.onerror?.());
    expect(result.current.interrupted).toBe(true);
    expect(result.current.screen).toBe("kept");
    send({ source: "pane", type: "screen", screen: "fresh", seq: 2, ts: 2 });
    expect(result.current.interrupted).toBe(false);
    expect(result.current.status).toBe("open");
    expect(result.current.screen).toBe("fresh");
  });

  it("retry opens a new stream for the same session and keeps the last screen", () => {
    const { result } = renderHook(() => usePaneStream("s1"));
    send({ source: "pane", type: "screen", screen: "kept", seq: 1, ts: 1 });
    const first = MockEventSource.last;
    act(() => { first!.readyState = 2; first!.onerror?.(); });
    act(() => result.current.retry());
    expect(first!.closed).toBe(true);
    expect(MockEventSource.last).not.toBe(first);
    expect(result.current.screen).toBe("kept");
    expect(result.current.status).toBe("connecting");
    expect(result.current.interrupted).toBe(true);
    send({ source: "pane", type: "screen", screen: "after retry", seq: 1, ts: 3 });
    expect(result.current.screen).toBe("after retry");
    expect(result.current.interrupted).toBe(false);
  });

  it("a different session starts blank", () => {
    const { result, rerender } = renderHook(({ id }) => usePaneStream(id), { initialProps: { id: "s1" } });
    send({ source: "pane", type: "screen", screen: "s1 screen", seq: 1, ts: 1 });
    rerender({ id: "s2" });
    expect(result.current.screen).toBeNull();
    expect(result.current.lastFrameAt).toBeNull();
  });

  it("bounds a runaway screen", () => {
    const { result } = renderHook(() => usePaneStream("s1"));
    send({ source: "pane", type: "screen", screen: "x".repeat(MAX_SCREEN_CHARS + 10), seq: 1, ts: 1 });
    expect(result.current.screen!.length).toBe(MAX_SCREEN_CHARS);
  });
});
