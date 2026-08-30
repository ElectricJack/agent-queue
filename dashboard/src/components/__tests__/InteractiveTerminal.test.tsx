import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import InteractiveTerminal from "../InteractiveTerminal";
import { TerminalMock, FitAddonMock, TerminalSocketMock, ResizeObserverMock } from "../../testUtils/terminal";

vi.mock("@xterm/xterm", async () => ({ Terminal: (await import("../../testUtils/terminal")).TerminalMock }));
vi.mock("@xterm/addon-fit", async () => ({ FitAddon: (await import("../../testUtils/terminal")).FitAddonMock }));
const api = vi.hoisted(() => ({ sessionInput: vi.fn() }));
vi.mock("../../api/client", () => api);

let frames: FrameRequestCallback[] = [];
beforeEach(() => {
  TerminalMock.instances = []; FitAddonMock.instances = []; TerminalSocketMock.instances = []; ResizeObserverMock.instances = [];
  frames = [];
  vi.stubGlobal("WebSocket", TerminalSocketMock);
  vi.stubGlobal("ResizeObserver", ResizeObserverMock);
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.push(callback); return frames.length; });
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ width: 800, height: 400 } as DOMRect);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.clearAllMocks(); });

function terminal() {
  const view = render(<InteractiveTerminal name="Builder" sessionId="session-b" />);
  return { view, term: TerminalMock.instances[0]!, socket: TerminalSocketMock.instances[0]! };
}
function inputs(socket: TerminalSocketMock) { return socket.inputs().map((bytes) => new TextDecoder().decode(bytes)); }

