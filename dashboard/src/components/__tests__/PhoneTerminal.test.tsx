import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import PhoneTerminal, { FONT_MAX, FONT_MIN } from "../PhoneTerminal";
import { MIN_COLUMNS, TERMINAL_FONT_FAMILY, TERMINAL_THEME } from "../terminalSetup";
import { TerminalMock, FitAddonMock, TerminalSocketMock, ResizeObserverMock } from "../../testUtils/terminal";

vi.mock("@xterm/xterm", async () => ({ Terminal: (await import("../../testUtils/terminal")).TerminalMock }));
vi.mock("@xterm/addon-fit", async () => ({ FitAddon: (await import("../../testUtils/terminal")).FitAddonMock }));
const api = vi.hoisted(() => ({ sessionShow: vi.fn(), terminalAccess: vi.fn() }));
vi.mock("../../api/client", () => api);

class MockEventSource {
  static all: MockEventSource[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 1;
  closed = false;
  constructor(public url: string) { MockEventSource.all.push(this); }
  close() { this.closed = true; this.readyState = 2; }
}

/** A cell is 0.6 em wide, so the columns follow the host width and the font size. */
let width = 390;
let frames: FrameRequestCallback[] = [];
const tick = (ms = 0) => act(() => vi.advanceTimersByTime(ms));
const runFrames = () => act(() => frames.splice(0).forEach((callback) => callback(performance.now())));
const status = () => screen.getByRole("status", { name: "worker-a terminal status" });
const input = () => screen.getByRole("textbox", { name: "worker-a terminal input" });
const output = () => screen.getByRole("region", { name: "worker-a terminal output" });
const decode = (bytes: Uint8Array) => new TextDecoder().decode(bytes);

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(Math, "random").mockReturnValue(1);
  TerminalMock.instances = []; FitAddonMock.instances = []; TerminalSocketMock.instances = []; ResizeObserverMock.instances = [];
  MockEventSource.all = [];
  frames = [];
  width = 390;
  api.sessionShow.mockReset().mockResolvedValue({ data: { session: { state: "running" } } });
  api.terminalAccess.mockReset();
  vi.stubGlobal("WebSocket", TerminalSocketMock);
  vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
  vi.stubGlobal("ResizeObserver", ResizeObserverMock);
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.push(callback); return frames.length; });
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => ({ width, height: 600 }) as DOMRect);
  vi.spyOn(FitAddonMock.prototype, "proposeDimensions").mockImplementation(function (this: FitAddonMock) {
    return { cols: Math.floor(width / ((this.terminal!.options.fontSize ?? FONT_MIN) * 0.6)), rows: 30 };
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.clearAllMocks(); vi.useRealTimers(); });

function renderPhone(focusHref?: string) {
  const view = render(<MemoryRouter><PhoneTerminal sessionId="s1" name="worker-a" focusHref={focusHref} /></MemoryRouter>);
  tick();
  return { view, term: TerminalMock.instances[0]!, socket: () => TerminalSocketMock.instances[TerminalSocketMock.instances.length - 1]! };
}
function connected() {
  const phone = renderPhone();
  act(() => phone.socket().ready());
  return phone;
}
const details = () => fireEvent.click(screen.getByRole("button", { name: "Details for worker-a" }));
const sent = (socket: TerminalSocketMock) => socket.inputs().map(decode);
const resizes = (socket: TerminalSocketMock) => socket.controls().filter((control) => control.type === "resize");

