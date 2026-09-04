import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  GRAPH_POSITIONS_CHANGED,
  GRAPH_POSITIONS_STORAGE_KEY,
  clearGraphPositions,
  saveGraphPosition,
  storedGraphPositions,
} from "../manualPositions";

beforeEach(() => localStorage.clear());

describe("manual graph positions", () => {
  it("persists finite positions by graph scope", () => {
    saveGraphPosition("p1", "task-1", { x: 1.2, y: 3.4 });
    saveGraphPosition("p2", "task-2", { x: -2, y: 5 });
    expect(storedGraphPositions()).toEqual({
      p1: { "task-1": { x: 1.2, y: 3.4 } },
      p2: { "task-2": { x: -2, y: 5 } },
    });
  });

  it("ignores malformed stored values", () => {
    localStorage.setItem(GRAPH_POSITIONS_STORAGE_KEY, JSON.stringify({
      p1: { good: { x: 1, y: 2 }, bad: { x: "1", y: 2 }, infinite: { x: 1, y: Infinity } },
    }));
    expect(storedGraphPositions()).toEqual({ p1: { good: { x: 1, y: 2 } } });
  });

  it("clears one project and notifies the mounted canvas", () => {
    saveGraphPosition("p1", "task-1", { x: 1, y: 2 });
    saveGraphPosition("p2", "task-2", { x: 3, y: 4 });
    const changed = vi.fn();
    window.addEventListener(GRAPH_POSITIONS_CHANGED, changed, { once: true });
    clearGraphPositions("p1");
    expect(storedGraphPositions()).toEqual({ p2: { "task-2": { x: 3, y: 4 } } });
    expect(changed).toHaveBeenCalledOnce();
  });
});
