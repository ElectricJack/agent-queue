import { useMemo, useState } from "react";
import type {
  LoopIterationOverlayDTO,
  PlaybookRunOverlayResponse,
  ReceiptDTO,
} from "../../api/client";
import ReceiptDetail, {
  ReceiptChooser,
  formatClock,
  formatDuration,
  receiptInIteration,
} from "./ReceiptDetail";
import { NODE_RUN_STATE_LABELS } from "./types";

/** A run has one selected receipt at a time. An iteration click is a shortcut
 *  to that iteration's first receipt plus a panel-wide narrowing: the receipts
 *  of a loop iteration are recorded against the *body* steps, not against the
 *  `foreach` node whose overlay lists the iterations, so the narrowing has to
 *  reach rows other than the one that was clicked. */
type Selection = {
  /** The run the selection was made in. Switching runs in the picker must not
   *  carry a receipt id into a run that never recorded it. */
  runId: string;
  iterationStepId: string | null;
  iterationIndex: number | null;
  receiptId: string | null;
};

/** Choosing a receipt directly clears any iteration narrowing: the receipt is
 *  the answer, and a filter left behind would hide its siblings. */
const EMPTY_ITERATION = { iterationStepId: null, iterationIndex: null } as const;

function shortSha(sha: string): string {
  return sha.replace(/^sha256:/, "").slice(0, 12);
}

function iterationSummary(iteration: LoopIterationOverlayDTO): string {
  const parts = [`Iteration ${iteration.index}: ${iteration.item_display}`];
  if (iteration.outcome) parts.push(iteration.outcome);
  if (iteration.started_at != null && iteration.completed_at != null) {
    parts.push(formatDuration(iteration.completed_at - iteration.started_at));
  }
  return parts.join(" · ");
}

export interface RunOverlayPanelProps {
  overlay?: PlaybookRunOverlayResponse;
}

/** The run half of the Package 5 operator surface: the pinned-artifact banner,
 *  per-node run state, the loop iteration list, and the detail of whichever
 *  receipt is selected.
 *
 *  The graph is the artifact and shows one definition node per step; this panel
 *  is where a step visited five times becomes five iterations and however many
 *  attempts, each with its own inspectable receipt. */
