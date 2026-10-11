import { describe, expect, it } from "vitest";

import { childCountText, displayedTaskStatus, epicLeafProgress, rolledState } from "../epicActivityFormat";

describe("rolledState", () => {
  it("shows the rolled-up activity unless an epic batch is in flight", () => {
    expect(rolledState({ display_state: "waiting" })).toBe("waiting");
    expect(rolledState({ display_state: "waiting" }, { state: "implementing" })).toBe("waiting");
    expect(rolledState({ display_state: "active" }, { state: "not_tracked" })).toBe("active");
    expect(rolledState({ display_state: "waiting" }, { state: "integrating" })).toBeNull();
    expect(rolledState({ display_state: "active" }, { state: "verifying" })).toBeNull();
    expect(rolledState({}, null)).toBeNull();
  });
});

describe("epic activity text", () => {
  it("lists child counts and displays an idle epic as WAITING", () => {
    expect(childCountText({ completed: 1, waiting: 2 })).toBe("1 completed · 2 waiting");
    expect(childCountText(undefined)).toBe("");
    expect(displayedTaskStatus({ status: "IN_PROGRESS", display_state: "waiting" })).toBe("WAITING");
    expect(displayedTaskStatus({ status: "IN_PROGRESS" })).toBe("IN_PROGRESS");
  });

  it("builds leaf progress from stored descendant statuses", () => {
    expect(epicLeafProgress({ COMPLETED: 1, IN_PROGRESS: 1, DEFINED: 2 })).toMatchObject({ done: 1, total: 4 });
  });
});
