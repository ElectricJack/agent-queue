import { CommandLineIcon } from "@heroicons/react/24/outline";
import { useStartAgentTerminal, type FlockAgent } from "../../api/agents";
import { usePaneStream } from "../../ws/usePaneStream";
import InteractiveTerminal from "../../components/InteractiveTerminal";

export default function AgentTerminal({ agent }: { agent: FlockAgent }) {
  const running = !!agent.session_id && (agent.session_state === "running" || agent.session_state === "draining");
  const tmux = agent.session_provider === "tmux";
  const pane = usePaneStream(agent.session_id, { enabled: running && tmux });
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
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center justify-between border-b border-gray-800 px-3 py-1 text-[10px] text-gray-500">
        <span>Live tmux · interactive</span>
        <span className="capitalize">{pane.status}</span>
      </div>
      <InteractiveTerminal key={agent.session_id} sessionId={agent.session_id!} name={agent.name}
        screen={pane.screen} status={pane.status} error={pane.error} />
    </div>
  );
}
