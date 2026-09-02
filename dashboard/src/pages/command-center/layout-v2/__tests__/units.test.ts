import { describe, expect, it } from "vitest";
import { cellDistance, cellRect, cellsForRect, maxDepthForZoom, toPx, worldRectFromViewport } from "../units";

describe("units", () => {
  it("scales units to pixels", () => {
    expect(toPx(2, 3)).toEqual({ x: 480, y: 468 });
  });
  it("maps a viewport to a world rect", () => {
    // zoom 1, translated so world (0,0) is at screen (0,0): 1920x1080 → 8x6.92 units
    const r = worldRectFromViewport({ x: 0, y: 0, zoom: 1 }, 1920, 1080);
    expect(r.x0).toBe(0); expect(r.y0).toBe(0);
    expect(r.x1).toBeCloseTo(8); expect(r.y1).toBeCloseTo(1080 / 156);
    // panned right by 240px at zoom 0.5: world x starts at (0+240)/0.5/240 = 2
    const p = worldRectFromViewport({ x: -240, y: 0, zoom: 0.5 }, 1920, 1080);
    expect(p.x0).toBeCloseTo(2); expect(p.x1).toBeCloseTo(2 + 16);
  });
  it("lists cells with padding", () => {
    expect(cellsForRect({ x0: 0, y0: 0, x1: 1, y1: 1 }, 0)).toEqual(["0:0"]);
    expect(cellsForRect({ x0: 0, y0: 0, x1: 1, y1: 1 }, 1).sort()).toEqual(
      ["-1:-1", "-1:0", "-1:1", "0:-1", "0:0", "0:1", "1:-1", "1:0", "1:1"].sort());
    expect(cellsForRect({ x0: 7.9, y0: 0, x1: 8.1, y1: 1 }, 0)).toEqual(["0:0", "1:0"]);
  });
  it("cell rect and distance", () => {
    expect(cellRect(["0:0", "1:0"])).toEqual({ x0: 0, y0: 0, x1: 16, y1: 8 });
    expect(cellDistance("0:0", "3:-2")).toBe(3);
  });
  it("lod thresholds", () => {
    expect(maxDepthForZoom(0.2)).toBe(0);
    expect(maxDepthForZoom(0.5)).toBe(1);
    expect(maxDepthForZoom(1)).toBeNull();
  });
});
