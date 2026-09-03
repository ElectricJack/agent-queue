import type { LoopIterationOverlayDTO, ReceiptDTO } from "../../api/client";
import { Block, Pairs, Rows, Value } from "./IntentSections";
import { STEP_KIND_LABELS } from "./types";

/** True when this receipt is part of that iteration. Both wirings count: the
 *  iteration may name the receipt, and the receipt may carry the index. */
export function receiptInIteration(
  receipt: ReceiptDTO,
  iteration: LoopIterationOverlayDTO | undefined,
): boolean {
  if (!iteration) return false;
  if (receipt.iteration_index === iteration.index) return true;
  return (iteration.receipt_ids ?? []).includes(receipt.receipt_id);
}

/** Epoch seconds → a wall-clock time an operator can line up against a log.
 *  The date lives on the `title` so a receipt list stays one line wide. */
export function formatClock(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toISOString().slice(11, 23) + "Z";
}

/** A duration in the unit that reads: sub-second work is milliseconds, an
 *  agent task that ran for minutes is not "412.7s". */
export function formatDuration(seconds: number): string {
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${Number(seconds.toFixed(seconds < 10 ? 2 : 1))}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds - minutes * 60);
  return `${minutes}m ${rest}s`;
}

/** `duration_seconds` is authoritative when the backend recorded it; a receipt
 *  that only carries both timestamps still has a duration, and one that is
 *  still open has none rather than a wrong one. */
export function receiptDuration(receipt: ReceiptDTO): number | null {
  if (receipt.duration_seconds != null) return receipt.duration_seconds;
  if (receipt.completed_at == null) return null;
  return receipt.completed_at - receipt.started_at;
}

/** What distinguishes one receipt of a step from another: which iteration it
 *  belongs to, which attempt it was, and how it ended. All three are needed —
 *  a retried step inside a loop has receipts that differ in only one of them. */
export function receiptLabel(receipt: ReceiptDTO): string {
  const parts: string[] = [];
  if (receipt.iteration_index != null) parts.push(`iteration ${receipt.iteration_index}`);
  parts.push(`attempt ${receipt.attempt ?? 1}`);
  parts.push(receipt.outcome);
  return parts.join(" · ");
}

export interface ReceiptChooserProps {
  receipts: ReceiptDTO[];
  selectedId?: string | null;
  onSelect: (receiptId: string) => void;
  /** Names the group, and disambiguates the several choosers a run overlay
   *  renders — one per step — from each other. */
  label?: string;
}

/** Every receipt is its own control. The first one is not special: a step that
 *  failed twice before succeeding is three receipts, and an operator asking
 *  "why did this retry?" needs the two that are not the last. */
export function ReceiptChooser({ receipts, selectedId, onSelect, label = "Receipts" }: ReceiptChooserProps) {
  if (receipts.length === 0) return null;
  return (
    <div role="group" aria-label={label} className="flex flex-wrap gap-1">
      {receipts.map((receipt) => {
        const selected = receipt.receipt_id === selectedId;
        return (
          <button
            type="button"
            key={receipt.receipt_id}
            aria-pressed={selected}
            aria-label={`Receipt for ${receipt.step_id}, ${receiptLabel(receipt)}`}
            onClick={() => onSelect(receipt.receipt_id)}
            className={`rounded px-2 py-1 text-[11px] ${
              selected ? "bg-indigo-600 text-white" : "bg-gray-800 text-gray-200 hover:bg-gray-700"
            }`}
          >
            {receiptLabel(receipt)}
          </button>
        );
      })}
    </div>
  );
}

export interface ReceiptDetailProps {
  receipt?: ReceiptDTO | null;
  /** Shown when a receipt id is referenced by the overlay but its receipt was
   *  not returned — the response caps receipts (`truncated`) rather than
   *  silently dropping them, so the panel says which case this is. */
  missingReceiptId?: string | null;
}

/** One receipt, in full: what went in, what came out, how long it took, what it
 *  cost, and every identity fact the run recorded. This is the difference
 *  between "the step failed" and "attempt 1 failed with this error on these
 *  inputs after 1.2s, and attempt 2 succeeded against the same idempotency
 *  key". */
