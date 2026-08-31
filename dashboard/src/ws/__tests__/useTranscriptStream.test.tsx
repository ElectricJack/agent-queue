import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useTranscriptStream } from "../useTranscriptStream";

class Source {
  static instances: Source[] = [];
  url: string;
  closed = false;
  onmessage: ((message: { data: string }) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, (event: { data: string }) => void>();
  constructor(url: string) { this.url = url; Source.instances.push(this); }
  addEventListener(name: string, listener: (event: { data: string }) => void) { this.listeners.set(name, listener); }
  close() { this.closed = true; }
  message(text: string) { this.onmessage?.({ data: JSON.stringify({ source: "transcript", text, ts: 1 }) }); }
  event(name: string, data: unknown) { this.listeners.get(name)?.({ data: JSON.stringify(data) }); }
}
beforeEach(() => { Source.instances = []; vi.stubGlobal("EventSource", Source); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("attempt transcript streaming", () => {
  it("pins the stream URL to the selected attempt", () => {
    renderHook(() => useTranscriptStream("session/a", { attemptId: "attempt?old" }));
    const url = new URL(Source.instances[0]!.url);
    expect(url.pathname).toBe("/api/sessions/session%2Fa/stream");
    expect(url.searchParams.get("attempt_id")).toBe("attempt?old");
  });
  it("discards old entries and ignores late frames when switching attempts", () => {
    const hook = renderHook(({ attemptId }) => useTranscriptStream("session-a", { attemptId }), { initialProps: { attemptId: "old" } });
    const old = Source.instances[0]!;
    act(() => old.message("Old output"));
    expect(hook.result.current.entries[0]!.text).toBe("Old output");
    hook.rerender({ attemptId: "new" });
    expect(old.closed).toBe(true);
    expect(hook.result.current.entries).toEqual([]);
    act(() => old.message("Late old output"));
    expect(hook.result.current.entries).toEqual([]);
    act(() => Source.instances[1]!.message("New output"));
    expect(hook.result.current.entries.map((entry) => entry.text)).toEqual(["New output"]);
  });
  it("surfaces a missing transcript and closes the stream to prevent retries", () => {
    const hook = renderHook(() => useTranscriptStream("session-a", { attemptId: "old" }));
    act(() => Source.instances[0]!.event("unavailable", { text: "Transcript file unavailable" }));
    expect(hook.result.current.unavailable).toBe("Transcript file unavailable");
    expect(hook.result.current.status).toBe("closed");
    expect(Source.instances[0]!.closed).toBe(true);
    expect(hook.result.current.entries).toEqual([]);
  });
  it("keeps final output and closes a completed replay without retrying", () => {
    const hook = renderHook(() => useTranscriptStream("session-a", { attemptId: "old" }));
    act(() => Source.instances[0]!.message("Final output"));
    act(() => Source.instances[0]!.event("complete", {}));
    expect(hook.result.current.status).toBe("closed");
    expect(Source.instances[0]!.closed).toBe(true);
    expect(hook.result.current.entries.map((entry) => entry.text)).toEqual(["Final output"]);
    act(() => Source.instances[0]!.onerror?.());
    expect(hook.result.current.error).toBeNull();
  });
  it("does not open a stream while disabled and keeps the legacy URL unchanged", () => {
    const hook = renderHook(({ enabled }) => useTranscriptStream("session-a", { enabled }), { initialProps: { enabled: false } });
    expect(Source.instances).toHaveLength(0);
    hook.rerender({ enabled: true });
    expect(new URL(Source.instances[0]!.url).search).toBe("");
  });
});
