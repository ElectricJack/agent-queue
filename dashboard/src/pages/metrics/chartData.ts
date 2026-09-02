/**
 * The data contract between a sample and a chart line.
 *
 * Split out of `TimeSeriesChart.tsx` so the shape can be built and tested
 * without importing uPlot (which touches `matchMedia` and a canvas at import
 * time), and so the component file exports only a component.
 */

export interface Series {
  key: string;
  label: string;
  color: string;
  /** Pulls this series' value out of one sample. */
  value: (sample: Record<string, unknown>) => number | null;
}

/** uPlot's `[xs, ...ys]` layout: one parallel array per series. */
export type AlignedData = [number[], ...Array<Array<number | null>>];

export function toAlignedData(
  samples: Array<Record<string, unknown>>,
  series: Series[],
): AlignedData {
  const xs: number[] = [];
  const ys: Array<Array<number | null>> = series.map(() => []);
  for (const sample of samples) {
    xs.push(Number(sample.ts ?? 0));
    series.forEach((definition, index) => {
      const raw = definition.value(sample);
      // A gap is real: a series with no reading for a bucket must be null,
      // not zero, or the line is drawn through a value nobody measured.
      ys[index]?.push(raw == null || Number.isNaN(raw) ? null : raw);
    });
  }
  return [xs, ...ys];
}
