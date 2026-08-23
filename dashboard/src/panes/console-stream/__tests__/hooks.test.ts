import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useConsoleStream } from "../hooks";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  close() {
    this.closed = true;
  }

  emit(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) });
  }
}

beforeEach(() => {
  FakeEventSource.instances = [];
  // @ts-expect-error test stub
  global.EventSource = FakeEventSource;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useConsoleStream", () => {
  it("starts in connecting status", () => {
    const { result } = renderHook(() => useConsoleStream("abc"));
    expect(result.current.status).toBe("connecting");
  });

  it("appends line frames and flips to running", async () => {
    const { result } = renderHook(() => useConsoleStream("abc"));
    const es = FakeEventSource.instances[0]!;
    es.emit({ type: "line", seq: 0, stream: "stdout", text: "hi", ts: 1 });

    await waitFor(() => {
      expect(result.current.lines).toHaveLength(1);
    });
    expect(result.current.status).toBe("running");
    expect(result.current.lines[0]).toMatchObject({ stream: "stdout", text: "hi" });
  });

  it("flips to exited on an exit frame and closes the EventSource", async () => {
    const { result } = renderHook(() => useConsoleStream("abc"));
    const es = FakeEventSource.instances[0]!;
    es.emit({ type: "exit", seq: 1, rc: 0, ts: 2 });

    await waitFor(() => {
      expect(result.current.status).toBe("exited");
    });
    expect(result.current.exitCode).toBe(0);
    expect(es.closed).toBe(true);
  });

  it("flips to killed on a killed frame", async () => {
    const { result } = renderHook(() => useConsoleStream("abc"));
    const es = FakeEventSource.instances[0]!;
    es.emit({ type: "killed", seq: 1, ts: 2 });

    await waitFor(() => {
      expect(result.current.status).toBe("killed");
    });
  });

  it("does nothing when streamId is null", () => {
    renderHook(() => useConsoleStream(null));
    expect(FakeEventSource.instances).toHaveLength(0);
  });

  it("closes the EventSource on unmount", () => {
    const { unmount } = renderHook(() => useConsoleStream("abc"));
    const es = FakeEventSource.instances[0]!;
    unmount();
    expect(es.closed).toBe(true);
  });
});
