import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import SessionPeekPane from "../index";
import { manifest, sessionPeekArgsSchema } from "../manifest";

const mockUseSession = vi.fn();
const mockUseSessionKill = vi.fn();

// The pane renders the host shell page's terminal, so the xterm and its socket
// are the test's, not a capture-pane stream.
vi.mock("../../../components/InteractiveTerminal", () => ({
  default: ({ sessionId, name }: { sessionId: string; name: string }) => <p>Terminal {name} ({sessionId})</p>,
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

const actionsOf = (props: ReturnType<typeof baseProps>) =>
  (lastCallArg0(props.setToolbar) as { id: string; label: string; onClick: () => void; disabled?: boolean }[]);
const shortcutsOf = (props: ReturnType<typeof baseProps>) =>
  (lastCallArg0(props.setShortcuts) as { key: string; onFire: () => void }[]);

describe("SessionPeekPane", () => {
  beforeEach(() => {
    mockNavigate.mockReset();
    mockUseSession.mockReset();
    mockUseSessionKill.mockReset();
    mockUseSessionKill.mockReturnValue({ mutate: vi.fn() });
    mockUseSession.mockReturnValue({ data: { id: "sess-1", name: "worker-a", state: "running", lifecycle: "running" } });
  });

  it("draws the host shell page's terminal for a live session", () => {
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText("Terminal worker-a (sess-1)")).toBeInTheDocument();
    expect(screen.queryByText(/waiting for pane/i)).toBeNull();
  });

  it("does not crash while the session record is still loading", () => {
    mockUseSession.mockReturnValue({ data: undefined });
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText(/not attached/)).toHaveTextContent("unknown");
    expect(screen.queryByText(/^Terminal /)).toBeNull();
  });

  it("says why a session that is not live has no terminal", () => {
    mockUseSession.mockReturnValue({ data: { id: "sess-1", name: "worker-a", state: "sleeping", lifecycle: "running" } });
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText(/its terminal is not attached/)).toHaveTextContent("sleeping");
    expect(screen.queryByText(/^Terminal /)).toBeNull();
  });

  it("registers the toolbar actions on mount and clears them on unmount", () => {
    const props = baseProps();
    const { unmount } = render(<SessionPeekPane {...props} />);
    expect(actionsOf(props).map((action) => action.id)).toEqual(["open-full", "kill-session"]);
    unmount();
    expect(props.setToolbar).toHaveBeenLastCalledWith([]);
    expect(props.setShortcuts).toHaveBeenLastCalledWith([]);
  });

  it("opens full session detail, closing the pane and keeping the workspace source", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    actionsOf(props).find((action) => action.id === "open-full")!.onClick();
    expect(props.close).toHaveBeenCalledOnce();
    expect(mockNavigate).toHaveBeenCalledWith("/sessions/sess-1", {
      state: { from: "/projects/demo/sessions?q=active" },
    });
  });

  it("kill-session arms on first click, commits on second, and the keyboard shares that state", () => {
    const mutate = vi.fn();
    mockUseSessionKill.mockReturnValue({ mutate });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    act(() => actionsOf(props).find((action) => action.id === "kill-session")!.onClick());
    expect(actionsOf(props).find((action) => action.id === "kill-session")!.label).toBe("Confirm kill?");
    expect(mutate).not.toHaveBeenCalled();
    act(() => actionsOf(props).find((action) => action.id === "kill-session")!.onClick());
    expect(mutate).toHaveBeenCalledWith({ session_id: "sess-1" });

    mockUseSessionKill.mockReturnValue({ mutate: vi.fn() });
    act(() => shortcutsOf(props).find((binding) => binding.key === "k")!.onFire());
    expect(mutate).toHaveBeenCalledTimes(1);
  });

  it("kill-session is disabled once the session has exited", () => {
    mockUseSession.mockReturnValue({ data: { id: "sess-1", name: "worker-a", state: "stopped", lifecycle: "exited" } });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    expect(actionsOf(props).find((action) => action.id === "kill-session")!.disabled).toBe(true);
    expect(actionsOf(props).find((action) => action.id === "open-full")!.disabled).toBeFalsy();
  });

  it("registers the two keyboard shortcuts", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    expect(shortcutsOf(props).map((binding) => binding.key)).toEqual(["o", "k"]);
    expect(() => shortcutsOf(props).find((binding) => binding.key === "o")!.onFire()).not.toThrow();
  });

  it("says a session that has exited is not attached, and does not draw a terminal", () => {
    mockUseSession.mockReturnValue({ data: { id: "sess-1", name: "worker-a", state: "stopped", lifecycle: "terminated" } });
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText("Session exited — open full session detail for its transcript.")).toBeInTheDocument();
    expect(screen.queryByText(/^Terminal /)).toBeNull();
  });

  it("shows no notice while the session is running", () => {
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.queryByText(/Session exited/)).toBeNull();
    expect(screen.queryByText(/not attached/)).toBeNull();
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
