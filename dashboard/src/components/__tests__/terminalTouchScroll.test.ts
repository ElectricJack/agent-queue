import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { attachTouchScroll } from "../terminalTouchScroll";

let element: HTMLDivElement;
let frames: FrameRequestCallback[];
let now: number;
const scroll = vi.fn<(lines: number) => void>();
const onTap = vi.fn();
let detach: () => void;

/** jsdom has no Touch constructor: a plain event carries the touch list and time. */
function touch(type: string, y: number | null, at: number, x = 0) {
  const event = new Event(type, { cancelable: true, bubbles: true });
  Object.defineProperty(event, "touches", { value: y === null ? [] : [{ clientX: x, clientY: y }] });
  Object.defineProperty(event, "timeStamp", { value: at });
  element.dispatchEvent(event);
  return event;
}
function runFrame(advance: number) {
  now += advance;
  frames.splice(0).forEach((callback) => callback(now));
}
const lines = () => scroll.mock.calls.map(([n]) => n);

beforeEach(() => {
  element = document.createElement("div");
  document.body.append(element);
  frames = [];
  now = 1_000;
  scroll.mockClear();
  onTap.mockClear();
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.push(callback); return frames.length; });
  vi.stubGlobal("cancelAnimationFrame", () => { frames = []; });
  vi.spyOn(performance, "now").mockImplementation(() => now);
  detach = attachTouchScroll(element, { lineHeight: () => 10, scroll, onTap });
});
afterEach(() => {
  detach();
  element.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("terminal touch scrolling", () => {
  it("a drag scrolls whole lines with the finger and keeps the gesture from the page", () => {
    touch("touchstart", 100, 0);
    // Under the tap slop nothing moves and the page is not told no yet.
    expect(touch("touchmove", 96, 10).defaultPrevented).toBe(false);
    expect(lines()).toEqual([]);
    // Finger up 25 px: two lines toward newer output, 5 px carried over.
    expect(touch("touchmove", 75, 20).defaultPrevented).toBe(true);
    expect(lines()).toEqual([2]);
    touch("touchmove", 70, 30);
    expect(lines()).toEqual([2, 1]);
    // Finger down 30 px: three lines toward earlier output.
    touch("touchmove", 100, 40);
    expect(lines()).toEqual([2, 1, -3]);
    // Released after the finger stopped: no momentum, and no tap.
    touch("touchend", null, 400);
    expect(frames).toEqual([]);
    expect(onTap).not.toHaveBeenCalled();
  });

  it("a fling glides on with decaying momentum until it stops, and a new touch stops it at once", () => {
    touch("touchstart", 300, 0);
    touch("touchmove", 250, 20);
    touch("touchmove", 150, 40);
    touch("touchmove", 100, 50); // 200 px over 50 ms: 4 px/ms toward newer output
    scroll.mockClear();
    touch("touchend", null, 55);
    expect(frames).toHaveLength(1);
    runFrame(16);
    const first = lines().reduce((sum, n) => sum + n, 0);
    expect(first).toBeGreaterThan(0);
    for (let i = 0; i < 400 && frames.length; i++) runFrame(16);
    expect(frames).toEqual([]); // the glide ran out
    const total = lines().reduce((sum, n) => sum + n, 0);
    // Total distance ≈ v·τ = 4 px/ms × 325 ms ≈ 1300 px ≈ 130 lines.
    expect(total).toBeGreaterThan(100);
    expect(total).toBeLessThan(140);

    touch("touchstart", 300, 0);
    touch("touchmove", 200, 25);
    touch("touchend", null, 30);
    expect(frames).toHaveLength(1);
    touch("touchstart", 300, 100);
    expect(frames).toEqual([]);
  });

  it("a tap focuses through onTap inside touchend and cancels the synthesized mouse events", () => {
    touch("touchstart", 100, 0, 50);
    touch("touchmove", 104, 50, 53); // a wobble under the slop is still a tap
    const end = touch("touchend", null, 120);
    expect(onTap).toHaveBeenCalledOnce();
    expect(end.defaultPrevented).toBe(true);
    expect(lines()).toEqual([]);
  });

  it("a long press, a second finger or a cancelled touch is neither a tap nor a scroll", () => {
    touch("touchstart", 100, 0);
    expect(touch("touchend", null, 700).defaultPrevented).toBe(false);
    const pinch = new Event("touchstart", { cancelable: true });
    Object.defineProperty(pinch, "touches", { value: [{ clientX: 0, clientY: 0 }, { clientX: 9, clientY: 9 }] });
    element.dispatchEvent(pinch);
    touch("touchmove", 10, 10);
    touch("touchend", null, 20);
    touch("touchstart", 100, 30);
    touch("touchcancel", null, 40);
    touch("touchend", null, 50);
    expect(onTap).not.toHaveBeenCalled();
    expect(lines()).toEqual([]);
  });

  it("detaching removes every listener and stops a glide", () => {
    touch("touchstart", 300, 0);
    touch("touchmove", 200, 25);
    touch("touchend", null, 30);
    detach();
    expect(frames).toEqual([]);
    touch("touchstart", 100, 100);
    touch("touchend", null, 120);
    expect(onTap).not.toHaveBeenCalled();
    detach = () => {};
  });
});
