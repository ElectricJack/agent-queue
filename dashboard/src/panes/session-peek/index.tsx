/**
 * session-peek pane view — live tmux peek-frame scrollback for one session.
 *
 * Same rendering + data hook as dashboard/src/components/PaneView.tsx
 * (SessionDetail's pane-view toggle), rehosted inside the shell's
 * <ShellPane> right surface. Follow-tail state lives in pane `args.tail`
 * (not a local ref) so the toolbar button, keyboard shortcuts, and any
 * future route persistence all observe/flip the same source of truth.
 *
 * See docs/superpowers/specs/2026-08-22-pane-session-peek-design.md.
 */
import { useEffect, useRef, useState } from "react";
import {
  PlayIcon,
  StopIcon,
  ClipboardIcon,
  ArrowTopRightOnSquareIcon,
  XCircleIcon,
} from "@heroicons/react/24/outline";
import { useNavigate } from "react-router-dom";
import { useTranscriptStream } from "../../ws/useTranscriptStream";
import { useSession, useSessionKill } from "../../api/hooks";
import PeekFrameConsole from "../../components/PeekFrameConsole";
import type { PaneViewProps } from "../types";
import type { SessionPeekArgs } from "./manifest";

export default function SessionPeekPane({
  args,
  setArgs,
  setToolbar,
  setShortcuts,
}: PaneViewProps<SessionPeekArgs>) {
  const { sessionId } = args;
  const tail = args.tail ?? true;
  const navigate = useNavigate();

  const { data: session } = useSession(sessionId);
  const kill = useSessionKill();
  const { entries, status, error } = useTranscriptStream(sessionId, { enabled: true });

  const peekFrames = entries.filter((e) => e.source === "peek");
  const exited = session?.lifecycle === "exited" || session?.lifecycle === "terminated";

  const boxRef = useRef<HTMLDivElement>(null);
  const [confirmingKill, setConfirmingKill] = useState(false);

  // Follow-tail: snap to bottom on new frames when args.tail is on.
  useEffect(() => {
    if (!tail) return;
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [peekFrames.length, tail]);

  // Sticky-when-scrolled-up: manual scroll away from bottom turns tail off.
  // Re-enabling is explicit (toolbar / space / End).
  const onScroll = () => {
    const el = boxRef.current;
    if (!el || !tail) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    if (!nearBottom) setArgs({ ...args, tail: false });
  };

  const copyScrollback = () =>
    navigator.clipboard.writeText(peekFrames.map((f) => f.text).join("\n"));
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
        id: "toggle-tail",
        label: tail ? "Pause tail" : "Follow tail",
        icon: tail ? StopIcon : PlayIcon,
        onClick: () => setArgs({ ...args, tail: !tail }),
      },
      {
        id: "copy-scrollback",
        label: "Copy scrollback",
        icon: ClipboardIcon,
        onClick: copyScrollback,
        disabled: peekFrames.length === 0,
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
  }, [tail, peekFrames.length, confirmingKill, exited]);

  useEffect(() => {
    setShortcuts([
      {
        key: "space",
        label: "Toggle follow tail",
        onFire: () => setArgs({ ...args, tail: !tail }),
      },
      { key: "k", label: "Kill session", onFire: doKill },
      { key: "o", label: "Open full session detail", onFire: openFullSession },
      { key: "c", label: "Copy scrollback", onFire: copyScrollback },
      {
        key: "Home",
        label: "Scroll to top",
        onFire: () => {
          setArgs({ ...args, tail: false });
          if (boxRef.current) boxRef.current.scrollTop = 0;
        },
      },
      {
        key: "End",
        label: "Scroll to bottom",
        onFire: () => {
          setArgs({ ...args, tail: true });
          if (boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
        },
      },
    ]);
    return () => setShortcuts([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tail, confirmingKill, exited]);

  return (
    <div className="flex h-full flex-col">
      {exited && (
        <div className="border-b border-amber-900/60 bg-amber-950/40 px-3 py-1.5 text-xs text-amber-300">
          Session exited — showing last scrollback.
        </div>
      )}
      {error && (
        <p className="border-b border-gray-800 px-3 py-1 text-xs text-amber-400">{error}</p>
      )}
      {status === "connecting" && peekFrames.length === 0 && (
        <p className="px-3 py-2 text-xs text-gray-500">Connecting…</p>
      )}
      <PeekFrameConsole
        frames={peekFrames}
        containerRef={boxRef}
        onScroll={onScroll}
        className="flex-1"
      />
    </div>
  );
}
