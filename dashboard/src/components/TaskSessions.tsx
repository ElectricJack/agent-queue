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
    <section aria-labelledby={headingId} className="space-y-3">
      <h2 id={headingId} className="text-sm font-semibold uppercase text-gray-500">
        Sessions{attempts && ` (${attempts.length})`}
      </h2>
      <p className="text-xs text-gray-400">Recorded execution attempts, newest first. Open a session to inspect its transcript.</p>
      {history.isPending && <p role="status" className="text-sm text-gray-400">Loading sessions…</p>}
      {history.isError ? (
        <div role="alert" className="rounded-lg border border-red-800 bg-red-950/30 p-3 text-sm text-red-300">
          Could not load sessions. {history.error.message}
          <button type="button" onClick={() => history.refetch()} className="ml-2 underline">Retry sessions</button>
        </div>
      ) : attempts && (attempts.length === 0 ? (
        <p className="text-sm text-gray-400">No recorded sessions yet.</p>
      ) : (
        <ol aria-label="Session attempts" className="space-y-2">
          {attempts.map((attempt) => (
            <li key={attempt.id} className="rounded-lg border border-gray-800 bg-gray-900 p-3">
              <Link
                to={`/sessions/${encodeURIComponent(attempt.session_id)}?attempt=${encodeURIComponent(attempt.id)}&taskId=${encodeURIComponent(taskId)}`}
                state={{ from, taskPane: fromTaskPane ? { taskId } : undefined }} onClick={onOpenSession}
                className="font-medium text-indigo-400 hover:underline"
              >
                {attempt.agent_name || attempt.agent_id || attempt.session_id}
              </Link>
              <dl className="mt-2 grid grid-cols-1 gap-x-4 gap-y-2 text-xs sm:grid-cols-2">
                <div><dt className="text-gray-500">Model</dt><dd className="break-words text-gray-300">{attempt.model || "Not recorded"}</dd></div>
                <div><dt className="text-gray-500">Intelligence class</dt><dd className="text-gray-300">{attempt.intelligence_class || "Not recorded"}</dd></div>
                <div><dt className="text-gray-500">Started</dt><dd className="text-gray-300"><AttemptTime value={attempt.started_at} /></dd></div>
                <div><dt className="text-gray-500">Ended</dt><dd className="text-gray-300"><AttemptTime value={attempt.ended_at} /></dd></div>
                <div><dt className="text-gray-500">State</dt><dd className="text-gray-300">{attempt.state || "Not recorded"}</dd></div>
                <div><dt className="text-gray-500">Outcome</dt><dd className="text-gray-300">{attempt.outcome || "Not recorded"}</dd></div>
                <div className="sm:col-span-2"><dt className="text-gray-500">Exit reason</dt><dd className="whitespace-pre-wrap break-words text-gray-300">{attempt.end_reason || "Not recorded"}</dd></div>
              </dl>
            </li>
          ))}
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
