import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useSession } from "../../api/hooks";
import { useAgentFlock, type FlockAgent } from "../../api/agents";
import type { SessionSummary } from "../../api/client";
import InteractiveTerminal from "../../components/InteractiveTerminal";
import { FocusError } from "./FocusNotice";
import { useFocusChrome } from "./focusChrome";
import { focusSessionHref, focusTaskHref } from "./routes";

const LINK = "inline-flex items-center rounded-md border border-gray-700 px-3 text-sm text-indigo-300";
const time = (seconds: number) => new Date(seconds * 1000).toLocaleString();
const isLive = (state?: string | null) => state === "running" || state === "draining";

/** The agent's live session now, when it is a different one (never followed silently). */
function currentSessionFor(session: SessionSummary, flock: FlockAgent[]): FlockAgent | null {
  if (!session.agent_id) return null;
  const agent = flock.find((candidate) => candidate.id === session.agent_id);
  if (!agent?.session_id || agent.session_id === session.id || !isLive(agent.session_state)) return null;
  return agent;
}

/**
 * `/focus/sessions/:sessionId` — a session's terminal (mobile dashboard §4,
 * §4.1). The view is pinned to one process: the link's `?started=` or the
 * first one it saw. A restart shows a notice and waits for a tap; a session
 * that is not running shows why and links the agent's current session. The
 * terminal is always the host shell page's terminal, on any screen.
 */
export default function FocusSession() {
  const { sessionId = "" } = useParams();
  const [params] = useSearchParams();
  const started = Number(params.get("started") || NaN);
  const pinned = Number.isFinite(started) ? started : null;
  return <FocusSessionContent key={`${sessionId}:${pinned ?? ""}`} sessionId={sessionId} pinned={pinned} />;
}

function FocusSessionContent({ sessionId, pinned }: { sessionId: string; pinned: number | null }) {
  const navigate = useNavigate();
  const query = useSession(sessionId);
  const session = query.data;
  const [watched, setWatched] = useState<number | null>(pinned);
  useEffect(() => {
    if (watched === null && session?.started_at != null) setWatched(session.started_at);
  }, [watched, session?.started_at]);
  useFocusChrome({ title: session?.name ?? sessionId, fullHref: `/sessions/${encodeURIComponent(sessionId)}` });

  if (query.isPending) return <p role="status" className="p-4 text-sm text-gray-500">Loading session…</p>;
  if (query.isError || !session) {
    return (
      <FocusError
        title="Session"
        message={query.error instanceof Error ? query.error.message : "The session could not be read."}
        onRetry={() => void query.refetch()}
      />
    );
  }

  const restartedAt = session.started_at;
  if (watched !== null && restartedAt != null && restartedAt !== watched) {
    return (
      <section className="space-y-3 p-4 text-sm">
        <p className="text-amber-300">
          This session restarted at {time(restartedAt)}. You were watching the process started at {time(watched)}.
        </p>
        <button
          type="button"
          data-primary-control
          className={LINK}
          onClick={() => navigate(focusSessionHref(sessionId, { started: restartedAt }), { replace: true })}
        >
          Watch the new process
        </button>
      </section>
    );
  }

  if (!isLive(session.state)) return <SessionNotRunning session={session} />;
  if (session.provider && session.provider !== "tmux") {
    return (
      <p className="p-4 text-sm text-gray-400">
        This session runs on {session.provider}; there is no terminal view for it.
      </p>
    );
  }
  return (
    <div className="flex h-full min-h-0 flex-col">
      <InteractiveTerminal sessionId={sessionId} name={session.name} />
    </div>
  );
}

/** Why there is no live pane, with the task and the agent's current session. */
function SessionNotRunning({ session }: { session: SessionSummary }) {
  // Mounted only here: a live watch does not poll the (expensive) roster.
  const { data: flock = [] } = useAgentFlock();
  const current = currentSessionFor(session, flock);
  const starting = session.state === "starting";
  const headline = starting
    ? "This session is starting; its terminal appears once it runs."
    : session.state === "sleeping"
      ? `Session asleep${session.sleep_reason ? ` (${session.sleep_reason})` : ""}.`
      : `Session ended${session.ended_at ? ` at ${time(session.ended_at)}` : ""} (${session.state ?? "unknown state"}).`;
  return (
    <section className="space-y-3 p-4 text-sm">
      <p className="text-amber-300">{headline}</p>
      {session.end_reason && <p className="whitespace-pre-wrap break-words text-gray-300">{session.end_reason}</p>}
      <div className="flex flex-wrap gap-2">
        {session.task_id && (
          <Link to={focusTaskHref(session.task_id)} data-primary-control className={LINK}>
            Task {session.task_id}
          </Link>
        )}
        {current ? (
          <Link to={focusSessionHref(current.session_id!)} data-primary-control className={LINK}>
            Watch {current.name}'s current session
          </Link>
        ) : (
          !starting && <p className="text-gray-500">No current session for this agent.</p>
        )}
      </div>
      {!starting && <p className="text-xs text-gray-500">The transcript is in the full dashboard.</p>}
    </section>
  );
}
