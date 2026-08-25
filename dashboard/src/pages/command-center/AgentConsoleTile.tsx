/**
 * AgentConsoleTile — one agent's live terminal screen in the Agents grid.
 *
 * Subscribes only while mounted, so tiles cost nothing in Table view.
 *
 * The tile is a `div`, not a `button`: `<button>` takes phrasing content
 * only, and wrapping a scrollable console in one meant that scrolling the
 * screen or selecting text inside it fired the click and navigated away.
 * The header is the clickable affordance, so keyboard access to "open this
 * session" survives.
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
    <div className="flex flex-col overflow-hidden rounded border border-gray-800 focus-within:border-gray-600 hover:border-gray-600">
      <button
        type="button"
        aria-label={`Open ${title}`}
        onClick={onOpen}
        className="flex items-baseline justify-between gap-2 bg-gray-900 px-2 py-1 text-left hover:bg-gray-800 focus:outline-none focus-visible:ring-1 focus-visible:ring-gray-500"
      >
        <span className="truncate font-mono text-xs text-gray-200">{title}</span>
        <span className="shrink-0 text-[10px] uppercase text-gray-500">
          {subtitle ?? status}
        </span>
      </button>
      <LivePaneConsole
        screen={screen}
        status={status}
        error={error}
        className="h-48 w-full"
      />
    </div>
  );
}
