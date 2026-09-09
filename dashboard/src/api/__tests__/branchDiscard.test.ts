import { describe, expect, it } from "vitest";

import { branchesAwaitingChoice } from "../branchDiscard";

/**
 * The parser decides whether a failed delete is a failure to report or a
 * question to put to the operator. Getting that wrong in either direction is
 * bad: a missed prompt makes delete look permanently broken, and a false
 * positive asks about branches on an unrelated error.
 */
describe("branchesAwaitingChoice", () => {
  const error = (payload: unknown) => Object.assign(new Error("API 422"), { payload });

  it("returns the branches the refusal named", () => {
    const branches = branchesAwaitingChoice(
      error({
        code: "hierarchy.branch_discard_required",
        branches: [{ task_id: "azure-beacon", branch: "aq/azure-beacon", base_sha: "abc" }],
      }),
    );

    expect(branches).toEqual([
      { task_id: "azure-beacon", branch: "aq/azure-beacon", base_sha: "abc" },
    ]);
  });

  it("asks anyway when the code arrives with no list", () => {
    expect(branchesAwaitingChoice(error({ code: "hierarchy.branch_discard_required" }))).toEqual([]);
  });

  it("is null for every other hierarchy refusal", () => {
    expect(branchesAwaitingChoice(error({ code: "hierarchy.sealed" }))).toBeNull();
    expect(branchesAwaitingChoice(error({ code: "hierarchy.delivery_target_fixed" }))).toBeNull();
  });

  it("is null for errors that carry no payload at all", () => {
    expect(branchesAwaitingChoice(new Error("network down"))).toBeNull();
    expect(branchesAwaitingChoice(null)).toBeNull();
    expect(branchesAwaitingChoice(error("not an object"))).toBeNull();
  });

  it("drops malformed entries rather than rendering undefined", () => {
    const branches = branchesAwaitingChoice(
      error({
        code: "hierarchy.branch_discard_required",
        branches: [{ task_id: "ok", branch: "aq/ok", base_sha: "abc" }, { nope: true }, null],
      }),
    );

    expect(branches).toHaveLength(1);
  });
});