export default function ReceiptDetail({ receipt, missingReceiptId }: ReceiptDetailProps) {
  if (!receipt) {
    return (
      <p className="text-xs text-gray-500">
        {missingReceiptId
          ? `Receipt ${missingReceiptId} was not returned with this overlay — the receipt list is capped.`
          : "Select a receipt to see what the step actually did."}
      </p>
    );
  }

  const duration = receiptDuration(receipt);
  const inputs = receipt.inputs ?? [];
  const usage = receipt.token_usage;

  const facts: [string, React.ReactNode][] = [
    ["step", <span className="font-mono">{receipt.step_id}</span>],
    ["rule", <span className="font-mono">{receipt.rule_id}</span>],
    ["kind", STEP_KIND_LABELS[receipt.step_kind] ?? receipt.step_kind],
    ["attempt", receipt.attempt ?? 1],
    ...(receipt.iteration_index != null
      ? ([["iteration", receipt.iteration_index]] as [string, React.ReactNode][])
      : []),
    ["started", <span title={new Date(receipt.started_at * 1000).toISOString()}>{formatClock(receipt.started_at)}</span>],
    [
      "completed",
      receipt.completed_at == null ? (
        <span className="text-gray-500">still running</span>
      ) : (
        <span title={new Date(receipt.completed_at * 1000).toISOString()}>{formatClock(receipt.completed_at)}</span>
      ),
    ],
    ["duration", duration == null ? <span className="text-gray-500">—</span> : formatDuration(duration)],
    ...(receipt.selected_edge_id
      ? ([["selected edge", <span className="break-all font-mono">{receipt.selected_edge_id}</span>]] as [string, React.ReactNode][])
      : []),
    ...(receipt.profile_id
      ? ([["profile", <span className="font-mono">{receipt.profile_id}</span>]] as [string, React.ReactNode][])
      : []),
    ...(receipt.idempotency_key
      ? ([["idempotency key", <span className="break-all font-mono">{receipt.idempotency_key}</span>]] as [string, React.ReactNode][])
      : []),
    ...(receipt.principal_fingerprint
      ? ([["principal", <span className="break-all font-mono">{receipt.principal_fingerprint}</span>]] as [string, React.ReactNode][])
      : []),
    ...(receipt.contract_fingerprint
      ? ([["contract", <span className="break-all font-mono">{receipt.contract_fingerprint}</span>]] as [string, React.ReactNode][])
      : []),
  ];

  return (
    <section aria-label="Receipt detail" className="min-w-0 space-y-3 rounded border border-gray-800 bg-gray-950 p-3">
      <header className="min-w-0 space-y-1">
        <p className="text-sm text-gray-200">Outcome: {receipt.outcome}</p>
        <p className="break-all font-mono text-[10px] text-gray-500">{receipt.receipt_id}</p>
      </header>

      <Pairs pairs={facts} />

      {receipt.error && (
        <p role="alert" className="break-words rounded border border-rose-800 bg-rose-950/50 px-2 py-1 text-xs text-rose-200">
          {receipt.error}
        </p>
      )}

      {inputs.length > 0 && (
        <Block name="Receipt inputs">
          <Rows rows={inputs} />
        </Block>
      )}

      {receipt.result && (
        <Block name="Receipt result">
          <Value value={receipt.result} />
        </Block>
      )}

      {usage && (
        <Block name="Token usage">
          <Pairs
            pairs={[
              ["input", usage.input_tokens ?? 0],
              ["output", usage.output_tokens ?? 0],
              ["total", usage.total_tokens ?? 0],
              ["counted", usage.estimated ? "estimated" : "reported"],
            ]}
          />
        </Block>
      )}

      {receipt.wait && (
        <Block name="Wait facts">
          <Pairs
            pairs={[
              ["wait kind", receipt.wait.wait_kind],
              ["correlation key", <span className="break-all font-mono">{receipt.wait.correlation_key}</span>],
              ["registered", formatClock(receipt.wait.registered_at)],
              [
                "deadline",
                receipt.wait.deadline_at == null
                  ? "none"
                  : `${formatClock(receipt.wait.deadline_at)}${
                      receipt.wait.deadline_source ? ` (from the ${receipt.wait.deadline_source})` : ""
                    }`,
              ],
              [
                "matched",
                receipt.wait.matched_at == null ? (
                  <span className="text-gray-500">not matched</span>
                ) : (
                  `${formatClock(receipt.wait.matched_at)}${
                    receipt.wait.matched_event_id ? ` by ${receipt.wait.matched_event_id}` : ""
                  }`
                ),
              ],
            ]}
          />
        </Block>
      )}

      {receipt.cancellation && (
        <Block name="Cancellation">
          <Pairs
            pairs={[
              ["requested", formatClock(receipt.cancellation.requested_at)],
              [
                "acknowledged",
                receipt.cancellation.acknowledged_at == null ? (
                  <span className="text-gray-500">not acknowledged</span>
                ) : (
                  formatClock(receipt.cancellation.acknowledged_at)
                ),
              ],
              ["cancelled child", receipt.cancellation.cancelled_child ? "yes" : "no"],
            ]}
          />
        </Block>
      )}
    </section>
  );
}
