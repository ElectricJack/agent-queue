import { ClipboardDocumentListIcon } from "@heroicons/react/24/outline";
import { useShellPaneStore } from "../../panes/store";
import { TERMINAL_TOOL } from "../../components/TerminalPane";
import type { FlockAgent } from "../../api/agents";

/** Header shortcut for the agent's current task; absent while the agent is idle. */
export default function AgentShowTaskButton({ agent }: { agent: FlockAgent }) {
  const { open } = useShellPaneStore();
  if (!agent.current_task_id) return null;
  const taskId = agent.current_task_id;
  return <button type="button" data-primary-control aria-label="Show task"
    title={(agent.current_task_title || taskId) + " (" + taskId + ")"}
    onClick={() => open("task-detail", { taskId })}
    className={TERMINAL_TOOL}>
    <ClipboardDocumentListIcon aria-hidden="true" className="h-4 w-4" />
    Show task
  </button>;
}
