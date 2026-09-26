import { useMemo } from "react";
import type { Task } from "../../api/hooks";
import { useProjectGraphs } from "../../api/graph";
import { useRecentActivity, type TaskActivityItem } from "../../api/activity";
import { useProviderHeldTasks, type ProviderHeldTask } from "../../api/providers";
import { useTaskWorkspace } from "./TaskWorkspace";
import { activityWindowHours, matchesTask } from "./taskFilters";

/** Render an activity row through the same table as a graph task row. */
export function activityToTask(item: TaskActivityItem): Task {
  const latest = item.attempts[0];
  return {
    id: item.task_id,
    title: item.title,
    status: item.status,
    project_id: item.project_id ?? "",
    priority: item.priority ?? undefined,
    parent_task_id: item.parent_task_id,
    assigned_agent: latest?.agent_name ?? latest?.agent_id ?? null,
    assigned_agent_id: latest?.agent_id ?? null,
    profile_id: latest?.profile_id ?? null,
    intelligence_class: latest?.intelligence_class ?? null,
    created_at: item.created_at ?? undefined,
    updated_at: item.updated_at ?? undefined,
    pr_url: item.pr_url,
  } as unknown as Task;
}

export interface TaskListRows {
  rows: Task[];
  isLoading: boolean;
  error: boolean;
  inWindow: boolean;
  activity: ReturnType<typeof useRecentActivity>;
  activityById: Map<string, TaskActivityItem>;
  held: ReturnType<typeof useProviderHeldTasks>;
  heldById: Map<string, ProviderHeldTask>;
  names: Map<string, string>;
}

/**
 * The Tasks tab's rows: the complete graph snapshots (or the activity read
 * while a time window is set), narrowed by the URL filters, in the tab's
 * order. The table and the focus task cards read the same rows (mobile
 * dashboard §4.2: same filters, same order).
 */
export function useTaskListRows(): TaskListRows {
  const { projectId, projectIds, projects, filters, isLoadingProjects, projectsError } = useTaskWorkspace();
  // The ordinary list endpoint truncates completed history. Both workspace
  // views use the complete graph snapshots so searches always cover the same tasks.
  const { data: graph, isLoading: graphLoading, errors } = useProjectGraphs(projectIds);
  // A time range answers "what was worked on", which the graph snapshot
  // cannot: it carries no per-attempt model and drops archived tasks.  The
  // activity read replaces the row source outright while a window is set.
  const windowHours = activityWindowHours(filters.window);
  const activity = useRecentActivity(windowHours, projectId);
  const inWindow = windowHours !== null;
  // "Held by provider" narrows to the server's held-task ids (D18/D20): a
  // row carries no hold of its own, and whether a provider outage holds a
  // task is the daemon's call, so the list is fetched only while the filter
  // is on and never inferred from status or profile.
  const held = useProviderHeldTasks(projectId, filters.held);
  const heldById = useMemo(() => new Map(
    (held.data?.tasks ?? []).map((item) => [item.task_id, item] as const)), [held.data]);
  const isLoading = (inWindow
    ? activity.isLoading
    : graphLoading || (!projectId && isLoadingProjects)) || (filters.held && held.isLoading);
  const error = (inWindow ? !!activity.error : projectsError || errors.some(Boolean))
    || (filters.held && held.isError);
  const activityById = useMemo(() => new Map(
    (activity.data?.items ?? []).map((item) => [item.task_id, item] as const)), [activity.data]);
  const tasks = useMemo<Task[]>(() => {
    if (inWindow) return (activity.data?.items ?? []).map(activityToTask);
    return graph.tasks.map((task) => ({
      ...task, project_id: graph.taskProject[task.id] ?? "",
      assigned_agent: task.assigned_agent_id, priority: task.priority ?? undefined,
    }));
  }, [graph, inWindow, activity.data]);
  const names = useMemo(() => new Map(projects.map((p) => [p.id, p.name || p.id])), [projects]);
  const rows = useMemo(
    () => tasks.filter((task) => (!projectId || task.project_id === projectId)
      && (!filters.held || heldById.has(task.id))
      && matchesTask(task, filters, names.get(task.project_id ?? "") ?? "")),
    [tasks, projectId, filters, names, heldById],
  );
  return { rows, isLoading, error: !!error, inWindow, activity, activityById, held, heldById, names };
}
