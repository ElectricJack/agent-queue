import { useEffect, useState } from "react";

/**
 * The enable/disable switch shown on a flock row.
 *
 * Both callers (a pool row in the directory, a non-pooled agent row in the
 * rail) hold server state that a poll refreshes every few seconds, so the
 * switch renders the *persisted* value and only borrows the requested one
 * while its mutation is in flight. A refusal therefore restores the displayed
 * state on its own — there is no local copy of the truth to get stuck.
 *
 * The row around it is itself a button (it opens the agent view), so this one
 * lives beside it rather than inside it and stops its click from selecting.
 */
export default function EnableToggle({ enabled, subject, pending, error, onChange }: {
  /** Persisted state, straight from the last poll. */
  enabled: boolean;
  /** What is being switched, e.g. "worker-fast-medium-claude pool". */
  subject: string;
  /** True while this row's own update is in flight. */
  pending?: boolean;
  /** Message from a failed update, shown beside the row. */
  error?: string | null;
  onChange: (next: boolean) => void;
}) {
  // What the user asked for, held only for the length of the request.
  const [requested, setRequested] = useState<boolean | null>(null);
  useEffect(() => { if (!pending) setRequested(null); }, [pending]);
  const shown = pending && requested !== null ? requested : enabled;

  return (
    <span className="flex shrink-0 flex-col items-end gap-0.5">
      <button
        type="button"
        role="switch"
        aria-checked={shown}
        aria-label={(shown ? "Disable " : "Enable ") + subject}
        title={shown
          ? "Enabled — accepting new work. Disabling stops new work; anything already running finishes."
          : "Disabled — no new work is started or claimed. Enable to restore eligibility."}
        disabled={!!pending}
        onClick={(event) => {
          event.stopPropagation();
          setRequested(!shown);
          onChange(!shown);
        }}
        className={"flex items-center gap-1.5 rounded-full border px-1.5 py-0.5 text-[10px] disabled:opacity-50 "
          + (shown
            ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
            : "border-gray-700 bg-gray-800/60 text-gray-400")}
      >
        <span aria-hidden="true"
          className={"h-2 w-2 rounded-full " + (shown ? "bg-emerald-400" : "bg-gray-500")} />
        {pending ? "Saving…" : shown ? "Enabled" : "Disabled"}
      </button>
      {error && <span role="alert" className="max-w-40 text-right text-[10px] text-red-300">{error}</span>}
    </span>
  );
}
