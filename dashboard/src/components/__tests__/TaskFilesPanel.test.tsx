import { describe, expect, it } from "vitest";
import { statusColor } from "../TaskFilesPanel";

describe("statusColor", () => {
  it("colors Added green", () => {
    expect(statusColor("A")).toBe("text-green-400");
  });

  it("colors Deleted red", () => {
    expect(statusColor("D")).toBe("text-red-400");
  });

  it("colors Renamed and Copied blue", () => {
    expect(statusColor("R")).toBe("text-blue-400");
    expect(statusColor("C")).toBe("text-blue-400");
  });

  it("colors Modified and unknown statuses amber", () => {
    expect(statusColor("M")).toBe("text-amber-300");
    expect(statusColor("?")).toBe("text-amber-300");
  });
});
