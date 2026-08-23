import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import ConsoleStreamPane from "../index";
import type { ConsoleStreamArgs } from "../manifest";

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

function makeProps(overrides: Partial<ConsoleStreamArgs> = {}) {
  return {
    args: { streamId: "abc123", ...overrides } as ConsoleStreamArgs,
    close: vi.fn(),
    setArgs: vi.fn(),
    setToolbar: vi.fn(),
    setShortcuts: vi.fn(),
  };
}

beforeEach(() => {
  FakeEventSource.instances = [];
  // @ts-expect-error test stub
  global.EventSource = FakeEventSource;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true }));
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
    configurable: true,
  });
  window.localStorage.removeItem("aq:session:id");
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ConsoleStreamPane", () => {
  it("renders connecting immediately, then running once a line frame arrives", async () => {
    const props = makeProps();
    render(<ConsoleStreamPane {...props} />);
    expect(screen.getByText("connecting…")).toBeInTheDocument();

    const es = FakeEventSource.instances[0];
    es.emit({ type: "line", seq: 0, stream: "stdout", text: "hello", ts: Date.now() / 1000 });

    await waitFor(() => expect(screen.getByText(/running/)).toBeInTheDocument());
    expect(screen.getByText("hello")).toBeInTheDocument();
  });

  it("interleaves stdout/stderr with stderr getting a red left border", async () => {
    const props = makeProps();
    render(<ConsoleStreamPane {...props} />);
    const es = FakeEventSource.instances[0];
    es.emit({ type: "line", seq: 0, stream: "stdout", text: "out line", ts: 1 });
    es.emit({ type: "line", seq: 1, stream: "stderr", text: "err line", ts: 2 });

    await waitFor(() => expect(screen.getByText("err line")).toBeInTheDocument());
    const errRow = screen.getByText("err line").closest("div");
    expect(errRow?.className).toContain("border-red-500");
  });

  it("space toggles follow-tail and flips the toolbar label", async () => {
    const props = makeProps();
    render(<ConsoleStreamPane {...props} />);
    await waitFor(() => expect(props.setToolbar).toHaveBeenCalled());

    const lastCall = props.setToolbar.mock.calls.at(-1)![0];
    const pauseAction = lastCall.find((a: { id: string }) => a.id === "pause-tail");
    expect(pauseAction.label).toBe("Pause tail");

    const lastShortcuts = props.setShortcuts.mock.calls.at(-1)![0];
    const spaceBinding = lastShortcuts.find((s: { key: string }) => s.key === "space");
    spaceBinding.onFire();

    await waitFor(() => {
      const latest = props.setToolbar.mock.calls.at(-1)![0];
      const action = latest.find((a: { id: string }) => a.id === "pause-tail");
      expect(action.label).toBe("Resume tail");
    });
  });

  it("copy output copies plain-text (ANSI-stripped) scrollback", async () => {
    const props = makeProps();
    render(<ConsoleStreamPane {...props} />);
    const es = FakeEventSource.instances[0];
    es.emit({ type: "line", seq: 0, stream: "stdout", text: "\x1b[32mgreen\x1b[0m", ts: 1 });

    await waitFor(() => expect(screen.getByText("green")).toBeInTheDocument());

    const latestShortcuts = props.setShortcuts.mock.calls.at(-1)![0];
    const copyBinding = latestShortcuts.find((s: { key: string }) => s.key === "c");
    copyBinding.onFire();

    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith("green");
    });
  });

  it("kill button is present while running and absent after a terminal frame", async () => {
    const props = makeProps();
    render(<ConsoleStreamPane {...props} />);
    const es = FakeEventSource.instances[0];
    es.emit({ type: "line", seq: 0, stream: "stdout", text: "hi", ts: 1 });

    await waitFor(() => {
      const latest = props.setToolbar.mock.calls.at(-1)![0];
      expect(latest.some((a: { id: string }) => a.id === "kill")).toBe(true);
    });

    es.emit({ type: "exit", seq: 1, rc: 0, ts: 2 });

    await waitFor(() => {
      const latest = props.setToolbar.mock.calls.at(-1)![0];
      expect(latest.some((a: { id: string }) => a.id === "kill")).toBe(false);
    });
  });

  it("k opens the kill confirm popover; confirm calls the kill endpoint", async () => {
    const props = makeProps();
    render(<ConsoleStreamPane {...props} />);
    const es = FakeEventSource.instances[0];
    es.emit({ type: "line", seq: 0, stream: "stdout", text: "hi", ts: 1 });

    // Wait for the setShortcuts call captured while status === "running"
    // (that binding activates the popover; the initial-render binding is
    // captured when status is "connecting" and no-ops).
    await waitFor(() => {
      const latest = props.setToolbar.mock.calls.at(-1)![0];
      expect(latest.some((a: { id: string }) => a.id === "kill")).toBe(true);
    });
    const latestShortcuts = props.setShortcuts.mock.calls.at(-1)![0];
    const killBinding = latestShortcuts.find((s: { key: string }) => s.key === "k");
    await act(async () => {
      killBinding.onFire();
    });

    expect(screen.getByRole("dialog", { name: "Kill this process?" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("Confirm"));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/streams/abc123/kill",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("cancel on the kill confirm popover does not call the kill endpoint", async () => {
    const props = makeProps();
    render(<ConsoleStreamPane {...props} />);
    const es = FakeEventSource.instances[0];
    es.emit({ type: "line", seq: 0, stream: "stdout", text: "hi", ts: 1 });
    await waitFor(() => {
      const latest = props.setToolbar.mock.calls.at(-1)![0];
      expect(latest.some((a: { id: string }) => a.id === "kill")).toBe(true);
    });
    const latestShortcuts = props.setShortcuts.mock.calls.at(-1)![0];
    await act(async () => {
      latestShortcuts.find((s: { key: string }) => s.key === "k").onFire();
    });

    fireEvent.click(screen.getByText("Cancel"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("terminal frame freezes header, appends exit banner, force-disables follow-tail", async () => {
    const props = makeProps();
    render(<ConsoleStreamPane {...props} />);
    const es = FakeEventSource.instances[0];
    es.emit({ type: "line", seq: 0, stream: "stdout", text: "hi", ts: 1 });
    es.emit({ type: "exit", seq: 1, rc: 1, ts: 35 });

    await waitFor(() => expect(screen.getByText("exited (1)")).toBeInTheDocument());
    expect(screen.getByText(/exited with code 1 after/)).toBeInTheDocument();
  });

  it("sessionId mismatch renders scope-mismatch state without opening the EventSource", () => {
    window.localStorage.setItem("aq:session:id", "supervisor-demo");
    const props = makeProps({ sessionId: "supervisor-other" });
    render(<ConsoleStreamPane {...props} />);
    expect(screen.getByText(/don't have access/)).toBeInTheDocument();
    expect(FakeEventSource.instances).toHaveLength(0);
  });

  it("unmount closes the EventSource (no leaked subscription)", () => {
    const props = makeProps();
    const { unmount } = render(<ConsoleStreamPane {...props} />);
    const es = FakeEventSource.instances[0];
    unmount();
    expect(es.closed).toBe(true);
  });
});
