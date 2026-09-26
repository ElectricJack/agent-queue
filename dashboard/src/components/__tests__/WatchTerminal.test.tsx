import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import WatchTerminal, { FONT_MAX, FONT_MIN } from "../WatchTerminal";

const terminal = vi.hoisted(() => ({ connect: vi.fn() }));
vi.mock("../../ws/terminalSocket", () => ({ connectTerminal: terminal.connect, terminalDimensions: vi.fn() }));
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

beforeEach(() => {
  MockEventSource.all = [];
  terminal.connect.mockClear();
  vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
});
afterEach(() => vi.unstubAllGlobals());

const renderWatch = (focusHref?: string) =>
  render(<MemoryRouter><WatchTerminal sessionId="s1" name="worker-a" focusHref={focusHref} /></MemoryRouter>);

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

  it("keeps the last screen visibly stale and offers Retry after a drop", () => {
    renderWatch();
    frame("last good screen");
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    act(() => { latest().readyState = 2; latest().onerror?.(); });
    expect(screen.getByText("last good screen")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "worker-a terminal status" })).toHaveTextContent(/^Disconnected/);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(MockEventSource.all).toHaveLength(2);
    expect(screen.getByText("last good screen")).toBeInTheDocument();
    frame("fresh screen");
    expect(screen.getByRole("status", { name: "worker-a terminal status" })).toHaveTextContent(/^Live/);
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("says it is reconnecting while the browser retries on its own", () => {
    renderWatch();
    frame("kept");
    act(() => { latest().readyState = 0; latest().onerror?.(); });
    expect(screen.getByRole("status", { name: "worker-a terminal status" })).toHaveTextContent(/^Reconnecting/);
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
