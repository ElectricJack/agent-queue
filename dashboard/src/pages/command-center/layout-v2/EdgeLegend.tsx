import { edgeStyleForType } from "./edgeStyle";

/** The four edge types the layout engine draws (`DRAWN_TYPES` in
 *  src/task_graph/layout/constants.py), in the legend's reading order. */
const LEGEND: ReadonlyArray<readonly [depType: string, label: string]> = [
  ["blocks", "blocks"],
  ["waits-for", "waits for"],
  ["conditional-blocks", "conditional"],
  ["discovered-from", "discovered from"],
];

/**
 * §2.3: one line, bottom-left, always visible. Every edge is the same neutral
 * stroke, so the dash pattern is the only thing that tells the types apart and
 * the legend names all four whether or not the view holds one of each.
 */
export function EdgeLegend() {
  return (
    <div data-edge-legend className="flex flex-wrap items-center gap-x-3.5 gap-y-1 rounded-lg border border-g-border bg-g-panel px-2.5 py-1.5 font-g text-[11px] text-g-muted">
      {LEGEND.map(([depType, label]) => (
        <span key={depType} className="flex items-center gap-1.5">
          <svg aria-hidden width="26" height="8">
            <path d="M1 4H25" fill="none" style={edgeStyleForType(depType)} />
          </svg>
          {label}
        </span>
      ))}
      <span className="text-g-dim">arrows point to the dependent task · ×N folds links from hidden tasks</span>
    </div>
  );
}
