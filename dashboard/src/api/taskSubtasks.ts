import { queryOptions, useQuery } from "@tanstack/react-query";
import { taskSubtasks } from "./client";

/**
 * The durable subtask checklist a single agent ticks off inside one task
 * (``task_subtasks``) -- distinct from the hierarchy's own "subtasks" (child
 * tasks flagged ``is_plan_subtask``, surfaced via ``TaskRef``).
 *
 * ``task.subtasks_updated`` already matches the generic ``task.*`` prefix
 * invalidation in ``useEventStream`` (it invalidates ``["task", taskId]``),
 * so this query key nests under that prefix rather than adding a new
 * subscription.
 */
export const taskSubtasksQuery = (taskId: string) => queryOptions({
  queryKey: ["task", taskId, "subtasks"],
  queryFn: async () => (await taskSubtasks({
    body: { task_id: taskId }, throwOnError: true,
  })).data,
});

export function useTaskSubtasks(taskId: string) {
  return useQuery({ ...taskSubtasksQuery(taskId), enabled: !!taskId });
}
