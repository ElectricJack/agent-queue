import { useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import {
  PlayIcon,
  StopIcon,
  PaperAirplaneIcon,
  ArrowPathIcon,
  ClipboardIcon,
} from "@heroicons/react/24/outline";
import {
  useSession,
  useSessionAttach,
  useSessionNudge,
  useSessionKill,
} from "../api/hooks";
import { useTaskSessions, type TaskSessionAttempt } from "../api/taskSessions";
import type { SessionSummary } from "../api/client";
import { AttemptTime } from "../components/TaskSessions";
import { useTranscriptStream } from "../ws/useTranscriptStream";
import InteractiveTerminal from "../components/InteractiveTerminal";
import { workspaceHref } from "../shell/projectNavigation";

export default function SessionDetail() {
  const { sessionId = "" } = useParams();
  const location = useLocation();
  const query = new URLSearchParams(location.search);
  const attemptId = query.get("attempt");
  return attemptId !== null ? (
    <AttemptSessionDetail key={`${sessionId}:${attemptId}:${query.get("taskId")}`} sessionId={sessionId} attemptId={attemptId} taskId={query.get("taskId") ?? ""} />
  ) : <CurrentSessionDetail key={sessionId} sessionId={sessionId} />;
}

function CurrentSessionDetail({ sessionId }: { sessionId: string }) {
  const { data: session, isLoading } = useSession(sessionId);
  if (isLoading) return <div className="p-6 text-gray-400">Loading…</div>;
  if (!session) return <div className="p-6 text-gray-400">Session not found</div>;
  return <SessionDetailContent session={session} interactive={session.state === "running" || session.state === "draining"} />;
}

function AttemptSessionDetail({ sessionId, attemptId, taskId }: { sessionId: string; attemptId: string; taskId: string }) {
  const history = useTaskSessions(taskId);
  const current = useSession(sessionId);
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? `/tasks/${encodeURIComponent(taskId)}`;
  const back = <SessionBackLink from={from} toTask />;
  if (!taskId) return <div className="p-6 space-y-3">{back}<p role="alert">Task context is required to open a session attempt.</p></div>;
  if (history.isPending) return <div className="p-6 space-y-3">{back}<p role="status">Loading session attempt…</p></div>;
  if (history.isError) return (
    <div className="p-6 space-y-3">{back}<p role="alert">Could not load session attempt. {history.error.message}</p>
      <button onClick={() => history.refetch()} className="text-sm text-indigo-400 underline">Retry session attempt</button>
    </div>
  );
  const attempt = history.data?.sessions.find((row) => row.id === attemptId && row.session_id === sessionId && row.task_id === taskId);
  if (!attempt) return <div className="p-6 space-y-3">{back}<p role="alert">Session attempt not found.</p></div>;

  // A worker session can be reused for a different task or restarted. A live
  // terminal is safe only when both the assignment and process launch match.
  const newestOpen = history.data?.sessions.find((row) => row.session_id === sessionId && row.ended_at === null);
  const interactive = !current.isError
    && attempt.ended_at === null
    && newestOpen?.id === attempt.id
    && attempt.session_started_at != null
    && attempt.session_started_at === current.data?.started_at
    && current.data?.task_id === attempt.task_id
    && (current.data?.state === "running" || current.data?.state === "draining");
  const session: SessionSummary = {
    id: attempt.session_id, name: attempt.agent_name || attempt.agent_id || attempt.session_id,
    task_id: attempt.task_id, harness: attempt.harness, provider: attempt.provider,
    state: attempt.state, work_dir: attempt.work_dir, started_at: attempt.started_at,
  };
  return <SessionDetailContent session={session} attempt={attempt} interactive={interactive} />;
}

function SessionDetailContent({ session, attempt, interactive }: { session: SessionSummary; attempt?: TaskSessionAttempt; interactive: boolean }) {
  const sessionId = session.id;
  const location = useLocation();
  const attach = useSessionAttach(interactive ? sessionId : "");
  const nudge = useSessionNudge();
  const kill = useSessionKill();
  const [text, setText] = useState("");
  const [streamOn, setStreamOn] = useState(true);
  const [viewMode, setViewMode] = useState<"transcript" | "pane">("transcript");
  const { entries, status, error, unavailable, clear } = useTranscriptStream(sessionId, {
    enabled: streamOn,
    attemptId: attempt?.id,
  });
  const running = session?.state === "running" || session?.state === "draining";
  const paneAvailable = interactive && (attempt ? true : running) && session?.provider === "tmux";
  const showingPane = paneAvailable && viewMode === "pane";

  const from = (location.state as { from?: string } | null)?.from ?? (attempt ? `/tasks/${encodeURIComponent(attempt.task_id)}` : workspaceHref(session.project_id, "sessions"));
  return (
    <div className="h-full overflow-y-auto p-6 space-y-4">
      <SessionBackLink from={from} toTask={!!attempt} />
      <header className="space-y-1">
        <p className="text-xs uppercase tracking-wider text-gray-500">Session</p>
        <h1 className="text-2xl font-bold">{session.name}</h1>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-500">
          <span>id: <span className="font-mono">{session.id}</span></span>
          <span>task: {session.task_id ? <Link className="text-indigo-400 hover:underline" to={`/tasks/${encodeURIComponent(session.task_id)}`} state={{ from }}>{session.task_id}</Link> : "—"}</span>
          {!attempt && <span>project: {session.project_id ? <Link className="text-indigo-400 hover:underline" to={workspaceHref(session.project_id, "sessions")}>{session.project_id}</Link> : "—"}</span>}
          <span>harness: {session.harness ?? "—"}</span>
          <span>provider: {session.provider ?? "—"}</span>
          {!attempt && <span>lifecycle: {session.lifecycle ?? "—"}</span>}
          <span>state: {session.state ?? "—"}</span>
          {!attempt && <span>idle: {Math.round(session.idle_seconds ?? 0)}s</span>}
          {session.stalled && (
            <span className="rounded bg-amber-500/10 px-1 text-amber-400">STALLED</span>
          )}
        </div>
        {attempt && (
          <dl className="grid gap-x-4 gap-y-2 pt-2 text-xs sm:grid-cols-2">
            <div><dt className="text-gray-500">Attempt</dt><dd className="font-mono">{attempt.id}</dd></div>
            <div><dt className="text-gray-500">Agent</dt><dd>{attempt.agent_id || "Not recorded"}</dd></div>
            <div><dt className="text-gray-500">Model</dt><dd>{attempt.model || "Not recorded"}</dd></div>
            <div><dt className="text-gray-500">Intelligence class</dt><dd>{attempt.intelligence_class || "Not recorded"}</dd></div>
            <div><dt className="text-gray-500">Started</dt><dd><AttemptTime value={attempt.started_at} /></dd></div>
            <div><dt className="text-gray-500">Ended</dt><dd><AttemptTime value={attempt.ended_at} /></dd></div>
            <div><dt className="text-gray-500">Outcome</dt><dd>{attempt.outcome || "Not recorded"}</dd></div>
            <div><dt className="text-gray-500">Exit reason</dt><dd className="whitespace-pre-wrap break-words">{attempt.end_reason || "Not recorded"}</dd></div>
          </dl>
        )}
        {attempt && !interactive && <p className="pt-2 text-sm text-gray-400">Historical attempt · read-only transcript</p>}
      </header>

      {interactive && <section className="grid gap-4 md:grid-cols-2">
        <div className="rounded border border-gray-800 bg-gray-950 p-3">
          <h2 className="mb-2 text-sm font-semibold text-gray-300">Attach</h2>
          {attach.data ? (
            <div className="flex items-center gap-2">
              <code className="flex-1 overflow-x-auto rounded bg-black/40 px-2 py-1 font-mono text-xs">
                {attach.data.attach_command}
              </code>
              <button
                className="rounded p-1 text-gray-400 hover:text-gray-200"
                aria-label="Copy attach command"
                onClick={() =>
                  navigator.clipboard.writeText(attach.data!.attach_command)
                }
              >
                <ClipboardIcon className="h-4 w-4" />
              </button>
            </div>
          ) : (
            <p className="text-xs text-gray-500">No attach command available.</p>
          )}
        </div>
        <div className="rounded border border-gray-800 bg-gray-950 p-3">
          <h2 className="mb-2 text-sm font-semibold text-gray-300">Nudge</h2>
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (!text.trim()) return;
              nudge.mutate(
                { session_id: sessionId, text },
                { onSuccess: () => setText("") },
              );
            }}
          >
            <input
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Message the agent…"
              className="flex-1 rounded border border-gray-800 bg-gray-900 px-2 py-1 text-sm text-gray-200"
            />
            <button
              type="submit"
              disabled={nudge.isPending || !text.trim()}
              className="inline-flex items-center gap-1 rounded bg-indigo-600 px-3 py-1 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              <PaperAirplaneIcon className="h-4 w-4" />
              Send
            </button>
          </form>
          {nudge.error && (
            <p className="mt-2 text-xs text-red-400">
              {(nudge.error as Error).message}
            </p>
          )}
        </div>
      </section>}

      <section className="overflow-hidden rounded-lg border border-gray-800 bg-gray-950">
        <div className="flex items-center justify-between border-b border-gray-800 px-3 py-2">
          <div className="flex items-center gap-3">
            <h2 className="text-sm font-semibold text-gray-300">
              {showingPane ? "Pane view" : "Transcript stream"}
              {!showingPane && <span className="ml-2 text-xs text-gray-500">({status})</span>}
            </h2>
            <div role="tablist" aria-label="Session views" className="inline-flex rounded border border-gray-800 text-xs">
              <button role="tab" aria-selected={!showingPane}
                onClick={() => setViewMode("transcript")}
                className={
                  "px-2 py-0.5 " +
                  (!showingPane
                    ? "bg-indigo-600 text-white"
                    : "text-gray-300 hover:bg-gray-900")
                }
              >
                Transcript
              </button>
              {paneAvailable && (
                <button role="tab" aria-selected={showingPane}
                  onClick={() => setViewMode("pane")}
                  className={
                    "px-2 py-0.5 " +
                    (showingPane
                      ? "bg-indigo-600 text-white"
                      : "text-gray-300 hover:bg-gray-900")
                  }
                >
                  Pane
                </button>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {!showingPane && (
              <>
                <button
                  onClick={() => setStreamOn((v) => !v)}
                  className="inline-flex items-center gap-1 rounded border border-gray-800 px-2 py-1 text-xs text-gray-300 hover:bg-gray-900"
                >
                  {streamOn ? <StopIcon className="h-3 w-3" /> : <PlayIcon className="h-3 w-3" />}
                  {streamOn ? "Pause" : "Resume"}
                </button>
                <button
                  onClick={clear}
                  className="inline-flex items-center gap-1 rounded border border-gray-800 px-2 py-1 text-xs text-gray-300 hover:bg-gray-900"
                >
                  <ArrowPathIcon className="h-3 w-3" />
                  Clear
                </button>
              </>
            )}
            {interactive && <button
              onClick={() => kill.mutate({ session_id: sessionId })}
              className="inline-flex items-center gap-1 rounded border border-red-900 px-2 py-1 text-xs text-red-400 hover:bg-red-950"
            >
              Kill
            </button>}
          </div>
        </div>
        {!showingPane && error && <p className="px-3 py-1 text-xs text-amber-400">{error}</p>}
        {showingPane ? (
          <div className="h-[60vh] min-h-80">
            <InteractiveTerminal key={sessionId} sessionId={sessionId} name={session.name} />
          </div>
        ) : (
          <div className="max-h-[60vh] overflow-y-auto p-3 font-mono text-xs">
            {entries.length === 0 ? (
              <p className="text-gray-500">{unavailable || (attempt && !interactive ? "No transcript output recorded for this attempt." : "Waiting for output…")}</p>
            ) : (
              entries.map((e) => (
                <div key={e._idx} className="mb-2 whitespace-pre-wrap">
                  <span className="mr-2 text-gray-600">
                    {e.source === "peek" ? "[peek]" : `[${e.type ?? "?"}]`}
                  </span>
                  <span className="text-gray-200">{e.text}</span>
                </div>
              ))
            )}
          </div>
        )}
      </section>
    </div>
  );
}

function SessionBackLink({ from, toTask }: { from: string; toTask: boolean }) {
  const location = useLocation();
  const taskPane = (location.state as { taskPane?: { taskId: string } } | null)?.taskPane;
  return (
    <Link to={from} state={taskPane ? { restoreTaskPane: taskPane } : undefined}
      className="text-sm text-indigo-400 hover:underline">
      {toTask ? "Back to task" : "Back to sessions"}
    </Link>
  );
}
