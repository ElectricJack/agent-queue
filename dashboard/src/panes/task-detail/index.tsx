import { useCallback } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useShellPaneStore } from "../store";
import type { PaneViewProps } from "../types";
import type { TaskDetailArgs } from "./manifest";
import TaskDetailBody from "./TaskDetailBody";

/** The right-surface pane: the shared task detail wired to the pane store. */
export default function TaskDetailPane({ args, setToolbar, setShortcuts }: PaneViewProps<TaskDetailArgs>) {
  const navigate = useNavigate();
  const location = useLocation();
  const { open, close } = useShellPaneStore();
  const from = (location.state as { from?: string } | null)?.from ?? location.pathname + location.search;
  const openFull = useCallback(() => {
    close();
    navigate(`/tasks/${encodeURIComponent(args.taskId)}`, { state: { from } });
  }, [args.taskId, close, from, navigate]);
  const openTask = useCallback((taskId: string) => open("task-detail", { taskId }), [open]);
  return (
    <TaskDetailBody
      taskId={args.taskId}
      onOpenTask={openTask}
      onClose={close}
      fromTaskPane
      host={{ setToolbar, setShortcuts, openFull }}
    />
  );
}
