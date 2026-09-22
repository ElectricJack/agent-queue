import { describe, expect, it } from "vitest";
import {
  INTEGRATION_HISTORY_MESSAGE,
  integrationHistoryRefusal,
  integrationRemovalRefusal,
} from "../deleteRefusals";

/** The real shape `_hierarchy_failure` renders (src/commands/task_commands.py). */
const refusal = () =>
  Object.assign(new Error("API 422"), {
    payload: {
      success: false,
      code: "hierarchy.integration_history_retained",
      error:
        "hierarchy.integration_history_retained: delete would orphan 2 integration record(s): " +
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

  it("still recognises the refusal from the prose alone", () => {
    // Belt and braces: if the 422 body is ever narrowed back to {error}, the
    // daemon's `"<code>: <detail>"` rendering is enough to classify it.
    const prose = Object.assign(new Error("API 422"), {
      payload: {
        error: "hierarchy.integration_history_retained: delete would orphan 1 integration record(s)",
      },
    });
    expect(integrationHistoryRefusal(prose)).toBe(INTEGRATION_HISTORY_MESSAGE);
  });

  it("leaves every other failure to the generic error surface", () => {
    expect(integrationHistoryRefusal(new Error("boom"))).toBeNull();
    expect(integrationHistoryRefusal(null)).toBeNull();
    expect(
      integrationHistoryRefusal(
        Object.assign(new Error("API 422"), {
          payload: {
            code: "hierarchy.branch_discard_required",
            error: "hierarchy.branch_discard_required: name a branch policy",
            branches: [],
          },
        }),
      ),
    ).toBeNull();
  });
});

describe("integrationRemovalRefusal", () => {
  it("keeps the daemon's actionable remedy for archive and delete guards", () => {
    const error = Object.assign(new Error("API 422"), {
      payload: {
        code: "hierarchy.integration_undelivered",
        error:
          "hierarchy.integration_undelivered: deliver it, or abandon it with aq task archive",
      },
    });
    expect(integrationRemovalRefusal(error)).toBe(
      "deliver it, or abandon it with aq task archive",
    );
  });

  it("does not replace unrelated errors", () => {
    expect(integrationRemovalRefusal(refusal())).toBe(
      "delete would orphan 2 integration record(s): integration_batch_members(azure-beacon), " +
        "integration_repair_stages(azure-beacon)",
    );
    expect(integrationRemovalRefusal(new Error("boom"))).toBeNull();
  });
});
