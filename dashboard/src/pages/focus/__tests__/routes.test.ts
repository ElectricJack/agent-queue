import { describe, expect, it } from "vitest";
import { focusReportHref, focusSessionHref, focusTaskHref, isFocusPath } from "../routes";

describe("focus routes", () => {
  it("builds the canonical paths", () => {
    expect(focusTaskHref("stark-impact-60.1")).toBe("/focus/tasks/stark-impact-60.1");
    expect(focusTaskHref("a/b")).toBe("/focus/tasks/a%2Fb");
    expect(focusSessionHref("supervisor-global")).toBe("/focus/sessions/supervisor-global");
    expect(focusSessionHref("s1", { started: 1790000000 })).toBe("/focus/sessions/s1?started=1790000000");
    expect(focusReportHref("morning-2026-09-25")).toBe("/focus/reports/morning-2026-09-25");
  });

  it("recognises only the focus tree", () => {
    expect(isFocusPath("/focus")).toBe(true);
    expect(isFocusPath("/focus/tasks/x")).toBe(true);
    expect(isFocusPath("/focused")).toBe(false);
    expect(isFocusPath("/tasks/x")).toBe(false);
  });
});
