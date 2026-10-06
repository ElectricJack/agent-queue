import { describe, expect, it } from "vitest";
import { taskReason } from "../taskReason";

const base = (over: Record<string, unknown> = {}) => ({
  status: "DEFINED",
  isBlocked: false,
  ...over,
});

describe("taskReason vocabulary (spec §3.3/§3.4)", () => {
  describe("DEFINED", () => {
    it("names the blocker task", () => {
      expect(taskReason(base({ blockerTitle: "Design API" }))).toBe("After Design API");
    });
    it("falls back to a count (and the singular form)", () => {
      expect(taskReason(base({ blockerCount: 2, blockerTitle: null }))).toBe("After 2 tasks");
      expect(taskReason(base({ blockerCount: 1, blockerTitle: null }))).toBe("After 1 task");
    });
    it("names a next phase", () => {
      expect(taskReason(base({ nextPhase: { order: 3, label: "Release" } }))).toBe("Before Phase 3 · Release");
    });
    it("says nothing gates it otherwise", () => {
      expect(taskReason(base())).toBe("Not yet ready");
    });
  });

  it("PENDING is not yet ready", () => {
    expect(taskReason(base({ status: "PENDING" }))).toBe("Not yet ready");
  });

  it("READY is claimable", () => {
    expect(taskReason(base({ status: "READY" }))).toBe("Claimable · next in frontier");
  });

  describe("ASSIGNED", () => {
    it("names the profile", () => {
      expect(taskReason(base({ status: "ASSIGNED", profileId: "deep-high" }))).toBe("Starting · deep-high");
    });
    it("without one", () => {
      expect(taskReason(base({ status: "ASSIGNED" }))).toBe("Starting");
    });
  });

  describe("IN_PROGRESS", () => {
    it("prefers subtask counts", () => {
      expect(taskReason(base({ status: "IN_PROGRESS", subtasks: { total: 5, settled: 3 } })))
        .toBe("3 of 5 subtasks");
    });
    it("then implementation progress with running/blocked", () => {
      expect(taskReason(base({
        status: "IN_PROGRESS",
        descDone: 3, descTotal: 5, descRunning: 1, descBlocked: 1,
      }))).toBe("3 of 5 done · 1 running · 1 blocked");
    });
    it("is time-free: never durations", () => {
      expect(taskReason(base({ status: "IN_PROGRESS" }))).toBeNull();
    });
  });

  it("WAITING_INPUT leads with the question", () => {
    expect(taskReason(base({ status: "WAITING_INPUT", question: "Approve r-7f3a?\nMore detail" })))
      .toBe("Approve r-7f3a?");
    expect(taskReason(base({ status: "WAITING_INPUT" }))).toBe("Needs your call");
  });

  it("PAUSED states the pause reason", () => {
    expect(taskReason(base({ status: "PAUSED", pauseReason: "Provider backoff\nsecond line" })))
      .toBe("Provider backoff");
    expect(taskReason(base({ status: "PAUSED" }))).toBe("Paused");
  });

  describe("COMPLETED", () => {
    it("is Finished alone until a PR exists", () => {
      expect(taskReason(base({ status: "COMPLETED" }))).toBe("Finished");
    });
    it("adds the PR number", () => {
      expect(taskReason(base({ status: "COMPLETED", prUrl: "https://github.com/x/y/pull/41" })))
        .toBe("Finished · PR #41");
    });
    it("ignores a URL without a number", () => {
      expect(taskReason(base({ status: "COMPLETED", prUrl: "https://codeberg.org/x/y" }))).toBe("Finished");
    });
  });

  it("FAILED shows the failure summary's first line, red", () => {
    expect(taskReason(base({ status: "FAILED", failureLine: "boom\nstack trace" }))).toBe("boom");
    expect(taskReason(base({ status: "FAILED" }))).toBeNull();
  });

  describe("cancelled / skipped", () => {
    it("CANCELLED and CANCELED", () => {
      expect(taskReason(base({ status: "CANCELLED" }))).toBe("Cancelled");
      expect(taskReason(base({ status: "CANCELED" }))).toBe("Cancelled");
    });
    it("names what superseded it when known", () => {
      expect(taskReason(base({ status: "CANCELLED", supersededBy: "v2 task" })))
        .toBe("Superseded by v2 task");
    });
    it("SKIPPED says nothing", () => {
      expect(taskReason(base({ status: "SKIPPED" }))).toBeNull();
    });
  });

  describe("block overrides (spec §3.3: BLOCKED or is_blocked on an open status)", () => {
    it("BLOCKED names the blocker", () => {
      expect(taskReason(base({ status: "BLOCKED", blockerTitle: "Design API" })))
        .toBe("Waiting on Design API");
    });
    it("a review wait wins the phrasing", () => {
      expect(taskReason(base({
        status: "DEFINED", isBlocked: true, reviewWait: { review_id: "r-7f3a" },
      }))).toBe("Waiting on review r-7f3a");
    });
    it("then a gate", () => {
      expect(taskReason(base({ status: "DEFINED", isBlocked: true, gateType: "review" })))
        .toBe("Waiting on gate review");
    });
    it("then a count, then the bare word", () => {
      expect(taskReason(base({ status: "READY", isBlocked: true, blockerCount: 3 })))
        .toBe("Waiting on dependencies");
      expect(taskReason(base({ status: "READY", isBlocked: true }))).toBe("Blocked");
    });
  });

  it("an unknown status says nothing rather than guessing", () => {
    expect(taskReason(base({ status: "SOMETHING_ELSE" }))).toBeNull();
  });
});
