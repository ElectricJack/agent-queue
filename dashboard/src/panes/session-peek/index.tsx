/**
 * session-peek pane view — live tmux pane snapshot stream for one session.
 *
 * Same data hook as dashboard/src/pages/SessionDetail.tsx's pane-view
 * toggle, rehosted inside the shell's <ShellPane> right surface. A `screen`
 * frame is a full `capture-pane` snapshot that supersedes the last one, so
 * there is no scrollback and no follow-tail state to track here.
 *
 * See docs/superpowers/specs/2026-08-22-pane-session-peek-design.md.
 */
import { useEffect, useState } from "react";
import {
  ClipboardIcon,
  ArrowTopRightOnSquareIcon,
  XCircleIcon,
} from "@heroicons/react/24/outline";
import { useNavigate } from "react-router-dom";
import { usePaneStream } from "../../ws/usePaneStream";
import { useSession, useSessionKill } from "../../api/hooks";
import LivePaneConsole from "../../components/LivePaneConsole";
import type { PaneViewProps } from "../types";
import type { SessionPeekArgs } from "./manifest";

export default function SessionPeekPane({
  args,
  setToolbar,
  setShortcuts,
}: PaneViewProps<SessionPeekArgs>) {
  const { sessionId } = args;
  const navigate = useNavigate();

  const { data: session } = useSession(sessionId);
  const kill = useSessionKill();
  const { screen, status, error } = usePaneStream(sessionId, { enabled: true });

  const exited = session?.lifecycle === "exited" || session?.lifecycle === "terminated";

  const [confirmingKill, setConfirmingKill] = useState(false);

  const copyScrollback = () => navigator.clipboard.writeText(screen ?? "");
  const openFullSession = () => navigate(`/sessions/${sessionId}`);
  const doKill = () => {
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
        id: "copy-scrollback",
        label: "Copy scrollback",
        icon: ClipboardIcon,
        onClick: copyScrollback,
        disabled: !screen,
      },
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
  }, [screen, confirmingKill, exited]);

  useEffect(() => {
    setShortcuts([
      { key: "k", label: "Kill session", onFire: doKill },
      { key: "o", label: "Open full session detail", onFire: openFullSession },
      { key: "c", label: "Copy scrollback", onFire: copyScrollback },
    ]);
    return () => setShortcuts([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [confirmingKill, exited]);

  return (
    <div className="flex h-full flex-col">
      {exited && (
        <div className="border-b border-amber-900/60 bg-amber-950/40 px-3 py-1.5 text-xs text-amber-300">
          Session exited — showing last scrollback.
        </div>
      )}
      <LivePaneConsole screen={screen} status={status} error={error} className="flex-1" />
    </div>
  );
}