describe("Interactive live terminal", () => {
  it("waits for the PTY ready handshake before accepting input", () => {
    const { term, socket } = terminal();
    expect(screen.getByRole("textbox", { name: "Builder terminal input" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("button", { name: "Send Enter to Builder" })).toBeDisabled();
    term.emitData("discard");
    act(() => socket.open());
    term.emitData("also discard");
    expect(socket.inputs()).toEqual([]);
    act(() => socket.ready());
    expect(screen.getByRole("textbox", { name: "Builder terminal input" })).toHaveAttribute("aria-disabled", "false");
    expect(term.options.disableStdin).toBe(false);
  });

  it("sends typing, IME/paste sequences and controls immediately over the session socket", () => {
    const { term, socket } = terminal();
    act(() => socket.ready());
    act(() => {
      term.emitData("h");
      term.emitData("i");
      term.emitData("\x1b[200~hello\n世界\n\x1b[201~");
    });
    fireEvent.click(screen.getByRole("button", { name: "Send Enter to Builder" }));
    fireEvent.click(screen.getByRole("button", { name: "Interrupt Builder" }));
    expect(inputs(socket)).toEqual(["h", "i", "\x1b[200~hello\n世界\n\x1b[201~", "\r", "\x03"]);
    expect(api.sessionInput).not.toHaveBeenCalled();
  });

  it("forwards xterm onBinary bytes without UTF-8 re-encoding", () => {
    const { term, socket } = terminal();
    act(() => socket.ready());
    act(() => term.emitBinary("\x1b[M\xff\x80"));
    expect([...socket.inputs()[0]!]).toEqual([27, 91, 77, 255, 128]);
  });

  it("passes raw color and cursor sequences directly to xterm and ACKs after parsing", () => {
    const { term, socket } = terminal();
    act(() => socket.ready());
    const bytes = new TextEncoder().encode("\x1b[38;2;255;90;0m\x1b[48;5;32m\x1b[7mCOLOR\x1b[0m");
    act(() => socket.message(bytes));
    expect(term.write).toHaveBeenCalledWith(Uint8Array.from(bytes), expect.any(Function));
    expect(socket.controls()).toEqual([]);
    act(() => term.flushWrites());
    expect(socket.controls()).toEqual([{ type: "ack", bytes: bytes.length }]);
  });

  it("disconnects without replay and reconnects only after explicit action", () => {
    const { term, socket } = terminal();
    act(() => socket.ready());
    act(() => socket.serverClose());
    term.emitData("discard after disconnect");
    expect(screen.getByRole("alert")).toHaveTextContent(/disconnected.*discarded/i);
    expect(screen.getByRole("button", { name: "Interrupt Builder" })).toBeDisabled();
    expect(TerminalSocketMock.instances).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Reconnect terminal" }));
    const next = TerminalSocketMock.instances[1]!;
    expect(term.disposed).toBe(true);
    expect(ResizeObserverMock.instances[0]!.disconnect).toHaveBeenCalled();
    act(() => TerminalMock.instances[1]!.emitData("discard during reconnect"));
    act(() => next.ready());
    expect(next.inputs()).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Send Enter to Builder" }));
    expect(inputs(next)).toEqual(["\r"]);
    expect(socket.inputs()).toEqual([]);
  });

  it("disposes the old session, renderer and pending ACKs before showing another session", () => {
    const { view, term, socket } = terminal();
    act(() => socket.ready());
    act(() => socket.message(new TextEncoder().encode("old output")));
    view.rerender(<InteractiveTerminal name="Reviewer" sessionId="session-c" />);
    act(() => term.flushWrites());
    expect(socket.closed).toBe(true);
    expect(socket.controls()).toEqual([]);
    expect(term.disposed).toBe(true);
    expect(TerminalSocketMock.instances[1]!.url).toContain("/ws/terminal/session-c");
    expect(screen.getByRole("button", { name: "Send Enter to Reviewer" })).toBeDisabled();
  });

  it("fits every tile independently and sends changed dimensions after layout resize", () => {
    render(<>{["a", "b", "c", "d"].map((id) => <InteractiveTerminal key={id} name={id} sessionId={id} />)}</>);
    act(() => TerminalSocketMock.instances.forEach((socket) => socket.ready()));
    FitAddonMock.instances.forEach((fit, index) => { fit.dimensions = { cols: 40 + index, rows: 12 }; });
    act(() => {
      ResizeObserverMock.instances.forEach((observer) => observer.emit());
      frames.splice(0).forEach((frame) => frame(0));
    });
    TerminalSocketMock.instances.forEach((socket, index) => {
      expect(socket.controls()).toEqual([{ type: "resize", cols: 40 + index, rows: 12 }]);
      expect(TerminalMock.instances[index]!.cols).toBe(40 + index);
    });
  });

  it("ignores hidden containers and clamps the browser renderer to server size limits", () => {
    const { term, socket } = terminal();
    act(() => socket.ready());
    const host = term.element!.parentElement!;
    vi.spyOn(host, "getBoundingClientRect").mockReturnValue({ width: 0, height: 0 } as DOMRect);
    FitAddonMock.instances[0]!.dimensions = { cols: 2, rows: 1 };
    act(() => { ResizeObserverMock.instances[0]!.emit(); frames.splice(0).forEach((frame) => frame(0)); });
    expect(socket.controls()).toEqual([]);
    vi.spyOn(host, "getBoundingClientRect").mockReturnValue({ width: 9000, height: 9000 } as DOMRect);
    FitAddonMock.instances[0]!.dimensions = { cols: 900, rows: 700 };
    act(() => { ResizeObserverMock.instances[0]!.emit(); frames.splice(0).forEach((frame) => frame(0)); });
    expect(socket.controls()).toEqual([{ type: "resize", cols: 500, rows: 300 }]);
    expect([term.cols, term.rows]).toEqual([500, 300]);
  });

  it("focuses without scrolling and lets Ctrl+M release the keyboard without input", () => {
    const { term, socket } = terminal();
    act(() => socket.ready());
    const focus = vi.spyOn(term.textarea!, "focus");
    fireEvent.click(screen.getByRole("button", { name: "Focus Builder terminal" }));
    expect(focus).toHaveBeenCalledWith({ preventScroll: true });
    const key = new KeyboardEvent("keydown", { key: "m", ctrlKey: true, cancelable: true });
    expect(term.keyHandler!(key)).toBe(false);
    expect(key.defaultPrevented).toBe(true);
    expect(screen.getByRole("button", { name: "Focus Builder terminal" })).toHaveFocus();
    expect(socket.inputs()).toEqual([]);
  });

  it("lets keyboard users leave the terminal after its connection drops", () => {
    const { term, socket } = terminal();
    act(() => socket.ready());
    term.focus();
    act(() => socket.serverClose());
    expect(term.keyHandler!(new KeyboardEvent("keydown", { key: "Tab" }))).toBe(false);
    const release = new KeyboardEvent("keydown", { key: "m", ctrlKey: true, cancelable: true });
    expect(term.keyHandler!(release)).toBe(false);
    expect(screen.getByRole("button", { name: "Focus Builder terminal" }).parentElement).toHaveFocus();
    expect(socket.inputs()).toEqual([]);
  });

  it("preserves selected-output copy and keeps typing away from dashboard navigation", () => {
    const navigate = vi.fn();
    render(<div onKeyDown={navigate}><InteractiveTerminal name="Builder" sessionId="session-b" /></div>);
    const term = TerminalMock.instances[0]!;
    term.selection = "selected output";
    expect(term.keyHandler!(new KeyboardEvent("keydown", { key: "c", ctrlKey: true }))).toBe(false);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Builder terminal input" }), { key: "g" });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("disables OSC clipboard and hyperlink actions without changing ANSI colors", () => {
    const { term } = terminal();
    const handlers = term.parser.registerOscHandler.mock.calls as unknown as [number, () => boolean][];
    expect(handlers.map(([code]) => code)).toEqual(expect.arrayContaining([8, 52]));
    expect(handlers.every(([, handler]) => handler())).toBe(true);
    expect(term.options.logLevel).toBe("off");
    expect(term.options.theme?.foreground).not.toBe("#bbf7d0");
  });

  it("leaves only one live viewer after StrictMode re-mount and closes it on unmount", () => {
    const view = render(<StrictMode><InteractiveTerminal name="Builder" sessionId="session-b" /></StrictMode>);
    expect(TerminalSocketMock.instances.filter((socket) => !socket.closed)).toHaveLength(1);
    view.unmount();
    expect(TerminalSocketMock.instances.every((socket) => socket.closed)).toBe(true);
    expect(TerminalMock.instances.every((term) => term.disposed)).toBe(true);
    expect(api.sessionInput).not.toHaveBeenCalled();
  });
});
