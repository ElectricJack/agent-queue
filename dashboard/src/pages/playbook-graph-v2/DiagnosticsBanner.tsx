import type { GraphDiagnosticDTO } from "../../api/client";

const SEVERITY_TONES: Record<string, string> = {
  error: "border-red-600 bg-red-950/60 text-red-100",
  warning: "border-amber-600 bg-amber-950/60 text-amber-100",
  question: "border-indigo-500 bg-indigo-950/60 text-indigo-100",
  info: "border-gray-700 bg-gray-900/60 text-gray-200",
};

const SEVERITY_ORDER = ["error", "warning", "question", "info"];

export interface DiagnosticsBannerProps {
  diagnostics: GraphDiagnosticDTO[];
  /** Jump to the offending step, when the diagnostic names one. */
  onSelectNode?: (stepId: string) => void;
}

/** Compile questions, invalid references, stale contracts and disabled
 *  activations.
 *
 *  Diagnostics annotate the graph; they never hide it. This banner sits above
 *  the canvas and the canvas renders every node regardless of what is in here —
 *  an operator with a broken artifact still needs to see what it says. */
export default function DiagnosticsBanner({ diagnostics, onSelectNode }: DiagnosticsBannerProps) {
  if (diagnostics.length === 0) return null;
  const ordered = [...diagnostics].sort(
    (a, b) => SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity),
  );

  return (
    <section aria-label="Graph diagnostics" className="min-w-0 space-y-1">
      <ul className="space-y-1">
        {ordered.map((diagnostic, index) => (
          <li
            key={`${diagnostic.code}:${diagnostic.step_id ?? diagnostic.rule_id ?? index}`}
            className={`flex min-w-0 flex-wrap items-baseline gap-2 rounded border px-2 py-1 text-xs ${SEVERITY_TONES[diagnostic.severity] ?? SEVERITY_TONES.info}`}
          >
            <span className="shrink-0 rounded bg-black/30 px-1 font-mono text-[9px] uppercase tracking-wide">
              {diagnostic.severity}
            </span>
            <span className="shrink-0 font-mono text-[10px] opacity-80">{diagnostic.code}</span>
            <span className="min-w-0 break-words">{diagnostic.message}</span>
            {diagnostic.step_id && onSelectNode && (
              <button
                type="button"
                onClick={() => onSelectNode(diagnostic.step_id!)}
                className="shrink-0 rounded bg-black/30 px-1.5 py-0.5 font-mono text-[10px] underline-offset-2 hover:underline"
              >
                {diagnostic.step_id}
              </button>
            )}
            {diagnostic.source && (
              <span className="shrink-0 font-mono text-[10px] opacity-70">
                {diagnostic.source.path}:{diagnostic.source.start_line}
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
