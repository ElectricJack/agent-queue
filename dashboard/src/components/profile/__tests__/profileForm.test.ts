import { describe, expect, it } from "vitest";
import { profileToForm } from "../profileForm";
import type { ProfileDetail } from "../../../api/hooks";

describe("profileToForm", () => {
  it("maps a full profile", () => {
    const profile = {
      name: "Reviewer",
      description: "Reviews PRs",
      default_class: "standard-medium",
      permission_mode: "acceptEdits",
      system_prompt_suffix: "Be terse.",
      allowed_tools: ["Read", "Edit"],
      mcp_servers: ["aq-files"],
    } as unknown as ProfileDetail;
    expect(profileToForm(profile)).toEqual({
      name: "Reviewer",
      description: "Reviews PRs",
      default_class: "standard-medium",
      permission_mode: "acceptEdits",
      system_prompt_suffix: "Be terse.",
      allowed_tools: ["Read", "Edit"],
      mcp_servers: ["aq-files"],
    });
  });

  it("maps null/undefined to empty defaults", () => {
    expect(profileToForm(null)).toEqual({
      name: "",
      description: "",
      default_class: "",
      permission_mode: "",
      system_prompt_suffix: "",
      allowed_tools: [],
      mcp_servers: [],
    });
  });

  it("maps the sentinel '(default)' permission_mode to empty string", () => {
    const profile = { permission_mode: "(default)" } as unknown as ProfileDetail;
    expect(profileToForm(profile).permission_mode).toBe("");
  });
});
