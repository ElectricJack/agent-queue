import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import WatchTerminal, { FONT_MAX, FONT_MIN } from "../WatchTerminal";

const terminal = vi.hoisted(() => ({ connect: vi.fn() }));
const api = vi.hoisted(() => ({ sessionShow: vi.fn() }));
vi.mock("../../ws/terminalSocket", () => ({ connectTerminal: terminal.connect, terminalDimensions: vi.fn() }));
vi.mock("../../api/client", () => api);
vi.mock("../InteractiveTerminal", () => ({ default: () => { throw new Error("InteractiveTerminal must never mount in watch mode"); } }));

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
const latest = () => MockEventSource.all[MockEventSource.all.length - 1]!;
const frame = (screenText: string) => act(() => latest().onmessage?.({ data: JSON.stringify({ type: "screen", screen: screenText, seq: 1 }) }));

const tick = (ms = 0) => act(() => vi.advanceTimersByTime(ms));
const status = () => screen.getByRole("status", { name: "worker-a terminal status" });

beforeEach(() => {
  vi.useFakeTimers();
  MockEventSource.all = [];
  terminal.connect.mockClear();
  api.sessionShow.mockReset().mockResolvedValue({ data: { session: { state: "running" } } });
  vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function renderWatch(focusHref?: string) {
  const view = render(<MemoryRouter><WatchTerminal sessionId="s1" name="worker-a" focusHref={focusHref} /></MemoryRouter>);
  tick(); // the stream opens on the next tick
  return view;
}

describe("WatchTerminal", () => {
  it("font size changes rendering only: 12–20 px, no new stream, no terminal socket", () => {
    renderWatch();
    fireEvent.click(screen.getByRole("button", { name: "Details for worker-a" }));
    frame("hello");
    const consoleBox = () => screen.getByText("hello").closest("[data-allow-overflow-x]");
    expect(consoleBox()).toHaveStyle({ fontSize: `${FONT_MIN}px` });
    const larger = screen.getByRole("button", { name: "Larger text" });
    for (let i = 0; i < 10; i++) fireEvent.click(larger);
    expect(screen.getByLabelText("Text size")).toHaveTextContent(`${FONT_MAX}px`);
    expect(consoleBox()).toHaveStyle({ fontSize: `${FONT_MAX}px` });
    expect(larger).toBeDisabled();
    const smaller = screen.getByRole("button", { name: "Smaller text" });
    for (let i = 0; i < 10; i++) fireEvent.click(smaller);
    expect(screen.getByLabelText("Text size")).toHaveTextContent(`${FONT_MIN}px`);
    expect(smaller).toBeDisabled();
    expect(MockEventSource.all).toHaveLength(1);
    expect(terminal.connect).not.toHaveBeenCalled();
  });

  it("keeps the last screen visibly stale after a drop, and Retry reconnects at once", () => {
    renderWatch();
    fireEvent.click(screen.getByRole("button", { name: "Details for worker-a" }));
    frame("last good screen");
    expect(status()).toHaveTextContent(/^Live$/);
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    act(() => latest().onerror?.());
    expect(screen.getByText("last good screen")).toBeInTheDocument();
    expect(screen.getByText("last good screen").closest("[data-allow-overflow-x]")).toHaveClass("opacity-60");
    expect(status()).toHaveTextContent(/^Reconnecting · screen from /);
    // The console's own "Reconnect now" line would duplicate the toolbar.
    expect(screen.queryByRole("button", { name: "Reconnect now" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    tick();
    expect(MockEventSource.all).toHaveLength(2);
    expect(screen.getByText("last good screen")).toBeInTheDocument();
    frame("fresh screen");
    expect(status()).toHaveTextContent(/^Live$/);
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("retries a drop on its own, keeping the screen until frames flow again", () => {
    renderWatch();
    frame("kept");
    act(() => latest().onerror?.());
    tick(1_000);
    expect(MockEventSource.all).toHaveLength(2);
    act(() => latest().onopen?.());
    expect(status()).toHaveTextContent(/^Reconnecting/); // connected, nothing new on screen yet
    frame("new");
    expect(status()).toHaveTextContent(/^Live$/);
  });

  it("an error frame keeps the screen and offers Retry, which opens a fresh stream", () => {
    renderWatch();
    fireEvent.click(screen.getByRole("button", { name: "Details for worker-a" }));
    frame("kept");
    act(() => latest().onmessage?.({ data: JSON.stringify({ type: "error", message: "tmux is gone", seq: 2 }) }));
    expect(status()).toHaveTextContent(/^Stream error/);
    expect(screen.getByText("tmux is gone")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    tick();
    expect(MockEventSource.all).toHaveLength(2);
    expect(screen.getByText("kept")).toBeInTheDocument();
  });

  it("full screen is a focus-trapped sheet that Escape closes, returning focus", () => {
    renderWatch();
    const open = screen.getByRole("button", { name: "Full screen" });
    open.focus();
    fireEvent.click(open);
    const dialog = screen.getByRole("dialog", { name: "worker-a terminal, full screen" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(screen.getByRole("button", { name: "Exit full screen" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("button", { name: "Full screen" })).toHaveFocus();
    expect(MockEventSource.all).toHaveLength(1);
  });

  it("Exit full screen closes the sheet", () => {
    renderWatch();
    fireEvent.click(screen.getByRole("button", { name: "Full screen" }));
    fireEvent.click(screen.getByRole("button", { name: "Exit full screen" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("with focusHref, Full screen is a link to the focus session", () => {
    renderWatch("/focus/sessions/s1");
    expect(screen.getByRole("link", { name: "Full screen" })).toHaveAttribute("href", "/focus/sessions/s1");
  });});

type Options = { sessionId: string; mode?: string; onState: (state: { status: string; attempt?: number; message?: string }) => void };

describe("WatchTerminal typing (mobile interactive terminal)", () => {
  const decode = (bytes: Uint8Array) => new TextDecoder().decode(bytes);
  let conn: { sendInput: ReturnType<typeof vi.fn>; resize: ReturnType<typeof vi.fn>; setVisible: ReturnType<typeof vi.fn>; reconnect: ReturnType<typeof vi.fn>; close: ReturnType<typeof vi.fn> };
  let options: Options | undefined;
  const sent = () => conn.sendInput.mock.calls.map(([bytes]) => decode(bytes as Uint8Array));
  const setState = (state: Parameters<Options["onState"]>[0]) => act(() => options!.onState(state));
  const input = () => screen.getByRole("textbox", { name: "worker-a terminal input" });

  beforeEach(() => {
    options = undefined;
    conn = { sendInput: vi.fn(), resize: vi.fn(), setVisible: vi.fn(), reconnect: vi.fn(), close: vi.fn() };
    terminal.connect.mockImplementation((opts: Options) => { options = opts; return conn; });
  });

  function startTyping() {
    renderWatch();
    frame("❯ waiting for you");
    fireEvent.click(screen.getByRole("button", { name: "Type" }));
    expect(terminal.connect).toHaveBeenCalledOnce();
    setState({ status: "connected" });
  }

  it("opens watch only: no input bar, no keys, no socket, and a tap types nothing", () => {
    renderWatch();
    frame("screen");
    expect(screen.queryByRole("button", { name: "Watch only" })).toBeNull();
    expect(screen.getByRole("button", { name: "Type" })).toHaveAttribute("aria-description", "Watch only. Enable typing.");
    fireEvent.click(screen.getByText("screen"));
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("toolbar")).toBeNull();
    expect(terminal.connect).not.toHaveBeenCalled();
  });

  it("Type opens only the input-only socket — no dimensions, never an attach — and Watch only closes it", () => {
    renderWatch();
    fireEvent.click(screen.getByRole("button", { name: "Type" }));
    expect(terminal.connect).toHaveBeenCalledOnce();
    expect(options).toMatchObject({ sessionId: "s1", mode: "input" });
    expect(options).not.toHaveProperty("cols");
    expect(screen.getByRole("button", { name: "Watch only" })).toHaveAttribute("aria-description", "Typing is active. Switch to watch only.");
    // Until the socket is ready nothing can be sent.
    expect(screen.getByRole("status", { name: "worker-a keyboard status" })).toHaveTextContent("Connecting the keyboard…");
    expect(screen.getByRole("button", { name: "Send Escape" })).toBeDisabled();
    setState({ status: "connected" });
    expect(screen.getByRole("status", { name: "worker-a keyboard status" })).toBeEmptyDOMElement();
    expect(screen.getByRole("button", { name: "Send Escape" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Watch only" }));
    expect(conn.close).toHaveBeenCalledOnce();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(MockEventSource.all).toHaveLength(1); // the pane stream is untouched
    expect(conn.resize).not.toHaveBeenCalled();
  });

  it("a line goes as typed, then Enter as its own write; the key strip sends its bytes in order", () => {
    startTyping();
    fireEvent.change(input(), { target: { value: "please continue" } });
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(sent()).toEqual(["please continue"]);
    // A key tapped before Enter went out waits behind it.
    fireEvent.click(screen.getByRole("button", { name: "Send 2" }));
    expect(sent()).toEqual(["please continue"]);
    tick(150);
    expect(sent()).toEqual(["please continue", "\r", "2"]);
    for (const name of ["Send Escape", "Send Tab", "Send Ctrl-C", "Send Up arrow", "Send Down arrow", "Send Enter", "Send 1", "Send 3"]) {
      fireEvent.click(screen.getByRole("button", { name }));
    }
    expect(sent().slice(3)).toEqual(["\x1b", "\t", "\x03", "\x1b[A", "\x1b[B", "\r", "1", "3"]);
  });

  it("a pasted multi-line entry is one bracketed paste followed by Enter", () => {
    startTyping();
    fireEvent.change(input(), { target: { value: "line one\r\nline two" } });
    fireEvent.click(screen.getByRole("button", { name: "Send to worker-a" }));
    tick(150);
    expect(sent()).toEqual(["\x1b[200~line one\nline two\x1b[201~", "\r"]);
  });

  it("a drop discards the pending Enter instead of replaying it, and offers Reconnect now", () => {
    startTyping();
    fireEvent.change(input(), { target: { value: "half" } });
    fireEvent.keyDown(input(), { key: "Enter" });
    setState({ status: "reconnecting", attempt: 2, message: "Terminal disconnected. Unsent input was discarded." });
    tick(500);
    expect(sent()).toEqual(["half"]);
    expect(screen.getByRole("status", { name: "worker-a keyboard status" })).toHaveTextContent("Keyboard reconnecting… (attempt 2)");
    expect(screen.getByRole("button", { name: "Send to worker-a" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Reconnect now" }));
    expect(conn.reconnect).toHaveBeenCalledOnce();
    setState({ status: "error", message: "Terminal access refused. Check credentials, origin and loopback access." });
    expect(screen.getByRole("status", { name: "worker-a keyboard status" })).toHaveTextContent(/access refused/);
  });

  it("a tap on the screen focuses the input bar, not a terminal, and not over a selection", () => {
    startTyping();
    fireEvent.click(screen.getByText("❯ waiting for you"));
    expect(input()).toHaveFocus();
    input().blur();
    const selection = vi.spyOn(window, "getSelection").mockReturnValue({ toString: () => "copied" } as Selection);
    fireEvent.click(screen.getByText("❯ waiting for you"));
    expect(input()).not.toHaveFocus();
    selection.mockRestore();
  });

  it("stays scrolled to the prompt while typing, until the viewer scrolls up", () => {
    startTyping();
    const box = screen.getByText("❯ waiting for you").closest("[data-allow-overflow-x]") as HTMLDivElement;
    Object.defineProperty(box, "scrollHeight", { configurable: true, value: 900 });
    Object.defineProperty(box, "clientHeight", { configurable: true, value: 300 });
    frame("next screen");
    expect(box.scrollTop).toBe(900);
    box.scrollTop = 100;
    fireEvent.scroll(box);
    frame("another screen");
    expect(box.scrollTop).toBe(100);
    box.scrollTop = 600;
    fireEvent.scroll(box);
    frame("back at the bottom");
    expect(box.scrollTop).toBe(900);
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
      startTyping();
      const root = () => screen.getByRole("button", { name: "Watch only" }).closest("[tabindex='-1']") as HTMLElement;
      expect(root()).not.toHaveAttribute("data-keyboard-open");
      fireEvent.focus(input());
      tick(20);
      expect(root()).toHaveAttribute("data-keyboard-open");
      expect(root()).toHaveClass("fixed");
      expect(root()).toHaveStyle({ top: "120px", left: "0px", width: "390px", height: "420px" });
      // The keyboard closes: back in the page.
      vv.height = 844; vv.offsetTop = 0;
      act(() => listeners.get("resize")?.());
      tick(20);
      expect(root()).not.toHaveAttribute("data-keyboard-open");
      fireEvent.blur(input());
      expect(listeners.size).toBe(0);
    } finally {
      Object.defineProperty(window, "visualViewport", { configurable: true, writable: true, value: undefined });
      Object.defineProperty(window, "innerHeight", { configurable: true, writable: true, value: 768 });
    }
  });
});
