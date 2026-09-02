/**
 * A uPlot time-series chart, wrapped just enough to be a React component.
 *
 * Why uPlot rather than Recharts (no chart library was installed before this
 * page): the tab shows up to an hour of one-second samples — 3,600 points per
 * series across eight or so series — and appends a point every second.
 * Recharts renders SVG, one DOM node per point, and re-renders through React
 * on every data change; that is tens of thousands of nodes and a full
 * reconciliation pass at 1 Hz.  uPlot draws to a canvas, ships ~45 KB
 * minified against Recharts' ~500 KB with its d3 dependencies, and exposes
 * `setData` as an imperative call, so a live append repaints the canvas
 * without touching the React tree at all.
 *
 * The cost of that choice is this file: uPlot owns its own DOM, so the
 * instance is created once in an effect and fed by ref afterwards.
 */

import { useEffect, useMemo, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { toAlignedData, type Series } from "./chartData";

interface Props {
  title: string;
  samples: Array<Record<string, unknown>>;
  series: Series[];
  /** Series keys the user has switched off. */
  hidden?: ReadonlySet<string>;
  height?: number;
  /** Shown beside the chart title. */
  unit?: string;
}

export default function TimeSeriesChart({
  title,
  samples,
  series,
  hidden,
  height = 200,
  unit,
}: Props) {
  const holder = useRef<HTMLDivElement | null>(null);
  const plot = useRef<uPlot | null>(null);
  const data = useMemo(
    () => toAlignedData(samples, series) as uPlot.AlignedData,
    [samples, series],
  );

  // Recreate only when the *shape* changes (which series exist, how tall).
  // A data change goes through setData below and never rebuilds the canvas.
  const shapeKey = series.map((s) => `${s.key}:${s.color}`).join("|");

  useEffect(() => {
    const node = holder.current;
    if (!node) return;
    const options: uPlot.Options = {
      title: "",
      width: node.clientWidth || 600,
      height,
      // Legends live outside the canvas as toggle chips, so uPlot's own
      // legend would only duplicate them and steal vertical space.
      legend: { show: false },
      cursor: { drag: { x: true, y: false }, focus: { prox: 24 } },
      scales: { x: { time: true } },
      axes: [
        { stroke: "#9ca3af", grid: { stroke: "#37415155" }, ticks: { stroke: "#37415155" } },
        {
          stroke: "#9ca3af",
          grid: { stroke: "#37415155" },
          ticks: { stroke: "#37415155" },
          size: 52,
        },
      ],
      series: [
        {},
        ...series.map((s) => ({
          label: s.label,
          stroke: s.color,
          width: 1.5,
          points: { show: false },
          // Gaps are real: a series with no reading for a bucket must not be
          // drawn as a line through zero.
          spanGaps: false,
        })),
      ],
    };
    const instance = new uPlot(options, data, node);
    plot.current = instance;

    const observer = new ResizeObserver(() => {
      instance.setSize({ width: node.clientWidth || 600, height });
    });
    observer.observe(node);
    return () => {
      observer.disconnect();
      instance.destroy();
      plot.current = null;
    };
    // `data` is deliberately absent: it is pushed imperatively below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shapeKey, height]);

  useEffect(() => {
    plot.current?.setData(data);
  }, [data]);

  // `series` is rebuilt every render (new closures over the newest samples),
  // so depending on its identity would re-run `setSeries` — and therefore a
  // uPlot redraw per line — on every one-second tick.  The visibility state
  // only actually changes when the shape or the hidden set does.
  const hiddenKey = hidden ? [...hidden].sort().join("|") : "";
  useEffect(() => {
    const instance = plot.current;
    if (!instance) return;
    series.forEach((s, index) => {
      instance.setSeries(index + 1, { show: !hidden?.has(s.key) });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hiddenKey, shapeKey]);

  return (
    <section className="rounded-xl border border-gray-800 bg-gray-900/70 p-4">
      <header className="mb-3 flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-medium text-gray-200">{title}</h2>
        {unit && <span className="text-xs text-gray-500">{unit}</span>}
      </header>
      <div ref={holder} className="aq-uplot w-full" data-testid={`chart-${title}`} />
      {samples.length === 0 && (
        <p className="mt-2 text-xs text-gray-500">No samples in this range yet.</p>
      )}
    </section>
  );
}
