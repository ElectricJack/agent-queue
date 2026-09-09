import { describe, expect, it } from "vitest";
import { relativeAge } from "../activityFormat";

describe("activity timestamps", () => {
  const now = 1_000_000;

  it("reads as an age at every unit boundary", () => {
    expect(relativeAge(now, now)).toBe("0s ago");
    expect(relativeAge(now - 59, now)).toBe("59s ago");
    expect(relativeAge(now - 60, now)).toBe("1m ago");
    expect(relativeAge(now - 3599, now)).toBe("59m ago");
    expect(relativeAge(now - 3600, now)).toBe("1h ago");
    expect(relativeAge(now - 86399, now)).toBe("23h ago");
    expect(relativeAge(now - 86400, now)).toBe("1d ago");
  });

  it("never reads as the future when a clock runs behind the daemon", () => {
    expect(relativeAge(now + 30, now)).toBe("0s ago");
  });
});
