import { describe, expect, it } from "vitest";
import { fullSettingsRoute } from "../fullSettingsRoute";

describe("fullSettingsRoute", () => {
  it.each([
    [{ subject: "project", subjectId: "demo" } as const, "/projects/demo/config"],
    [{ subject: "profile", subjectId: "reviewer" } as const, "/settings/profiles"],
    [
      { subject: "project-profile", subjectId: "coder", projectId: "demo" } as const,
      "/projects/demo/profiles",
    ],
    [{ subject: "playbook", subjectId: "review-gate" } as const, "/playbooks/review-gate"],
    [
      { subject: "intelligence-class", subjectId: "fast-off" } as const,
      "/settings/intelligence-classes",
    ],
  ])("resolves %o to %s", (args, expected) => {
    expect(fullSettingsRoute(args)).toBe(expected);
  });
});
