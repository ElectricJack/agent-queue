import { describe, expect, it } from "vitest";
import {
  INTEGRATION_HISTORY_MESSAGE,
  integrationHistoryRefusal,
  integrationRemovalRefusal,
} from "../deleteRefusals";

/**
 * The real shape `_hierarchy_failure` renders (src/commands/task_commands.py)
 * for the history refusal `assert_integration_permits_removal` raises
 * (src/integration/removal_guard.py).
 */
const refusal = () =>
  Object.assign(new Error("API 422"), {
    payload: {
      success: false,
      code: "hierarchy.integration_history_retained",
      error:
        "hierarchy.integration_history_retained: delete is refused: 2 integration audit " +
        "record(s) name azure-beacon (integration_parent_episodes(azure-beacon), " +
        "integration_repair_operations(azure-beacon)) and audit history is append-only. " +
        "Archive the task instead; its history stays readable by id.",
      references: [
        { task_id: "azure-beacon", table: "integration_parent_episodes", column: "parent_task_id" },
        {
          task_id: "azure-beacon",
          table: "integration_repair_operations",
          column: "verifier_task_id",
        },
      ],
    },
  });

describe("integrationHistoryRefusal", () => {
  it("explains the refusal in words the operator can act on", () => {
    expect(integrationHistoryRefusal(refusal())).toBe(INTEGRATION_HISTORY_MESSAGE);
    // No table names, no error codes, no "try again".
    expect(INTEGRATION_HISTORY_MESSAGE).not.toMatch(/hierarchy\.|integration_|try again/i);
  });

  it("still recognises the refusal from the prose alone", () => {
    // Belt and braces: if the 422 body is ever narrowed back to {error}, the
    // daemon's `"<code>: <detail>"` rendering is enough to classify it.
    const prose = Object.assign(new Error("API 422"), {
      payload: {
        error: "hierarchy.integration_history_retained: delete is refused: 1 integration audit record(s)",
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

  it("explains retained history in plain words, not the daemon's audit detail", () => {
    // History never lets go, so there is no command to show; the audit table
    // names in the daemon's detail mean nothing to the operator.
    expect(integrationRemovalRefusal(refusal())).toBe(INTEGRATION_HISTORY_MESSAGE);
  });

  it("does not replace unrelated errors", () => {
    expect(integrationRemovalRefusal(new Error("boom"))).toBeNull();
    expect(integrationRemovalRefusal(null)).toBeNull();
    expect(
      integrationRemovalRefusal(
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
