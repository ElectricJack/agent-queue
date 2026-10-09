/**
 * LivePaneConsole — renders one live `capture-pane` screen.
 *
 * A screen is a full snapshot, so this replaces rather than appends: no
 * scrollback (`followBottom` only keeps a tall screen's last rows in view,
 * for a phone typing to the agent). Colour comes from tmux's `capture-pane -e`
 * (SGR only) rendered by the existing ansiToSpans converter — tmux has
 * already done the terminal emulation, so no emulator is needed here.
 */
import { useEffect, useLayoutEffect, useRef } from "react";
import { ansiToSpans } from "../panes/console-stream/ansi";
import type { PaneStatus } from "../ws/usePaneStream";

interface LivePaneConsoleProps {
  screen: string | null;
  status: PaneStatus;
  error?: string | null;
  attempt?: number;
  /** Shows the "Reconnecting… Reconnect now" line; a caller with its own status and Retry omits it. */
  reconnect?: () => void;
  className?: string;
  /** CSS px (12–20 in watch mode). Rendering only: never the tmux window's size. */
  fontSize?: number;
  /** Dim the screen while it may be out of date (a dropped stream). */
  stale?: boolean;
  /**
   * Keep the bottom rows (the agent's prompt) in view on every frame and size
   * change, until the viewer scrolls up; scrolling back down re-pins it.
   */
  followBottom?: boolean;
}

/** Within this of the bottom counts as at the bottom. */
const PIN_SLACK_PX = 24;

export default function LivePaneConsole({
  screen,
  status,
  error,
  attempt,
  reconnect,
  className,
  fontSize,
  stale = false,
  followBottom = false,
}: LivePaneConsoleProps) {
  const box = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  useLayoutEffect(() => {
    const el = box.current;
    if (followBottom && el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [followBottom, screen, fontSize]);
  useEffect(() => {
    const el = box.current;
    if (!followBottom || !el) return;
    pinned.current = true;
    el.scrollTop = el.scrollHeight;
    if (typeof ResizeObserver === "undefined") return;
    // The keyboard opening shrinks the console: stay on the prompt.
    const observer = new ResizeObserver(() => {
      if (pinned.current) el.scrollTop = el.scrollHeight;
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [followBottom]);
  // The agent's columns are preserved: a wide screen scrolls sideways inside
  // the console, never the page (data-allow-overflow-x for the layout checks).
  return (
    <div
      ref={box}
      data-allow-overflow-x
      onScroll={followBottom ? (event) => {
        const el = event.currentTarget;
        pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight <= PIN_SLACK_PX;
      } : undefined}
      style={fontSize ? { fontSize } : undefined}
      className={
        "overflow-auto bg-black p-3 font-mono leading-tight text-green-200 " +
        (fontSize ? "" : "text-xs ") +
        (stale ? "opacity-60 " : "") +
        (className ?? "")
      }
    >
      {status === "reconnecting" && reconnect && (
        <p role="status" className="mb-1 text-gray-400">
          Reconnecting… (attempt {attempt}){" "}
          <button type="button" onClick={reconnect} className="underline">Reconnect now</button>
        </p>
      )}
      {status === "stopped" && (
        <p className="mb-1 text-amber-400">Session ended — last screen below.</p>
      )}
      {status === "error" && (
        <p className="mb-1 text-red-400">{error ?? "pane stream error"}</p>
      )}
      {screen === null ? (
        status === "error" ? null : (
          <p className="text-gray-500">Waiting for pane snapshot…</p>
        )
      ) : (
        <pre className="whitespace-pre">{ansiToSpans(screen)}</pre>
      )}
    </div>
  );
}
