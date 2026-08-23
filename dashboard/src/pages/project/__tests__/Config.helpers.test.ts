import { describe, expect, it } from "vitest";
import {
  parseOptionalInt,
  parseOptionalFloat,
  projectToForm,
  profileOptionsFromRows,
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
      default_profile_id: "",
      max_concurrent_agents: "",
      credit_weight: "",
      budget_limit: "",
      discord_channel_id: "",
    });
    expect(projectToForm({ max_concurrent_agents: 3, credit_weight: 1.5 }).max_concurrent_agents).toBe(
      "3",
    );
  });

  it("profileOptionsFromRows dedupes scoped+global by id", () => {
    const rows = [
      { scoped: { id: "coder", name: "Coder (scoped)" }, global: { id: "coder", name: "Coder" } },
      { scoped: null, global: { id: "reviewer", name: "Reviewer" } },
    ];
    const opts = profileOptionsFromRows(rows);
    expect(opts.map((o) => o.id)).toEqual(["coder", "reviewer"]);
  });
});
