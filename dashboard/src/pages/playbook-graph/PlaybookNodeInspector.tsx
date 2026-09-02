import type { ReactNode } from "react";
import type { PlaybookNodeLlmConfig, PlaybookTransitionDetail } from "../../api/client";
import NodeExplanationCard from "./NodeExplanationCard";
import type { ExplainedPlaybookGraphNode } from "./explanation";
import { NODE_TYPE_LABELS } from "./types";

/** A labelled block. Every compiled field lives inside one of these, so the
 *  inspector never degrades into an unlabelled JSON dump. */
function Section({ name, children }: { name: string; children: ReactNode }) {
  return (
    <section role="group" aria-label={name} className="min-w-0 space-y-1">
      <h4 className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">{name}</h4>
      {children}
    </section>
  );
}

/** Compact formatted JSON for a nested payload — wraps inside the panel and
 *  never widens the page. */
function Payload({ value }: { value: unknown }) {
  return (
    <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded border border-gray-800 bg-gray-950 p-2 font-mono text-[11px] leading-4 text-gray-300">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function Rows({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-gray-500">{label}</dt>
          <dd className="min-w-0 break-words font-mono text-gray-200">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function llmRows(config: PlaybookNodeLlmConfig): [string, ReactNode][] {
  const rows: [string, ReactNode][] = [];
  if (config.provider) rows.push(["provider", config.provider]);
  if (config.model) rows.push(["model", config.model]);
  if (config.max_tokens != null) rows.push(["max tokens", String(config.max_tokens)]);
  if (config.temperature != null) rows.push(["temperature", String(config.temperature)]);
  return rows;
}

/** Conditions are authored as a string, but the compiler also accepts a
 *  structured predicate — render whichever arrived without inventing text. */
function condition(transition: PlaybookTransitionDetail) {
  if (transition.otherwise) return "otherwise";
  if (typeof transition.when === "string") return transition.when;
  if (transition.when) return JSON.stringify(transition.when);
  return "always";
}

export interface PlaybookNodeInspectorProps {
  node: ExplainedPlaybookGraphNode | null;
}

/** Read-only view of one compiled node's intent and `details`.
 *
 *  Everything here comes from the graph-view response — the inspector never
 *  fetches, and never reconstructs configuration from the rendered edges.
 *  Absent optional fields are omitted entirely rather than shown as `null`. */
export default function PlaybookNodeInspector({ node }: PlaybookNodeInspectorProps) {
  if (!node) {
    return (
      <aside
        aria-label="Node inspector"
        className="flex h-full min-w-0 items-center justify-center rounded-lg border border-gray-800 bg-gray-900/50 p-4"
      >
        <p className="text-center text-sm text-gray-500">
          Select a node to inspect its compiled configuration.
        </p>
      </aside>
    );
  }

  const d = node.details;
  const flags = [
    node.entry || d.entry ? "entry" : null,
    node.terminal || d.terminal ? "terminal" : null,
    node.wait_for_human || d.wait_for_human ? "human checkpoint" : null,
  ].filter((flag): flag is string => flag !== null);

  const transitions = d.transitions ?? [];
  const timeoutRows: [string, ReactNode][] = [];
  if (d.timeout_seconds != null) timeoutRows.push(["execution", `${d.timeout_seconds}s`]);
  if (d.pause_timeout_seconds != null) timeoutRows.push(["pause", `${d.pause_timeout_seconds}s`]);
  if (d.on_timeout) timeoutRows.push(["on timeout", d.on_timeout]);

  /* Contract-derived intent replaces the raw action as the default reading of
   * the node. The compiled payloads are never dropped — they move under the
   * Advanced disclosure, which opens by default for an uncontracted node so
   * that node (and the flag-off path) reads exactly as it does today. */
  const explanation = node.explanation ?? null;
  const hasRawPayloads = Boolean(d.action || d.for_each || d.output);

  const nodeLlm = d.llm_config ? llmRows(d.llm_config) : [];
  const transitionLlm = d.transition_llm_config ? llmRows(d.transition_llm_config) : [];

  return (
    <aside
      aria-label="Node inspector"
      className="h-full max-h-full min-w-0 space-y-4 overflow-y-auto rounded-lg border border-gray-800 bg-gray-900/50 p-4"
    >
      <Section name="Identity">
        <h3 className="break-all font-mono text-sm font-semibold text-gray-100">{node.id}</h3>
        <p className="text-xs text-gray-400">{NODE_TYPE_LABELS[node.type] ?? node.type}</p>
      </Section>

      {flags.length > 0 && (
        <Section name="Flags">
          <ul className="flex flex-wrap gap-1">
            {flags.map((flag) => (
              <li key={flag} className="rounded bg-gray-800 px-1.5 py-0.5 text-[11px] text-gray-200">
                {flag}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {d.prompt && (
        <Section name="Prompt">
          <p className="max-h-80 overflow-y-auto whitespace-pre-wrap break-words rounded border border-gray-800 bg-gray-950 p-2 text-xs leading-5 text-gray-200">
            {d.prompt}
          </p>
        </Section>
      )}

      {transitions.length > 0 && (
        <Section name="Transitions">
          <ul className="space-y-1">
            {transitions.map((transition, index) => (
              <li
                key={`${index}:${transition.goto}`}
                className="flex min-w-0 items-baseline gap-2 rounded border border-gray-800 bg-gray-950 px-2 py-1 text-xs"
              >
                <span className="min-w-0 flex-1 break-words font-mono text-amber-200">
                  {condition(transition)}
                </span>
                <span aria-hidden className="text-gray-600">
                  →
                </span>
                <span className="break-all font-mono text-gray-200">{transition.goto}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {transitions.length === 0 && d.goto && (
        <Section name="Goto">
          <p className="break-all font-mono text-xs text-gray-200">{d.goto}</p>
        </Section>
      )}

      {explanation && (
        <Section name="Intent">
          <NodeExplanationCard explanation={explanation} />
        </Section>
      )}

      {hasRawPayloads && (
        <details open={!explanation} className="min-w-0 space-y-2">
          <summary className="cursor-pointer text-[10px] font-semibold uppercase tracking-wide text-gray-500">
            Advanced
          </summary>

          {d.action && (
            <Section name="Action">
              <Payload value={d.action} />
            </Section>
          )}

          {d.for_each && (
            <Section name="For each">
              <Payload value={d.for_each} />
            </Section>
          )}

          {d.output && (
            <Section name="Output">
              <Payload value={d.output} />
            </Section>
          )}
        </details>
      )}

      {timeoutRows.length > 0 && (
        <Section name="Timeouts">
          <Rows rows={timeoutRows} />
        </Section>
      )}

      {nodeLlm.length > 0 && (
        <Section name="LLM">
          <Rows rows={nodeLlm} />
        </Section>
      )}

      {transitionLlm.length > 0 && (
        <Section name="Transition LLM">
          <Rows rows={transitionLlm} />
        </Section>
      )}
    </aside>
  );
}
