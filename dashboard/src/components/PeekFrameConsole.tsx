/**
 * PeekFrameConsole — monospace scrollback rendering of session pane peek
 * frames. Pure rendering: no follow-tail logic, no data fetching. Callers
 * (PaneView.tsx, panes/session-peek/index.tsx) own scroll behavior via
 * `containerRef` + `onScroll` and own data fetching via useTranscriptStream.
 *
 * Peek frames come from `tmux capture-pane -p` (src/sessions/tmux.py:445),
 * plain rendered text — no ANSI stripping needed.
 */
import type { Ref, UIEvent } from "react";
import type { TranscriptFrame } from "../ws/useTranscriptStream";

interface PeekFrameConsoleProps {
  frames: TranscriptFrame[];
  containerRef?: Ref<HTMLDivElement>;
  onScroll?: (e: UIEvent<HTMLDivElement>) => void;
  className?: string;
}

export default function PeekFrameConsole({
  frames,
  containerRef,
  onScroll,
  className,
}: PeekFrameConsoleProps) {
  return (
    <div
      ref={containerRef}
      onScroll={onScroll}
      className={
        "overflow-y-auto bg-black p-3 font-mono text-xs leading-tight text-green-200 " +
        (className ?? "")
      }
    >
      {frames.length === 0 ? (
        <p className="text-gray-500">
          Waiting for pane snapshot… (peek frames arrive whenever the
          harness has no readable transcript, or on fallback)
        </p>
      ) : (
        frames.map((f) => (
          <pre
            key={f._idx}
            className="whitespace-pre-wrap border-b border-gray-900/40 py-1"
          >
            {f.text}
          </pre>
        ))
      )}
    </div>
  );
}
