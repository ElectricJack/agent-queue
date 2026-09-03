import { describe, expect, it } from "vitest";
import { contextualSettingsArgsSchema } from "../args";

describe("contextualSettingsArgsSchema", () => {
  it("accepts all four valid shapes", () => {
    const valid = [
      { subject: "project", subjectId: "demo" },
      { subject: "profile", subjectId: "reviewer" },
      { subject: "playbook", subjectId: "review-gate" },
      { subject: "intelligence-class", subjectId: "fast-off" },
    ];
    for (const v of valid) {
      expect(contextualSettingsArgsSchema.safeParse(v).success).toBe(true);
    }
  });

  it("rejects a project arg missing subjectId", () => {
    expect(contextualSettingsArgsSchema.safeParse({ subject: "project" }).success).toBe(false);
  });

  it("rejects an unknown subject", () => {
    expect(
      contextualSettingsArgsSchema.safeParse({ subject: "bogus", subjectId: "x" }).success,
    ).toBe(false);
  });

  it("rejects the retired project-profile subject", () => {
    expect(
      contextualSettingsArgsSchema.safeParse({
        subject: "project-profile",
        subjectId: "coder",
        projectId: "demo",
      }).success,
    ).toBe(false);
  });
});
