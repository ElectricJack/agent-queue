import { describe, expect, it } from "vitest";
import type { HeldBatchSummary } from "@aq/ts-client";
import { statusPillFor } from "../statusPillModel";
import { EPIC_DELIVERY } from "../../../testUtils/epicDelivery";

const batch: HeldBatchSummary = { batch_id: "batch", state: "held", structure_revision: 2,
  source: "spec:source", lineage: { lineage_kind: "none" } };

describe("statusPill ingestion hold", () => {
  it.each(["held", "reviewing", "needs_scope"] as const)("shows %s over lifecycle, rollup, gates and delivery", (state) => {
    const pill = statusPillFor({ status: "IN_PROGRESS", held_batch: { ...batch, state },
      display_state: "active", isBlocked: true, delivery: EPIC_DELIVERY.activeIntegration });
    expect(pill.text).toBe("Held for fidelity review");
    expect(pill.title).toContain("batch batch · revision 2");
    expect(pill.title).toContain("task status IN_PROGRESS");
    expect(pill.working).toBe(false);
    expect(pill.treatment.stripe).toBe(false);
  });

  it.each(["released", "retired"] as const)("does not present historical %s membership as a hold", (state) => {
    expect(statusPillFor({ status: "READY", held_batch: { ...batch, state } }).text).toBe("Ready");
  });

  it("does not infer batch membership from readiness", () => {
    expect(statusPillFor({ status: "READY", claim_readiness: { state: "withheld", claimable: false,
      label: "Withheld", reason: "Ingestion hold", reasons: [{ code: "frontier_ingest_held", detail: "Awaiting review" }] } }).text)
      .toBe("Withheld");
  });
});
