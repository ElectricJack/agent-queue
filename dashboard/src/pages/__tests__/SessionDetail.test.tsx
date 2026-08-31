import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import SessionDetail from "../SessionDetail";
import { TerminalMock, FitAddonMock, TerminalSocketMock, ResizeObserverMock } from "../../testUtils/terminal";

const state = vi.hoisted(() => ({ session: { id: "session-a", name: "Worker", project_id: "p1", state: "running", provider: "tmux" } }));
vi.mock("../../api/hooks", () => ({
  useSession: () => ({ data: state.session }),
  useSessionAttach: () => ({ data: { attach_command: "tmux attach" } }),
  useSessionNudge: () => ({ mutate: vi.fn() }),
  useSessionKill: () => ({ mutate: vi.fn() }),
}));
vi.mock("../../ws/useTranscriptStream", () => ({ useTranscriptStream: () => ({
  entries: [{ _idx: 0, type: "assistant", text: "Saved transcript" }], status: "open", clear: vi.fn(),
}) }));
vi.mock("../../ws/usePaneStream", () => ({ usePaneStream: () => ({ screen: null, status: "connecting" }) }));
vi.mock("@xterm/xterm", async () => ({ Terminal: (await import("../../testUtils/terminal")).TerminalMock }));
vi.mock("@xterm/addon-fit", async () => ({ FitAddon: (await import("../../testUtils/terminal")).FitAddonMock }));

function page() {
  return <MemoryRouter initialEntries={["/sessions/session-a"]}><Routes>
    <Route path="/sessions/:sessionId" element={<SessionDetail />} />
  </Routes></MemoryRouter>;
}
beforeEach(() => {
  state.session.state = "running"; state.session.provider = "tmux";
  TerminalMock.instances = []; TerminalSocketMock.instances = []; FitAddonMock.instances = []; ResizeObserverMock.instances = [];
  vi.stubGlobal("WebSocket", TerminalSocketMock);
  vi.stubGlobal("ResizeObserver", ResizeObserverMock);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ width: 800, height: 400 } as DOMRect);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("Session terminal", () => {
  it.each(["stopped", "sleeping", "starting", "stopping", "failed"])("hides Pane for %s sessions", (sessionState) => {
    state.session.state = sessionState;
    render(page());
    expect(screen.queryByText("Pane")).not.toBeInTheDocument();
    expect(screen.getByText("Saved transcript")).toBeInTheDocument();
    expect(TerminalSocketMock.instances).toHaveLength(0);
  });

  it("hides Pane for non-tmux providers", () => {
    state.session.provider = "other";
    render(page());
    expect(screen.queryByText("Pane")).not.toBeInTheDocument();
    expect(TerminalSocketMock.instances).toHaveLength(0);
  });

  it.each(["running", "draining"])("uses the flock terminal for a %s session, including raw colors and input", (sessionState) => {
    state.session.state = sessionState;
    render(page());
    expect(TerminalSocketMock.instances).toHaveLength(0);
    fireEvent.click(screen.getByText("Pane"));
    expect(screen.getByText("Live tmux · interactive")).toBeInTheDocument();
    const socket = TerminalSocketMock.instances[0]!;
    const terminal = TerminalMock.instances[0]!;
    expect(socket.url).toContain("/ws/terminal/session-a");
    act(() => socket.ready());
    const colored = new TextEncoder().encode("\x1b[38;2;255;90;0mColor\x1b[0m");
    act(() => socket.message(colored));
    expect(terminal.write).toHaveBeenCalledWith(Uint8Array.from(colored), expect.any(Function));
    fireEvent.click(screen.getByRole("button", { name: "Send Enter to Worker" }));
    expect(socket.inputs().map((bytes) => new TextDecoder().decode(bytes))).toEqual(["\r"]);
    fireEvent.click(screen.getByText("Transcript", { exact: true }));
    expect(socket.closed).toBe(true);
    expect(screen.getByText("Saved transcript")).toBeInTheDocument();
  });

  it("closes the terminal and returns to Transcript when the session stops", () => {
    const view = render(page());
    fireEvent.click(screen.getByText("Pane"));
    const socket = TerminalSocketMock.instances[0]!;
    expect(screen.getByText("Live tmux · interactive")).toBeInTheDocument();
    state.session.state = "stopped";
    view.rerender(page());
    expect(screen.queryByText("Pane")).not.toBeInTheDocument();
    expect(socket.closed).toBe(true);
    expect(screen.getByText("Saved transcript")).toBeInTheDocument();
  });
});
