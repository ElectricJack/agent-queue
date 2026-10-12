import { describe, expect, it } from "vitest";
import { STATUS_TREATMENT, isKnownStatus, treatmentFor } from "../taskStatus";
import { FINISHED_STATUSES, TASK_STATUSES } from "../taskFilters";

/** §7.1: the table is the whole vocabulary — every status the dashboard can
 *  render has a row and there is no fallback tone. PENDING is the stub's
 *  status and a legacy spelling of DEFINED. */
const KNOWN = [...new Set([...TASK_STATUSES, ...FINISHED_STATUSES, "PENDING"])];
const OPEN = ["READY", "ASSIGNED", "IN_PROGRESS", "WAITING_INPUT", "PAUSED"];

/** The table's own row, or a failed expectation naming the missing status. */
function rowOf(status: string) {
  expect(isKnownStatus(status), status).toBe(true);
  if (!isKnownStatus(status)) throw new Error(`no row for ${status}`);
  return STATUS_TREATMENT[status];
}

describe("STATUS_TREATMENT (spec §3.3)", () => {
  it("covers every status the dashboard can render, with no fallback row", () => {
    for (const status of KNOWN) {
      const row = rowOf(status);
      expect(row.pill).toBeTruthy();
      expect(row.pillClass).toBeTruthy();
      // Canceled and skipped alias the pending colour.
      if (status === "CANCELLED" || status === "CANCELED" || status === "SKIPPED") {
        expect(row.pillClass).toBe(STATUS_TREATMENT.DEFINED.pillClass);
      }
    }
  });

  it("pills and card treatments are distinct words, nothing colour-only", () => {
    expect(STATUS_TREATMENT.IN_PROGRESS.pill).toBe("In progress");
    expect(STATUS_TREATMENT.IN_PROGRESS.stripe).toBe(true);
    expect(STATUS_TREATMENT.BLOCKED.pill).toBe("Blocked");
    expect(STATUS_TREATMENT.WAITING_INPUT.pill).toBe("Needs your call");
    expect(STATUS_TREATMENT.COMPLETED.pill).toBe("Done");
    expect(STATUS_TREATMENT.DEFINED.pill).toBe("Not started");
  });

  it("only IN_PROGRESS carries stripes", () => {
    for (const status of KNOWN) {
      expect(rowOf(status).stripe, status).toBe(status === "IN_PROGRESS");
    }
  });
});

describe("treatmentFor", () => {
  it("returns the status's own row by default", () => {
    expect(treatmentFor("READY")).toBe(STATUS_TREATMENT.READY);
    expect(treatmentFor("COMPLETED")).toBe(STATUS_TREATMENT.COMPLETED);
  });

  it("is_blocked on an open status reads as Blocked (§3.3 first row)", () => {
    for (const status of OPEN) {
      expect(treatmentFor(status, true).pill).toBe("Blocked");
    }
  });

  it("a not-started task's predecessors keep it Not started (its After form)", () => {
    expect(treatmentFor("DEFINED", true)).toBe(STATUS_TREATMENT.DEFINED);
    expect(treatmentFor("PENDING", true)).toBe(STATUS_TREATMENT.PENDING);
  });

  it("a settled status with is_blocked keeps its own row", () => {
    expect(treatmentFor("FAILED", true)).toBe(STATUS_TREATMENT.FAILED);
    expect(treatmentFor("COMPLETED", true)).toBe(STATUS_TREATMENT.COMPLETED);
    expect(treatmentFor("CANCELLED", true)).toBe(STATUS_TREATMENT.CANCELLED);
  });

  it("an unknown status keeps its own word on a neutral pill, never Not started", () => {
    const row = treatmentFor("MERGE_QUEUED");
    expect(row.pill).toBe("Merge queued");
    expect(row.pillClass).toBe(STATUS_TREATMENT.DEFINED.pillClass);
    expect(row.stripe).toBe(false);
  });

  it("no pill text uses the pending colour, which fails text contrast", () => {
    for (const status of KNOWN) {
      expect(rowOf(status).pillClass, status).not.toMatch(/\btext-g-pending\b/);
    }
  });

  it("keeps BLOCKED when blocked and stored as BLOCKED", () => {
    expect(treatmentFor("BLOCKED", true)).toBe(STATUS_TREATMENT.BLOCKED);
    expect(treatmentFor("BLOCKED", false)).toBe(STATUS_TREATMENT.BLOCKED);
  });
});
