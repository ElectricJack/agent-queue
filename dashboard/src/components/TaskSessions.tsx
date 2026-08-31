import { useId } from "react";
import { Link, useLocation } from "react-router-dom";
import { useTaskSessions } from "../api/taskSessions";

export default function TaskSessions({ taskId, onOpenSession, fromTaskPane = false }: { taskId: string; onOpenSession?: () => void; fromTaskPane?: boolean }) {
  const headingId = useId();
  const location = useLocation();
  const history = useTaskSessions(taskId);
  const attempts = history.data?.sessions;
  const from = location.pathname + location.search + location.hash;

  return (
    <section aria-labelledby={headingId} className="space-y-2">
      <h2 id={headingId} className="text-sm font-semibold uppercase text-gray-500">
        Sessions{attempts && ` (${attempts.length})`}
      </h2>
      <p className="text-xs text-gray-400">Newest first · select an agent to view its transcript.</p>
      {history.isPending && <p role="status" className="text-sm text-gray-400">Loading sessions…</p>}
      {history.isError ? (
        <div role="alert" className="rounded-lg border border-red-800 bg-red-950/30 p-3 text-sm text-red-300">
          Could not load sessions. {history.error.message}
          <button type="button" onClick={() => history.refetch()} className="ml-2 underline">Retry sessions</button>
        </div>
      ) : attempts && (attempts.length === 0 ? (
        <p className="text-sm text-gray-400">No recorded sessions yet.</p>
      ) : (
        <ol aria-label="Session attempts" className="divide-y divide-gray-800 overflow-hidden rounded-lg border border-gray-800 bg-gray-900">
          {attempts.map((attempt) => {
            const name = attempt.agent_name || attempt.agent_id || attempt.session_id;
            const hasDetails = attempt.ended_at != null || !!attempt.outcome || !!attempt.end_reason;
            const started = <span className="text-[11px] text-gray-400"><span className="sr-only">Started </span><AttemptTime value={attempt.started_at} /></span>;
            return (
              <li key={attempt.id} className="px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <Link
                    to={`/sessions/${encodeURIComponent(attempt.session_id)}?attempt=${encodeURIComponent(attempt.id)}&taskId=${encodeURIComponent(taskId)}`}
                    state={{ from, taskPane: fromTaskPane ? { taskId } : undefined }} onClick={onOpenSession}
                    title={name} className="min-w-0 truncate text-sm font-medium text-indigo-400 hover:underline"
                  >{name}</Link>
                  {attempt.state && <span className="shrink-0 rounded bg-gray-800 px-1.5 py-0.5 text-[10px] text-gray-300">{attempt.state}</span>}
                </div>
                {(attempt.model || attempt.intelligence_class) && <div className="mt-0.5 flex min-w-0 items-center gap-2 text-xs text-gray-400">
                  {attempt.model && <span title={attempt.model} className="min-w-0 truncate">{attempt.model}</span>}
                  {attempt.intelligence_class && <span title={`Intelligence: ${attempt.intelligence_class}`} className="shrink-0">int: {attempt.intelligence_class}</span>}
                </div>}
                {hasDetails ? (
                  <details className="mt-1">
                    <summary className="flex cursor-pointer list-none items-center justify-between gap-2 rounded focus-visible:outline focus-visible:outline-1 focus-visible:outline-indigo-400">
                      {started}<span className="text-[11px] text-indigo-400">Details</span>
                    </summary>
                    <dl className="mt-2 space-y-1 border-t border-gray-800 pt-2 text-xs">
                      {attempt.ended_at != null && <div className="flex flex-wrap gap-x-2"><dt className="text-gray-500">Ended</dt><dd className="text-gray-300"><AttemptTime value={attempt.ended_at} /></dd></div>}
                      {attempt.outcome && <div className="flex flex-wrap gap-x-2"><dt className="text-gray-500">Outcome</dt><dd className="text-gray-300">{attempt.outcome}</dd></div>}
                      {attempt.end_reason && <div><dt className="text-gray-500">Exit reason</dt><dd className="whitespace-pre-wrap break-words text-gray-300">{attempt.end_reason}</dd></div>}
                    </dl>
                  </details>
                ) : <div className="mt-1">{started}</div>}
              </li>
            );
          })}
        </ol>
      ))}
    </section>
  );
}

export function AttemptTime({ value }: { value: number | null }) {
  if (value == null || !Number.isFinite(value)) return <>Not recorded</>;
  const date = new Date(value * 1000);
  return <time dateTime={date.toISOString()}>{date.toLocaleString()}</time>;
}
