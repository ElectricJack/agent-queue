import { describe, expect, it } from "vitest";
import { rememberTaskPreview, taskPreview } from "../preview";

describe("task pane preview", () => {
  it("keeps the row's title, status, priority and project", () => {
    rememberTaskPreview({ id: "a", title: "Alpha", status: "READY", priority: 4, project_id: "p", extra: 1 } as never);
    expect(taskPreview("a")).toEqual({
      id: "a", title: "Alpha", status: "READY", display_state: null, display_reason: null, priority: 4, project_id: "p",
    });
  });

  it("keeps a container's rolled-up activity so the header does not flash its stored lifecycle", () => {
    rememberTaskPreview({ id: "epic", title: "Epic", status: "IN_PROGRESS", display_state: "waiting", display_reason: "Paused" });
    expect(taskPreview("epic")).toMatchObject({ status: "IN_PROGRESS", display_state: "waiting", display_reason: "Paused" });
    rememberTaskPreview({ id: "odd", title: "Odd", display_state: "sleeping" });
    expect(taskPreview("odd")?.display_state).toBeNull();
  });

  it("ignores a payload that carries nothing to show", () => {
    rememberTaskPreview({ id: "bare" });
    expect(taskPreview("bare")).toBeNull();
  });

  it("forgets the oldest once it holds fifty", () => {
    for (let i = 0; i < 51; i++) rememberTaskPreview({ id: "n" + i, title: "T" + i });
    expect(taskPreview("n0")).toBeNull();
    expect(taskPreview("n50")?.title).toBe("T50");
  });
});
