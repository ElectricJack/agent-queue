import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
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

function stubMetadataFetch(body: unknown) {
  const mock = vi.fn().mockResolvedValue({ ok: true, json: async () => body });
  vi.stubGlobal("fetch", mock);
  return mock;
}

beforeEach(() => {
  FakeEventSource.instances = [];
  // @ts-expect-error test stub
  global.EventSource = FakeEventSource;
  // The hook fetches GET /api/streams/{id} for its reconnect budget; keep
  // that off the network for every test.
  stubMetadataFetch({});
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
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

  it("caps reconnects at the server's client_reconnect_attempts", async () => {
    // streams.client_reconnect_attempts = 1 -> one retry, then give up.
    const fetchMock = stubMetadataFetch({ client_reconnect_attempts: 1 });
    const { result } = renderHook(() => useConsoleStream("abc"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    // let the metadata promise chain settle before the first error fires
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    act(() => FakeEventSource.instances[0]!.onerror?.());
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(2), { timeout: 3000 });

    act(() => FakeEventSource.instances[1]!.onerror?.());
    await waitFor(() => expect(result.current.errorMessage).toBe("connection lost"));
    expect(FakeEventSource.instances).toHaveLength(2);
  });

  it("honours a zero reconnect budget by giving up immediately", async () => {
    const fetchMock = stubMetadataFetch({ client_reconnect_attempts: 0 });
    const { result } = renderHook(() => useConsoleStream("abc"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    act(() => FakeEventSource.instances[0]!.onerror?.());
    await waitFor(() => expect(result.current.errorMessage).toBe("connection lost"));
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it("still reconnects on the built-in default when the metadata fetch fails", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("offline"));
    vi.stubGlobal("fetch", fetchMock);
    renderHook(() => useConsoleStream("abc"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    act(() => FakeEventSource.instances[0]!.onerror?.());
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(2), { timeout: 3000 });
  });

  it("closes the EventSource on unmount", () => {
    const { unmount } = renderHook(() => useConsoleStream("abc"));
    const es = FakeEventSource.instances[0]!;
    unmount();
    expect(es.closed).toBe(true);
  });
});
