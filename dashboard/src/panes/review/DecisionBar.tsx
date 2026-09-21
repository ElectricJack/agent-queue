import { useRef, useState, type RefObject } from "react";

export type ReviewDecision = "approve" | "request_changes";

export function DecisionBar({
  disabledReason,
  onDecide,
  pending = false,
  approveButtonRef,
}: {
  disabledReason: string | null;
  onDecide: (decision: ReviewDecision, note: string) => Promise<void>;
  pending?: boolean;
  approveButtonRef?: RefObject<HTMLButtonElement | null>;
}) {
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const localApproveRef = useRef<HTMLButtonElement>(null);
  const approveRef = approveButtonRef ?? localApproveRef;
  const disabled = Boolean(disabledReason) || pending;

  const decide = async (decision: ReviewDecision) => {
    if (disabled) return;
    setError(null);
    try {
      await onDecide(decision, note.trim());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not record the decision.");
    }
  };

  return (
    <div className="sticky bottom-0 z-20 border-t border-gray-800 bg-gray-950/95 p-3 backdrop-blur">
      {disabledReason && <p className="mb-2 text-xs text-amber-300">{disabledReason}</p>}
      {error && <p role="alert" className="mb-2 text-xs text-red-300">{error}</p>}
      <label className="sr-only" htmlFor="review-decision-note">Decision note</label>
      <div className="flex flex-wrap items-end gap-2">
        <textarea
          id="review-decision-note"
          rows={2}
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="Overall note (optional)"
          className="min-w-[16rem] flex-1 resize-y rounded border border-gray-700 bg-gray-900 p-2 text-sm text-gray-100 outline-none focus:border-indigo-500"
          disabled={disabled}
        />
        <button
          ref={approveRef}
          type="button"
          onClick={() => void decide("approve")}
          disabled={disabled}
          className="rounded bg-emerald-700 px-3 py-2 text-sm font-medium text-white hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Approve
        </button>
        <button
          type="button"
          onClick={() => void decide("request_changes")}
          disabled={disabled}
          className="rounded bg-red-800 px-3 py-2 text-sm font-medium text-white hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Request changes
        </button>
      </div>
    </div>
  );
}