describe("PhoneTerminal", () => {
  it("attaches like the desktop terminal, with the desktop theme, scrollback and the size restored on leave", () => {
    const { view, term, socket } = renderPhone();
    expect(term.options).toMatchObject({ theme: TERMINAL_THEME, fontFamily: TERMINAL_FONT_FAMILY, fontSize: FONT_MIN });
    const url = new URL(socket().url);
    expect(url.pathname).toBe("/ws/terminal/s1");
    // 390 px at 12 px is 54 columns; tmux is attached at the phone's own size.
    expect(Object.fromEntries(url.searchParams)).toMatchObject({ cols: "54", rows: "30", history: "2000", restore_size: "1" });
    // No watch/type mode: the input bar is there from the start, live once the attach is ready.
    expect(screen.queryByRole("button", { name: "Type" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Watch only" })).toBeNull();
    expect(input()).toHaveAttribute("placeholder", "Connecting…");
    expect(screen.getByRole("button", { name: "Send Escape" })).toBeDisabled();
    expect(status()).toHaveTextContent(/^Connecting…$/);
    act(() => socket().ready());
    expect(input()).toHaveAttribute("placeholder", "Type to the agent…");
    expect(screen.getByRole("button", { name: "Send Escape" })).toBeEnabled();
    expect(status()).toHaveTextContent(/^Live$/);
    expect(MockEventSource.all).toHaveLength(0);
    view.unmount();
    expect(socket().closed).toBe(true);
    expect(term.disposed).toBe(true);
  });

  it("follows the phone's size: rotation resizes tmux, and the font gives way to keep 40 columns", () => {
    const { term, socket } = connected();
    details();
    const larger = screen.getByRole("button", { name: "Larger text" });
    fireEvent.click(larger); // 14 px: 46 columns
    runFrames();
    fireEvent.click(larger); // 16 px: 40 columns
    runFrames();
    expect(term.cols).toBe(MIN_COLUMNS);
    fireEvent.click(larger); // 18 px would be 36 columns: held at 16 px
    runFrames();
    expect(term.options.fontSize).toBe(16);
    expect(term.cols).toBe(MIN_COLUMNS);
    expect(screen.getByLabelText("Text size")).toHaveTextContent("16px");
    expect(screen.getByText(`Text is 16px here to keep ${MIN_COLUMNS} columns.`)).toBeInTheDocument();
    expect(larger).toBeDisabled();
    // Landscape: room for the chosen 18 px again, and tmux follows the new width.
    width = 844;
    act(() => ResizeObserverMock.instances[0]!.emit());
    runFrames();
    expect(term.options.fontSize).toBe(18);
    expect(screen.queryByText(/here to keep/)).toBeNull();
    expect(resizes(socket()).slice(-1)[0]).toEqual({ type: "resize", cols: 78, rows: 30 });
    expect(resizes(socket()).map((control) => control.cols)).toEqual([46, 40, 78]);
  });

  it("a second tap on Larger before the refit still counts", () => {
    width = 1200;
    const { term } = connected();
    details();
    const larger = screen.getByRole("button", { name: "Larger text" });
    fireEvent.click(larger);
    expect(larger).toBeEnabled();
    fireEvent.click(larger);
    runFrames();
    expect(term.options.fontSize).toBe(16);
    expect(screen.getByLabelText("Text size")).toHaveTextContent("16px");
    expect(screen.queryByText(/here to keep/)).toBeNull();
  });

  it("text size steps between 12 and 20 px on a wide screen without reconnecting", () => {
    width = 1200;
    const { term } = connected();
    details();
    const larger = screen.getByRole("button", { name: "Larger text" });
    for (let i = 0; i < 10; i++) { fireEvent.click(larger); runFrames(); }
    expect(term.options.fontSize).toBe(FONT_MAX);
    expect(screen.getByLabelText("Text size")).toHaveTextContent(`${FONT_MAX}px`);
    expect(larger).toBeDisabled();
    const smaller = screen.getByRole("button", { name: "Smaller text" });
    for (let i = 0; i < 10; i++) { fireEvent.click(smaller); runFrames(); }
    expect(term.options.fontSize).toBe(FONT_MIN);
    expect(smaller).toBeDisabled();
    expect(TerminalSocketMock.instances).toHaveLength(1);
    expect(TerminalMock.instances).toHaveLength(1);
  });

  it("a line goes as typed, then Enter as its own write; the key strip sends its bytes in order", () => {
    const { term, socket } = connected();
    fireEvent.change(input(), { target: { value: "please continue" } });
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(sent(socket())).toEqual(["please continue"]);
    expect(term.scrollToBottom).toHaveBeenCalled();
    // A key tapped before Enter went out waits behind it.
    fireEvent.click(screen.getByRole("button", { name: "Send 2" }));
    expect(sent(socket())).toEqual(["please continue"]);
    tick(150);
    expect(sent(socket())).toEqual(["please continue", "\r", "2"]);
    for (const name of ["Send Escape", "Send Tab", "Send Ctrl-C", "Send Up arrow", "Send Down arrow", "Send Enter", "Send 1", "Send 3"]) {
      fireEvent.click(screen.getByRole("button", { name }));
    }
    expect(sent(socket()).slice(3)).toEqual(["\x1b", "\t", "\x03", "\x1b[A", "\x1b[B", "\r", "1", "3"]);
  });

  it("a pasted multi-line entry is one bracketed paste with Enter-key line breaks, then Enter", () => {
    const { socket } = connected();
    fireEvent.change(input(), { target: { value: "line one\r\nline two" } });
    fireEvent.click(screen.getByRole("button", { name: "Send to worker-a" }));
    tick(150);
    expect(sent(socket())).toEqual(["\x1b[200~line one\rline two\x1b[201~", "\r"]);
  });

  it("answers tmux's terminal queries over the attach, as the desktop does", () => {
    const { term, socket } = connected();
    expect(term.options.disableStdin).toBe(false);
    act(() => term.emitData("\x1b[?1;2c"));
    expect(sent(socket())).toEqual(["\x1b[?1;2c"]);
  });

  it("a drop discards the pending Enter instead of replaying it, and Reconnect now attaches again", () => {
    const { term, socket } = connected();
    fireEvent.change(input(), { target: { value: "half" } });
    fireEvent.keyDown(input(), { key: "Enter" });
    const first = socket();
    act(() => first.serverClose(1001));
    tick(500);
    expect(sent(first)).toEqual(["half"]);
    expect(term.options.disableStdin).toBe(true);
    expect(status()).toHaveTextContent(/^Reconnecting… \(attempt 1\)$/);
    expect(screen.getByRole("button", { name: "Send to worker-a" })).toBeDisabled();
    details();
    fireEvent.click(screen.getByRole("button", { name: "Reconnect now" }));
    tick();
    expect(socket()).not.toBe(first);
    expect(new URL(socket().url).searchParams.get("history")).toBe("2000");
    act(() => socket().ready());
    expect(status()).toHaveTextContent(/^Live$/);
    expect(sent(socket())).toEqual([]);
  });

  it("a tap or a click on the output opens the input bar, but not over a selection", () => {
    const { term } = connected();
    const touch = (type: string, y: number | null) => {
      const event = new Event(type, { cancelable: true, bubbles: true });
      Object.defineProperty(event, "touches", { value: y === null ? [] : [{ clientX: 10, clientY: y }] });
      output().dispatchEvent(event);
    };
    act(() => { touch("touchstart", 200); touch("touchend", null); });
    expect(input()).toHaveFocus();
    input().blur();
    fireEvent.click(output());
    expect(input()).toHaveFocus();
    input().blur();
    term.selection = "copied";
    fireEvent.click(output());
    expect(input()).not.toHaveFocus();
    expect(term.focus).not.toHaveBeenCalled();
  });

  it("a drag scrolls back through history and Jump to latest returns to the live screen", () => {
    const { term } = connected();
    term.buffer.active.baseY = 500;
    term.buffer.active.viewportY = 500;
    const touch = (type: string, y: number | null) => {
      const event = new Event(type, { cancelable: true, bubbles: true });
      Object.defineProperty(event, "touches", { value: y === null ? [] : [{ clientX: 10, clientY: y }] });
      output().dispatchEvent(event);
      return event;
    };
    expect(screen.queryByRole("button", { name: "Jump to latest output" })).toBeNull();
    act(() => { touch("touchstart", 100); });
    // Finger down 72 px at 14.4 px rows: five lines toward earlier output.
    let move: Event | undefined;
    act(() => { move = touch("touchmove", 172); });
    expect(move!.defaultPrevented).toBe(true);
    expect(term.scrollLines).toHaveBeenLastCalledWith(-5);
    expect(term.buffer.active.viewportY).toBe(495);
    act(() => { touch("touchend", null); });
    expect(input()).not.toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Jump to latest output" }));
    expect(term.buffer.active.viewportY).toBe(500);
    expect(screen.queryByRole("button", { name: "Jump to latest output" })).toBeNull();
  });

  it("a session that has ended keeps its last screen, watch only", () => {
    const { term, socket } = connected();
    act(() => socket().message(new TextEncoder().encode("final output")));
    act(() => socket().message(JSON.stringify({ type: "exit" })));
    expect(status()).toHaveTextContent(/^Session ended — last screen$/);
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("toolbar", { name: "worker-a terminal keys" })).toBeNull();
    expect(term.output!.textContent).toBe("final output");
    expect(term.disposed).toBe(false);
    expect(MockEventSource.all).toHaveLength(0);
    fireEvent.click(output());
    expect(document.body).toHaveFocus();
  });

  it("a viewer the daemon will not attach watches the pane stream in the same terminal, with no input", () => {
    const { term, socket } = renderPhone();
    act(() => socket().serverClose(4403));
    tick();
    expect(MockEventSource.all).toHaveLength(1);
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("toolbar", { name: "worker-a terminal keys" })).toBeNull();
    expect(status()).toHaveTextContent(/^Watch only — connecting…$/);
    act(() => MockEventSource.all[0]!.onmessage?.({ data: JSON.stringify({ type: "screen", screen: "line one\nline two", seq: 1 }) }));
    expect(status()).toHaveTextContent(/^Watch only — typing is not available to this viewer$/);
    expect(term.write).toHaveBeenLastCalledWith("\x1b[0m\x1b[?25l\x1b[H\x1b[2J\x1b[3Jline one\r\nline two");
    expect(TerminalMock.instances).toHaveLength(1);
    expect(TerminalSocketMock.instances).toHaveLength(1);
    // A dropped stream keeps the last screen and says how old it is.
    act(() => MockEventSource.all[0]!.onerror?.());
    expect(status()).toHaveTextContent(/^Watch only — reconnecting · screen from /);
  });

  it("full screen is a focus-trapped sheet that Escape closes, returning focus, on the same attach", () => {
    const { socket } = connected();
    const open = screen.getByRole("button", { name: "Full screen" });
    open.focus();
    fireEvent.click(open);
    const dialog = screen.getByRole("dialog", { name: "worker-a terminal, full screen" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog.contains(document.activeElement)).toBe(true);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("button", { name: "Full screen" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Full screen" }));
    fireEvent.click(screen.getByRole("button", { name: "Exit full screen" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(TerminalSocketMock.instances).toEqual([socket()]);
  });

  it("with focusHref, Full screen is a link to the focus session", () => {
    renderPhone("/focus/sessions/s1");
    expect(screen.getByRole("link", { name: "Full screen" })).toHaveAttribute("href", "/focus/sessions/s1");
  });

  it("with the keyboard up, fills exactly the visual viewport above it", () => {
    const listeners = new Map<string, () => void>();
    const vv = {
      offsetTop: 120, offsetLeft: 0, width: 390, height: 420, scale: 1,
      addEventListener: (type: string, fn: () => void) => listeners.set(type, fn),
      removeEventListener: (type: string) => listeners.delete(type),
    };
    Object.defineProperty(window, "visualViewport", { configurable: true, writable: true, value: vv });
    Object.defineProperty(window, "innerHeight", { configurable: true, writable: true, value: 844 });
    try {
      connected();
      const root = () => screen.getByRole("button", { name: "Full screen" }).closest("[tabindex='-1']") as HTMLElement;
      expect(root()).not.toHaveAttribute("data-keyboard-open");
      fireEvent.focus(input());
      tick(20);
      runFrames();
      expect(root()).toHaveAttribute("data-keyboard-open");
      expect(root()).toHaveClass("fixed");
      expect(root()).toHaveStyle({ top: "120px", left: "0px", width: "390px", height: "420px" });
      // The keyboard closes: back in the page.
      vv.height = 844; vv.offsetTop = 0;
      act(() => listeners.get("resize")?.());
      tick(20);
      runFrames();
      expect(root()).not.toHaveAttribute("data-keyboard-open");
      fireEvent.blur(input());
      expect(listeners.size).toBe(0);
    } finally {
      Object.defineProperty(window, "visualViewport", { configurable: true, writable: true, value: undefined });
      Object.defineProperty(window, "innerHeight", { configurable: true, writable: true, value: 768 });
    }
  });
});
