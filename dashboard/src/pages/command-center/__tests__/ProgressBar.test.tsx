import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ProgressBar } from "../ProgressBar";

afterEach(cleanup);

function segmentWidths(bar: HTMLElement): number[] {
  return Array.from(bar.children).map((child) => {
    const width = (child as HTMLElement).style.width;
    return Number(width.replace("%", ""));
  });
}

describe("ProgressBar", () => {
  it("renders nothing when total is zero", () => {
    const { container } = render(<ProgressBar done={0} total={0} />);
    expect(container.firstChild).toBeNull();
  });

  it("carries an accessible label of the form 'N of M done'", () => {
    render(<ProgressBar done={3} total={7} />);
    expect(screen.getByRole("progressbar", { name: "3 of 7 done" })).toBeInTheDocument();
  });

  it("sizes the done/running/blocked segments as percentages of total", () => {
    render(<ProgressBar done={2} total={4} running={1} blocked={1} />);
    const bar = screen.getByRole("progressbar");
    const [done, running, blocked] = segmentWidths(bar);
    expect(done).toBeCloseTo(50);
    expect(running).toBeCloseTo(25);
    expect(blocked).toBeCloseTo(25);
  });

  it("clamps overlapping done/running/blocked counts so the bar never overflows", () => {
    // A running task that is also blocked can push done + running + blocked
    // past total; the rendered segments must still sum to at most 100%.
    render(<ProgressBar done={5} total={5} running={3} blocked={3} />);
    const bar = screen.getByRole("progressbar");
    const widths = segmentWidths(bar);
    expect(widths.every((w) => w >= 0)).toBe(true);
    expect(widths.reduce((a, b) => a + b, 0)).toBeLessThanOrEqual(100);
  });

  it("carries no running/blocked segment width when neither is given", () => {
    render(<ProgressBar done={1} total={2} />);
    const bar = screen.getByRole("progressbar");
    const [, running, blocked] = segmentWidths(bar);
    expect(running).toBe(0);
    expect(blocked).toBe(0);
  });
});
