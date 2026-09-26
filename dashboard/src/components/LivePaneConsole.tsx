/**
 * LivePaneConsole — renders one live `capture-pane` screen.
 *
 * A screen is a full snapshot, so this replaces rather than scrolls: no
 * follow-tail, no scrollback. Colour comes from tmux's `capture-pane -e`
 * (SGR only) rendered by the existing ansiToSpans converter — tmux has
 * already done the terminal emulation, so no emulator is needed here.
 */
import { ansiToSpans } from "../panes/console-stream/ansi";
import type { PaneStatus } from "../ws/usePaneStream";

interface LivePaneConsoleProps {
  screen: string | null;
  status: PaneStatus;
  error?: string | null;
  className?: string;
  /** CSS px (12–20 in watch mode). Rendering only: never the tmux window's size. */
  fontSize?: number;
  /** Dim the screen while it may be out of date (a dropped stream). */
  stale?: boolean;
}

export default function LivePaneConsole({
  screen,
  status,
  error,
  className,
  fontSize,
  stale = false,
}: LivePaneConsoleProps) {
  // The agent's columns are preserved: a wide screen scrolls sideways inside
  // the console, never the page (data-allow-overflow-x for the layout checks).
  return (
    <div
      data-allow-overflow-x
      style={fontSize ? { fontSize } : undefined}
      className={
        "overflow-auto bg-black p-3 font-mono leading-tight text-green-200 " +
        (fontSize ? "" : "text-xs ") +
        (stale ? "opacity-60 " : "") +
        (className ?? "")
      }
    >
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
