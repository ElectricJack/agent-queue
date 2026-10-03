import { describe, expect, it } from "vitest";
import {
  hasActiveKnowledgeFilters,
  readKnowledgeFilters,
  readKnowledgeSelection,
  writeKnowledgeFilters,
  writeKnowledgeSelection,
} from "../knowledgeUrlState";
import { DEFAULT_KNOWLEDGE_FILTERS } from "../model";

describe("knowledge URL filters", () => {
  it("writes the defaults as no parameters and reads them back", () => {
    const params = writeKnowledgeFilters(new URLSearchParams("other=1"), DEFAULT_KNOWLEDGE_FILTERS);
    expect(params.toString()).toBe("other=1");
    expect(readKnowledgeFilters(params)).toEqual(DEFAULT_KNOWLEDGE_FILTERS);
    expect(hasActiveKnowledgeFilters(DEFAULT_KNOWLEDGE_FILTERS)).toBe(false);
  });

  it("round-trips every facet, spelling the empty lifecycle as any", () => {
    const filters = { query: "json columns", category: "incident", lifecycle: "", verification: "disputed" } as const;
    const params = writeKnowledgeFilters(new URLSearchParams(), filters);
    expect(Object.fromEntries(params)).toEqual({
      q: "json columns", category: "incident", lifecycle: "any", verification: "disputed",
    });
    expect(readKnowledgeFilters(params)).toEqual(filters);
    expect(hasActiveKnowledgeFilters(filters)).toBe(true);
  });

  it("drops unknown facet values instead of widening or narrowing silently", () => {
    const params = new URLSearchParams("category=rumour&lifecycle=zombie&verification=maybe");
    expect(readKnowledgeFilters(params)).toEqual({ ...DEFAULT_KNOWLEDGE_FILTERS });
    expect(readKnowledgeFilters(new URLSearchParams("lifecycle=retired")).lifecycle).toBe("retired");
  });

  it("addresses a record and optionally a pinned revision", () => {
    const params = writeKnowledgeSelection(new URLSearchParams(), { recordId: "r1", revisionId: "rev-1-2" });
    expect(Object.fromEntries(params)).toEqual({ record: "r1", revision: "rev-1-2" });
    expect(readKnowledgeSelection(params)).toEqual({ recordId: "r1", revisionId: "rev-1-2" });

    const cleared = writeKnowledgeSelection(params, { recordId: null, revisionId: "rev-1-2" });
    expect(cleared.toString()).toBe("");
    // A revision without its record addresses nothing.
    expect(readKnowledgeSelection(new URLSearchParams("revision=rev-1-2"))).toEqual({ recordId: null, revisionId: null });
  });
});