export default function RunOverlayPanel({ overlay }: RunOverlayPanelProps) {
  const [chosen, setChosen] = useState<Selection | null>(null);
  // A selection belongs to the run it was made in and to no other.
  const selection = chosen && chosen.runId === overlay?.run_id ? chosen : null;

  const receiptsById = useMemo(
    () => new Map<string, ReceiptDTO>((overlay?.receipts ?? []).map((receipt) => [receipt.receipt_id, receipt])),
    [overlay],
  );

  if (!overlay) return <p className="text-sm text-gray-500">Select a run to inspect its exact artifact overlay.</p>;

  const nodes = overlay.nodes ?? [];
  // The iteration a click narrowed the panel to, resolved back out of the node
  // that owns it so every row can test its own receipts against it.
  const selectedIteration =
    selection?.iterationStepId != null && selection.iterationIndex != null
      ? (nodes
          .find((node) => node.step_id === selection.iterationStepId)
          ?.iterations ?? []).find((iteration) => iteration.index === selection.iterationIndex)
      : undefined;
  const selectedReceipt = selection?.receiptId ? (receiptsById.get(selection.receiptId) ?? null) : null;
  const runDuration =
    overlay.started_at != null && overlay.completed_at != null
      ? overlay.completed_at - overlay.started_at
      : null;

  return (
    <section aria-label="Run overlay" className="min-w-0 space-y-3 rounded-lg border border-gray-800 bg-gray-900 p-4">
      {!overlay.artifact_is_active && (
        <p role="status" className="break-all rounded bg-amber-500/10 p-2 text-sm text-amber-200">
          This run used an older artifact: {overlay.artifact.artifact_sha256}
        </p>
      )}

      <dl className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-400">
        <div className="flex items-center gap-1">
          <dt className="text-gray-500">run</dt>
          <dd className="font-mono text-gray-300">{overlay.run_id}</dd>
        </div>
        <div className="flex items-center gap-1">
          <dt className="text-gray-500">lifecycle</dt>
          <dd className="text-gray-300">{overlay.lifecycle}</dd>
        </div>
        <div className="flex items-center gap-1">
          <dt className="text-gray-500">rule</dt>
          <dd className="font-mono text-gray-300">{overlay.rule_id}</dd>
        </div>
        <div className="flex items-center gap-1">
          <dt className="text-gray-500">artifact</dt>
          <dd className="font-mono text-gray-300" title={overlay.artifact.artifact_sha256}>
            {shortSha(overlay.artifact.artifact_sha256)}
          </dd>
        </div>
        {overlay.started_at != null && (
          <div className="flex items-center gap-1">
            <dt className="text-gray-500">started</dt>
            <dd className="text-gray-300">{formatClock(overlay.started_at)}</dd>
          </div>
        )}
        {runDuration != null && (
          <div className="flex items-center gap-1">
            <dt className="text-gray-500">took</dt>
            <dd className="text-gray-300">{formatDuration(runDuration)}</dd>
          </div>
        )}
        {overlay.budget && (
          <div className="flex items-center gap-1">
            <dt className="text-gray-500">tokens</dt>
            <dd className="text-gray-300">
              {overlay.budget.total_tokens ?? 0}
              {overlay.budget.max_total_tokens != null ? ` / ${overlay.budget.max_total_tokens}` : ""}
              {` in ${overlay.budget.llm_calls ?? 0} call${(overlay.budget.llm_calls ?? 0) === 1 ? "" : "s"}`}
            </dd>
          </div>
        )}
      </dl>

      {overlay.operator_decision && (
        <p role="alert" className="rounded border border-amber-700 bg-amber-950/50 px-2 py-1 text-xs text-amber-200">
          {`Waiting on an operator decision for ${overlay.operator_decision.step_id} (attempt ${overlay.operator_decision.attempt}): ${overlay.operator_decision.reason}. Options: ${(overlay.operator_decision.options ?? []).join(", ") || "none offered"}.`}
        </p>
      )}

      {overlay.truncated && (
        <p role="status" className="rounded bg-amber-500/10 p-2 text-xs text-amber-200">
          {`Showing the newest ${(overlay.receipts ?? []).length} of ${overlay.receipt_total ?? 0} receipts.`}
        </p>
      )}

      {nodes.length === 0 ? (
        <p className="text-sm text-gray-500">This run recorded no step state.</p>
      ) : (
        <ul aria-label="Steps in this run" className="min-w-0 space-y-2">
          {nodes.map((node) => {
            const iterations = node.iterations ?? [];
            const receipts = (node.receipt_ids ?? [])
              .map((id) => receiptsById.get(id))
              .filter((receipt): receipt is ReceiptDTO => Boolean(receipt));
            // A chosen iteration narrows every row that holds receipts of it.
            // A row that holds none is left whole rather than emptied — the
            // filter is there to focus the loop's work, not to hide a step.
            const narrowed = selectedIteration
              ? receipts.filter((receipt) => receiptInIteration(receipt, selectedIteration))
              : [];
            const shown = narrowed.length > 0 ? narrowed : receipts;

            return (
              <li
                key={node.step_id}
                aria-label={`Step ${node.step_id}`}
                className="min-w-0 space-y-2 rounded border border-gray-800 bg-gray-950/50 p-2"
              >
                <p className="flex flex-wrap items-baseline gap-2 text-xs">
                  <span className="font-mono text-gray-200">{node.step_id}</span>
                  <span className="rounded bg-gray-800 px-1 py-0.5 text-[10px] uppercase tracking-wide text-gray-300">
                    {NODE_RUN_STATE_LABELS[node.state ?? "not_visited"] ?? node.state}
                  </span>
                  <span className="text-gray-500">
                    {node.visit_count ?? 0} visit{(node.visit_count ?? 0) === 1 ? "" : "s"}
                  </span>
                  {node.last_outcome && <span className="text-gray-400">last outcome: {node.last_outcome}</span>}
                </p>

                {iterations.length > 0 && (
                  <div role="group" aria-label={`Iterations of ${node.step_id}`} className="flex flex-wrap gap-1">
                    {iterations.map((iteration) => {
                      const chosen =
                        selection?.iterationStepId === node.step_id &&
                        selection?.iterationIndex === iteration.index;
                      return (
                        <button
                          type="button"
                          key={`${node.step_id}-${iteration.index}`}
                          aria-pressed={chosen}
                          aria-label={`Iteration ${iteration.index}: ${iteration.item_display}`}
                          onClick={() =>
                            setChosen({
                              runId: overlay.run_id,
                              iterationStepId: node.step_id,
                              iterationIndex: iteration.index,
                              receiptId: iteration.receipt_ids?.[0] ?? null,
                            })
                          }
                          className={`rounded px-2 py-1 text-[11px] ${
                            chosen ? "bg-indigo-600 text-white" : "bg-gray-800 text-gray-200 hover:bg-gray-700"
                          }`}
                        >
                          {iterationSummary(iteration)}
                        </button>
                      );
                    })}
                  </div>
                )}

                {receipts.length > 0 ? (
                  <ReceiptChooser
                    receipts={shown}
                    selectedId={selection?.receiptId}
                    label={`Receipts for ${node.step_id}`}
                    onSelect={(receiptId) => setChosen({ ...EMPTY_ITERATION, runId: overlay.run_id, receiptId })}
                  />
                ) : (
                  <p className="text-[11px] text-gray-500">No receipt was returned for this step.</p>
                )}
              </li>
            );
          })}
        </ul>
      )}

      <ReceiptDetail
        receipt={selectedReceipt}
        missingReceiptId={selection?.receiptId && !selectedReceipt ? selection.receiptId : null}
      />
    </section>
  );
}
