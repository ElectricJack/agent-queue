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
}

export default function LivePaneConsole({
  screen,
  status,
  error,
  className,
}: LivePaneConsoleProps) {
  return (
    <div
      className={
        "overflow-auto bg-black p-3 font-mono text-xs leading-tight text-green-200 " +
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
