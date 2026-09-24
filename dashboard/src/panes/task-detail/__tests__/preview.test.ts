import { describe, expect, it } from "vitest";
import { rememberTaskPreview, taskPreview } from "../preview";

describe("task pane preview", () => {
  it("keeps the row's title, status, priority and project", () => {
    rememberTaskPreview({ id: "a", title: "Alpha", status: "READY", priority: 4, project_id: "p", extra: 1 } as never);
    expect(taskPreview("a")).toEqual({ id: "a", title: "Alpha", status: "READY", priority: 4, project_id: "p" });
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
