import { useState } from "react";
import { ExclamationTriangleIcon } from "@heroicons/react/24/outline";

import Modal from "../../components/Modal";
import { useWithdrawReview } from "../../api/reviews";

interface Props {
  review: { id: string; title: string };
  onClose: () => void;
  onWithdrawn: () => void;
}

/** Confirm closing a review; the reason is optional and recorded on the review. */
export default function WithdrawReviewModal({ review, onClose, onWithdrawn }: Props) {
  const withdraw = useWithdrawReview();
  const [reason, setReason] = useState("");
  const [fatal, setFatal] = useState<string | null>(null);

  const onConfirm = async () => {
    setFatal(null);
    try {
      await withdraw.mutateAsync({ review_id: review.id, reason: reason.trim() });
      onWithdrawn();
    } catch (err) {
      setFatal(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <Modal open onClose={onClose} title="Close review">
      <div className="space-y-4">
        <p className="text-sm text-gray-300">
          Close <strong>{review.title}</strong>? Its approval gate will be cancelled and
          dependent tasks flagged for attention. They still require approval. The author
          and project supervisor receive your reason. You can reopen this review later
          with the same dependent tasks.
        </p>
        <label className="block text-xs text-gray-400">
          Reason (optional)
          <textarea
            aria-label="Reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            rows={3}
            className="mt-1 w-full rounded border border-gray-700 bg-gray-950 px-2 py-1.5 text-sm text-gray-200"
            placeholder="Why this review is no longer wanted"
          />
        </label>
        {fatal && (
          <div role="alert" className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
            <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{fatal}</span>
          </div>
        )}
        <div className="flex items-center justify-end gap-2 border-t border-gray-800 pt-3">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={withdraw.isPending}
            className="rounded-md bg-red-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:cursor-not-allowed disabled:bg-gray-700"
          >
            {withdraw.isPending ? "Closing…" : "Close review"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
