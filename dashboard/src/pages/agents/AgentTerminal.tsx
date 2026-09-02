import { CommandLineIcon } from "@heroicons/react/24/outline";
import { useStartAgentTerminal, type FlockAgent } from "../../api/agents";
import type { SessionSummary } from "../../api/hooks";
import InteractiveTerminal from "../../components/InteractiveTerminal";

export default function AgentTerminal({ agent }: { agent: FlockAgent }) {
  const running = !!agent.session_id && (agent.session_state === "running" || agent.session_state === "draining");
  const tmux = agent.session_provider === "tmux";
  const start = useStartAgentTerminal();
  const sleeping = agent.session_state === "sleeping";
  const starting = agent.session_state === "starting" || agent.session_state === "stopping";
  const taskOwned = !!agent.current_task_id || agent.state === "busy";
  const unsupported = !!agent.session_provider && !tmux;
  const canStart = agent.enabled !== false && !taskOwned && !starting && !unsupported;

  if (running && !tmux) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center">
        <CommandLineIcon className="mb-1 h-8 w-8 text-gray-600" />
        <p className="text-sm text-gray-300">Tmux view unavailable</p>
        <p className="max-w-sm text-xs leading-relaxed text-gray-500">
          {agent.session_provider
            ? "This session uses " + agent.session_provider + "; no tmux pane is available."
            : "This session's terminal transport is unknown."}
        </p>
      </div>
    );
  }
  if (!running) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 overflow-auto p-6 text-center">
        <CommandLineIcon className="h-8 w-8 shrink-0 text-gray-600" />
        <p className="text-sm text-gray-300">
          {sleeping ? "Session is sleeping" : "No active tmux session"}
        </p>
        <p className="max-w-sm text-xs leading-relaxed text-gray-500">
          {agent.session_id
            ? "Session state: " + (agent.session_state || "unknown") + ". Viewing this agent will not wake or restart it."
            : "This worker has no live terminal. Viewing it does not start a session."}
        </p>
        <button type="button" disabled={!canStart || start.isPending}
          onClick={() => start.mutate({ agent_id: agent.id })}
          className="rounded bg-indigo-600 px-3 py-2 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-40">
          {start.isPending || starting ? "Starting…" : sleeping ? "Resume terminal" : "Start terminal"}
        </button>
        {!canStart && (
          <p className="max-w-sm text-xs text-gray-500">
            {taskOwned ? "The assigned task controls this session."
              : agent.enabled === false ? "Enable this agent in Settings before starting a terminal."
                : unsupported ? "Interactive terminals require the tmux provider."
                  : "The session is already changing state."}
          </p>
        )}
        {start.error && <p role="alert" className="max-w-sm text-xs text-red-300">{start.error.message}</p>}
      </div>
    );
  }
  return <InteractiveTerminal key={agent.session_id} sessionId={agent.session_id!} name={agent.name} />;
}

/**
 * The tmux pane of one pool instance.
 *
 * A pool session belongs to the daemon's sizer, not to the viewer: there is no
 * start button here, because starting a worker is what raising ``min_active``
 * on the Settings tab does.
 */
export function PoolInstanceTerminal({ instance }: { instance: SessionSummary | null }) {
  const live = instance && (instance.state === "running" || instance.state === "draining");
  // `SessionSummary.provider` is the session transport (tmux), not the LLM one.
  const tmux = !instance?.provider || instance.provider === "tmux";
  if (!live || !tmux) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center">
        <CommandLineIcon className="mb-1 h-8 w-8 text-gray-600" />
        <p className="text-sm text-gray-300">
          {!instance ? "No live pool instance" : !live ? "Instance is not running" : "Tmux view unavailable"}
        </p>
        <p className="max-w-sm text-xs leading-relaxed text-gray-500">
          {!instance
            ? "The daemon starts a worker when this pool has demand and its bounds allow it."
            : !live
              ? "Session state: " + (instance.state || "unknown") + ". Pool sessions are started and stopped by the daemon."
              : "This session uses " + instance.provider + "; no tmux pane is available."}
        </p>
      </div>
    );
  }
  return <InteractiveTerminal key={instance.id} sessionId={instance.id} name={instance.name} />;
}
