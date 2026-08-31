import { useCallback } from "react";
import { useShellPaneStore } from "../../panes/store";

type SelectableTask = { id: string; playbook_run_id?: string | null };

/** Selection follows the detail pane, including its close button and Escape. */
export function useTaskSelection() {
  const { state, open, close } = useShellPaneStore();
  const taskPane = state.kind === "open" && state.view === "task-detail";
  const runPane = state.kind === "open" && state.view === "playbook-run-inspector";
  const args = taskPane || runPane ? state.args as { taskId?: string } : null;
  const selectedTaskId = args?.taskId ?? null;
  const selectTask = useCallback((task: SelectableTask) => {
    if (task.playbook_run_id) {
      open("playbook-run-inspector", { runId: task.playbook_run_id, taskId: task.id });
    } else {
      open("task-detail", { taskId: task.id });
    }
  }, [open]);
  const clearTask = useCallback(() => { if (taskPane || runPane) close(); }, [taskPane, runPane, close]);
  return { selectedTaskId, selectTask, clearTask };
}
