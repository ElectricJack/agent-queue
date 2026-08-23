/**
 * PaneView — terminal-styled scrollback of session pane peek frames, sized
 * for the SessionDetail full-page pane-view toggle.
 *
 * Rendering lives in PeekFrameConsole (shared with panes/session-peek);
 * this component owns SessionDetail's specific follow-tail ref and
 * max-height wrapper.
 */
import { useEffect, useRef } from "react";
import PeekFrameConsole from "./PeekFrameConsole";
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
    <PeekFrameConsole
      frames={peekFrames}
      containerRef={boxRef}
      onScroll={onScroll}
      className={"max-h-[60vh] " + (className ?? "")}
    />
  );
}
