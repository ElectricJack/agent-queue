import { describe, expect, it } from "vitest";
import {
  FOCUS_CONVERSATIONS,
  FOCUS_INBOX,
  focusBatchHref,
  focusConversationHref,
  focusEscalationHref,
  focusInboxHref,
  focusReportHref,
  focusReviewHref,
  focusSessionHref,
  focusTaskHref,
  isFocusPath,
} from "../routes";

describe("focus routes", () => {
  it("builds the canonical paths", () => {
    expect(focusTaskHref("stark-impact-60.1")).toBe("/focus/tasks/stark-impact-60.1");
    expect(focusTaskHref("a/b")).toBe("/focus/tasks/a%2Fb");
    expect(focusSessionHref("supervisor-global")).toBe("/focus/sessions/supervisor-global");
    expect(focusSessionHref("s1", { started: 1790000000 })).toBe("/focus/sessions/s1?started=1790000000");
    expect(focusReportHref("morning-2026-09-25")).toBe("/focus/reports/morning-2026-09-25");
    expect(focusReviewHref("rev-123")).toBe("/focus/reviews/rev-123");
    expect(focusEscalationHref("escalation-abc")).toBe("/focus/escalations/escalation-abc");
    expect(focusBatchHref("batch-77")).toBe("/focus/batches/batch-77");
    expect(focusConversationHref("conv-1")).toBe("/focus/conversations/conv-1");
  });

  it("escapes an id that would otherwise change the route", () => {
    expect(focusReviewHref("a/b")).toBe("/focus/reviews/a%2Fb");
    expect(focusEscalationHref("../inbox")).toBe("/focus/escalations/..%2Finbox");
    expect(focusBatchHref("b 1")).toBe("/focus/batches/b%201");
    expect(focusConversationHref("c?x=1")).toBe("/focus/conversations/c%3Fx%3D1");
  });

  it("addresses the needs-you page and the conversation list", () => {
    expect(focusInboxHref()).toBe("/focus/inbox");
    expect(focusInboxHref()).toBe(FOCUS_INBOX);
    expect(FOCUS_CONVERSATIONS).toBe("/focus/conversations");
  });

  it("recognises only the focus tree", () => {
    expect(isFocusPath("/focus")).toBe(true);
    expect(isFocusPath("/focus/tasks/x")).toBe(true);
    expect(isFocusPath("/focus/inbox")).toBe(true);
    expect(isFocusPath("/focused")).toBe(false);
    expect(isFocusPath("/tasks/x")).toBe(false);
  });
});