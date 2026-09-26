import { useRef, useState, type RefObject } from "react";
import { useIntelligenceClasses, useProfiles } from "../../api/hooks";

export type ReviewDecision = "approve" | "request_changes" | "reject";

export type ResponseRoute = {
  kind: string;
  summary: string;
  class_summaries: Record<string, string>;
};

export function DecisionBar({
  disabledReason,
  onDecide,
  pending = false,
  approveButtonRef,
  responseRoute,
}: {
  disabledReason: string | null;
  onDecide: (decision: ReviewDecision, note: string, responderClass: string, responderProfile: string) => Promise<void>;
  pending?: boolean;
  approveButtonRef?: RefObject<HTMLButtonElement | null>;
  responseRoute: ResponseRoute;
}) {
  const [note, setNote] = useState("");
  const [responderClass, setResponderClass] = useState("");
  const [responderProfile, setResponderProfile] = useState("");
  const [showRevisionOptions, setShowRevisionOptions] = useState(false);
  const { data: classData } = useIntelligenceClasses();
  const { data: profiles } = useProfiles();
  const [error, setError] = useState<string | null>(null);
  const localApproveRef = useRef<HTMLButtonElement>(null);
  const approveRef = approveButtonRef ?? localApproveRef;
  const disabled = Boolean(disabledReason) || pending;
  const routeSummary = showRevisionOptions && responderProfile
    ? `Request changes → new revision task on ${responderClass} (explicit); Approve → sent to the supervisor.`
    : showRevisionOptions && responderClass
      ? responseRoute.class_summaries[responderClass]
        ?? `No eligible revision profile is available for ${responderClass}. Approve → sent to the supervisor.`
      : responseRoute.summary;

  const decide = async (decision: ReviewDecision) => {
    if (disabled) return;
    setError(null);
    try {
      await onDecide(
        decision, note.trim(),
        decision !== "approve" ? responderClass : "",
        decision !== "approve" ? responderProfile : "",
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not record the decision.");
    }
  };

  return (
    <div className="sticky bottom-0 z-20 border-t border-gray-800 bg-gray-950/95 p-3 backdrop-blur">
      {disabledReason && <p className="mb-2 text-xs text-amber-300">{disabledReason}</p>}
      {error && <p role="alert" className="mb-2 text-xs text-red-300">{error}</p>}
      <p className="mb-2 text-xs text-indigo-200" aria-label="Response route">{routeSummary}</p>
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
          onClick={() => setShowRevisionOptions(true)}
          disabled={disabled}
          className="rounded bg-red-800 px-3 py-2 text-sm font-medium text-white hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Request changes
        </button>
        <button
          type="button"
          onClick={() => void decide("reject")}
          disabled={disabled || !note.trim()}
          className="rounded bg-gray-700 px-3 py-2 text-sm font-medium text-white hover:bg-gray-600 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Reject
        </button>
      </div>
      {showRevisionOptions && (
        <div className="mt-3 border-t border-gray-800 pt-3">
          <p className="mb-2 text-xs font-medium text-gray-200">Who revises this after your feedback?</p>
          <div className="mb-3 flex flex-wrap gap-2">
            <label className="text-xs text-gray-400">
              Revision intelligence class
              <select
                value={responderClass}
                onChange={(event) => { setResponderClass(event.target.value); setResponderProfile(""); }}
                disabled={disabled}
                className="ml-2 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-gray-100"
              >
                <option value="">Project default</option>
                {(classData?.classes ?? []).map((row) => <option key={row.id} value={row.id}>{row.id}</option>)}
              </select>
            </label>
            {responderClass && (
              <label className="text-xs text-gray-400">
                Revision profile
                <select
                  value={responderProfile}
                  onChange={(event) => setResponderProfile(event.target.value)}
                  disabled={disabled}
                  className="ml-2 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-gray-100"
                >
                  <option value="">Choose by class</option>
                  {(profiles ?? []).filter((profile) => profile.default_class === responderClass)
                    .map((profile) => <option key={profile.id} value={profile.id}>{profile.id}</option>)}
                </select>
              </label>
            )}
          </div>
          <button
            type="button"
            onClick={() => void decide("request_changes")}
            disabled={disabled}
            className="rounded bg-red-800 px-3 py-2 text-sm font-medium text-white hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Send changes request
          </button>
        </div>
      )}
    </div>
  );
}
