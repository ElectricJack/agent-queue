/**
 * session-peek pane view — one session's terminal inside the shell's
 * <ShellPane> right surface.
 *
 * It is the host shell page's terminal, the same component every agent
 * terminal renders (`components/InteractiveTerminal`), so a pane that opens an
 * agent's tmux is not a second kind of terminal. It used to replay
 * `capture-pane` snapshots through `LivePaneConsole` — a `<pre>` of ANSI spans
 * in a green-on-black console with no palette, no scrollback and no touch
 * scrolling.
 *
 * See docs/superpowers/specs/2026-08-22-pane-session-peek-design.md.
 */
import { useEffect, useState } from "react";
import { ArrowTopRightOnSquareIcon, XCircleIcon } from "@heroicons/react/24/outline";
import { useLocation, useNavigate } from "react-router-dom";
import { useSession, useSessionKill } from "../../api/hooks";
import InteractiveTerminal from "../../components/InteractiveTerminal";
import type { PaneViewProps } from "../types";
import type { SessionPeekArgs } from "./manifest";

export default function SessionPeekPane({
  args,
  close,
  setToolbar,
  setShortcuts,
}: PaneViewProps<SessionPeekArgs>) {
  const { sessionId } = args;
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? location.pathname + location.search;

  const { data: session } = useSession(sessionId);
  const kill = useSessionKill();

  const exited = session?.lifecycle === "exited" || session?.lifecycle === "terminated";
  const live = session?.state === "running" || session?.state === "draining";

  const [confirmingKill, setConfirmingKill] = useState(false);

  const openFullSession = () => {
    close();
    navigate(`/sessions/${encodeURIComponent(sessionId)}`, { state: { from } });
  };
  const doKill = () => {
    if (exited) return;
    if (!confirmingKill) {
      setConfirmingKill(true);
      return;
    }
    kill.mutate({ session_id: sessionId });
    setConfirmingKill(false);
  };

  useEffect(() => {
    setToolbar([
      {
        id: "open-full",
        label: "Open full session detail",
        icon: ArrowTopRightOnSquareIcon,
        onClick: openFullSession,
      },
      {
        id: "kill-session",
        label: confirmingKill ? "Confirm kill?" : "Kill session",
        icon: XCircleIcon,
        onClick: doKill,
        disabled: exited,
      },
    ]);
    return () => setToolbar([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [exited, confirmingKill]);

  useEffect(() => {
    setShortcuts([
      { key: "o", label: "Open full session detail", onFire: openFullSession },
      { key: "k", label: "Kill session", onFire: doKill },
    ]);
    return () => setShortcuts([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [confirmingKill, exited]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      {exited && (
        <div className="border-b border-amber-900/60 bg-amber-950/40 px-3 py-1.5 text-xs text-amber-300">
          Session exited — open full session detail for its transcript.
        </div>
      )}
      {!live && !exited && (
        <div className="border-b border-gray-800 px-3 py-1.5 text-xs text-gray-400">
          This session is {session?.state ?? "unknown"}; its terminal is not attached.
        </div>
      )}
      {live && (
        <div className="min-h-0 flex-1">
          <InteractiveTerminal sessionId={sessionId} name={session?.name ?? sessionId} />
        </div>
      )}
    </div>
  );
}
