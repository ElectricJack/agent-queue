import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import HostShell from "../../pages/host-shell/HostShell";
import InteractiveTerminal from "../InteractiveTerminal";
import { TERMINAL_FONT_FAMILY, TERMINAL_FONT_SIZE, TERMINAL_THEME, terminalOptions } from "../terminalSetup";
import { TerminalMock, FitAddonMock, TerminalSocketMock, ResizeObserverMock } from "../../testUtils/terminal";

vi.mock("@xterm/xterm", async () => ({ Terminal: (await import("../../testUtils/terminal")).TerminalMock }));
vi.mock("@xterm/addon-fit", async () => ({ FitAddon: (await import("../../testUtils/terminal")).FitAddonMock }));
vi.mock("../../api/client", () => ({ terminalAccess: vi.fn(), sessionShow: vi.fn() }));
vi.mock("../../api/hostShell", () => ({
  useHostShells: () => ({
    data: { enabled: true, shells: [{ name: "aq-host-shell-1", created_at: 1_790_000_000, attached_clients: 1 }] },
    isError: false,
    error: null,
  }),
  useOpenHostShell: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useCloseHostShell: () => ({ mutate: vi.fn(), isPending: false, error: null }),
}));

const original = window.matchMedia;
/** The dashboard's own compact breakpoint; a phone's window is under it. */
function setWidth(query: string) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true, writable: true,
    value: (asked: string) => ({
      matches: asked === query, media: asked,
      addEventListener: () => {}, removeEventListener: () => {},
    }),
  });
}

let width = 390;
let height = 600;
let frames: FrameRequestCallback[] = [];
beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(Math, "random").mockReturnValue(1);
  TerminalMock.instances = []; FitAddonMock.instances = []; TerminalSocketMock.instances = []; ResizeObserverMock.instances = [];
  frames = [];
  width = 390;
  height = 600;
  vi.stubGlobal("WebSocket", TerminalSocketMock);
  vi.stubGlobal("ResizeObserver", ResizeObserverMock);
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.push(callback); return frames.length; });
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => ({ width, height, top: 0 }) as DOMRect);
  // A cell is 0.6 em wide, so the columns follow the host width and the font size.
  vi.spyOn(FitAddonMock.prototype, "proposeDimensions").mockImplementation(function (this: FitAddonMock) {
    const cell = (this.terminal!.options.fontSize ?? TERMINAL_FONT_SIZE) * 0.6;
    return { cols: Math.floor(width / cell), rows: Math.floor(height / 14.4) };
  });
});
afterEach(() => {
  cleanup();
  Object.defineProperty(window, "matchMedia", { configurable: true, writable: true, value: original });
  vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.clearAllMocks(); vi.useRealTimers();
});

const newest = () => TerminalMock.instances[TerminalMock.instances.length - 1]!;
const lastSocket = () => TerminalSocketMock.instances[TerminalSocketMock.instances.length - 1]!;
const host = () => screen.getByTitle(/Click to type|Keyboard input/).closest("[data-interactive-terminal]") as HTMLElement;

/** A finger dragged `distance` px down (toward earlier output) and released. */
function drag(element: HTMLElement, distance: number) {
  const touch = (type: string, y: number | null) => {
    const event = new Event(type, { cancelable: true, bubbles: true });
    Object.defineProperty(event, "touches", { value: y === null ? [] : [{ clientX: 10, clientY: y }] });
    element.dispatchEvent(event);
    return event;
  };
  act(() => { touch("touchstart", 100); });
  act(() => { touch("touchmove", 100 + distance); });
  act(() => { touch("touchend", null); });
}

function mount(ui: React.ReactElement) {
  const view = render(<MemoryRouter>{ui}</MemoryRouter>);
  act(() => vi.advanceTimersByTime(0));
  return view;
}

/** Everything about an xterm that decides what it looks like. */
const drawn = (term: TerminalMock) => ({
  fontFamily: term.options.fontFamily,
  fontSize: term.options.fontSize,
  lineHeight: term.options.lineHeight,
  cursorBlink: term.options.cursorBlink,
  scrollback: term.options.scrollback,
  logLevel: term.options.logLevel,
  theme: term.options.theme,
});

