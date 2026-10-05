import { useLayoutEffect, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { ProviderHeldTask } from "../../api/providers";
import TaskCard from "../../components/TaskCard";
import { useCompactViewport } from "../../hooks/useCompactViewport";
import { holdKindLabel, providerName, stateLabel } from "../metrics/providerAvailabilityFormat";
import { useListNav } from "../../shell/hotkeys/useListNav";
import { useTaskWorkspace } from "./TaskWorkspace";
import { useTaskListRows } from "./useTaskListRows";
import { activityWindowLabel, taskStatusLabel } from "./taskFilters";
import { ActivityCell, ModelsCell } from "./TaskActivityCells";
import { absoluteTime } from "./activityFormat";
import { CopyTaskIdButton } from "./CopyTaskIdButton";
import { InlinePriority, InlineStatus, RowActions } from "./TaskRowActions";
import { useTaskSelection } from "./useTaskSelection";

export default function CommandCenterTasks({ selection }: {
  selection?: ReturnType<typeof useTaskSelection>;
}) {
  const { projectId, filters, setStatus, clearFilters } = useTaskWorkspace();
  const { rows: filtered, statusCounts, isLoading, error, inWindow, activity, activityById, held, heldById, names } = useTaskListRows();
  const paneSelection = useTaskSelection();
  const { selectedTaskId, selectTask, clearTask } = selection ?? paneSelection;
  const columns = (projectId ? 5 : 6) + (inWindow ? 2 : 0);
  // Below 768 px the rows are touch cards (mobile dashboard §3 gap 6): the
  // table needs 620 px. Inline editing and row menus stay on the desktop
  // table and in the task pane, which is a full-screen sheet here.
  const compact = useCompactViewport();
  const bodyRef = useRef<HTMLTableSectionElement>(null);
  const cardsRef = useRef<HTMLDivElement>(null);
  // List navigation binds once, at mount, so it lives on the region, which
  // outlasts a swap between table rows and cards when the width crosses
  // 768 px. loop: false — with virtualized rows "next after the last mounted
  // row" is the overscan edge, not the end of the list, so wrapping would
  // jump the focus a window up instead of to the first task.
  const scrollRef = useListNav<HTMLDivElement>({ axis: "vertical", loop: false });
  // The scroll element is the padded region, and the count line plus the
  // sticky header sit above the first row, so row 0 does not start at
  // scrollTop 0. scrollMargin tells the virtualizer where the list begins;
  // without it the visible window is computed ~1.5 rows early.
  const [scrollMargin, setScrollMargin] = useState(0);
  useLayoutEffect(() => {
    const scroller = scrollRef.current;
    const body = bodyRef.current ?? cardsRef.current;
    if (!scroller || !body) return;
    const offset = body.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
    setScrollMargin(Math.max(0, Math.round(offset)));
  }, [scrollRef, error, isLoading, projectId, inWindow, filters.held, compact]);
  // Only the rows in view are mounted: the graph snapshot carries every task
  // in the project, and a 5,000-row table with three interactive cells per
  // row re-rendered on every keystroke and every live refetch. Keyboard list
  // navigation (useListNav) walks mounted rows, i.e. the window + overscan.
  const virtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => (compact ? 104 : 64),
    overscan: 12,
    scrollMargin,
  });
  const items = virtualizer.getVirtualItems();
  // item.start/end include scrollMargin; getTotalSize() does not.
  const padTop = items.length ? items[0]!.start - scrollMargin : 0;
  const padBottom = items.length
    ? virtualizer.getTotalSize() - (items[items.length - 1]!.end - scrollMargin)
    : 0;
  const hasFilters = !!(filters.query || filters.status || filters.window || filters.held || filters.showCompleted);
  const emptyMessage = inWindow
    ? "No work recorded in this time range."
    : filters.held ? "No held tasks match these filters." : hasFilters ? "No tasks match these filters." : "No active tasks yet.";
  const emptyState = <div className="p-6 text-center">
    <p className="text-sm font-medium text-gray-200">{emptyMessage}</p>
    <p className="mt-1 text-xs text-gray-400">{hasFilters ? "Try a broader search or clear your filters." : "Add a task to start work, or show completed tasks to review past work."}</p>
    {hasFilters && <button type="button" data-primary-control onClick={clearFilters}
      className="mt-3 rounded-md border border-gray-600 px-3 py-2 text-sm text-gray-200 hover:bg-gray-800">Clear filters</button>}
  </div>;

  // From 768 px the table keeps its 620 px minimum and scrolls sideways inside
  // this region when the rail and a pane leave it less (a landscape phone:
  // 844 px less the rail); the page itself never does. Cards never need to.
  return (
    <div role="region" aria-label="Task list" aria-busy={isLoading} ref={scrollRef} className="h-full min-h-0 overflow-auto p-3 md:p-4"
      data-allow-overflow-x={compact ? undefined : ""}
      onClick={(event) => {
        const target = event.target as HTMLElement;
        if (!target.closest('[data-task-row], button, input, select, textarea, a, [role="dialog"]')) clearTask();
      }}>
      {!isLoading && !error && <div role="group" aria-label="Filter by task status"
        title="Counts match your search, time range and provider filters. Waiting input is a task status, not a count of approvals."
        className="mb-4 grid grid-cols-4 gap-1.5 md:gap-3">
        {([
          ["IN_PROGRESS", "text-blue-300"], ["BLOCKED", "text-orange-300"],
          ["WAITING_INPUT", "text-cyan-300"], ["FAILED", "text-red-300"],
        ] as const).map(([status, tone]) => <button key={status} type="button" data-primary-control
          aria-pressed={filters.status === status}
          aria-label={`${taskStatusLabel(status)}: ${statusCounts[status] ?? 0} ${statusCounts[status] === 1 ? "task" : "tasks"}`}
          onClick={() => setStatus(filters.status === status ? "" : status)}
          className={`focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400 min-w-0 rounded-lg border px-1 py-2 text-left transition-colors md:px-3 ${filters.status === status ? "border-indigo-400 bg-indigo-500/15" : "border-gray-800 bg-gray-900/60 hover:border-gray-600 hover:bg-gray-900"}`}>
          <span className={`block text-lg font-semibold tabular-nums ${tone}`}>{statusCounts[status] ?? 0}</span>
          <span className="block text-[11px] leading-4 text-gray-300 md:text-xs">{taskStatusLabel(status)}</span>
        </button>)}
      </div>}
      {!isLoading && !error && <p className="mb-3 text-xs text-gray-400">{filtered.length} {filtered.length === 1 ? "task" : "tasks"}</p>}
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
      {filters.held && held.data && (
        <p role="status" data-testid="held-filter-status" className="mb-3 rounded border border-amber-600/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">
          <span className="font-medium">Held by provider</span> — {heldById.size === 0
            ? "no task is waiting on an unavailable provider."
            : <>tasks waiting on an unavailable provider{heldSummary(held.data.by_kind)}.</>}
        </p>
      )}
      {error && <p role="alert" className="mb-3 rounded border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">Could not load tasks. Check the backend connection and try again.</p>}
      {compact ? (
        <>
          {isLoading && <p role="status" className="p-4 text-gray-400">Loading tasks…</p>}
          {!isLoading && !error && filtered.length === 0 && emptyState}
          <div ref={cardsRef} role="list" aria-label="Tasks" className="relative"
            style={{ height: virtualizer.getTotalSize() }}>
            {items.map((item) => {
              const task = filtered[item.index]!;
              const activityItem = inWindow ? activityById.get(task.id) : undefined;
              const hold = filters.held ? heldById.get(task.id) : undefined;
              return (
                <div key={task.id} role="listitem" data-index={item.index} ref={virtualizer.measureElement}
                  className="absolute left-0 top-0 w-full pb-2"
                  style={{ transform: `translateY(${item.start - scrollMargin}px)` }}>
                  <TaskCard
                    task={task}
                    selected={selectedTaskId === task.id}
                    onSelect={() => selectTask(task)}
                    projectName={projectId ? undefined : names.get(task.project_id ?? "") || task.project_id}
                    note={(hold || activityItem) && <>
                      {hold && <HoldNote hold={hold} />}
                      {activityItem && (
                        <span className="mt-1 flex flex-wrap items-start gap-x-3 gap-y-1">
                          <ModelsCell item={activityItem} />
                          <ActivityCell item={activityItem} />
                        </span>
                      )}
                    </>}
                  />
                </div>
              );
            })}
          </div>
        </>
      ) : (
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
          {isLoading && <tr><td colSpan={columns} className="p-4 text-gray-400"><span role="status">Loading tasks…</span></td></tr>}
          {!isLoading && !error && filtered.length === 0 && <tr><td colSpan={columns}>{emptyState}</td></tr>}
          {padTop > 0 && <tr aria-hidden="true"><td colSpan={columns} style={{ height: padTop, padding: 0, border: 0 }} /></tr>}
          {items.map((item) => {
            const task = filtered[item.index]!;
            const activityItem = activityById.get(task.id);
            const hold = filters.held ? heldById.get(task.id) : undefined;
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
                  {hold && <HoldNote hold={hold} />}
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
      )}
    </div>
  );
}

/** ", 3 waiting for failover capacity, 1 pinned" — the server's per-kind counts. */
function heldSummary(byKind: Record<string, number> | undefined): string {
  const parts = Object.entries(byKind ?? {})
    .filter(([, count]) => count > 0)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([kind, count]) => `${count} × ${holdKindLabel(kind).toLowerCase()}`);
  return parts.length ? `: ${parts.join("; ")}` : "";
}

/** Why a held row is waiting, in words, from the held-task read. */
function HoldNote({ hold }: { hold: ProviderHeldTask }) {
  return (
    <span data-testid={`held-note-${hold.task_id}`} className="mt-1 block text-[11px] text-amber-300/90"
      title={hold.detail || hold.remediation || undefined}>
      {providerName(hold.provider)} {stateLabel(hold.state).toLowerCase()} · {holdKindLabel(hold.kind, hold.ahead)}
    </span>
  );
}
