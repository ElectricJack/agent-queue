import { CommandLineIcon } from "@heroicons/react/24/outline";
import { useAgentFlock } from "../api/agents";
import type { Task } from "../api/hooks";
import { useAgentSelection } from "../pages/agents/useAgentSelection";

export default function TaskAgentTerminalButton({ task, onOpen }: {
  task: Task;
  onOpen?: () => void;
}) {
  const { data: agents, isError } = useAgentFlock();
  const { select } = useAgentSelection();
  // The flock validates current session ownership; task details may still name a previous worker.
  const agent = agents?.find((candidate) =>
    candidate.current_task_id === task.id &&
    candidate.current_project_id === task.project_id &&
    candidate.session_id && candidate.session_provider === "tmux" &&
    ["running", "draining"].includes(candidate.session_state ?? ""),
  );
  if (!agent || isError) return null;

  return (
    <button
      type="button"
      title={`Open ${agent.name}’s terminal`}
      onClick={() => { select(agent.id); onOpen?.(); }}
      className="inline-flex items-center gap-1.5 rounded-md border border-gray-600 bg-gray-800 px-3 py-1.5 text-sm font-medium text-gray-200 transition-colors hover:bg-gray-700"
    >
      <CommandLineIcon className="h-3.5 w-3.5" />
      Open agent terminal
    </button>
  );
}
