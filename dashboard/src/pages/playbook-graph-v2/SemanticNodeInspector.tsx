import { useEffect, useMemo, useState } from "react";
import type { CapabilityNamespacesDTO, GraphNodeDTO, NodeOverlayDTO, ReceiptDTO } from "../../api/client";
import AdvancedNodeDetail from "./AdvancedNodeDetail";
import IntentSections, { Block, Pairs, Value } from "./IntentSections";
import ReceiptDetail, { ReceiptChooser, formatDuration, receiptInIteration } from "./ReceiptDetail";
import { NODE_RUN_STATE_LABELS, STEP_KIND_LABELS } from "./types";

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
  /** This step's row of the applied run overlay. Absent when no run is
   *  selected or when the run pinned a different artifact — the inspector
   *  never invents run state for an artifact the run did not execute. */
  overlay?: NodeOverlayDTO | null;
  /** The run's receipts. Passed whole and filtered here by step, so the view
   *  does not have to keep a second index of the same response. */
  receipts?: ReceiptDTO[];
  /** The run the overlay belongs to. Only the run id resets the receipt
   *  selection: a live run re-fetches its overlay every few seconds, and
   *  resetting on the response object would clear the operator's selection
   *  under them on every poll. */
  runId?: string;
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
  overlay,
  receipts,
  runId,
}: SemanticNodeInspectorProps) {
  const [uncontrolled, setUncontrolled] = useState(false);
  const showAdvanced = advanced ?? uncontrolled;
  const setShowAdvanced = onAdvancedChange ?? setUncontrolled;

  // Which receipt of this step is open, and which iteration narrowed the list
  // down to it. Both are per-node: selecting a different step starts over.
  const [selectedReceiptId, setSelectedReceiptId] = useState<string | null>(null);
  const [selectedIteration, setSelectedIteration] = useState<number | null>(null);
  const stepId = node?.id;
  useEffect(() => {
    setSelectedReceiptId(null);
    setSelectedIteration(null);
  }, [stepId, runId]);

  const iterations = overlay?.iterations ?? [];
  const stepReceipts = useMemo(
    () => (receipts ?? []).filter((receipt) => receipt.step_id === stepId),
    [receipts, stepId],
  );
  const iteration =
    selectedIteration == null ? undefined : iterations.find((it) => it.index === selectedIteration);
  // Narrowing that would empty the list is not applied: a step whose receipts
  // belong to no iteration still has receipts worth reading.
  const narrowed = stepReceipts.filter((receipt) => receiptInIteration(receipt, iteration));
  const shownReceipts = narrowed.length > 0 ? narrowed : stepReceipts;
  // Resolved against the whole run, not just this step: a loop iteration's
  // receipts are recorded against the body step, so an iteration chosen on the
  // `foreach` node opens a receipt that belongs to a different step id.
  const selectedReceipt = (receipts ?? []).find((receipt) => receipt.receipt_id === selectedReceiptId) ?? null;

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

      {overlay && (
        <Block name="Run">
          <Pairs
            pairs={[
              ["state", NODE_RUN_STATE_LABELS[overlay.state ?? "not_visited"] ?? overlay.state],
              ["visits", overlay.visit_count ?? 0],
              ["last outcome", overlay.last_outcome ?? <span className="text-gray-500">—</span>],
            ]}
          />

          {iterations.length > 0 && (
            <div className="mt-2 space-y-1">
              <h6 className="text-[10px] uppercase tracking-wide text-gray-500">Iterations</h6>
              <div role="group" aria-label="Iterations" className="flex flex-wrap gap-1">
                {iterations.map((iteration) => {
                  const chosen = selectedIteration === iteration.index;
                  return (
                    <button
                      type="button"
                      key={iteration.index}
                      aria-pressed={chosen}
                      aria-label={`Iteration ${iteration.index}: ${iteration.item_display}`}
                      onClick={() => {
                        setSelectedIteration(iteration.index);
                        setSelectedReceiptId(iteration.receipt_ids?.[0] ?? null);
                      }}
                      className={`rounded px-2 py-1 text-[11px] ${
                        chosen ? "bg-indigo-600 text-white" : "bg-gray-800 text-gray-200 hover:bg-gray-700"
                      }`}
                    >
                      {`${iteration.index}: ${iteration.item_display}`}
                      {iteration.outcome ? ` · ${iteration.outcome}` : ""}
                      {iteration.started_at != null && iteration.completed_at != null
                        ? ` · ${formatDuration(iteration.completed_at - iteration.started_at)}`
                        : ""}
                    </button>
                  );
                })}
              </div>
              {selectedIteration != null && (
                <button
                  type="button"
                  onClick={() => {
                    setSelectedIteration(null);
                    setSelectedReceiptId(null);
                  }}
                  className="text-[10px] text-gray-400 underline"
                >
                  Show every iteration&rsquo;s receipts
                </button>
              )}
            </div>
          )}

          <div className="mt-2 space-y-2">
            {stepReceipts.length > 0 ? (
              <ReceiptChooser
                receipts={shownReceipts}
                selectedId={selectedReceiptId}
                onSelect={(receiptId) => {
                  setSelectedReceiptId(receiptId);
                  const iterationIndex = stepReceipts.find((r) => r.receipt_id === receiptId)?.iteration_index;
                  setSelectedIteration(iterationIndex ?? null);
                }}
              />
            ) : (
              <p className="text-[11px] text-gray-500">
                No receipt for this step came back with the overlay.
              </p>
            )}
            {(stepReceipts.length > 0 || selectedReceiptId) && (
              <ReceiptDetail
                receipt={selectedReceipt}
                missingReceiptId={selectedReceiptId && !selectedReceipt ? selectedReceiptId : null}
              />
            )}
          </div>
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
