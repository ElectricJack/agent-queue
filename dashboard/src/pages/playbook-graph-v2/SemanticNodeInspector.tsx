import { useState } from "react";
import type { CapabilityNamespacesDTO, GraphNodeDTO } from "../../api/client";
import AdvancedNodeDetail from "./AdvancedNodeDetail";
import IntentSections, { Block, Value } from "./IntentSections";
import { STEP_KIND_LABELS } from "./types";

function Pairs({ pairs }: { pairs: [string, React.ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
      {pairs.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-gray-500">{label}</dt>
          <dd className="min-w-0 break-words text-gray-200">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A namespace list. `[]` is deny-all and says so; a narrowing's `null` means
 *  "this step narrows nothing here", which is a different instruction. */
function Namespaces({
  capabilities,
  nullable = false,
}: {
  capabilities: CapabilityNamespacesDTO | { harness_tools?: string[] | null; aq_commands?: string[] | null; plugin_tools?: string[] | null };
  nullable?: boolean;
}) {
  const rows: [string, React.ReactNode][] = (
    [
      ["harness tools", capabilities.harness_tools],
      ["aq commands", capabilities.aq_commands],
      ["plugin tools", capabilities.plugin_tools],
    ] as [string, string[] | null | undefined][]
  ).map(([label, list]) => [
    label,
    list == null
      ? nullable
        ? <span className="text-gray-500">not narrowed here</span>
        : <span className="text-gray-500">—</span>
      : list.length === 0
        ? <span className="text-gray-500">none (deny-all)</span>
        : <span className="font-mono">{list.join(", ")}</span>,
  ]);
  return <Pairs pairs={rows} />;
}

export interface SemanticNodeInspectorProps {
  node: GraphNodeDTO | null;
  /** Controlled Advanced state. The view owns it so the toggle survives a
   *  selection change — an operator inspecting five nodes in Advanced mode
   *  should not have to re-open it five times. */
  advanced?: boolean;
  onAdvancedChange?: (next: boolean) => void;
}

/** Everything the artifact knows about one step, laid out rather than dumped.
 *
 *  The inspector never fetches and never re-derives command meaning: the intent
 *  block is the same `StepExplanationDTO` the card renders, and the canonical
 *  JSON lives behind Advanced. */
export default function SemanticNodeInspector({
  node,
  advanced,
  onAdvancedChange,
}: SemanticNodeInspectorProps) {
  const [uncontrolled, setUncontrolled] = useState(false);
  const showAdvanced = advanced ?? uncontrolled;
  const setShowAdvanced = onAdvancedChange ?? setUncontrolled;

  // Nothing selected: the panel is absent, not an empty box. An empty aside
  // would keep claiming space and be announced as a landmark with no content.
  if (!node) return null;

  const kindLabel = STEP_KIND_LABELS[node.step_kind] ?? node.step_kind;
  const diagnostics = node.diagnostics ?? [];

  return (
    <aside
      aria-label="Node inspector"
      className="flex h-full min-w-0 flex-col gap-3 overflow-y-auto rounded-lg border border-gray-800 bg-gray-900/50 p-4"
    >
      <header className="min-w-0 space-y-1">
        <h3 className="break-words text-sm font-semibold text-gray-100">{node.explanation.title}</h3>
        <p className="flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-gray-500">
          <span className="rounded bg-gray-800 px-1 py-0.5">{kindLabel}</span>
          <span className="font-mono lowercase tracking-normal">{node.id}</span>
          {node.entry && <span className="rounded bg-gray-800 px-1 py-0.5">entry</span>}
          {node.terminal_outcome && (
            <span className="rounded bg-gray-800 px-1 py-0.5">ends as {node.terminal_outcome}</span>
          )}
        </p>
        {node.description && <p className="text-xs text-gray-400">{node.description}</p>}
      </header>

      {diagnostics.length > 0 && (
        <Block name="Diagnostics">
          <ul className="space-y-1">
            {diagnostics.map((diagnostic, index) => (
              <li
                key={`${diagnostic.code}:${index}`}
                className="rounded border border-amber-700 bg-amber-950/50 px-2 py-1 text-[11px] text-amber-100"
              >
                <span className="font-mono text-[9px] uppercase tracking-wide text-amber-300">
                  {diagnostic.severity}/{diagnostic.code}
                </span>
                <span className="block break-words">{diagnostic.message}</span>
              </li>
            ))}
          </ul>
        </Block>
      )}

      <IntentSections explanation={node.explanation} />

      {node.ai && (
        <Block name={node.step_kind === "agent_task" ? "Delegated agent" : "AI"}>
          <Pairs
            pairs={[
              ["profile", <span className="font-mono">{node.ai.profile_id}</span>],
              ...(node.ai.intelligence_class
                ? ([["intelligence class", node.ai.intelligence_class]] as [string, React.ReactNode][])
                : []),
              ...(node.ai.provider ? ([["provider", node.ai.provider]] as [string, React.ReactNode][]) : []),
              ...(node.ai.model ? ([["model", <span className="font-mono">{node.ai.model}</span>]] as [string, React.ReactNode][]) : []),
              ["tool use", node.ai.tool_use_enabled ? "enabled" : "disabled"],
              ["capability fingerprint", <span className="break-all font-mono">{node.ai.capability_fingerprint}</span>],
            ]}
          />
          <div className="mt-2">
            <h6 className="text-[10px] uppercase tracking-wide text-gray-500">Capabilities</h6>
            <Namespaces capabilities={node.ai.capabilities} />
          </div>
          <div className="mt-2">
            <h6 className="text-[10px] uppercase tracking-wide text-gray-500">Budget</h6>
            <Pairs
              pairs={[
                ["max calls", node.ai.budget.max_calls ?? "unbounded"],
                ["max output tokens", node.ai.budget.max_output_tokens ?? "unbounded"],
                ["max total tokens", node.ai.budget.max_total_tokens ?? "unbounded"],
                ["timeout", node.ai.budget.timeout_seconds == null ? "none" : `${node.ai.budget.timeout_seconds}s`],
              ]}
            />
          </div>
          {node.ai.output_schema && (
            <div className="mt-2">
              <h6 className="text-[10px] uppercase tracking-wide text-gray-500">Output schema</h6>
              <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded border border-gray-800 bg-gray-950 p-2 font-mono text-[11px] text-gray-300">
                {JSON.stringify(node.ai.output_schema, null, 2)}
              </pre>
            </div>
          )}
          {node.ai.delegation && (
            <div className="mt-2">
              <h6 className="text-[10px] uppercase tracking-wide text-gray-500">Delegation policy</h6>
              <Pairs
                pairs={[
                  ["child profile", <span className="font-mono">{node.ai.delegation.child_profile_id}</span>],
                  ["wait for completion", node.ai.delegation.wait_for_completion ? "yes" : "no"],
                  ["cancel child", node.ai.delegation.cancel_child ? "yes" : "no"],
                  ...(node.ai.delegation.narrowed_from
                    ? ([["narrowed from", node.ai.delegation.narrowed_from]] as [string, React.ReactNode][])
                    : []),
                ]}
              />
              <p className="mt-1 text-[10px] text-gray-500">
                The child principal is parent ∩ child profile ∩ this narrowing.
              </p>
              {node.ai.delegation.capability_narrowing ? (
                <Namespaces capabilities={node.ai.delegation.capability_narrowing} nullable />
              ) : (
                <p className="text-[11px] text-gray-400">This step narrows nothing beyond the profile.</p>
              )}
            </div>
          )}
        </Block>
      )}

      {node.loop && (
        <Block name="Loop">
          <Pairs
            pairs={[
              ["collection", <Value value={node.loop.collection} />],
              ["item binding", <span className="font-mono">{node.loop.item_binding}</span>],
              ["failure policy", node.loop.failure_policy],
              ["body entry", <span className="font-mono">{node.loop.body_entry_step_id}</span>],
              ["continuation", <span className="font-mono">{node.loop.continuation_step_id ?? "—"}</span>],
            ]}
          />
        </Block>
      )}

      {node.wait && (
        <Block name="Wait">
          <Pairs
            pairs={[
              ["wait kind", node.wait.wait_kind],
              ["awaited", node.wait.awaited],
              ["correlation key", <Value value={node.wait.correlation_key} />],
              ["timeout", node.wait.timeout_seconds == null ? "none" : `${node.wait.timeout_seconds}s`],
              ["on timeout", <span className="font-mono">{node.wait.timeout_step_id ?? "—"}</span>],
            ]}
          />
        </Block>
      )}

      <Block name="Source">
        <p className="min-w-0 break-all font-mono text-[11px] text-gray-300">
          {node.source.path}:{node.source.start_line}
          {node.source.end_line !== node.source.start_line ? `-${node.source.end_line}` : ""}
        </p>
        {node.source.heading && <p className="text-[11px] text-gray-500">under “{node.source.heading}”</p>}
        {node.source.excerpt && (
          <pre className="max-h-32 overflow-auto whitespace-pre-wrap break-words rounded border border-gray-800 bg-gray-950 p-2 font-mono text-[11px] text-gray-400">
            {node.source.excerpt}
          </pre>
        )}
      </Block>

      <div className="min-w-0 border-t border-gray-800 pt-3">
        <button
          type="button"
          aria-expanded={showAdvanced}
          onClick={() => setShowAdvanced(!showAdvanced)}
          className="rounded-md bg-gray-800 px-3 py-1.5 text-xs text-gray-200 hover:bg-gray-700"
        >
          {showAdvanced ? "Hide advanced" : "Advanced"}
        </button>
        {showAdvanced && (
          <div className="mt-3">
            <AdvancedNodeDetail node={node} />
          </div>
        )}
      </div>
    </aside>
  );
}
