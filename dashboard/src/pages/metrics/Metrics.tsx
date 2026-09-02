/**
 * Fleet metrics over time, at the resolution the daemon samples them.
 *
 * History comes from ``GET /api/metrics/series`` once per range change;
 * everything after that is appended from ``metrics.tick`` frames on the
 * existing WebSocket, so the page updates every second without a single
 * extra request.
 */

import { useCallback, useMemo, useState } from "react";
import { ArrowPathIcon } from "@heroicons/react/24/outline";
import { RANGES, type RangeKey } from "../../api/metrics";
import { useEventStreamStatus } from "../../ws/EventStreamProvider";
import StatTiles from "./StatTiles";
import TimeSeriesChart from "./TimeSeriesChart";
import { buildCharts } from "./series";
import { useMetricsFeed } from "./useMetricsFeed";

const RANGE_KEYS = Object.keys(RANGES) as RangeKey[];

const STEP_LABEL: Record<string, string> = {
  "1s": "1-second samples",
  "1m": "1-minute averages",
  "1h": "hourly averages",
};

export default function Metrics() {
  const [range, setRange] = useState<RangeKey>("1h");
  const [hidden, setHidden] = useState<Record<string, Set<string>>>({});
  const feed = useMetricsFeed(range);
  const status = useEventStreamStatus();

  const charts = useMemo(() => buildCharts(feed.samples), [feed.samples]);

  const toggle = useCallback((chartId: string, seriesKey: string) => {
    setHidden((prev) => {
      const next = new Set(prev[chartId] ?? []);
      if (next.has(seriesKey)) next.delete(seriesKey);
      else next.add(seriesKey);
      return { ...prev, [chartId]: next };
    });
  }, []);

  return (
    <div className="dashboard-scrollbar h-full space-y-5 overflow-y-auto p-5">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-gray-100">Metrics</h1>
          <p className="text-xs text-gray-500">
            {STEP_LABEL[feed.step] ?? feed.step} ·{" "}
            {status === "connected"
              ? "live"
              : status === "connecting"
                ? "reconnecting — chart is paused"
                : "disconnected — showing the last fetch"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div
            role="group"
            aria-label="Time range"
            className="flex overflow-hidden rounded-lg border border-gray-800"
          >
            {RANGE_KEYS.map((key) => (
              <button
                key={key}
                type="button"
                onClick={() => setRange(key)}
                aria-pressed={range === key}
                className={`px-3 py-1.5 text-xs ${
                  range === key
                    ? "bg-indigo-500/20 text-indigo-200"
                    : "text-gray-400 hover:bg-gray-800 hover:text-gray-100"
                }`}
              >
                {key}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={() => feed.refetch()}
            aria-label="Reload history"
            className="rounded-lg border border-gray-800 p-2 text-gray-400 hover:bg-gray-800 hover:text-gray-100"
          >
            <ArrowPathIcon className="h-4 w-4" />
          </button>
        </div>
      </header>

      {feed.isError && (
        <p className="rounded-lg border border-red-900/60 bg-red-950/40 p-3 text-sm text-red-200">
          Could not load metrics history: {String((feed.error as Error)?.message ?? feed.error)}
        </p>
      )}

      <StatTiles sample={feed.latest} />

      {feed.isLoading ? (
        <p className="text-sm text-gray-500">Loading history…</p>
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          {charts.map((chart) => (
            <div key={chart.id} className="space-y-2">
              <TimeSeriesChart
                title={chart.title}
                unit={chart.unit}
                samples={feed.samples as unknown as Array<Record<string, unknown>>}
                series={chart.series}
                hidden={hidden[chart.id]}
              />
              <div className="flex flex-wrap gap-1.5 px-1">
                {chart.series.map((series) => {
                  const off = hidden[chart.id]?.has(series.key) ?? false;
                  return (
                    <button
                      key={series.key}
                      type="button"
                      onClick={() => toggle(chart.id, series.key)}
                      aria-pressed={!off}
                      className={`flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] ${
                        off
                          ? "border-gray-800 text-gray-600"
                          : "border-gray-700 text-gray-300 hover:border-gray-600"
                      }`}
                    >
                      <span
                        aria-hidden
                        className="h-2 w-2 rounded-full"
                        style={{ backgroundColor: off ? "transparent" : series.color,
                                 border: `1px solid ${series.color}` }}
                      />
                      {series.label}
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
