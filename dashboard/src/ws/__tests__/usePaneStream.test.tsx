import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import { MAX_SCREEN_CHARS, usePaneStream } from "../usePaneStream";

const api = vi.hoisted(() => ({ sessionShow: vi.fn() }));
vi.mock("../../api/client", () => api);

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
  vi.useFakeTimers();
  api.sessionShow.mockReset().mockResolvedValue({ data: { session: { state: "running" } } });
  MockEventSource.last = null;
  vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function pane(hook: () => ReturnType<typeof usePaneStream>) {
  const result = renderHook(hook);
  act(() => vi.advanceTimersByTime(0));
  return result;
}

function send(frame: Record<string, unknown>) {
  act(() => {
    MockEventSource.last?.onmessage?.({ data: JSON.stringify(frame) });
  });
}

describe("usePaneStream", () => {
  it("replaces the screen rather than appending", () => {
    const { result } = pane(() => usePaneStream("s1"));
    send({ source: "pane", type: "screen", screen: "first", seq: 1, ts: 1 });
    expect(result.current.screen).toBe("first");
    send({ source: "pane", type: "screen", screen: "second", seq: 2, ts: 2 });
    expect(result.current.screen).toBe("second");
  });

  it("surfaces a stopped frame as status", () => {
    const { result } = pane(() => usePaneStream("s1"));
    send({ source: "pane", type: "screen", screen: "last", seq: 1, ts: 1 });
    send({ source: "pane", type: "stopped", seq: 2, ts: 2 });
    expect(result.current.status).toBe("stopped");
    expect(result.current.screen).toBe("last");
  });

  it("surfaces an error frame with its message", () => {
    const { result } = pane(() => usePaneStream("s1"));
    send({ source: "pane", type: "error", message: "tmux is gone", seq: 1, ts: 1 });
    expect(result.current.status).toBe("error");
    expect(result.current.error).toBe("tmux is gone");
  });

  it("closes the stream on a stopped frame so the browser cannot reconnect", () => {
    // The server returns from its generator on a terminal frame, and per the
    // SSE spec a normally-closed stream is retried after ~3s. Left open, that
    // re-subscribes forever.
    const { result } = pane(() => usePaneStream("s1"));
    send({ source: "pane", type: "screen", screen: "last", seq: 1, ts: 1 });
    send({ source: "pane", type: "stopped", seq: 2, ts: 2 });
    expect(MockEventSource.last?.closed).toBe(true);
    expect(result.current.status).toBe("stopped");
    expect(result.current.screen).toBe("last");
  });

  it("closes the stream on an error frame", () => {
    pane(() => usePaneStream("s1"));
    send({ source: "pane", type: "error", message: "cap reached", seq: 1, ts: 1 });
    expect(MockEventSource.last?.closed).toBe(true);
  });

  it("keeps the last good screen when an empty one arrives after a terminal frame", () => {
    // A re-subscribe peeks a reaped tmux session and gets "", which is not
    // nullish — `f.screen ?? prev.screen` would happily blank the banner.
    const { result } = pane(() => usePaneStream("s1"));
    send({ source: "pane", type: "screen", screen: "last words", seq: 1, ts: 1 });
    send({ source: "pane", type: "stopped", seq: 2, ts: 2 });
    send({ source: "pane", type: "screen", screen: "", seq: 3, ts: 3 });
    expect(result.current.screen).toBe("last words");
    expect(result.current.status).toBe("stopped");
  });

  it("ignores onerror after we closed the stream ourselves", () => {
    const { result } = pane(() => usePaneStream("s1"));
    send({ source: "pane", type: "stopped", seq: 1, ts: 1 });
    act(() => {
      MockEventSource.last?.onerror?.();
    });
    expect(result.current.status).toBe("stopped");
  });

  it("retries even a CLOSED EventSource and restores its screen", async () => {
    const { result } = pane(() => usePaneStream("s1"));
    act(() => {
      MockEventSource.last!.readyState = 2;
      MockEventSource.last?.onerror?.();
    });
    expect(result.current.status).toBe("reconnecting");
    expect(result.current.attempt).toBe(1);
    const old = MockEventSource.last;
    await act(() => vi.advanceTimersByTimeAsync(500));
    expect(MockEventSource.last).not.toBe(old);
    send({ type: "screen", screen: "restored", seq: 1 });
    expect(result.current.status).toBe("open");
    expect(result.current.screen).toBe("restored");
  });

  it("opens no connection when disabled", () => {
    pane(() => usePaneStream("s1", { enabled: false }));
    expect(MockEventSource.last).toBeNull();
  });

  it("closes the connection on unmount", () => {
    const { unmount } = pane(() => usePaneStream("s1"));
    const es = MockEventSource.last;
    unmount();
    expect(es?.closed).toBe(true);
  });
});

