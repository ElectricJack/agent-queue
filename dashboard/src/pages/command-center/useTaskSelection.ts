import { useCallback } from "react";
import { useShellPaneStore } from "../../panes/store";

/** Selection follows the detail pane, including its close button and Escape. */
export function useTaskSelection() {
  const { state, open, close } = useShellPaneStore();
  const taskPane = state.kind === "open" && state.view === "task-detail";
  const args = taskPane ? state.args as { taskId?: string } : null;
  const selectedTaskId = args?.taskId ?? null;
  const selectTask = useCallback((taskId: string) => open("task-detail", { taskId }), [open]);
  const clearTask = useCallback(() => { if (taskPane) close(); }, [taskPane, close]);
  return { selectedTaskId, selectTask, clearTask };
}
