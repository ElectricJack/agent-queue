import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { Task } from "../../api/hooks";
import { useProjectGraphs } from "../../api/graph";
import { useRecentActivity, type TaskActivityItem } from "../../api/activity";
import { useListNav } from "../../shell/hotkeys/useListNav";
import { useTaskWorkspace } from "./TaskWorkspace";
import { activityWindowHours, activityWindowLabel, matchesTask } from "./taskFilters";
import { ActivityCell, ModelsCell } from "./TaskActivityCells";
import { absoluteTime } from "./activityFormat";
import { CopyTaskIdButton } from "./CopyTaskIdButton";
import { InlinePriority, InlineStatus, RowActions } from "./TaskRowActions";
import { useTaskSelection } from "./useTaskSelection";

/** Render an activity row through the same table as a graph task row. */
function activityToTask(item: TaskActivityItem): Task {
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

export default function CommandCenterTasks() {
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
  const isLoading = inWindow
    ? activity.isLoading
    : graphLoading || (!projectId && isLoadingProjects);
  const error = inWindow ? !!activity.error : projectsError || errors.some(Boolean);
  const activityById = useMemo(() => new Map(
    (activity.data?.items ?? []).map((item) => [item.task_id, item] as const)), [activity.data]);
  const tasks = useMemo<Task[]>(() => {
    if (inWindow) return (activity.data?.items ?? []).map(activityToTask);
    return graph.tasks.map((task) => ({
      ...task, project_id: graph.taskProject[task.id] ?? "",
      assigned_agent: task.assigned_agent_id, priority: task.priority ?? undefined,
    }));
  }, [graph, inWindow, activity.data]);
  const { selectedTaskId, selectTask, clearTask } = useTaskSelection();
  // loop: false — with virtualized rows "next after the last mounted row"
  // is the overscan edge, not the end of the list, so wrapping would jump
  // the focus a window up instead of to the first task.
  const bodyRef = useListNav<HTMLTableSectionElement>({ axis: "vertical", loop: false });
  const names = useMemo(() => new Map(projects.map((p) => [p.id, p.name || p.id])), [projects]);
  const filtered = useMemo(
    () => tasks.filter((task) => (!projectId || task.project_id === projectId)
      && matchesTask(task, filters, names.get(task.project_id ?? "") ?? "")),
    [tasks, projectId, filters, names],
  );
  const columns = (projectId ? 5 : 6) + (inWindow ? 2 : 0);
  const scrollRef = useRef<HTMLDivElement>(null);
  // The scroll element is the padded region, and the count line plus the
  // sticky header sit above the first row, so row 0 does not start at
  // scrollTop 0. scrollMargin tells the virtualizer where the list begins;
  // without it the visible window is computed ~1.5 rows early.
  const [scrollMargin, setScrollMargin] = useState(0);
  useLayoutEffect(() => {
    const scroller = scrollRef.current;
    const body = bodyRef.current;
    if (!scroller || !body) return;
    const offset = body.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
    setScrollMargin(Math.max(0, Math.round(offset)));
  }, [bodyRef, error, isLoading, projectId, inWindow]);
  // Only the rows in view are mounted: the graph snapshot carries every task
  // in the project, and a 5,000-row table with three interactive cells per
  // row re-rendered on every keystroke and every live refetch. Keyboard list
  // navigation (useListNav) walks mounted rows, i.e. the window + overscan.
  const virtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 64,
    overscan: 12,
    scrollMargin,
  });
  const items = virtualizer.getVirtualItems();
  // item.start/end include scrollMargin; getTotalSize() does not.
  const padTop = items.length ? items[0]!.start - scrollMargin : 0;
  const padBottom = items.length
    ? virtualizer.getTotalSize() - (items[items.length - 1]!.end - scrollMargin)
    : 0;

  return (
    <div role="region" aria-label="Task list" ref={scrollRef} className="h-full min-h-0 overflow-auto p-4"
      onClick={(event) => {
        const target = event.target as HTMLElement;
        if (!target.closest('[data-task-row], button, input, select, textarea, a, [role="dialog"]')) clearTask();
      }}>
      <p className="mb-3 text-xs text-gray-500">{filtered.length} {filtered.length === 1 ? "task" : "tasks"}</p>
      {inWindow && (
        <p role="status" className="mb-3 rounded border border-indigo-500/30 bg-indigo-500/10 px-3 py-2 text-xs text-indigo-100">
          <span className="font-medium">{activityWindowLabel(filters.window)}</span>
          {activity.data
            ? <> — work between <span title={absoluteTime(activity.data.since)}>{absoluteTime(activity.data.since)}</span>{" "}
              and <span title={absoluteTime(activity.data.until)}>{absoluteTime(activity.data.until)}</span>.{" "}
              Completed and in-progress tasks both count; the model column shows what each session attempt reported.
              {activity.data.truncated && <> Showing the {activity.data.items.length} most recent of {activity.data.total}.</>}</>
            : <> — loading work in this window…</>}
        </p>
      )}
      {error && <p role="alert" className="mb-3 rounded border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">Could not load tasks. Check the backend connection and try again.</p>}
      <table className="w-full min-w-[620px] text-left text-sm" aria-rowcount={filtered.length + 1}>
        <thead className="sticky top-0 z-10 border-b border-gray-800 bg-gray-950 text-xs uppercase text-gray-500">
          <tr>
            <th className="px-3 py-3">Task</th>
            {!projectId && <th className="px-3 py-3">Project</th>}
            <th className="px-3 py-3">Status</th>
            <th className="px-3 py-3">Priority</th>
            <th className="px-3 py-3">Agent</th>
            {inWindow && <th className="px-3 py-3">Models</th>}
            {inWindow && <th className="px-3 py-3">Last activity</th>}
            <th className="px-3 py-3"><span className="sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody ref={bodyRef} className="divide-y divide-gray-800">
          {isLoading && <tr><td colSpan={columns} className="p-4 text-gray-500">Loading tasks…</td></tr>}
          {!isLoading && !error && filtered.length === 0 && <tr><td colSpan={columns} className="p-8 text-center text-gray-500">{inWindow ? "No work recorded in this time range." : "No tasks match these filters."}</td></tr>}
          {padTop > 0 && <tr aria-hidden="true"><td colSpan={columns} style={{ height: padTop, padding: 0, border: 0 }} /></tr>}
          {items.map((item) => {
            const task = filtered[item.index]!;
            const activityItem = activityById.get(task.id);
            return (
              <tr key={task.id} data-index={item.index} ref={virtualizer.measureElement} aria-rowindex={item.index + 2}
                tabIndex={0} data-listnav="1" data-task-row={task.id} aria-selected={selectedTaskId === task.id}
                onClick={(event) => {
                  if ((event.target as HTMLElement).closest('button, input, select, textarea, a, [role="dialog"]')) return;
                  selectTask(task);
                }}
                onKeyDown={(event) => {
                  if (event.target !== event.currentTarget) return;
                  if (event.key === "Enter" || event.key === " " || event.key === "o") {
                    event.preventDefault(); selectTask(task);
                  }
                }}
                className={`cursor-pointer focus:outline focus:outline-1 focus:outline-indigo-400 ${selectedTaskId === task.id ? "bg-indigo-500/15" : "hover:bg-gray-900/70"}`}>
                <td className="min-w-48 max-w-md px-3 py-3">
                  <span className="line-clamp-2 font-medium text-indigo-300">{task.title || task.id}</span>
                  <span className="mt-1 flex items-center gap-1">
                    <span className="font-mono text-[10px] text-gray-500">{task.id}</span>
                    <CopyTaskIdButton taskId={task.id} />
                  </span>
                </td>
                {!projectId && <td className="max-w-40 truncate px-3 py-3 text-xs text-gray-400" title={task.project_id}>{names.get(task.project_id ?? "") || task.project_id}</td>}
                <td className="px-3 py-3"><InlineStatus task={task} /></td>
                <td className="px-3 py-3"><InlinePriority task={task} /></td>
                <td className="px-3 py-3 text-gray-400">{task.assigned_agent || "Unassigned"}</td>
                {inWindow && <td className="px-3 py-3">{activityItem && <ModelsCell item={activityItem} />}</td>}
                {inWindow && <td className="px-3 py-3">{activityItem && <ActivityCell item={activityItem} />}</td>}
                <td className="px-3 py-3"><RowActions task={task} /></td>
              </tr>
            );
          })}
          {padBottom > 0 && <tr aria-hidden="true"><td colSpan={columns} style={{ height: padBottom, padding: 0, border: 0 }} /></tr>}
        </tbody>
      </table>
    </div>
  );
}
