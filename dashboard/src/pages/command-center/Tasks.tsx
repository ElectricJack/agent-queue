import { useMemo, useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { Task } from "../../api/hooks";
import { useProjectGraphs } from "../../api/graph";
import { useListNav } from "../../shell/hotkeys/useListNav";
import { useTaskWorkspace } from "./TaskWorkspace";
import { matchesTask } from "./taskFilters";
import { InlinePriority, InlineStatus, RowActions } from "./TaskRowActions";
import { useTaskSelection } from "./useTaskSelection";

export default function CommandCenterTasks() {
  const { projectId, projectIds, projects, filters, isLoadingProjects, projectsError } = useTaskWorkspace();
  // The ordinary list endpoint truncates completed history. Both workspace
  // views use the complete graph snapshots so searches always cover the same tasks.
  const { data: graph, isLoading: graphLoading, errors } = useProjectGraphs(projectIds);
  const isLoading = graphLoading || (!projectId && isLoadingProjects);
  const error = projectsError || errors.some(Boolean);
  const tasks = useMemo<Task[]>(() => graph.tasks.map((task) => ({
    ...task, project_id: graph.taskProject[task.id] ?? "",
    assigned_agent: task.assigned_agent_id, priority: task.priority ?? undefined,
  })), [graph]);
  const { selectedTaskId, selectTask, clearTask } = useTaskSelection();
  const bodyRef = useListNav<HTMLTableSectionElement>({ axis: "vertical" });
  const names = useMemo(() => new Map(projects.map((p) => [p.id, p.name || p.id])), [projects]);
  const filtered = useMemo(
    () => tasks.filter((task) => (!projectId || task.project_id === projectId)
      && matchesTask(task, filters, names.get(task.project_id ?? "") ?? "")),
    [tasks, projectId, filters, names],
  );
  const columns = projectId ? 5 : 6;
  const scrollRef = useRef<HTMLDivElement>(null);
  // Only the rows in view are mounted: the graph snapshot carries every task
  // in the project, and a 5,000-row table with three interactive cells per
  // row re-rendered on every keystroke and every live refetch. Keyboard list
  // navigation (useListNav) walks mounted rows, i.e. the window + overscan.
  const virtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 64,
    overscan: 12,
    initialRect: { width: 800, height: 600 },
  });
  const items = virtualizer.getVirtualItems();
  const padTop = items.length ? items[0]!.start : 0;
  const padBottom = items.length ? virtualizer.getTotalSize() - items[items.length - 1]!.end : 0;

  return (
    <div role="region" aria-label="Task list" ref={scrollRef} className="h-full min-h-0 overflow-auto p-4"
      onClick={(event) => {
        const target = event.target as HTMLElement;
        if (!target.closest('[data-task-row], button, input, select, textarea, a, [role="dialog"]')) clearTask();
      }}>
      <p className="mb-3 text-xs text-gray-500">{filtered.length} {filtered.length === 1 ? "task" : "tasks"}</p>
      {error && <p role="alert" className="mb-3 rounded border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">Could not load tasks. Check the backend connection and try again.</p>}
      <table className="w-full min-w-[620px] text-left text-sm">
        <thead className="sticky top-0 z-10 border-b border-gray-800 bg-gray-950 text-xs uppercase text-gray-500">
          <tr>
            <th className="px-3 py-3">Task</th>
            {!projectId && <th className="px-3 py-3">Project</th>}
            <th className="px-3 py-3">Status</th>
            <th className="px-3 py-3">Priority</th>
            <th className="px-3 py-3">Agent</th>
            <th className="px-3 py-3"><span className="sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody ref={bodyRef} className="divide-y divide-gray-800">
          {isLoading && <tr><td colSpan={columns} className="p-4 text-gray-500">Loading tasks…</td></tr>}
          {!isLoading && !error && filtered.length === 0 && <tr><td colSpan={columns} className="p-8 text-center text-gray-500">No tasks match these filters.</td></tr>}
          {padTop > 0 && <tr aria-hidden="true"><td colSpan={columns} style={{ height: padTop, padding: 0, border: 0 }} /></tr>}
          {items.map((item) => {
            const task = filtered[item.index]!;
            return (
              <tr key={task.id} data-index={item.index} ref={virtualizer.measureElement}
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
                  <span className="mt-1 block font-mono text-[10px] text-gray-500">{task.id}</span>
                </td>
                {!projectId && <td className="max-w-40 truncate px-3 py-3 text-xs text-gray-400" title={task.project_id}>{names.get(task.project_id ?? "") || task.project_id}</td>}
                <td className="px-3 py-3"><InlineStatus task={task} /></td>
                <td className="px-3 py-3"><InlinePriority task={task} /></td>
                <td className="px-3 py-3 text-gray-400">{task.assigned_agent || "Unassigned"}</td>
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
