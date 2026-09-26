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
  });
});
