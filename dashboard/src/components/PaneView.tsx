/**
 * PaneView — terminal-styled scrollback of session pane peek frames.
 *
 * Renders every peek-source frame from useTranscriptStream as a single
 * monospace scrollback area with follow-tail (auto-scroll when the user
 * is at the bottom; do NOT snap when they've scrolled up to read).
 *
 * Peek frames come from ``tmux capture-pane -p`` (src/sessions/tmux.py:445)
 * which emits plain rendered text — no ANSI escapes to strip.
 */
import { useEffect, useRef } from "react";
import type { TranscriptFrame } from "../ws/useTranscriptStream";

interface PaneViewProps {
  entries: TranscriptFrame[];
  className?: string;
}

export default function PaneView({ entries, className }: PaneViewProps) {
  const boxRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);

  const peekFrames = entries.filter((e) => e.source === "peek");

  // Follow-tail: auto-scroll only when the user is (nearly) at the bottom.
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    if (followRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [peekFrames.length]);

  const onScroll = () => {
    const el = boxRef.current;
    if (!el) return;
    const nearBottom =
      el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    followRef.current = nearBottom;
  };

  return (
    <div
      ref={boxRef}
      onScroll={onScroll}
      className={
        "max-h-[60vh] overflow-y-auto bg-black p-3 font-mono text-xs " +
        "leading-tight text-green-200 " +
        (className ?? "")
      }
    >
      {peekFrames.length === 0 ? (
        <p className="text-gray-500">
          Waiting for pane snapshot… (peek frames arrive whenever the
          harness has no readable transcript, or on fallback)
        </p>
      ) : (
        peekFrames.map((f) => (
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