it.each(["API 401: token required", "API 403: denied"])("does not reconnect on %s", async (message) => {
  api.sessionShow.mockRejectedValue(new Error(message));
  const { result } = pane(() => usePaneStream("s1"));
  const old = MockEventSource.last!;
  act(() => { old.readyState = 2; old.onerror?.(); });
  await act(() => vi.advanceTimersByTimeAsync(60_000));
  expect(result.current.status).toBe("error");
  expect(result.current.error).toBe(message);
  expect(MockEventSource.last).toBe(old);
  expect(vi.getTimerCount()).toBe(0);
});

it("stops when a CLOSED stream's session has ended", async () => {
  api.sessionShow.mockResolvedValue({ data: { session: { state: "stopped" } } });
  const { result } = pane(() => usePaneStream("s1"));
  send({ type: "screen", screen: "last words" });
  const old = MockEventSource.last!;
  act(() => { old.readyState = 2; old.onerror?.(); });
  await act(() => vi.advanceTimersByTimeAsync(60_000));
  expect(result.current.status).toBe("stopped");
  expect(result.current.screen).toBe("last words");
  expect(MockEventSource.last).toBe(old);
});

it("pauses while hidden and resumes immediately without replacing the last screen", () => {
  const { result } = pane(() => usePaneStream("s1"));
  send({ type: "screen", screen: "last" });
  const visible = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
  const old = MockEventSource.last!;
  act(() => old.onerror?.());
  act(() => vi.advanceTimersByTime(60_000));
  expect(MockEventSource.last).toBe(old);
  expect(result.current.screen).toBe("last");
  visible.mockReturnValue("visible");
  act(() => { document.dispatchEvent(new Event("visibilitychange")); vi.advanceTimersByTime(0); });
  expect(MockEventSource.last).not.toBe(old);
  visible.mockRestore();
});

it("manual pane recovery opens one subscription and ignores old callbacks", () => {
  const { result } = pane(() => usePaneStream("s1"));
  const old = MockEventSource.last!;
  act(() => old.onerror?.());
  act(() => { result.current.reconnect?.(); vi.advanceTimersByTime(0); });
  send({ type: "screen", screen: "new" });
  act(() => old.onmessage?.({ data: JSON.stringify({ type: "screen", screen: "stale" }) }));
  expect(result.current.screen).toBe("new");
  expect(old.closed).toBe(true);
});

describe("usePaneStream — stale screen and manual reconnect", () => {
  it("marks the screen interrupted from a drop until the next frame, keeping it on show", async () => {
    const { result } = pane(() => usePaneStream("s1"));
    send({ type: "screen", screen: "kept", seq: 1 });
    expect(result.current.lastFrameAt).not.toBeNull();
    expect(result.current.interrupted).toBe(false);
    act(() => MockEventSource.last?.onerror?.());
    expect(result.current.status).toBe("reconnecting");
    expect(result.current.interrupted).toBe(true);
    expect(result.current.screen).toBe("kept");
    await act(() => vi.advanceTimersByTimeAsync(1_000));
    act(() => MockEventSource.last?.onopen?.());
    expect(result.current.interrupted).toBe(true); // connected, but nothing new on screen yet
    send({ type: "screen", screen: "fresh", seq: 2 });
    expect(result.current.interrupted).toBe(false);
    expect(result.current.screen).toBe("fresh");
  });

  it("reconnect after an error frame opens a fresh stream and keeps the last screen", () => {
    const { result } = pane(() => usePaneStream("s1"));
    send({ type: "screen", screen: "kept", seq: 1 });
    send({ type: "error", message: "tmux is gone", seq: 2 });
    const first = MockEventSource.last!;
    expect(first.closed).toBe(true);
    act(() => result.current.reconnect());
    act(() => vi.advanceTimersByTime(0));
    expect(MockEventSource.last).not.toBe(first);
    expect(result.current.screen).toBe("kept");
    expect(result.current.status).toBe("connecting");
    expect(result.current.interrupted).toBe(true);
    send({ type: "screen", screen: "after reconnect", seq: 1 });
    expect(result.current.status).toBe("open");
    expect(result.current.screen).toBe("after reconnect");
  });

  it("a different session starts blank", () => {
    const { result, rerender } = renderHook(({ id }) => usePaneStream(id), { initialProps: { id: "s1" } });
    act(() => vi.advanceTimersByTime(0));
    send({ type: "screen", screen: "s1 screen", seq: 1 });
    rerender({ id: "s2" });
    expect(result.current.screen).toBeNull();
    expect(result.current.lastFrameAt).toBeNull();
    expect(result.current.interrupted).toBe(false);
  });

  it("bounds a runaway screen to its newest characters", () => {
    const { result } = pane(() => usePaneStream("s1"));
    send({ type: "screen", screen: "old" + "x".repeat(MAX_SCREEN_CHARS), seq: 1 });
    expect(result.current.screen).toHaveLength(MAX_SCREEN_CHARS);
    expect(result.current.screen!.startsWith("old")).toBe(false);
  });
});
