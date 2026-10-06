import { describe, expect, it } from "vitest";
import { STATUS_TREATMENT, treatmentFor } from "../taskStatus";

/** §7.1: the table is the whole vocabulary — every status the dashboard can
 *  render has a row and there is no fallback tone. */
const KNOWN = [
  "DEFINED", "PENDING", "READY", "ASSIGNED", "IN_PROGRESS", "WAITING_INPUT",
  "PAUSED", "BLOCKED", "FAILED", "COMPLETED", "CANCELLED", "CANCELED", "SKIPPED",
];
const OPEN = KNOWN.filter((s) => s !== "BLOCKED" && s !== "FAILED" && s !== "COMPLETED" && !["CANCELLED", "CANCELED", "SKIPPED"].includes(s));

describe("STATUS_TREATMENT (spec §3.3)", () => {
  it("covers every status the dashboard can render, with no fallback row", () => {
    for (const status of KNOWN) {
      const row = STATUS_TREATMENT[status];
      expect(row, status).toBeDefined();
      expect(row!.pill).toBeTruthy();
      expect(row!.pillClass).toBeTruthy();
      // Canceled and skipped alias the pending colour.
      if (row && (status === "CANCELLED" || status === "CANCELED" || status === "SKIPPED")) {
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
      expect(STATUS_TREATMENT[status].stripe, status).toBe(status === "IN_PROGRESS");
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

  it("a settled status with is_blocked keeps its own row", () => {
    expect(treatmentFor("FAILED", true)).toBe(STATUS_TREATMENT.FAILED);
    expect(treatmentFor("COMPLETED", true)).toBe(STATUS_TREATMENT.COMPLETED);
    expect(treatmentFor("CANCELLED", true)).toBe(STATUS_TREATMENT.CANCELLED);
  });

  it("keeps BLOCKED when blocked and stored as BLOCKED", () => {
    expect(treatmentFor("BLOCKED", true)).toBe(STATUS_TREATMENT.BLOCKED);
    expect(treatmentFor("BLOCKED", false)).toBe(STATUS_TREATMENT.BLOCKED);
  });
});
