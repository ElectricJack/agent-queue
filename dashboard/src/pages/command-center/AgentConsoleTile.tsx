/**
 * AgentConsoleTile — one agent's live terminal screen in the Agents grid.
 *
 * Subscribes only while mounted, so tiles cost nothing in Table view.
 */
import { usePaneStream } from "../../ws/usePaneStream";
import LivePaneConsole from "../../components/LivePaneConsole";

interface AgentConsoleTileProps {
  sessionId: string;
  title: string;
  subtitle?: string;
  onOpen?: () => void;
}

export default function AgentConsoleTile({
  sessionId,
  title,
  subtitle,
  onOpen,
}: AgentConsoleTileProps) {
  const { screen, status, error } = usePaneStream(sessionId, { enabled: true });

  return (
    <button
      type="button"
      aria-label={`Open ${title}`}
      onClick={onOpen}
      className="flex flex-col overflow-hidden rounded border border-gray-800 text-left hover:border-gray-600"
    >
      <span className="flex items-baseline justify-between gap-2 bg-gray-900 px-2 py-1">
        <span className="truncate font-mono text-xs text-gray-200">{title}</span>
        <span className="shrink-0 text-[10px] uppercase text-gray-500">
          {subtitle ?? status}
        </span>
      </span>
      <LivePaneConsole
        screen={screen}
        status={status}
        error={error}
        className="h-48 w-full"
      />
    </button>
  );
}
