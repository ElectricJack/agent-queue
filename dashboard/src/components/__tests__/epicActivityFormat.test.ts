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

it("keeps delivery holds and unavailable evidence out of ready counts", () => {
  const readiness = { state: "waiting_on_delivery" as const, claimable: false,
    label: "Waiting on delivery", reason: "Land source.6 on dev" };
  expect(displayedTaskStatus({ status: "READY", claim_readiness: readiness })).toBe("WAITING_ON_DELIVERY");
  expect(displayedTaskStatus({ status: "READY", claim_readiness: { ...readiness, state: "unknown" } })).toBe("WITHHELD");
  expect(displayedTaskStatus({ status: "READY", claim_readiness: { ...readiness, state: "claimable", claimable: true } })).toBe("READY");
});

it("preserves epic activity when the container itself is outside the leaf frontier", () => {
  expect(displayedTaskStatus({ status: "READY", display_state: "waiting", child_state_counts: { waiting: 2 },
    claim_readiness: { state: "withheld", claimable: false, label: "Withheld", reason: "Has children" } })).toBe("WAITING");
});
