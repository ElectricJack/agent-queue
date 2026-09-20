import { describe, expect, it } from "vitest";
import { INTEGRATION_HISTORY_MESSAGE, integrationHistoryRefusal } from "../deleteRefusals";

/** The real shape `_hierarchy_failure` renders (src/commands/task_commands.py). */
const refusal = () =>
  Object.assign(new Error("API 422"), {
    payload: {
      success: false,
      code: "hierarchy.integration_owned",
      error:
        "hierarchy.integration_owned: delete would orphan 2 integration record(s): " +
        "integration_batch_members(azure-beacon), integration_repair_stages(azure-beacon)",
      references: [
        { task_id: "azure-beacon", table: "integration_batch_members" },
        { task_id: "azure-beacon", table: "integration_repair_stages" },
      ],
    },
  });

describe("integrationHistoryRefusal", () => {
  it("explains the refusal in words the operator can act on", () => {
    expect(integrationHistoryRefusal(refusal())).toBe(INTEGRATION_HISTORY_MESSAGE);
    // No table names, no error codes, no "try again".
    expect(INTEGRATION_HISTORY_MESSAGE).not.toMatch(/hierarchy\.|integration_batch|try again/i);
  });

  it("leaves every other failure to the generic error surface", () => {
    expect(integrationHistoryRefusal(new Error("boom"))).toBeNull();
    expect(integrationHistoryRefusal(null)).toBeNull();
    expect(
      integrationHistoryRefusal(
        Object.assign(new Error("API 422"), {
          payload: { code: "hierarchy.branch_discard_required", branches: [] },
        }),
      ),
    ).toBeNull();
  });
});
