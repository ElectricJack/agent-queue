/** Expand/collapse is owned by the user, never by the viewport.
 *
 *  The operator report behind these tests: "zooming out collapses epics, it is
 *  jarring and disorienting". Nesting must change only on an explicit user
 *  action (the container toggle) — never as a side effect of zoom, pan,
 *  resize or a live graph refresh. A future level-of-detail mode may simplify
 *  how a tile is drawn, but it may not mutate the expanded set. These tests
 *  are the guard rail for that invariant.
 */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setExpandedTaskIds, useExpandedTaskIds } from "../useGraphHierarchy";

const STORAGE_KEY = "aq:command-center:expanded-task-ids:v1";

beforeEach(() => localStorage.clear());
afterEach(cleanup);

describe("expanded state ownership", () => {
  it("changes only through an explicit toggle, and survives a remount", () => {
    const first = renderHook(() => useExpandedTaskIds());
    expect(first.result.current.expandedTaskIds.has("epic")).toBe(false);

    act(() => first.result.current.toggleExpanded("epic"));
    expect(first.result.current.expandedTaskIds.has("epic")).toBe(true);
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "[]")).toEqual(["epic"]);

    // A re-render — what a live graph refresh or a resize produces — leaves
    // the set alone.
    first.rerender();
    expect(first.result.current.expandedTaskIds.has("epic")).toBe(true);
    expect(first.result.current.expandedTaskIds.has("other")).toBe(false);

    // A remount (tab switch, page reload, desktop/mobile view swap) restores
    // the same choices from storage.
    first.unmount();
    const second = renderHook(() => useExpandedTaskIds());
    expect(second.result.current.expandedTaskIds.has("epic")).toBe(true);
    expect(second.result.current.expandedTaskIds.has("other")).toBe(false);
  });

  it("broadcasts one set to every consumer, so the canvas and toolbar agree", () => {
    const canvas = renderHook(() => useExpandedTaskIds());
    const toolbar = renderHook(() => useExpandedTaskIds());

    act(() => canvas.result.current.toggleExpanded("epic"));
    expect(toolbar.result.current.expandedTaskIds.has("epic")).toBe(true);

    act(() => setExpandedTaskIds(new Set(["other"])));
    expect(canvas.result.current.expandedTaskIds.has("epic")).toBe(false);
    expect(toolbar.result.current.expandedTaskIds.has("other")).toBe(true);
  });
});
