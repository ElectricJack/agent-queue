import { describe, expect, it } from "vitest";
import type { EpicDeliveryStatus } from "@aq/ts-client";
import { choiceLabels, isBatchRef, referencesBatch, when } from "../focusFormat";

describe("focusFormat", () => {
  it("renders every choice shape the daemon tolerates", () => {
    expect(choiceLabels(["Keep Opus", "Revert"])).toEqual(["Keep Opus", "Revert"]);
    expect(choiceLabels({ a: "Keep Opus", b: "Revert" })).toEqual(["Keep Opus", "Revert"]);
    expect(choiceLabels("Keep Opus")).toEqual(["Keep Opus"]);
    expect(choiceLabels([{ label: "Keep Opus" }])).toEqual(['{"label":"Keep Opus"}']);
    expect(choiceLabels(null)).toEqual([]);
    expect(choiceLabels(undefined)).toEqual([]);
  });

  it("matches a delivery ref that names the batch, and nothing else", () => {
    expect(isBatchRef({ kind: "batch", id: "batch-77", label: "" }, "batch-77")).toBe(true);
    expect(isBatchRef({ kind: "task", id: "batch-77", label: "" }, "batch-77")).toBe(false);
    expect(isBatchRef({ kind: "batch", id: "batch-78", label: "" }, "batch-77")).toBe(false);
    expect(isBatchRef(null, "batch-77")).toBe(false);
  });

  it("finds a batch named by either the responsible party or a link", () => {
    const responsible: EpicDeliveryStatus = {
      state: "integrating",
      label: "Integrating",
      display_status: "Integrating",
      responsible: { kind: "batch", id: "batch-77", label: "" },
    };
    const linked: EpicDeliveryStatus = {
      state: "verifying",
      label: "Verifying",
      display_status: "Verifying",
      links: [{ kind: "batch", id: "batch-77", label: "" }],
    };
    expect(referencesBatch(responsible, "batch-77")).toBe(true);
    expect(referencesBatch(linked, "batch-77")).toBe(true);
    expect(referencesBatch(responsible, "batch-78")).toBe(false);
    expect(referencesBatch(null, "batch-77")).toBe(false);
    expect(
      referencesBatch({ state: "implementing", label: "Implementing", display_status: "Implementing" }, "batch-77"),
    ).toBe(false);
  });

  it("renders absent evidence as a dash", () => {
    expect(when(1790000000)).not.toBe("—");
    expect(when(null)).toBe("—");
    expect(when(undefined)).toBe("—");
    expect(when(0)).toBe("—");
  });
});