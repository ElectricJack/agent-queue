import { describe, expect, it } from "vitest";
import {
  profileEditPayload,
  profileToForm,
  type ProfileFormState,
} from "../profileForm";
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

const FORM: ProfileFormState = {
  name: "Supervisor",
  description: "Coordinates work",
  default_class: "standard-medium",
  permission_mode: "acceptEdits",
  system_prompt_suffix: "Be terse.",
  allowed_tools: ["Read", "Edit"],
  mcp_servers: ["playwright"],
};

describe("profileEditPayload", () => {
  it("sends mcp_servers as a list of registry names", () => {
    const body = profileEditPayload("supervisor", FORM);
    expect(body).toEqual({
      profile_id: "supervisor",
      name: "Supervisor",
      description: "Coordinates work",
      default_class: "standard-medium",
      permission_mode: "acceptEdits",
      system_prompt_suffix: "Be terse.",
      allowed_tools: ["Read", "Edit"],
      mcp_servers: ["playwright"],
    });
  });

  it("sends an empty mcp_servers list, not an empty object", () => {
    // The regression: the global edit route used to type mcp_servers as a
    // dict, so clearing every server 422'd on `[]`.
    const body = profileEditPayload("supervisor", { ...FORM, mcp_servers: [] });
    expect(body.mcp_servers).toEqual([]);
    expect(Array.isArray(body.mcp_servers)).toBe(true);
  });

  it("clears blank text fields with null", () => {
    const body = profileEditPayload("supervisor", {
      ...FORM,
      name: "",
      description: "",
      permission_mode: "",
      system_prompt_suffix: "",
    });
    expect(body.name).toBeNull();
    expect(body.description).toBeNull();
    expect(body.permission_mode).toBeNull();
    expect(body.system_prompt_suffix).toBeNull();
  });
});
