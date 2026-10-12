import { describe, expect, it } from "vitest";
import type { EpicDelivery } from "../../../components/epicDeliveryFormat";
import { taskReason, type ReasonInput } from "../taskReason";

const text = (input: ReasonInput) => taskReason(input)?.text ?? null;
const strongRuns = (input: ReasonInput) =>
  taskReason(input)?.segments.filter((s) => s.strong).map((s) => s.text) ?? [];

const delivery = (over: Partial<EpicDelivery>): EpicDelivery =>
  ({ state: "paused", label: "Paused", display_status: "Paused", ...over }) as EpicDelivery;

describe("taskReason vocabulary (spec §3.3/§3.4)", () => {
  describe("not started", () => {
    it("names the first loaded blocker, in bold, with the rest as a count", () => {
      expect(text({ status: "DEFINED", blockerTitle: "Design API", blockerCount: 1 })).toBe("After Design API");
      expect(text({ status: "DEFINED", blockerTitle: "Design API", blockerCount: 3 })).toBe("After Design API and 2 more");
      expect(strongRuns({ status: "DEFINED", blockerTitle: "Design API", blockerCount: 1 })).toEqual(["Design API"]);
    });
    it("falls back to a count when the blocker is not loaded", () => {
      expect(text({ status: "DEFINED", blockerCount: 2 })).toBe("After 2 tasks");
      expect(text({ status: "DEFINED", blockerCount: 1 })).toBe("After 1 task");
    });
    it("names the task it comes before when nothing gates it", () => {
      expect(text({ status: "DEFINED", dependentTitle: "Dashboard polish" })).toBe("Before Dashboard polish");
    });
    it("otherwise is not yet ready; PENDING (a stub) reads the same way", () => {
      expect(text({ status: "DEFINED" })).toBe("Not yet ready");
      expect(text({ status: "PENDING" })).toBe("Not yet ready");
    });
  });

  describe("READY", () => {
    it("is next in the frontier only when nothing loaded sorts first", () => {
      expect(text({ status: "READY", readyAhead: 0 })).toBe("Claimable · next in frontier");
      expect(text({ status: "READY", readyAhead: 4 })).toBe("Claimable · 4 ahead");
    });
    it("says only claimable when a priority tie hides the order", () => {
      expect(text({ status: "READY", readyAhead: null })).toBe("Claimable");
      expect(text({ status: "READY" })).toBe("Claimable");
    });
  });

  it("ASSIGNED names the profile it is starting on", () => {
    expect(text({ status: "ASSIGNED", profileId: "deep-high-codex" })).toBe("Starting · deep-high-codex");
    expect(text({ status: "ASSIGNED" })).toBe("Starting");
  });

  describe("IN_PROGRESS", () => {
    it("counts subtasks, the count in bold", () => {
      const input = { status: "IN_PROGRESS", subtasks: { total: 5, settled: 3 } };
      expect(text(input)).toBe("3 of 5 subtasks");
      expect(strongRuns(input)).toEqual(["3 of 5"]);
    });
    it("uses an integrating epic's delivery reason, else says it is working", () => {
      expect(text({ status: "IN_PROGRESS", delivery: delivery({ state: "integrating", reason: "Rebasing onto main\nmore" }) }))
        .toBe("Rebasing onto main");
      expect(text({ status: "IN_PROGRESS", subtasks: { total: 0, settled: 0 } })).toBe("Working");
    });
  });

  describe("blocked", () => {
    it("a review wait names your review whatever the stored status", () => {
      const input = { status: "IN_PROGRESS", reviewWait: { review_id: "r-7f3a" } };
      expect(text(input)).toBe("Waiting on your review of r-7f3a");
      expect(strongRuns(input)).toEqual(["your review"]);
    });
    it("an open gate names its type", () => {
      expect(text({ status: "BLOCKED", openGateTypes: ["approval"] })).toBe("Waiting on gate approval");
    });
    it("names the blocker task, or counts them", () => {
      expect(text({ status: "BLOCKED", blockerTitle: "Schema", blockerCount: 1 })).toBe("Waiting on Schema");
      expect(text({ status: "BLOCKED", blockerCount: 2 })).toBe("Waiting on 2 tasks");
      expect(text({ status: "BLOCKED" })).toBe("Waiting on dependencies");
    });
    it("is_blocked on an open status reads as blocked", () => {
      expect(text({ status: "READY", isBlocked: true, blockerTitle: "Schema", blockerCount: 1 })).toBe("Waiting on Schema");
      expect(text({ status: "PAUSED", isBlocked: true, blockerCount: 1 })).toBe("Waiting on 1 task");
    });
    it("is_blocked on a not-started task keeps its After form", () => {
      expect(text({ status: "DEFINED", isBlocked: true, blockerTitle: "Schema", blockerCount: 1 })).toBe("After Schema");
    });
    it("is_blocked on a settled status does not", () => {
      expect(text({ status: "COMPLETED", isBlocked: true })).toBe("Finished");
    });
  });

  it("WAITING_INPUT asks for your answer unless a delivery says why", () => {
    expect(text({ status: "WAITING_INPUT" })).toBe("Waiting on your answer");
    expect(text({ status: "WAITING_INPUT", delivery: delivery({ state: "awaiting_approval", reason: "Approve the merge" }) }))
      .toBe("Approve the merge");
  });

  it("PAUSED shows the delivery hold's reason and nothing invented otherwise", () => {
    expect(text({ status: "PAUSED", delivery: delivery({ hold: "integration", reason: "Held by integration" }) }))
      .toBe("Held by integration");
    expect(taskReason({ status: "PAUSED" })).toBeNull();
  });

  it("COMPLETED is finished, with the PR number when there is one", () => {
    expect(text({ status: "COMPLETED", prUrl: "https://github.com/o/r/pull/412" })).toBe("Finished · PR #412");
    expect(text({ status: "COMPLETED", prUrl: "https://example.com/branches/12" })).toBe("Finished");
    expect(text({ status: "COMPLETED" })).toBe("Finished");
  });

  it("FAILED, CANCELLED and SKIPPED say nothing the pill does not", () => {
    for (const status of ["FAILED", "CANCELLED", "CANCELED", "SKIPPED"]) {
      expect(taskReason({ status }), status).toBeNull();
    }
  });

  it("a collapsed epic's reason is its count line", () => {
    const input = { status: "PAUSED", childCount: 6, descTotal: 6, descDone: 3, descRunning: 1, descBlocked: 1 };
    expect(text(input)).toBe("3 of 6 done · 1 running · 1 blocked");
    expect(strongRuns(input)).toEqual(["3 of 6"]);
    expect(text({ status: "READY", childCount: 2, descTotal: 4, descDone: 0 })).toBe("0 of 4 done");
  });

  it("no form carries a time or duration (those arrive with phase C)", () => {
    const forms = [
      { status: "IN_PROGRESS" }, { status: "COMPLETED" }, { status: "READY", readyAhead: 0 },
      { status: "BLOCKED", blockerCount: 3 }, { status: "ASSIGNED", profileId: "p" },
    ];
    for (const input of forms) expect(text(input)).not.toMatch(/\d+[smhd]\b|\d{1,2}:\d{2}|ago/);
  });
});
