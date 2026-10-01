import { describe, expect, it } from "vitest";
import {
  parseOptionalInt,
  parseOptionalFloat,
  projectToForm,
} from "../Config";

describe("Config.tsx form helpers", () => {
  it("parseOptionalInt parses, trims, and nulls on empty/invalid", () => {
    expect(parseOptionalInt(" 4 ")).toBe(4);
    expect(parseOptionalInt("")).toBeNull();
    expect(parseOptionalInt("abc")).toBeNull();
  });

  it("parseOptionalFloat parses, trims, and nulls on empty/invalid", () => {
    expect(parseOptionalFloat(" 4.5 ")).toBe(4.5);
    expect(parseOptionalFloat("")).toBeNull();
    expect(parseOptionalFloat("abc")).toBeNull();
  });

  it("projectToForm maps nulls to empty strings and numbers to strings", () => {
    expect(projectToForm({})).toEqual({
      name: "",
      repo_default_branch: "",
      max_concurrent_agents: "",
      credit_weight: "",
      budget_limit: "",
      git_identity_override: false,
      git_identity_name: "",
      git_identity_email: "",
    });
    expect(projectToForm({ max_concurrent_agents: 3, credit_weight: 1.5 }).max_concurrent_agents).toBe(
      "3",
    );
  });

  it("projectToForm marks a project override and prefills an inheriting project", () => {
    const installation = { name: "Ops Bot", email: "ops@example.com" };
    const fallback = { name: "Agent Queue", email: "agent-queue@localhost" };
    expect(
      projectToForm({
        git_identity_name: "Jane Doe",
        git_identity_email: "jane@example.com",
        git_identity: {
          name: "Jane Doe",
          email: "jane@example.com",
          source: "project",
          configured: true,
          installation,
          project_override: { name: "Jane Doe", email: "jane@example.com" },
          fallback,
        },
      }),
    ).toMatchObject({
      git_identity_override: true,
      git_identity_name: "Jane Doe",
      git_identity_email: "jane@example.com",
    });
    expect(
      projectToForm({
        git_identity_name: null,
        git_identity_email: null,
        git_identity: {
          ...installation,
          source: "installation",
          configured: true,
          installation,
          project_override: null,
          fallback,
        },
      }),
    ).toMatchObject({
      git_identity_override: false,
      git_identity_name: "Ops Bot",
      git_identity_email: "ops@example.com",
    });
  });
});
