import { describe, expect, it } from "vitest";
import { senderLabel } from "./senderLabel";

describe("senderLabel", () => {
  it.each([
    [{ from_kind: "user", from_id: "dashboard" }, "You"],
    [{ from_kind: "user", from_id: "discord:111111111111111111", body_kind: "conversation_input" }, "Discord · 111111111111111111 (verified)"],
    [{ from_kind: "session", from_id: "supervisor-global" }, "Agent Q"],
    [{ from_kind: "session", from_id: "worker-1" }, "worker-1"],
    [{ from_kind: "session", from_id: "discord:111" }, "discord:111"],
    [{ from_kind: "user", from_id: "someone" }, "someone"],
  ])("labels %j", (message, expected) => {
    expect(senderLabel(message)).toBe(expected);
  });
});
