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
import { useTranscriptStream } from "../ws/useTranscriptStream";
import InteractiveTerminal from "../components/InteractiveTerminal";
import { workspaceHref } from "../shell/projectNavigation";

export default function SessionDetail() {
  const { sessionId = "" } = useParams();
  const location = useLocation();
  const { data: session, isLoading } = useSession(sessionId);
  const attach = useSessionAttach(sessionId);
  const nudge = useSessionNudge();
  const kill = useSessionKill();
  const [text, setText] = useState("");
  const [streamOn, setStreamOn] = useState(true);
  const [viewMode, setViewMode] = useState<"transcript" | "pane">("transcript");
  const { entries, status, error, clear } = useTranscriptStream(sessionId, {
    enabled: streamOn,
  });
  const running = session?.state === "running" || session?.state === "draining";
  const paneAvailable = running && session?.provider === "tmux";
  const showingPane = paneAvailable && viewMode === "pane";

  if (isLoading) return <div className="p-6 text-gray-400">Loading…</div>;
  if (!session) return <div className="p-6 text-gray-400">Session not found</div>;

  const from = (location.state as { from?: string } | null)?.from ?? workspaceHref(session.project_id, "sessions");
  return (
    <div className="h-full overflow-y-auto p-6 space-y-4">
      <Link to={from} className="text-sm text-indigo-400 hover:underline">Back to sessions</Link>
      <header className="space-y-1">
        <p className="text-xs uppercase tracking-wider text-gray-500">Session</p>
        <h1 className="text-2xl font-bold">{session.name}</h1>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-500">
          <span>id: <span className="font-mono">{session.id}</span></span>
          <span>task: {session.task_id ? <Link className="text-indigo-400 hover:underline" to={`/tasks/${encodeURIComponent(session.task_id)}`} state={{ from }}>{session.task_id}</Link> : "—"}</span>
          <span>project: {session.project_id ? <Link className="text-indigo-400 hover:underline" to={workspaceHref(session.project_id, "sessions")}>{session.project_id}</Link> : "—"}</span>
          <span>harness: {session.harness ?? "—"}</span>
          <span>provider: {session.provider ?? "—"}</span>
          <span>lifecycle: {session.lifecycle ?? "—"}</span>
          <span>state: {session.state ?? "—"}</span>
          <span>idle: {Math.round(session.idle_seconds ?? 0)}s</span>
          {session.stalled && (
            <span className="rounded bg-amber-500/10 px-1 text-amber-400">STALLED</span>
          )}
        </div>
      </header>

      <section className="grid gap-4 md:grid-cols-2">
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
      </section>

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
            <button
              onClick={() => kill.mutate({ session_id: sessionId })}
              className="inline-flex items-center gap-1 rounded border border-red-900 px-2 py-1 text-xs text-red-400 hover:bg-red-950"
            >
              Kill
            </button>
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
              <p className="text-gray-500">Waiting for output…</p>
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
