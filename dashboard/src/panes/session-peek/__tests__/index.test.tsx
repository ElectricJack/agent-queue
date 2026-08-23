import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import SessionPeekPane from "../index";
import { manifest, sessionPeekArgsSchema } from "../manifest";

const mockUseTranscriptStream = vi.fn();
const mockUseSession = vi.fn();
const mockUseSessionKill = vi.fn();

vi.mock("../../../ws/useTranscriptStream", () => ({
  useTranscriptStream: (...args: unknown[]) => mockUseTranscriptStream(...args),
}));
vi.mock("../../../api/hooks", () => ({
  useSession: (...args: unknown[]) => mockUseSession(...args),
  useSessionKill: () => mockUseSessionKill(),
}));
vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
}));

function baseProps() {
  return {
    args: { sessionId: "sess-1" },
    close: vi.fn(),
    setArgs: vi.fn(),
    setToolbar: vi.fn(),
    setShortcuts: vi.fn(),
  };
}

function frame(text: string, idx: number, source: "peek" | "transcript" = "peek") {
  return { source, text, ts: idx, _idx: idx };
}

// Avoids Array.prototype.at() — this package's tsconfig lib target (ES2020)
// predates it.
function lastCallArg0<T>(mockFn: { mock: { calls: T[][] } }): T | undefined {
  const calls = mockFn.mock.calls;
  return calls[calls.length - 1]?.[0];
}