describe("an agent terminal is the host shell page's terminal", () => {
  it("is the same component, so its options cannot differ", () => {
    setWidth("(min-width: 768px)");
    mount(<HostShell />);
    const shell = newest().options;
    expect(shell).toEqual(terminalOptions(TERMINAL_FONT_SIZE));
    expect(shell.theme).toBe(TERMINAL_THEME);
    expect(shell.fontFamily).toBe(TERMINAL_FONT_FAMILY);

    // The agent flock's window, a session page and a pool instance all draw it too.
    mount(<InteractiveTerminal sessionId="s1" name="worker-a" />);
    expect(drawn(newest())).toEqual(drawn({ options: shell } as TerminalMock));
  });

  it("attaches at the host's size and scrolls back on a touch drag, at every width", () => {
    for (const query of ["(max-width: 767.98px)", "(min-width: 768px)"]) {
      setWidth(query);
      const view = mount(<InteractiveTerminal sessionId="s1" name="worker-a" />);
      const socket = lastSocket();
      const term = newest();
      act(() => socket.ready());
      expect(Object.fromEntries(new URL(socket.url).searchParams)).toMatchObject({ cols: "54", rows: "41" });
      expect(term.cols).toBe(54);
      term.buffer.active.baseY = 400;
      term.buffer.active.viewportY = 400;
      drag(host(), 72);
      expect(term.scrollLines).toHaveBeenLastCalledWith(-5);
      expect(term.buffer.active.viewportY).toBe(395);
      view.unmount();
    }
  });

  it("asks tmux for scrollback and its window size back only on a phone", () => {
    setWidth("(max-width: 767.98px)");
    mount(<InteractiveTerminal sessionId="s1" name="worker-a" />);
    expect(Object.fromEntries(new URL(lastSocket().url).searchParams)).toMatchObject({ history: "2000", restore_size: "1" });
    cleanup();

    setWidth("(min-width: 768px)");
    mount(<InteractiveTerminal sessionId="s1" name="worker-a" />);
    const query = new URL(lastSocket().url).searchParams;
    expect(query.get("history")).toBeNull();
    expect(query.get("restore_size")).toBeNull();
  });

  it("fits below page headers, follows keyboard pan, and ignores pinch zoom", () => {
    const originalViewport = window.visualViewport;
    const listeners = new Map<string, () => void>();
    const vv = {
      offsetTop: 0, offsetLeft: 0, width: 390, height: 900, scale: 1,
      addEventListener: (type: string, fn: () => void) => listeners.set(type, fn),
      removeEventListener: (type: string) => listeners.delete(type),
    };
    Object.defineProperty(window, "visualViewport", { configurable: true, writable: true, value: vv });
    setWidth("(max-width: 767.98px)");
    const resize = () => act(() => {
      listeners.get("resize")?.();
      frames.splice(0).forEach((frame) => frame(0));
    });
    try {
      const view = mount(<InteractiveTerminal sessionId="s1" name="worker-a" />);
      const frame = host().parentElement!;
      // Unlike the old test, only the visual viewport changes: the layout
      // height stays at 600. Rows must be derived from the available band.
      vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
        const cap = Number.parseFloat(frame.style.maxHeight);
        const available = Number.isFinite(cap) ? Math.min(height, cap) : height;
        return { width, height: this === host() ? Math.max(0, available - 16) : height, top: 150 } as DOMRect;
      });
      vi.spyOn(FitAddonMock.prototype, "proposeDimensions").mockImplementation(() => {
        const bounds = host().getBoundingClientRect();
        return { cols: Math.floor(width / 7.2), rows: Math.floor(bounds.height / 14.4) };
      });
      const socket = lastSocket();
      const term = newest();
      act(() => socket.ready());
      resize();
      const initialRows = term.rows;
      vv.height = 450;
      resize();
      expect(frame.style.maxHeight).toBe("300px");
      expect(term.rows).toBe(19);
      expect(socket.controls().pop()).toMatchObject({ type: "resize", rows: 19 });
      vv.offsetTop = 40;
      act(() => listeners.get("scroll")?.());
      act(() => frames.splice(0).forEach((callback) => callback(0)));
      expect(frame.style.maxHeight).toBe("340px");
      vv.scale = 2;
      resize();
      expect(frame.style.maxHeight).toBe("");
      vv.height = 900;
      vv.offsetTop = 0;
      vv.scale = 1;
      resize();
      expect(term.rows).toBe(initialRows);
      view.unmount();
      expect(listeners.size).toBe(0);
    } finally {
      Object.defineProperty(window, "visualViewport", { configurable: true, writable: true, value: originalViewport });
    }
  });

  it("caps the terminal when mounted with the keyboard already open", () => {
    const originalViewport = window.visualViewport;
    Object.defineProperty(window, "visualViewport", {
      configurable: true, writable: true,
      value: { height: 240, offsetTop: 20, scale: 1, addEventListener: vi.fn(), removeEventListener: vi.fn() },
    });
    try {
      mount(<InteractiveTerminal sessionId="s1" name="worker-a" />);
      expect(host().parentElement!.style.maxHeight).toBe("260px");
    } finally {
      Object.defineProperty(window, "visualViewport", { configurable: true, writable: true, value: originalViewport });
    }
  });

  it("has no watch/type mode: the output and the typing surface are there from the start", () => {
    setWidth("(max-width: 767.98px)");
    mount(<InteractiveTerminal sessionId="s1" name="worker-a" />);
    expect(screen.getByRole("button", { name: "Focus worker-a terminal" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /watch only/i })).toBeNull();
    // A tap types; it never drags the page. xterm's own textarea takes it.
    const touch = (type: string, y: number | null) => {
      const event = new Event(type, { cancelable: true, bubbles: true });
      Object.defineProperty(event, "touches", { value: y === null ? [] : [{ clientX: 10, clientY: y }] });
      host().dispatchEvent(event);
    };
    act(() => { touch("touchstart", 200); touch("touchend", null); });
    expect(newest().focus).toHaveBeenCalled();
  });
});
