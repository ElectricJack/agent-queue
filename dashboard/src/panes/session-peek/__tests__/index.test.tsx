import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import SessionPeekPane from "../index";
import { manifest, sessionPeekArgsSchema } from "../manifest";

const mockUsePaneStream = vi.fn();
const mockUseSession = vi.fn();
const mockUseSessionKill = vi.fn();

vi.mock("../../../ws/usePaneStream", () => ({
  usePaneStream: (...args: unknown[]) => mockUsePaneStream(...args),
}));
vi.mock("../../../api/hooks", () => ({
  useSession: (...args: unknown[]) => mockUseSession(...args),
  useSessionKill: () => mockUseSessionKill(),
}));
const mockNavigate = vi.fn();
vi.mock("react-router-dom", () => ({
  useLocation: () => ({ pathname: "/projects/demo/sessions", search: "?q=active" }),
  useNavigate: () => mockNavigate,
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

// Avoids Array.prototype.at() — this package's tsconfig lib target (ES2020)
// predates it.
function lastCallArg0<T>(mockFn: { mock: { calls: T[][] } }): T | undefined {
  const calls = mockFn.mock.calls;
  return calls[calls.length - 1]?.[0];
}

describe("SessionPeekPane component", () => {
  beforeEach(() => {
    mockNavigate.mockReset();
    mockUsePaneStream.mockReset();
    mockUseSession.mockReset();
    mockUseSessionKill.mockReset();
    mockUseSessionKill.mockReturnValue({ mutate: vi.fn() });
    mockUseSession.mockReturnValue({ data: { lifecycle: "running" } });
    mockUsePaneStream.mockReturnValue({
      screen: "hello",
      status: "open",
      error: null,
      seq: 1,
    });
  });

  it("renders without crashing given valid args", () => {
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText(/hello/)).toBeInTheDocument();
  });

  it("shows a waiting state while connecting with no screen yet", () => {
    mockUsePaneStream.mockReturnValue({
      screen: null,
      status: "connecting",
      error: null,
      seq: 0,
    });
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText(/waiting for pane/i)).toBeInTheDocument();
  });

  it("renders the stream error banner", () => {
    mockUsePaneStream.mockReturnValue({
      screen: null,
      status: "error",
      error: "stream error (EventSource will retry)",
      seq: 0,
    });
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText("stream error (EventSource will retry)")).toBeInTheDocument();
  });

  // --- Task 7: toolbar actions ---

  it("registers three toolbar actions on mount", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const lastCall = lastCallArg0(props.setToolbar);
    const ids = lastCall.map((a: { id: string }) => a.id);
    expect(ids).toEqual(["copy-scrollback", "open-full", "kill-session"]);
  });

  it("copy-scrollback writes the current screen to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      writable: true,
      value: { writeText },
    });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const actions = lastCallArg0(props.setToolbar);
    const copy = actions.find((a: { id: string }) => a.id === "copy-scrollback");
    copy.onClick();
    expect(writeText).toHaveBeenCalledWith("hello");
  });

  it("copy-scrollback is disabled with no screen yet", () => {
    mockUsePaneStream.mockReturnValue({ screen: null, status: "open", error: null, seq: 0 });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const actions = lastCallArg0(props.setToolbar);
    const copy = actions.find((a: { id: string }) => a.id === "copy-scrollback");
    expect(copy.disabled).toBe(true);
  });

  it("open-full closes the pane and retains its workspace source", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const actions = lastCallArg0(props.setToolbar);
    const open = actions.find((a: { id: string }) => a.id === "open-full");
    open.onClick();
    expect(props.close).toHaveBeenCalledOnce();
    expect(mockNavigate).toHaveBeenCalledWith("/sessions/sess-1", {
      state: { from: "/projects/demo/sessions?q=active" },
    });
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

  it("registers three shortcuts on mount", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const lastCall = lastCallArg0(props.setShortcuts);
    const keys = lastCall.map((b: { key: string }) => b.key);
    expect(keys).toEqual(["k", "o", "c"]);
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

  it("c copies the current screen", () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      writable: true,
      value: { writeText },
    });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const bindings = lastCallArg0(props.setShortcuts);
    bindings.find((b: { key: string }) => b.key === "c").onFire();
    expect(writeText).toHaveBeenCalledWith("hello");
  });

  it("unmount clears shortcuts", () => {
    const props = baseProps();
    const { unmount } = render(<SessionPeekPane {...props} />);
    unmount();
    expect(props.setShortcuts).toHaveBeenLastCalledWith([]);
  });

  // --- Task 9: session-exited banner ---

  it("shows the exited banner and keeps the last screen visible when lifecycle is exited", () => {
    mockUseSession.mockReturnValue({ data: { lifecycle: "exited" } });
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText("Session exited — showing last scrollback.")).toBeInTheDocument();
    expect(screen.getByText(/hello/)).toBeInTheDocument();
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
    mockUsePaneStream.mockReturnValue({
      screen: "hello",
      status: "error",
      error: "stream error (EventSource will retry)",
      seq: 1,
    });
    render(<SessionPeekPane {...baseProps()} />);
    expect(
      screen.queryByText("Session exited — showing last scrollback."),
    ).not.toBeInTheDocument();
  });

  it("open-full stays enabled while exited", () => {
    mockUseSession.mockReturnValue({ data: { lifecycle: "exited" } });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const actions = lastCallArg0(props.setToolbar);
    expect(actions.find((a: { id: string }) => a.id === "open-full").disabled).toBeFalsy();
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