describe("SessionPeekPane component", () => {
  beforeEach(() => {
    mockUseTranscriptStream.mockReset();
    mockUseSession.mockReset();
    mockUseSessionKill.mockReset();
    mockUseSessionKill.mockReturnValue({ mutate: vi.fn() });
    mockUseSession.mockReturnValue({ data: { lifecycle: "running" } });
    mockUseTranscriptStream.mockReturnValue({
      entries: [frame("hello", 0), frame("world", 1)],
      status: "open",
      error: null,
    });
  });

  it("renders without crashing given valid args", () => {
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(screen.getByText("world")).toBeInTheDocument();
  });

  it("filters out non-peek frames", () => {
    mockUseTranscriptStream.mockReturnValue({
      entries: [frame("hello", 0, "peek"), frame("ignored", 1, "transcript")],
      status: "open",
      error: null,
    });
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(screen.queryByText("ignored")).not.toBeInTheDocument();
  });

  it("shows a loading state while connecting with no frames yet", () => {
    mockUseTranscriptStream.mockReturnValue({ entries: [], status: "connecting", error: null });
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText("Connecting…")).toBeInTheDocument();
  });

  it("renders the stream error banner", () => {
    mockUseTranscriptStream.mockReturnValue({
      entries: [],
      status: "error",
      error: "stream error (EventSource will retry)",
    });
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText("stream error (EventSource will retry)")).toBeInTheDocument();
  });

  // --- Task 7: toolbar actions ---

  it("registers four toolbar actions on mount", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const lastCall = lastCallArg0(props.setToolbar);
    const ids = lastCall.map((a: { id: string }) => a.id);
    expect(ids).toEqual(["toggle-tail", "copy-scrollback", "open-full", "kill-session"]);
  });

  it("toggle-tail toolbar action flips args.tail via setArgs", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const actions = lastCallArg0(props.setToolbar);
    const toggle = actions.find((a: { id: string }) => a.id === "toggle-tail");
    toggle.onClick();
    expect(props.setArgs).toHaveBeenCalledWith({ sessionId: "sess-1", tail: false });
  });

  it("copy-scrollback writes joined peek text to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const actions = lastCallArg0(props.setToolbar);
    const copy = actions.find((a: { id: string }) => a.id === "copy-scrollback");
    copy.onClick();
    expect(writeText).toHaveBeenCalledWith("hello\nworld");
  });

  it("copy-scrollback is disabled with no frames", () => {
    mockUseTranscriptStream.mockReturnValue({ entries: [], status: "open", error: null });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const actions = lastCallArg0(props.setToolbar);
    const copy = actions.find((a: { id: string }) => a.id === "copy-scrollback");
    expect(copy.disabled).toBe(true);
  });

  it("open-full navigates to the session detail route", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const actions = lastCallArg0(props.setToolbar);
    const open = actions.find((a: { id: string }) => a.id === "open-full");
    expect(() => open.onClick()).not.toThrow();
  });

  it("kill-session arms on first click, commits on second", () => {
    const mutate = vi.fn();
    mockUseSessionKill.mockReturnValue({ mutate });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    let actions = lastCallArg0(props.setToolbar);
    let kill = actions.find((a: { id: string }) => a.id === "kill-session");
    expect(kill.label).toBe("Kill session");
    act(() => kill.onClick());
    expect(mutate).not.toHaveBeenCalled();

    actions = lastCallArg0(props.setToolbar);
    kill = actions.find((a: { id: string }) => a.id === "kill-session");
    expect(kill.label).toBe("Confirm kill?");
    act(() => kill.onClick());
    expect(mutate).toHaveBeenCalledWith({ session_id: "sess-1" });
  });

  it("kill-session is disabled when exited", () => {
    mockUseSession.mockReturnValue({ data: { lifecycle: "exited" } });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const actions = lastCallArg0(props.setToolbar);
    const kill = actions.find((a: { id: string }) => a.id === "kill-session");
    expect(kill.disabled).toBe(true);
  });

  it("unmount clears the toolbar", () => {
    const props = baseProps();
    const { unmount } = render(<SessionPeekPane {...props} />);
    unmount();
    expect(props.setToolbar).toHaveBeenLastCalledWith([]);
  });

  // --- Task 8: keyboard shortcuts ---

  it("registers six shortcuts on mount", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const lastCall = lastCallArg0(props.setShortcuts);
    const keys = lastCall.map((b: { key: string }) => b.key);
    expect(keys).toEqual(["space", "k", "o", "c", "Home", "End"]);
  });

  it("space flips tail via setArgs", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const bindings = lastCallArg0(props.setShortcuts);
    bindings.find((b: { key: string }) => b.key === "space").onFire();
    expect(props.setArgs).toHaveBeenCalledWith({ sessionId: "sess-1", tail: false });
  });

  it("k shares kill's arm/confirm state with the toolbar", () => {
    const mutate = vi.fn();
    mockUseSessionKill.mockReturnValue({ mutate });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    let bindings = lastCallArg0(props.setShortcuts);
    act(() => bindings.find((b: { key: string }) => b.key === "k").onFire());
    expect(mutate).not.toHaveBeenCalled();

    bindings = lastCallArg0(props.setShortcuts);
    act(() => bindings.find((b: { key: string }) => b.key === "k").onFire());
    expect(mutate).toHaveBeenCalledWith({ session_id: "sess-1" });
  });

  it("o opens full session detail", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const bindings = lastCallArg0(props.setShortcuts);
    expect(() => bindings.find((b: { key: string }) => b.key === "o").onFire()).not.toThrow();
  });

  it("c copies scrollback", () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const bindings = lastCallArg0(props.setShortcuts);
    bindings.find((b: { key: string }) => b.key === "c").onFire();
    expect(writeText).toHaveBeenCalledWith("hello\nworld");
  });

  it("Home scrolls to top and turns tail off", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const bindings = lastCallArg0(props.setShortcuts);
    bindings.find((b: { key: string }) => b.key === "Home").onFire();
    expect(props.setArgs).toHaveBeenCalledWith({ sessionId: "sess-1", tail: false });
  });

  it("End scrolls to bottom and turns tail on", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const bindings = lastCallArg0(props.setShortcuts);
    bindings.find((b: { key: string }) => b.key === "End").onFire();
    expect(props.setArgs).toHaveBeenCalledWith({ sessionId: "sess-1", tail: true });
  });

  it("unmount clears shortcuts", () => {
    const props = baseProps();
    const { unmount } = render(<SessionPeekPane {...props} />);
    unmount();
    expect(props.setShortcuts).toHaveBeenLastCalledWith([]);
  });

  // --- Task 9: session-exited banner ---

  it("shows the exited banner and keeps frames visible when lifecycle is exited", () => {
    mockUseSession.mockReturnValue({ data: { lifecycle: "exited" } });
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText("Session exited — showing last scrollback.")).toBeInTheDocument();
    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(screen.getByText("world")).toBeInTheDocument();
  });

  it("shows the exited banner for terminated lifecycle too", () => {
    mockUseSession.mockReturnValue({ data: { lifecycle: "terminated" } });
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText("Session exited — showing last scrollback.")).toBeInTheDocument();
  });

  it("does not show the exited banner while running", () => {
    mockUseSession.mockReturnValue({ data: { lifecycle: "running" } });
    render(<SessionPeekPane {...baseProps()} />);
    expect(
      screen.queryByText("Session exited — showing last scrollback."),
    ).not.toBeInTheDocument();
  });

  it("does not show the exited banner on a transient stream error alone", () => {
    mockUseSession.mockReturnValue({ data: { lifecycle: "running" } });
    mockUseTranscriptStream.mockReturnValue({
      entries: [frame("hello", 0)],
      status: "error",
      error: "stream error (EventSource will retry)",
    });
    render(<SessionPeekPane {...baseProps()} />);
    expect(
      screen.queryByText("Session exited — showing last scrollback."),
    ).not.toBeInTheDocument();
  });

  it("toggle-tail and open-full stay enabled while exited", () => {
    mockUseSession.mockReturnValue({ data: { lifecycle: "exited" } });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const actions = lastCallArg0(props.setToolbar);
    expect(actions.find((a: { id: string }) => a.id === "toggle-tail").disabled).toBeFalsy();
    expect(actions.find((a: { id: string }) => a.id === "open-full").disabled).toBeFalsy();
  });

  // --- Task 10: sticky-scroll ---

  it("scrolling away from bottom while tail is true calls setArgs with tail:false", () => {
    const props = baseProps();
    const { container } = render(<SessionPeekPane {...props} />);
    const scrollBox = container.querySelector(".overflow-y-auto") as HTMLDivElement;
    Object.defineProperty(scrollBox, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(scrollBox, "clientHeight", { value: 300, configurable: true });
    Object.defineProperty(scrollBox, "scrollTop", { value: 200, configurable: true });
    scrollBox.dispatchEvent(new Event("scroll"));
    expect(props.setArgs).toHaveBeenCalledWith({ sessionId: "sess-1", tail: false });
  });

  it("does not call setArgs when already near the bottom", () => {
    const props = baseProps();
    const { container } = render(<SessionPeekPane {...props} />);
    const scrollBox = container.querySelector(".overflow-y-auto") as HTMLDivElement;
    Object.defineProperty(scrollBox, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(scrollBox, "clientHeight", { value: 300, configurable: true });
    Object.defineProperty(scrollBox, "scrollTop", { value: 690, configurable: true }); // slack 10 < 24
    scrollBox.dispatchEvent(new Event("scroll"));
    expect(props.setArgs).not.toHaveBeenCalled();
  });
});

describe("session-peek manifest", () => {
  it("id matches the directory name", () => {
    expect(manifest.id).toBe("session-peek");
  });

  it("args schema accepts a bare sessionId", () => {
    const result = sessionPeekArgsSchema.safeParse({ sessionId: "sess-1" });
    expect(result.success).toBe(true);
  });

  it("args schema accepts sessionId + tail", () => {
    const result = sessionPeekArgsSchema.safeParse({ sessionId: "sess-1", tail: false });
    expect(result.success).toBe(true);
  });

  it("args schema rejects an empty object", () => {
    const result = sessionPeekArgsSchema.safeParse({});
    expect(result.success).toBe(false);
  });

  it("args schema rejects an empty sessionId", () => {
    const result = sessionPeekArgsSchema.safeParse({ sessionId: "" });
    expect(result.success).toBe(false);
  });

  it("has no open_shortcut", () => {
    expect(manifest.open_shortcut).toBeUndefined();
  });
});
