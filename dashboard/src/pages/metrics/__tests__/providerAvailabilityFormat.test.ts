/**
 * The availability readers map server values to words and colours; none of
 * them decides a state.
 */

import { describe, expect, it } from "vitest";
import {
  formatDuration,
  formatOverrideExpiry,
  formatRecovery,
  formatSince,
  holdKindLabel,
  intentLabel,
  isUnavailable,
  probeLabel,
  providerName,
  sortStatuses,
  stateLabel,
  stateTone,
  stateWords,
} from "../providerAvailabilityFormat";
import type { ProviderAvailabilityStatus } from "../../../api/providers";

const NOW = 1_789_200_000;

describe("provider availability readers", () => {
  it("colours green / amber / red / grey by effective state", () => {
    expect(stateTone("available")).toBe("green");
    expect(stateTone("degraded")).toBe("amber");
    expect(stateTone("exhausted")).toBe("red");
    expect(stateTone("unauthenticated")).toBe("red");
    expect(stateTone("failing")).toBe("red");
    // An operator's choice, not an alarm.
    expect(stateTone("disabled")).toBe("grey");
    expect(stateTone("something-new")).toBe("grey");
  });

  it("labels states for the pill and the banner, keeping unknown ones as named", () => {
    expect(stateLabel("unauthenticated")).toBe("Logged out");
    expect(stateLabel("disabled")).toBe("Disabled");
    expect(stateLabel("paused-by-vendor")).toBe("paused-by-vendor");
    expect(stateWords("unauthenticated")).toBe("logged out");
    expect(stateWords("exhausted")).toBe("out of usage");
    expect(stateWords("disabled")).toBe("disabled by an operator");
  });

  it("reads the half from the server, never from the state name", () => {
    expect(isUnavailable({ half: "unavailable" })).toBe(true);
    expect(isUnavailable({ half: "launchable" })).toBe(false);
  });

  it("names providers and orders them stably", () => {
    expect(providerName("codex")).toBe("Codex");
    expect(providerName("claude")).toBe("Claude");
    expect(providerName("mistral")).toBe("Mistral");
    const rows = [{ provider: "codex" }, { provider: "claude" }] as ProviderAvailabilityStatus[];
    expect(sortStatuses(rows).map((r) => r.provider)).toEqual(["claude", "codex"]);
    expect(rows.map((r) => r.provider)).toEqual(["codex", "claude"]);
  });

  it("formats a countdown coarsely", () => {
    expect(formatDuration(20)).toBe("<1m");
    expect(formatDuration(14 * 60)).toBe("14m");
    expect(formatDuration(2 * 3600 + 14 * 60)).toBe("2h 14m");
    expect(formatDuration(3 * 3600)).toBe("3h");
    expect(formatDuration(3 * 86400 + 4 * 3600)).toBe("3d 4h");
    expect(formatDuration(-5)).toBe("<1m");
  });

  it("says when recovery is expected, and when it is overdue rather than counting below zero", () => {
    expect(formatRecovery(null, NOW)).toBeNull();
    expect(formatRecovery(NOW + 5400, NOW)).toMatch(/^expected back in 1h 30m \(\d{2}:\d{2}\)$/);
    expect(formatRecovery(NOW - 60, NOW)).toMatch(/^recovery was due /);
  });

  it("formats since and override expiry", () => {
    expect(formatSince(null, NOW)).toBeNull();
    expect(formatSince(NOW - 10, NOW)).toMatch(/\(just now\)$/);
    expect(formatSince(NOW - 3 * 3600, NOW)).toMatch(/^since .+ \(3h ago\)$/);
    expect(formatOverrideExpiry(null, NOW)).toBe("no expiry");
    expect(formatOverrideExpiry(NOW + 1800, NOW)).toMatch(/^until .+ \(in 30m\)$/);
    expect(formatOverrideExpiry(NOW - 1, NOW)).toMatch(/^expired /);
  });

  it("puts every hold kind into words, with the queue position only where it means something", () => {
    expect(holdKindLabel("awaiting_failover_capacity", 3)).toBe("Waiting for failover capacity (3 ahead)");
    expect(holdKindLabel("awaiting_failover_capacity", null)).toBe("Waiting for failover capacity");
    expect(holdKindLabel("provider_pinned", 3)).toBe("Pinned to this provider — waiting for it to recover");
    for (const kind of [
      "class_policy_hold", "no_equivalent_rung", "no_available_target", "reroute_limit_reached",
      "all_providers_unavailable", "failover_inactive", "priority_policy_hold",
    ]) {
      expect(holdKindLabel(kind)).not.toContain("_");
    }
    expect(holdKindLabel("brand_new_kind")).toBe("brand new kind");
  });

  it("labels intents and probe answers", () => {
    expect(intentLabel("pinned")).toBe("Pinned");
    expect(intentLabel("preferred")).toBe("Preferred");
    expect(intentLabel("class_only")).toBe("Class only");
    expect(intentLabel(undefined)).toBe("Class only");
    expect(probeLabel("not_authenticated")).toBe("not logged in");
    expect(probeLabel("weird")).toBe("weird");
  });
});
